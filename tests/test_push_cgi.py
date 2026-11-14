"""Уведомления: CGI на роутере (router/push.cgi), сервер для Mac и установка (флаги, файлы)."""
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
import push
import server

ROOT = Path(__file__).resolve().parent.parent
FCM = "https://fcm.googleapis.com/fcm/send/abc123"
try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    HAVE = True
except ImportError:  # pragma: no cover
    HAVE = False


def sub_json(endpoint=FCM):
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": endpoint, "keys": {"p256dh": push.b64u(pub), "auth": push.b64u(os.urandom(16))}}


def run_cgi(home, body=None, method="POST", cookie=None, raw=None, gap=None):
    data = raw if raw is not None else (b"" if body is None else json.dumps(body, ensure_ascii=False).encode())
    env = {"PATH": os.environ["PATH"], "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(data)), "DNEVNIK_HOME": str(home), "PYTHONPATH": str(ROOT),
           "DNEVNIK_PUSH_GAP": str(gap or (Path(home) / "gap"))}
    if cookie:
        env["HTTP_COOKIE"] = cookie
    r = subprocess.run([sys.executable, str(ROOT / "router" / "push.cgi")], input=data, capture_output=True, env=env, timeout=60)
    head, _, payload = r.stdout.decode("utf-8").partition("\r\n\r\n")
    return int(head.split("Status: ")[1].split()[0]), (json.loads(payload) if payload else {}), head, r.stderr.decode()


@unittest.skipUnless(HAVE, "нужен пакет cryptography")
class PushCgiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "data").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def cookie(self):
        auth.set_password(self.home / "data", "семейный-пароль")
        return "dnevnik_auth=" + auth.make_token(self.home / "data")

    def test_open_while_no_password_is_set(self):
        status, body, head, _ = run_cgi(self.home, {"action": "config"})
        self.assertEqual((status, body["ready"]), (200, True))
        self.assertIn("no-store", head)

    def test_locked_without_cookie_when_password_is_on(self):
        self.cookie()
        for cookie in (None, "dnevnik_auth=1.abc", "x=1"):
            status, body, head, _ = run_cgi(self.home, {"action": "config"}, cookie=cookie)
            self.assertEqual(status, 401, cookie)
            self.assertIn("Status: 401 Unauthorized", head)
            self.assertTrue(body["auth"])
        self.assertFalse((self.home / "data" / "push.json").exists())                      # ничего не создано

    def test_subscribe_get_update_unsubscribe_through_the_cgi(self):
        ck = self.cookie()
        status, body, _, err = run_cgi(self.home, {"action": "subscribe", "subscription": sub_json(), "ua": "Android Chrome"}, cookie=ck)
        self.assertEqual(status, 200, (body, err))
        status, body, _, _ = run_cgi(self.home, {"action": "update", "endpoint": FCM, "prefs": {"types": {"homework": False}}}, cookie=ck)
        self.assertEqual((status, body["prefs"]["types"]["homework"]), (200, False))
        status, body, _, _ = run_cgi(self.home, {"action": "get", "endpoint": FCM}, cookie=ck)
        self.assertEqual((body["subscribed"], body["prefs"]["types"]["homework"]), (True, False))
        run_cgi(self.home, {"action": "unsubscribe", "endpoint": FCM}, cookie=ck)
        self.assertFalse(run_cgi(self.home, {"action": "get", "endpoint": FCM}, cookie=ck)[1]["subscribed"])
        self.assertEqual(oct((self.home / "data" / "push.json").stat().st_mode & 0o777), "0o600")

    def test_private_key_never_leaves_the_router(self):
        status, body, head, _ = run_cgi(self.home, {"action": "config"})
        raw = (self.home / "data" / "push.json").read_text()
        private = json.loads(raw)["vapid"]["private"]
        self.assertNotIn(private, json.dumps(body) + head)

    def test_bad_requests(self):
        self.assertEqual(run_cgi(self.home, method="GET")[0], 405)
        self.assertEqual(run_cgi(self.home, raw=b"not json")[0], 400)
        self.assertEqual(run_cgi(self.home, raw=b"[1,2]")[0], 400)
        self.assertEqual(run_cgi(self.home, raw=b"x" * 9000)[0], 400)
        self.assertEqual(run_cgi(self.home, {"action": "format"})[0], 400)

    def test_foreign_push_service_addresses_are_refused(self):
        status, body, _, _ = run_cgi(self.home, {"action": "subscribe", "subscription": sub_json("https://attacker.example.com/x")})
        self.assertEqual(status, 400)
        self.assertFalse((self.home / "data" / "push.json").exists() and json.loads((self.home / "data" / "push.json").read_text())["subs"])

    def test_test_button_is_rate_limited(self):
        run_cgi(self.home, {"action": "subscribe", "subscription": sub_json()})
        first = run_cgi(self.home, {"action": "test", "endpoint": FCM})
        self.assertNotEqual(first[0], 429)
        second = run_cgi(self.home, {"action": "test", "endpoint": FCM})
        self.assertEqual(second[0], 429)
        self.assertIn("Status: 429 Too Many Requests", second[2])


@unittest.skipUnless(HAVE, "нужен пакет cryptography")
class ServerPushTests(unittest.TestCase):
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
        self.old = (server.DEMO, server.PUSH_DIR, server.AUTH_DIR)
        server.DEMO, server.PUSH_DIR, server.AUTH_DIR = True, Path(self.tmp.name), None

    def tearDown(self):
        server.DEMO, server.PUSH_DIR, server.AUTH_DIR = self.old
        self.tmp.cleanup()

    def post(self, body, cookie=None):
        r = urllib.request.Request(self.base + "/cgi-bin/push", data=json.dumps(body).encode(), method="POST")
        if cookie:
            r.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_same_address_and_behaviour_as_on_the_router(self):
        self.assertEqual(self.post({"action": "config"})[1]["ready"], True)
        self.assertEqual(self.post({"action": "subscribe", "subscription": sub_json()})[0], 200)
        self.assertTrue(self.post({"action": "get", "endpoint": FCM})[1]["subscribed"])
        self.assertEqual(self.post({"action": "subscribe", "subscription": sub_json("https://evil.example.com/x")})[0], 400)

    def test_demo_without_a_folder_has_no_notifications_and_touches_nothing(self):
        server.PUSH_DIR = None
        code, body = self.post({"action": "config"})
        self.assertFalse(body["ok"])
        self.assertIn("демо", body["message"])

    def test_locked_with_a_family_password(self):
        server.AUTH_DIR = Path(self.tmp.name)
        auth.set_password(server.AUTH_DIR, "семейный-пароль")
        code, body = self.post({"action": "config"})
        self.assertEqual((code, body["auth"]), (401, True))
        cookie = "dnevnik_auth=" + auth.make_token(server.AUTH_DIR)
        self.assertEqual(self.post({"action": "config"}, cookie)[0], 200)


class InstallFilesTests(unittest.TestCase):
    def read(self, rel):
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_installer_ships_the_notification_files_and_has_the_flag(self):
        install, deploy, init = self.read("router/install.sh"), self.read("router/deploy.sh"), self.read("router/dnevnik.init")
        for name in ("notify.py", "push.py", "router/push.cgi", "router/push.sh"):
            self.assertIn(name, install, name)
        self.assertIn("--push", install)
        self.assertIn("notify.py push.py", deploy)
        self.assertIn('cp -f "$APP/router/push.cgi" "$WWW/cgi-bin/push"', init)
        self.assertEqual(subprocess.run(["sh", "-n", str(ROOT / "router" / "install.sh")]).returncode, 0)
        r = subprocess.run(["sh", str(ROOT / "router" / "install.sh"), "--bogus"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--push", r.stderr)

    def test_sync_runs_notifications_after_a_successful_update_and_survives_their_failure(self):
        sync = self.read("sync.py")
        self.assertIn("notify.process(args.data, o)", sync)
        self.assertIn("except Exception as e:", sync.split("notify.process")[1])

    def test_scripts_are_executable_and_the_cgi_is_python(self):
        for name in ("push.cgi", "push.sh"):
            self.assertTrue(os.access(ROOT / "router" / name, os.X_OK), name)
        self.assertTrue(self.read("router/push.cgi").startswith("#!/usr/bin/python3"))

    def test_the_cgi_never_imports_cryptography_directly(self):
        """Лёгкий запуск: тяжёлый модуль подгружает только push.py и только когда нужна отправка."""
        self.assertNotIn("cryptography", self.read("router/push.cgi").replace("python3-cryptography", ""))


if __name__ == "__main__":
    unittest.main()
