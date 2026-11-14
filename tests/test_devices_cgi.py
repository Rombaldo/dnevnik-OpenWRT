"""Журнал входов и «последний заход» в настоящих CGI, имя устройства и список подключённых устройств."""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import auth
import devices
import push
import server
from tests.test_push import FCM, APPLE, device, sub_json, HAVE

ROOT = Path(__file__).resolve().parent.parent
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.7 Mobile/15E148 Safari/604.1"
UA_TABLET = "Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
PASSWORD = "семейный-пароль"


def run(script, home, body=None, method="POST", cookie=None, ua=UA_IPHONE, addr="192.168.10.147", host=None, seen=None):
    data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
    env = {"PATH": os.environ["PATH"], "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(data)), "DNEVNIK_HOME": str(home), "PYTHONPATH": str(ROOT),
           "DNEVNIK_AUTH_THROTTLE": str(Path(home) / "throttle"), "DNEVNIK_OUT": str(Path(home) / "page.json"), "DNEVNIK_SEEN": str(seen or Path(home) / "seen.json")}
    if cookie:
        env["HTTP_COOKIE"] = cookie
    if ua:
        env["HTTP_USER_AGENT"] = ua
    if addr:
        env["REMOTE_ADDR"] = addr
    if host:
        env["HTTP_HOST"] = host
    r = subprocess.run([sys.executable, str(ROOT / "router" / script)], input=data, capture_output=True, env=env, timeout=60)
    head, _, payload = r.stdout.decode("utf-8").partition("\r\n\r\n")
    return int(head.split("Status: ")[1].split()[0]), (json.loads(payload) if payload.strip().startswith("{") else payload), head, r.stderr.decode()


def cookies(head):
    return re.findall(r"Set-Cookie: ([^;\r\n]+)", head)


class CgiBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "data").mkdir()
        (self.home / "page.json").write_text(json.dumps({"days": [], "status": {}}))
        auth.set_password(self.home / "data", PASSWORD)
        self.data = self.home / "data"

    def tearDown(self):
        self.tmp.cleanup()

    def login(self, password=PASSWORD, **kw):
        return run("auth.cgi", self.home, {"action": "login", "password": password}, **kw)

    def log(self):
        return devices.recent_logins(self.data)


class LoginLogCgiTests(CgiBase):
    def test_failed_and_successful_logins_are_recorded_with_device_and_place(self):
        code, _, head, _ = self.login("неверный-пароль", ua=UA_TABLET, addr="192.168.10.50")
        self.assertEqual(code, 401)
        e = self.log()[0]
        self.assertEqual((e["ok"], e["dev"], e["where"]), (False, "Android (SM-X710) · Chrome", "home"))
        code, _, head, _ = self.login(ua=UA_IPHONE, addr="127.0.0.1")                  # из интернета (Funnel приходит с самого роутера)
        self.assertEqual(code, 200)
        e = self.log()[0]
        self.assertEqual((e["ok"], e["dev"], e["where"]), (True, "iPhone · Safari", "internet"))
        raw = (self.data / "logins.json").read_text()
        for private in ("192.168", "127.0.0.1", "Mozilla", PASSWORD, "неверный-пароль"):
            self.assertNotIn(private, raw, private)                                   # в журнале ни адресов, ни строки браузера, ни паролей

    def test_device_cookie_is_issued_once_and_groups_attempts_of_one_device(self):
        _, _, head, _ = self.login("нет-нет")
        dev = [c for c in cookies(head) if c.startswith("dnevnik_dev=")]
        self.assertEqual(len(dev), 1)
        self.assertIn("HttpOnly", head)
        did = dev[0].split("=")[1]
        _, _, head2, _ = self.login(cookie=dev[0])                                     # то же устройство вошло: новой метки не выдаётся
        self.assertEqual([c for c in cookies(head2) if c.startswith("dnevnik_dev=")], [])
        self.assertEqual({e["did"] for e in self.log()}, {did})
        self.assertTrue(any(c.startswith("dnevnik_auth=") for c in cookies(head2)))    # а входной cookie выдан

    def test_repeated_failures_merge_and_throttled_attempts_are_not_logged(self):
        _, _, head, _ = self.login("нет-нет")
        dev = [c for c in cookies(head) if c.startswith("dnevnik_dev=")][0]            # дальше браузер возвращает свою метку
        for _ in range(auth.FREE_TRIES - 1):
            self.login("нет-нет", cookie=dev)
        self.assertEqual(len(self.log()), 1)
        self.assertEqual(self.log()[0]["n"], auth.FREE_TRIES)
        code, _, head, _ = self.login("нет-нет", cookie=dev)                           # слишком частые попытки: ответ 429
        self.assertEqual(code, 429)
        self.assertEqual(self.log()[0]["n"], auth.FREE_TRIES)                          # лишней записи нет: флеш не изнашиваем
        self.assertNotIn("dnevnik_dev=", head)

    def test_login_while_no_password_is_set_is_not_logged(self):
        auth.disable(self.data)
        code, body, head, _ = self.login()
        self.assertEqual((code, body["enabled"]), (200, False))
        self.assertEqual(self.log(), [])
        self.assertEqual(cookies(head), [])

    def test_other_actions_are_not_logged(self):
        run("auth.cgi", self.home, {"action": "status"})
        run("auth.cgi", self.home, {"action": "logout"})
        self.assertEqual(self.log(), [])

    def test_tailscale_and_unknown_places(self):
        self.login(addr="100.109.6.85")
        self.assertEqual(self.log()[0]["where"], "tailscale")
        self.login(addr=None)
        self.assertEqual(self.log()[0]["where"], "unknown")

    def test_https_host_makes_the_device_cookie_secure(self):
        _, _, head, _ = self.login(host="dnevnik.tail-example.ts.net")
        self.assertTrue(all("Secure" in h for h in re.findall(r"Set-Cookie: ([^\r\n]+)", head)))

    def test_a_broken_log_file_does_not_stop_the_login(self):
        (self.data / "logins.json").mkdir()                                            # вместо файла папка: записать нельзя
        code, body, head, _ = self.login()
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(any(c.startswith("dnevnik_auth=") for c in cookies(head)))

    def test_successful_login_also_marks_last_seen(self):
        self.login(ua=UA_TABLET)
        rows = devices.seen_list(self.home / "seen.json")
        self.assertEqual((len(rows), rows[0]["dev"]), (1, "Android (SM-X710) · Chrome"))


class LastSeenCgiTests(CgiBase):
    def token(self):
        return "dnevnik_auth=" + auth.make_token(self.data)

    def test_open_page_with_password_marks_the_device_and_gives_it_a_label_once(self):
        code, body, head, _ = run("data.cgi", self.home, method="GET", cookie=self.token(), ua=UA_TABLET, addr="192.168.10.50")
        self.assertEqual(code, 200)
        dev = [c for c in cookies(head) if c.startswith("dnevnik_dev=")]
        self.assertEqual(len(dev), 1)
        row = devices.seen_list(self.home / "seen.json")[0]
        self.assertEqual((row["dev"], row["where"], row["did"]), ("Android (SM-X710) · Chrome", "home", dev[0].split("=")[1]))
        code, _, head2, _ = run("data.cgi", self.home, method="GET", cookie=self.token() + "; " + dev[0], ua=UA_TABLET)       # с меткой: новая не выдаётся
        self.assertEqual([c for c in cookies(head2) if c.startswith("dnevnik_dev=")], [])

    def test_repeated_opening_does_not_rewrite_the_file(self):
        ck = self.token()
        run("data.cgi", self.home, method="GET", cookie=ck + "; dnevnik_dev=" + "a" * 12)
        m = (self.home / "seen.json").stat().st_mtime_ns
        run("data.cgi", self.home, method="GET", cookie=ck + "; dnevnik_dev=" + "a" * 12)
        self.assertEqual((self.home / "seen.json").stat().st_mtime_ns, m)

    def test_locked_page_tracks_nobody(self):
        code, body, head, _ = run("data.cgi", self.home, method="GET", cookie=None)
        self.assertEqual(code, 401)
        self.assertFalse((self.home / "seen.json").exists())
        self.assertEqual(cookies(head), [])

    def test_page_without_a_password_is_not_tracked(self):
        auth.disable(self.data)
        code, _, head, _ = run("data.cgi", self.home, method="GET")
        self.assertEqual(code, 200)
        self.assertFalse((self.home / "seen.json").exists())
        self.assertEqual(cookies(head), [])

    def test_unwritable_seen_file_does_not_break_the_page(self):
        bad = self.home / "no" / "such" / "dir" / "x"
        (self.home / "no").write_text("файл вместо папки")
        code, body, _, _ = run("data.cgi", self.home, method="GET", cookie=self.token(), seen=bad)
        self.assertEqual(code, 200)
        self.assertIn("days", body)

    def test_a_forged_device_cookie_is_ignored(self):
        code, _, head, _ = run("data.cgi", self.home, method="GET", cookie=self.token() + "; dnevnik_dev=<script>")
        self.assertEqual(code, 200)
        self.assertEqual(len([c for c in cookies(head) if c.startswith("dnevnik_dev=")]), 1)       # плохая метка заменена новой
        self.assertNotIn("<script>", (self.home / "seen.json").read_text())


@unittest.skipUnless(HAVE, "нужен пакет cryptography")
class DeviceNameAndListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def sub(self, endpoint=FCM, **extra):
        return push.handle(self.dir, dict({"action": "subscribe", "subscription": sub_json(endpoint, device()), "ua": UA_TABLET}, **extra))

    def test_name_and_label_are_stored_and_returned(self):
        code, b = self.sub(name="  Планшет Ани  ")
        self.assertEqual(code, 200)
        self.assertEqual(push.handle(self.dir, {"action": "get", "endpoint": FCM})[1]["name"], "Планшет Ани")
        s = list(push.load(self.dir)["subs"].values())[0]
        self.assertEqual(s["label"], "Android (SM-X710) · Chrome")                       # название по полной строке браузера, а не по обрезанной

    def test_name_is_sanitised_and_limited(self):
        self.sub(name='<img src=x onerror="1"> ' + "я" * 80)
        name = list(push.load(self.dir)["subs"].values())[0]["name"]
        self.assertNotIn("<", name)
        self.assertNotIn('"', name)
        self.assertLessEqual(len(name), 30)
        for bad in (None, 5, ["x"]):
            self.assertEqual(push.clean_name(bad), "")

    def test_rename_through_update_without_touching_the_settings(self):
        self.sub(name="Старое", prefs={"types": {"mark": False}})
        code, b = push.handle(self.dir, {"action": "update", "endpoint": FCM, "name": "Новое"})
        self.assertEqual((code, b["name"], b["prefs"]["types"]["mark"]), (200, "Новое", False))
        push.handle(self.dir, {"action": "update", "endpoint": FCM, "prefs": {"types": {"mark": True}}})       # настройки меняются, имя остаётся
        self.assertEqual(push.handle(self.dir, {"action": "get", "endpoint": FCM})[1]["name"], "Новое")

    def test_resubscribing_without_a_name_keeps_the_old_one(self):
        self.sub(name="Планшет")
        self.sub()
        self.assertEqual(list(push.load(self.dir)["subs"].values())[0]["name"], "Планшет")

    def test_describe_hides_the_endpoint_and_names_the_service(self):
        self.sub(FCM, name="Планшет")
        self.sub(APPLE, ua=UA_IPHONE)
        rows = push.describe(self.dir)
        self.assertEqual([(r["name"], r["service"], r["label"]) for r in rows], [("Планшет", "Google", "Android (SM-X710) · Chrome"), ("", "Apple", "iPhone · Safari")])
        dump = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("fcm.googleapis.com", dump)                                      # адрес с токеном устройства наружу не отдаётся
        self.assertNotIn("p256dh", dump)
        self.assertEqual(push.service_name("https://foo.push.services.mozilla.com/x"), "Mozilla")
        self.assertEqual(push.service_name("https://wns2.notify.windows.com/x"), "Microsoft")
        self.assertEqual(push.service_name("https://evil.example.com/x"), "неизвестная")

    def test_old_subscription_without_a_label_still_gets_one(self):
        self.sub()
        st = push.load(self.dir)
        for s in st["subs"].values():
            s.pop("label")
            s["ua"] = "Mozilla/5.0 (iPhone CPU iPhone OS 18_7 like Mac OS X) Apple"
        push.save(self.dir, st)
        self.assertIn("iPhone", push.describe(self.dir)[0]["label"])

    def test_remove_device(self):
        self.sub(FCM)
        sid = push.describe(self.dir)[0]["id"]
        self.assertFalse(push.remove_sub(self.dir, "нет-такого"))
        self.assertTrue(push.remove_sub(self.dir, sid))
        self.assertEqual(push.describe(self.dir), [])
        self.assertFalse(push.remove_sub(self.dir, sid))


class ServerParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (server.DEMO, server.AUTH_DIR)
        server.DEMO, server.AUTH_DIR = True, Path(self.tmp.name)
        auth.set_password(server.AUTH_DIR, PASSWORD)

    def tearDown(self):
        server.DEMO, server.AUTH_DIR = self.old
        self.tmp.cleanup()

    def login(self, password):
        r = urllib.request.Request(self.base + "/cgi-bin/auth", data=json.dumps({"action": "login", "password": password}).encode(), method="POST")
        r.add_header("User-Agent", UA_TABLET)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get_all("Set-Cookie") or []

    def test_server_for_mac_keeps_the_same_log(self):
        self.assertEqual(self.login("неверный")[0], 401)
        code, cks = self.login(PASSWORD)
        self.assertEqual(code, 200)
        log = devices.recent_logins(server.AUTH_DIR)
        self.assertEqual([(e["ok"], e["dev"], e["where"]) for e in log], [(True, "Android (SM-X710) · Chrome", "internet"), (False, "Android (SM-X710) · Chrome", "internet")])      # с localhost: как через Funnel
        self.assertEqual(len(devices.seen_list(Path(server.AUTH_DIR) / "devices-seen.json")), 1)


if __name__ == "__main__":
    unittest.main()
