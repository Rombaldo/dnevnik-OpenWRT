"""Семейный пароль: модуль auth.py, CGI на роутере, сервер для Mac и сама страница в браузере."""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import auth
import server

ROOT = Path(__file__).resolve().parent.parent
ROUTER = ROOT / "router"
PASSWORD = "семейный-пароль-123"

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None


class AuthModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.throttle = self.dir / "throttle.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_password_is_off_by_default(self):
        self.assertFalse(auth.enabled(self.dir))
        self.assertTrue(auth.authed(self.dir, None))                       # пока пароля нет, страница открыта, как раньше

    def test_short_and_empty_passwords_are_refused(self):
        for bad in ("", "   ", "\t\n", None, 12345678, "x" * (auth.MAX_LEN + 1)):
            ok, message = auth.set_password(self.dir, bad)
            self.assertFalse(ok, bad)
            self.assertTrue(message)
        self.assertFalse(auth.enabled(self.dir))

    def test_even_one_character_is_a_valid_password(self):
        """Хозяин сам решил, что короткий пароль допустим (риск ему известен): запрета на длину нет, кроме пустого."""
        for pw in ("1", "я", "ab", "12345"):
            self.assertTrue(auth.set_password(self.dir, pw)[0], pw)
            self.assertTrue(auth.check_password(self.dir, pw), pw)
            self.assertFalse(auth.check_password(self.dir, pw + "x"), pw)
            self.assertFalse(auth.check_password(self.dir, ""), pw)
            token = auth.make_token(self.dir, time.time())
            self.assertTrue(auth.token_valid(self.dir, token), pw)
        self.assertEqual(auth.set_password(self.dir, "  ")[1], "Пароль не может быть пустым.")

    def test_hint_rule_for_very_short_passwords(self):
        self.assertFalse(auth.set_password(self.dir, "1", "1")[0])                          # подсказка = пароль: отказ
        self.assertFalse(auth.set_password(self.dir, "Я", "я")[0])                          # регистр не важен
        self.assertTrue(auth.set_password(self.dir, "1", "цифра")[0])                       # а вот «содержит букву» для одной буквы не повод для отказа
        self.assertTrue(auth.set_password(self.dir, "я", "первая буква алфавита, наоборот")[0])
        self.assertFalse(auth.set_password(self.dir, "кот", "мой кот")[0])                  # от трёх символов правило прежнее: пароль внутри подсказки нельзя
        self.assertTrue(auth.set_password(self.dir, "ко", "мой кот")[0])

    def test_password_is_stored_hashed_and_private(self):
        self.assertTrue(auth.set_password(self.dir, PASSWORD)[0])
        raw = (self.dir / "auth.json").read_text()
        self.assertNotIn(PASSWORD, raw)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / "auth.json").st_mode), 0o600)
        self.assertTrue(auth.check_password(self.dir, PASSWORD))
        self.assertTrue(auth.check_password(self.dir, "  " + PASSWORD + " "))   # пробелы по краям от телефонной клавиатуры не мешают
        for wrong in ("", "неверный-пароль", PASSWORD + "1", PASSWORD.upper(), None, "x" * 5000):
            self.assertFalse(auth.check_password(self.dir, wrong), wrong)

    def test_each_password_gets_its_own_salt(self):
        auth.set_password(self.dir, PASSWORD)
        first = json.loads((self.dir / "auth.json").read_text())
        auth.set_password(self.dir, PASSWORD)
        second = json.loads((self.dir / "auth.json").read_text())
        self.assertNotEqual(first["salt"], second["salt"])
        self.assertNotEqual(first["hash"], second["hash"])
        self.assertNotEqual(first["secret"], second["secret"])

    def test_token_lifecycle(self):
        auth.set_password(self.dir, PASSWORD)
        now = 1_800_000_000
        token = auth.make_token(self.dir, now)
        self.assertTrue(auth.token_valid(self.dir, token, now + 10))
        self.assertTrue(auth.token_valid(self.dir, token, now + 179 * 86400))
        self.assertFalse(auth.token_valid(self.dir, token, now + 181 * 86400))       # срок вышел
        exp, sig = token.split(".")
        for bad in (None, "", "abc", exp, f"{exp}.", f".{sig}", f"{int(exp) + 1}.{sig}", f"{exp}.{'0' * 64}", f"{exp}.{sig}.x", "x.y"):
            self.assertFalse(auth.token_valid(self.dir, bad, now + 10), bad)
        far = int(now + 400 * 86400)                                                   # «вечный» cookie, выданный не нами
        self.assertFalse(auth.token_valid(self.dir, f"{far}.{auth._sign(json.loads((self.dir / 'auth.json').read_text())['secret'], far)}", now))

    def test_changing_the_password_logs_everyone_out(self):
        auth.set_password(self.dir, PASSWORD)
        token = auth.make_token(self.dir, time.time())
        self.assertTrue(auth.token_valid(self.dir, token))
        auth.set_password(self.dir, PASSWORD + "новый")
        self.assertFalse(auth.token_valid(self.dir, token))

    def test_cookie_header_parsing(self):
        self.assertEqual(auth.cookie_value("a=1; dnevnik_auth=XYZ; b=2"), "XYZ")
        self.assertEqual(auth.cookie_value("dnevnik_auth=XYZ"), "XYZ")
        self.assertIsNone(auth.cookie_value("other=1"))
        self.assertIsNone(auth.cookie_value(None))
        self.assertIsNone(auth.cookie_value("xdnevnik_auth=1"))

    def test_broken_file_keeps_the_page_closed(self):
        (self.dir / "auth.json").write_text("{не json")
        self.assertTrue(auth.enabled(self.dir))
        self.assertFalse(auth.authed(self.dir, "dnevnik_auth=1.abc"))
        self.assertFalse(auth.check_password(self.dir, PASSWORD))
        self.assertIsNone(auth.make_token(self.dir))

    def test_throttle_backoff_and_reset(self):
        t = 1000.0
        for _ in range(auth.FREE_TRIES - 1):
            auth.throttle_fail(self.throttle, t)
            self.assertEqual(auth.throttle_wait(self.throttle, t), 0)
        auth.throttle_fail(self.throttle, t)                                           # пятая неудача подряд
        self.assertEqual(auth.throttle_wait(self.throttle, t), auth.BASE_LOCK)
        self.assertEqual(auth.throttle_wait(self.throttle, t + auth.BASE_LOCK + 1), 0)
        auth.throttle_fail(self.throttle, t + 100)
        self.assertEqual(auth.throttle_wait(self.throttle, t + 100), auth.BASE_LOCK * 2)
        for _ in range(30):
            auth.throttle_fail(self.throttle, t + 1000)
        self.assertEqual(auth.throttle_wait(self.throttle, t + 1000), auth.MAX_LOCK)    # дольше 15 минут не ждём
        auth.throttle_reset(self.throttle)
        self.assertEqual(auth.throttle_wait(self.throttle, t + 1000), 0)

    def test_throttle_survives_garbage(self):
        self.throttle.write_text("мусор")
        self.assertEqual(auth.throttle_wait(self.throttle), 0)
        auth.throttle_fail(self.throttle)

    def handle(self, payload, cookie=None, secure=False, now=None):
        return auth.handle(self.dir, payload, cookie, self.throttle, secure=secure, now=now)

    def test_handle_status_and_first_password(self):
        self.assertEqual(self.handle({"action": "status"})[1], {"ok": True, "enabled": False, "authed": True, "hint": ""})
        code, body, cookie = self.handle({"action": "set", "password": PASSWORD})            # пока пароля нет, задать можно без cookie
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertIn("dnevnik_auth=", cookie)
        self.assertEqual(self.handle({"action": "status"})[1], {"ok": True, "enabled": True, "authed": False, "hint": ""})
        self.assertEqual(self.handle({"action": "status"}, cookie.split(";")[0])[1]["authed"], True)

    def test_handle_login(self):
        auth.set_password(self.dir, PASSWORD)
        code, body, cookie = self.handle({"action": "login", "password": "не тот пароль"})
        self.assertEqual(code, 401)
        self.assertFalse(body["ok"])
        self.assertIsNone(cookie)
        self.assertNotIn(PASSWORD, json.dumps(body, ensure_ascii=False))
        code, body, cookie = self.handle({"action": "login", "password": PASSWORD})
        self.assertEqual(code, 200)
        self.assertTrue(auth.authed(self.dir, cookie.split(";")[0]))

    def test_cookie_flags(self):
        auth.set_password(self.dir, PASSWORD)
        plain = self.handle({"action": "login", "password": PASSWORD})[2]
        secure = self.handle({"action": "login", "password": PASSWORD}, secure=True)[2]
        for c in (plain, secure):
            self.assertIn("HttpOnly", c)
            self.assertIn("SameSite=Lax", c)
            self.assertIn("Path=/", c)
        self.assertNotIn("Secure", plain)                                                   # по http дома cookie с Secure не работала бы
        self.assertIn("Secure", secure)                                                     # по https из интернета обязательно
        self.assertIn("Max-Age=0", self.handle({"action": "logout"}, secure=True)[2])

    def test_handle_locks_out_guessing(self):
        auth.set_password(self.dir, PASSWORD)
        now = 5000.0
        for _ in range(auth.FREE_TRIES):
            self.assertEqual(self.handle({"action": "login", "password": "нет-нет-нет"}, now=now)[0], 401)
        code, body, cookie = self.handle({"action": "login", "password": PASSWORD}, now=now)   # даже верный пароль в этот момент не принимается
        self.assertEqual(code, 429)
        self.assertIsNone(cookie)
        self.assertEqual(self.handle({"action": "login", "password": PASSWORD}, now=now + auth.BASE_LOCK + 1)[0], 200)
        self.assertEqual(auth.throttle_wait(self.throttle, now + auth.BASE_LOCK + 2), 0)        # успех обнуляет счётчик

    def test_changing_needs_cookie_and_current_password(self):
        auth.set_password(self.dir, PASSWORD)
        cookie = self.handle({"action": "login", "password": PASSWORD})[2].split(";")[0]
        # без cookie: отказ с пометкой auth
        code, body, _ = self.handle({"action": "set", "current": PASSWORD, "password": "другой-пароль-1"})
        self.assertEqual((code, body.get("auth")), (401, True))
        # с cookie, но без текущего пароля (забытый открытым телефон): отказ
        code, body, _ = self.handle({"action": "set", "password": "другой-пароль-1"}, cookie)
        self.assertEqual(code, 401)
        self.assertTrue(auth.check_password(self.dir, PASSWORD))
        code, body, _ = self.handle({"action": "disable"}, cookie)
        self.assertEqual(code, 401)
        self.assertTrue(auth.enabled(self.dir))
        # с cookie и верным текущим паролем: меняется, старые cookie гаснут
        code, body, new_cookie = self.handle({"action": "set", "current": PASSWORD, "password": "другой-пароль-1"}, cookie)
        self.assertEqual(code, 200, body)
        self.assertFalse(auth.authed(self.dir, cookie))
        self.assertTrue(auth.authed(self.dir, new_cookie.split(";")[0]))
        # пустой новый пароль нельзя, короткий можно
        self.assertEqual(self.handle({"action": "set", "current": "другой-пароль-1", "password": "   "}, new_cookie.split(";")[0])[0], 400)
        self.assertEqual(self.handle({"action": "set", "current": "другой-пароль-1", "password": "7"}, new_cookie.split(";")[0])[0], 200)
        self.assertTrue(auth.check_password(self.dir, "7"))

    def test_hint_is_stored_cleaned_and_limited(self):
        self.assertTrue(auth.set_password(self.dir, PASSWORD, "  имя кота <b>и</b> год\x00 ")[0])
        self.assertEqual(auth.get_hint(self.dir), "имя кота bи/b год")
        self.assertTrue(auth.set_password(self.dir, PASSWORD, "я" * 500)[0])
        self.assertEqual(len(auth.get_hint(self.dir)), auth.HINT_MAX)
        self.assertTrue(auth.set_password(self.dir, PASSWORD)[0])                          # новый пароль без подсказки стирает старую подсказку
        self.assertEqual(auth.get_hint(self.dir), "")
        self.assertEqual(auth.get_hint(self.dir / "нет-такой-папки"), "")
        for bad in (None, 5, ["x"]):
            self.assertEqual(auth.clean_hint(bad), "")

    def test_hint_must_not_contain_the_password(self):
        for hint in (PASSWORD, "мой пароль: " + PASSWORD.upper(), f"  {PASSWORD}  "):
            ok, message = auth.set_password(self.dir, PASSWORD, hint)
            self.assertFalse(ok, hint)
            self.assertIn("не должна содержать", message)
        self.assertFalse(auth.enabled(self.dir))                                            # при отказе ничего не сохраняется
        self.assertTrue(auth.set_password(self.dir, PASSWORD, "семья, 123")[0])              # похожая, но другая подсказка допустима

    def test_changing_only_the_hint(self):
        auth.set_password(self.dir, PASSWORD, "старая подсказка")
        self.assertFalse(auth.set_hint(self.dir, "новая", "не-тот-пароль")[0])
        self.assertEqual(auth.get_hint(self.dir), "старая подсказка")
        self.assertFalse(auth.set_hint(self.dir, "тут " + PASSWORD, PASSWORD)[0])
        self.assertEqual(auth.get_hint(self.dir), "старая подсказка")
        self.assertTrue(auth.set_hint(self.dir, "новая подсказка", PASSWORD)[0])
        self.assertEqual(auth.get_hint(self.dir), "новая подсказка")
        self.assertTrue(auth.check_password(self.dir, PASSWORD))                              # пароль и cookie при этом не меняются
        self.assertEqual(auth.changed_at(self.dir) > 0, True)
        self.assertFalse(auth.set_hint(self.dir / "пусто", "x", PASSWORD)[0])

    def test_hint_in_requests(self):
        self.assertEqual(self.handle({"action": "hint", "hint": "x", "current": PASSWORD})[0], 400)         # пароля ещё нет
        code, body, cookie = self.handle({"action": "set", "password": PASSWORD, "hint": "имя кота"})
        self.assertEqual(code, 200)
        c = cookie.split(";")[0]
        status = self.handle({"action": "status"})[1]                                                       # без cookie: подсказка видна всем
        self.assertEqual((status["authed"], status["hint"]), (False, "имя кота"))
        code, body, _ = self.handle({"action": "set", "current": PASSWORD, "password": "другой-пароль-1", "hint": "тут другой-пароль-1"}, c)
        self.assertEqual(code, 400)                                                                         # подсказка с паролем: отказ, пароль прежний
        self.assertTrue(auth.check_password(self.dir, PASSWORD))
        self.assertEqual(self.handle({"action": "hint", "hint": "новая"}, c)[0], 401)                        # без текущего пароля нельзя
        self.assertEqual(self.handle({"action": "hint", "hint": "новая", "current": PASSWORD})[0], 401)      # без cookie нельзя
        code, body, _ = self.handle({"action": "hint", "hint": "новая подсказка", "current": PASSWORD}, c)
        self.assertEqual((code, body["ok"]), (200, True))
        self.assertEqual(auth.get_hint(self.dir), "новая подсказка")
        self.assertTrue(auth.authed(self.dir, c))                                                            # cookie после смены подсказки действует

    def test_page_data_carries_the_hint_only_for_authorised_people(self):
        out = self.dir / "page.json"
        out.write_text(json.dumps({"days": [], "status": {}}))
        auth.set_password(self.dir, PASSWORD, "имя кота")
        code, body = auth.private_page(self.dir, out, None)
        self.assertEqual(code, 401)
        self.assertNotIn("имя кота", body.decode())                                         # ответ «нужен пароль» подсказку не несёт: её берут через status
        cookie = self.handle({"action": "login", "password": PASSWORD})[2].split(";")[0]
        page = json.loads(auth.private_page(self.dir, out, cookie)[1])
        self.assertEqual(page["status"]["familyHint"], "имя кота")

    def test_disable(self):
        auth.set_password(self.dir, PASSWORD)
        cookie = self.handle({"action": "login", "password": PASSWORD})[2].split(";")[0]
        code, body, clear = self.handle({"action": "disable", "current": PASSWORD}, cookie)
        self.assertEqual(code, 200)
        self.assertFalse(auth.enabled(self.dir))
        self.assertIn("Max-Age=0", clear)

    def test_bad_requests(self):
        for payload in (None, [], "x", {"action": "format"}, {}):
            self.assertEqual(self.handle(payload)[0], 400, payload)


def run_cgi(script, home, body=None, method="POST", cookie=None, extra=None):
    data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
    env = {"PATH": os.environ["PATH"], "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(data)), "DNEVNIK_HOME": str(home),
           "PYTHONPATH": str(ROOT)}
    if cookie:
        env["HTTP_COOKIE"] = cookie
    env.update(extra or {})
    r = subprocess.run([sys.executable, str(ROUTER / script)], input=data, capture_output=True, env=env, timeout=30)
    head, _, payload = r.stdout.decode("utf-8").partition("\r\n\r\n")
    status = int(head.split("Status: ")[1].split()[0])
    return status, payload, head, r.stderr.decode()


class RouterCgiTests(unittest.TestCase):
    """Скрипты, которые запускает веб-сервер роутера. Пароль включён, cookie нет: ничего из дневника не отдаётся и ничего не меняется."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "data").mkdir()
        (self.home / "router").mkdir()
        self.out = self.home / "private" / "data.json"
        self.out.parent.mkdir()
        self.out.write_text(json.dumps({"updatedAt": "2026-11-12T01:00:00", "days": [{"date": "2026-11-12", "lessons": []}],
                                        "student": {"name": "СЕКРЕТНОЕ-ИМЯ"}, "status": {"needLogin": False}}, ensure_ascii=False))
        for name in ("session.json", "ids.json", "history.json"):
            (self.home / "data" / name).write_text("{}")
        sync = self.home / "router" / "sync.sh"
        sync.write_text("#!/bin/sh\ntouch \"$(dirname \"$0\")/sync-was-run\"\n")
        sync.chmod(0o755)
        self.extra = {"DNEVNIK_OUT": str(self.out), "DNEVNIK_AUTH_THROTTLE": str(self.home / "throttle"),
                      "DNEVNIK_LOGOUT_GAP": str(self.home / "gap"), "DNEVNIK_GAP_FILE": str(self.home / "gap2"),
                      "DNEVNIK_LAST_REFRESH": str(self.home / "last-refresh")}

    def tearDown(self):
        self.tmp.cleanup()

    def cgi(self, script, **kw):
        extra = {**self.extra, **kw.pop("extra", {})}
        return run_cgi(script, self.home, extra=extra, **kw)

    def login_cookie(self):
        auth.set_password(self.home / "data", PASSWORD)
        _, _, head, _ = self.cgi("auth.cgi", body={"action": "login", "password": PASSWORD})
        return re.search(r"Set-Cookie: (dnevnik_auth=[^;]+)", head).group(1)

    def test_data_is_open_while_no_password_is_set(self):
        status, payload, head, _ = self.cgi("data.cgi", method="GET")
        self.assertEqual(status, 200)
        page = json.loads(payload)
        self.assertEqual(page["days"][0]["date"], "2026-11-12")
        self.assertIs(page["status"]["family"], False)                    # страница узнаёт, что пароль не задан
        self.assertIn("no-store", head)

    def test_data_is_closed_without_cookie(self):
        auth.set_password(self.home / "data", PASSWORD)
        for cookie in (None, "dnevnik_auth=", "dnevnik_auth=1.abc", "other=1"):
            status, payload, head, _ = self.cgi("data.cgi", method="GET", cookie=cookie)
            self.assertEqual(status, 401, cookie)
            self.assertNotIn("СЕКРЕТНОЕ-ИМЯ", payload + head)
            self.assertTrue(json.loads(payload)["auth"])
            self.assertIn("Status: 401 Unauthorized", head)    # без пояснения после кода веб-сервер роутера (uhttpd) отвечает 200

    def test_data_is_served_with_cookie(self):
        cookie = self.login_cookie()
        status, payload, _, _ = self.cgi("data.cgi", method="GET", cookie=cookie)
        self.assertEqual(status, 200)
        page = json.loads(payload)
        self.assertEqual(page["student"]["name"], "СЕКРЕТНОЕ-ИМЯ")
        self.assertIs(page["status"]["family"], True)

    def test_data_cgi_refuses_writes_and_missing_file(self):
        self.assertEqual(self.cgi("data.cgi", method="POST")[0], 405)
        self.out.unlink()
        self.assertEqual(self.cgi("data.cgi", method="GET")[0], 404)

    def test_auth_cgi_login_flow(self):
        auth.set_password(self.home / "data", PASSWORD)
        status, payload, head, err = self.cgi("auth.cgi", body={"action": "login", "password": "неверный-пароль"})
        self.assertEqual(status, 401)
        self.assertNotIn("dnevnik_auth=", head)                                           # входного cookie после неверного пароля нет (метка устройства dnevnik_dev не в счёт)
        self.assertNotIn("неверный-пароль", payload + head + err)
        status, payload, head, _ = self.cgi("auth.cgi", body={"action": "login", "password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", head)
        self.assertNotIn("Secure", head)
        _, _, head, _ = self.cgi("auth.cgi", body={"action": "login", "password": PASSWORD}, extra={"HTTP_X_FORWARDED_PROTO": "https"})
        self.assertIn("Secure", head)                                      # через https cookie помечается Secure
        for host, expect in (("dnevnik.tail-example.ts.net", True),          # Tailscale Funnel: https, порта нет
                             ("dnevnik.tail-example.ts.net:8765", False),   # тот же хост по http через порт
                             ("dnevnik.lan", False), ("192.168.10.1:8765", False), ("100.64.0.1:8765", False), ("localhost", False), ("", False)):
            _, _, head, _ = self.cgi("auth.cgi", body={"action": "login", "password": PASSWORD}, extra={"HTTP_HOST": host})
            self.assertEqual("Secure" in head, expect, host)               # uhttpd заголовок X-Forwarded-Proto не передаёт, поэтому смотрим на имя

    def test_every_cgi_status_line_has_a_reason_phrase(self):
        """uhttpd принимает голое «Status: 401» за 200 (проверено на роутере), поэтому после кода всегда идёт пояснение."""
        auth.set_password(self.home / "data", PASSWORD)
        cases = [("auth.cgi", {"body": {"action": "login", "password": "нет-нет-нет-нет"}}, "401 Unauthorized"),
                 ("auth.cgi", {"method": "GET"}, "405 Method Not Allowed"),
                 ("auth.cgi", {"body": {"action": "format"}}, "400 Bad Request"),
                 ("data.cgi", {"method": "GET"}, "401 Unauthorized"),
                 ("refresh.cgi", {}, "401 Unauthorized"),
                 ("logout.cgi", {"body": {"action": "begin"}}, "401 Unauthorized"),
                 ("setup.cgi", {"body": {"login": "a", "password": "b"}}, "401 Unauthorized"),
                 ("logout.cgi", {"body": {"action": "begin"}, "method": "GET"}, "405 Method Not Allowed")]
        for script, kw, line in cases:
            head = self.cgi(script, **kw)[2]
            self.assertIn(f"Status: {line}\r\n", head + "\r\n", (script, kw))
        os.remove(self.out)
        self.assertIn("Status: 404 Not Found", self.cgi("data.cgi", method="GET", cookie=self.login_cookie())[2])
        self.assertEqual(auth.status_line(999), "Status: 999 Error")

    def test_auth_cgi_bad_requests(self):
        self.assertEqual(self.cgi("auth.cgi", method="GET")[0], 405)
        self.assertEqual(run_cgi("auth.cgi", self.home, method="POST", extra=self.extra)[0], 400)           # пустое тело
        self.assertEqual(self.cgi("auth.cgi", body={"action": "x" * 3000})[0], 400)                          # слишком большое
        self.assertEqual(self.cgi("auth.cgi", body={"action": "format"})[0], 400)

    def test_auth_cgi_throttles_guessing(self):
        auth.set_password(self.home / "data", PASSWORD)
        for _ in range(auth.FREE_TRIES):
            self.assertEqual(self.cgi("auth.cgi", body={"action": "login", "password": "нет-нет-нет"})[0], 401)
        self.assertEqual(self.cgi("auth.cgi", body={"action": "login", "password": PASSWORD})[0], 429)

    def test_hint_is_public_but_the_password_is_not(self):
        auth.set_password(self.home / "data", PASSWORD, "имя кота и год")
        status, payload, _, _ = self.cgi("auth.cgi", body={"action": "status"})              # без cookie: это экран входа
        body = json.loads(payload)
        self.assertEqual((status, body["enabled"], body["authed"], body["hint"]), (200, True, False, "имя кота и год"))
        self.assertNotIn(PASSWORD, payload)

    def test_password_can_be_set_and_data_gets_locked(self):
        status, _, head, _ = self.cgi("auth.cgi", body={"action": "set", "password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertTrue(auth.enabled(self.home / "data"))
        self.assertEqual(self.cgi("data.cgi", method="GET")[0], 401)
        cookie = re.search(r"Set-Cookie: (dnevnik_auth=[^;]+)", head).group(1)
        self.assertEqual(self.cgi("data.cgi", method="GET", cookie=cookie)[0], 200)

    def test_refresh_is_locked_and_rate_limited(self):
        self.assertEqual(self.cgi("refresh.cgi")[0], 200)                                  # без пароля работает, как раньше
        for _ in range(30):
            if (self.home / "router" / "sync-was-run").exists():
                break
            time.sleep(0.1)
        self.assertTrue((self.home / "router" / "sync-was-run").exists())
        (self.home / "router" / "sync-was-run").unlink()
        self.assertEqual(self.cgi("refresh.cgi")[0], 200)                                  # второй раз подряд: ответ ok, но обновление не запускается
        time.sleep(0.5)
        self.assertFalse((self.home / "router" / "sync-was-run").exists())
        cookie = self.login_cookie()
        os.remove(self.extra["DNEVNIK_LAST_REFRESH"])
        status, payload, _, _ = self.cgi("refresh.cgi")                                    # пароль включён, cookie нет
        self.assertEqual(status, 401)
        self.assertTrue(json.loads(payload)["auth"])
        time.sleep(0.5)
        self.assertFalse((self.home / "router" / "sync-was-run").exists())                # обновление не запущено
        self.assertEqual(self.cgi("refresh.cgi", cookie=cookie)[0], 200)

    def test_logout_and_setup_are_locked(self):
        auth.set_password(self.home / "data", PASSWORD)
        status, payload, _, _ = self.cgi("logout.cgi", body={"action": "begin"})
        self.assertEqual(status, 401)
        self.assertTrue(json.loads(payload)["auth"])
        self.assertFalse((self.home / "data" / "logout-pending.json").exists())
        self.assertTrue((self.home / "data" / "session.json").exists())
        status, payload, _, _ = self.cgi("setup.cgi", body={"login": "kate", "password": "supersecret-123"})
        self.assertEqual(status, 401)
        self.assertFalse((self.home / "data" / "credentials.json").exists())
        self.assertNotIn("supersecret-123", payload)

    def test_logout_and_setup_work_with_cookie(self):
        cookie = self.login_cookie()
        status, payload, _, _ = self.cgi("logout.cgi", body={"action": "begin"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["ok"])
        status, payload, _, _ = self.cgi("setup.cgi", body={"login": "", "password": ""}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Введите логин и пароль", json.loads(payload)["message"])

    def test_password_cli(self):
        def cli(*args):
            return subprocess.run([sys.executable, str(ROUTER / "password.py"), *args], capture_output=True, text=True,
                                  env={"PATH": os.environ["PATH"], "DNEVNIK_HOME": str(self.home), "PYTHONPATH": str(ROOT)})
        self.assertIn("выключен", cli("status").stdout)
        r = cli("set", PASSWORD)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(auth.check_password(self.home / "data", PASSWORD))
        self.assertIn("включён", cli("status").stdout)
        def cli_env(pw, hint=""):
            return subprocess.run([sys.executable, str(ROUTER / "password.py"), "set-env"], capture_output=True, text=True,
                                  env={"PATH": os.environ["PATH"], "DNEVNIK_HOME": str(self.home), "PYTHONPATH": str(ROOT),
                                       "DNEVNIK_NEW_PASSWORD": pw, "DNEVNIK_NEW_HINT": hint})
        r = cli_env("пароль из окружения", "кличка")                                            # так задаёт пароль установка с компьютера
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(auth.check_password(self.home / "data", "пароль из окружения"))
        self.assertEqual(auth.get_hint(self.home / "data"), "кличка")
        self.assertEqual(cli_env("  ").returncode, 1)                                          # пустой пароль нельзя и так
        self.assertEqual(cli_env("абв", "тут абв").returncode, 1)                              # подсказка с паролем нельзя и так
        self.assertEqual(cli("set", " ").returncode, 1)                                        # пустой пароль нельзя
        self.assertEqual(cli("set", "я").returncode, 0)                                         # а из одного символа можно
        self.assertTrue(auth.check_password(self.home / "data", "я"))
        r = cli("set", PASSWORD, "имя кота")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(auth.get_hint(self.home / "data"), "имя кота")
        r = cli("set", PASSWORD, "подсказка " + PASSWORD)                                   # пароль в подсказке: отказ
        self.assertEqual(r.returncode, 1)
        self.assertEqual(auth.get_hint(self.home / "data"), "имя кота")
        self.assertEqual(cli("bogus").returncode, 2)
        self.assertEqual(cli("off").returncode, 0)
        self.assertFalse(auth.enabled(self.home / "data"))


class RouterLayoutTests(unittest.TestCase):
    """Данные дневника не должны лежать в папке, которую раздаёт веб-сервер: иначе пароль можно обойти прямой ссылкой."""

    def read(self, rel):
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_nothing_writes_data_into_the_public_folder(self):
        for rel in ("router/dnevnik.init", "router/sync.sh"):
            text = self.read(rel)
            self.assertNotIn('$WWW/data.json', text.replace('rm -f "$WWW/data.json"', ""), rel)
            self.assertIn("$PRIV/data.json", text, rel)
        self.assertIn('/tmp/dnevnik-private/data.json', self.read("router/logout.cgi"))
        self.assertIn('/tmp/dnevnik-private/data.json', self.read("router/data.cgi"))
        self.assertIn('PRIV = os.environ.get("DNEVNIK_PRIV", "/tmp/dnevnik-private")', self.read("luci/rpcd/luci.dnevnik"))

    def test_old_public_copies_are_removed_on_start(self):
        init = self.read("router/dnevnik.init")
        self.assertIn('rm -f "$WWW/data.json" "$WWW/offline.html"', init)       # в offline.html те же данные лежали открыто
        self.assertIn('chmod 700 "$PRIV"', init)
        self.assertIn('cp -f "$APP/router/data.cgi" "$WWW/cgi-bin/data"', init)
        self.assertIn('cp -f "$APP/router/auth.cgi" "$WWW/cgi-bin/auth"', init)

    def test_installer_and_deploy_ship_the_new_files(self):
        install, deploy = self.read("router/install.sh"), self.read("router/deploy.sh")
        for name in ("auth.py", "router/auth.cgi", "router/data.cgi", "router/password.py"):
            self.assertIn(name, install, name)
        self.assertIn("core.py auth.py devices.py notify.py push.py sync.py", deploy)
        self.assertIn("dnevnik-private", self.read("router/uninstall.sh"))

    def test_web_folder_holds_no_personal_data(self):
        web = ROOT / "web"
        for f in web.iterdir():
            self.assertNotIn("data.json", f.name)
        html = (web / "index.html").read_text(encoding="utf-8")
        self.assertIn("/*INLINE_DATA*/null", html)                                 # в самой странице данных нет, только место для них
        self.assertNotIn('fetch("data.json"', html)                                 # страница берёт данные только через cgi-bin/data

    def test_scripts_are_executable(self):
        for name in ("auth.cgi", "data.cgi", "refresh.cgi", "password.py"):
            self.assertTrue(os.access(ROUTER / name, os.X_OK), name)
            self.assertTrue((ROUTER / name).read_text(encoding="utf-8").startswith("#!/usr/bin/python3"), name)


class ServerAuthTests(unittest.TestCase):
    """Сервер для Mac и разработки: те же правила, что на роутере."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (server.DEMO, server.AUTH_DIR)
        server.DEMO, server.AUTH_DIR = True, Path(self.tmp.name)

    def tearDown(self):
        server.DEMO, server.AUTH_DIR = self.old
        self.tmp.cleanup()

    def req(self, path, method="GET", body=None, cookie=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if cookie:
            r.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read().decode(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers

    def test_open_without_password(self):
        code, body, _ = self.req("/cgi-bin/data")
        self.assertEqual(code, 200)
        self.assertIs(json.loads(body)["status"]["family"], False)
        self.assertEqual(self.req("/data.json")[0], 200)

    def test_everything_private_is_closed_with_password(self):
        auth.set_password(server.AUTH_DIR, PASSWORD)
        for path in ("/cgi-bin/data", "/data.json", "/offline.html"):
            code, body, _ = self.req(path)
            self.assertEqual(code, 401, path)
            self.assertNotIn("Математика", body)
        for path in ("/cgi-bin/setup", "/cgi-bin/logout", "/cgi-bin/refresh"):
            code, body, _ = self.req(path, "POST", {"action": "begin"})
            self.assertEqual(code, 401, path)
            self.assertTrue(json.loads(body)["auth"])
        code, body, _ = self.req("/")                                               # сама страница открыта: на ней форма пароля
        self.assertEqual(code, 200)

    def test_hint_is_visible_before_login(self):
        auth.set_password(server.AUTH_DIR, PASSWORD, "имя кота")
        code, body, _ = self.req("/cgi-bin/auth", "POST", {"action": "status"})
        self.assertEqual((code, json.loads(body)["hint"]), (200, "имя кота"))
        self.assertNotIn("имя кота", self.req("/cgi-bin/data")[1])

    def test_login_then_data(self):
        auth.set_password(server.AUTH_DIR, PASSWORD)
        code, _, _ = self.req("/cgi-bin/auth", "POST", {"action": "login", "password": "неверный-пароль"})
        self.assertEqual(code, 401)
        code, body, headers = self.req("/cgi-bin/auth", "POST", {"action": "login", "password": PASSWORD})
        self.assertEqual(code, 200)
        cookie = headers["Set-Cookie"].split(";")[0]
        code, body, _ = self.req("/cgi-bin/data", cookie=cookie)
        self.assertEqual(code, 200)
        self.assertIs(json.loads(body)["status"]["family"], True)
        self.assertEqual(self.req("/offline.html", cookie=cookie)[0], 200)

    def test_demo_without_override_has_no_password(self):
        server.AUTH_DIR = None                                                      # в демо пароля нет и настоящие файлы не читаются
        code, body, _ = self.req("/cgi-bin/auth", "POST", {"action": "set", "password": PASSWORD})
        self.assertFalse(json.loads(body)["ok"])
        self.assertEqual(self.req("/cgi-bin/data")[0], 200)


@unittest.skipIf(sync_playwright is None, "Playwright не установлен")
class AuthUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (server.DEMO, server.AUTH_DIR)
        server.DEMO, server.AUTH_DIR = True, Path(self.tmp.name)

    def tearDown(self):
        server.DEMO, server.AUTH_DIR = self.old
        self.tmp.cleanup()

    def open(self, context=None, width=390, height=800):
        context = context or self.browser.new_context(viewport={"width": width, "height": height}, has_touch=True)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "status of 40" not in m.text else None)   # отказы сервера 400 и 401 ожидаемы
        page.goto(self.url)
        return context, page, errors

    def test_locked_page_shows_only_the_password_form(self):
        auth.set_password(server.AUTH_DIR, PASSWORD)
        _, page, errors = self.open()
        page.wait_for_selector("#lock-form")
        self.assertIn("Пароль семьи", page.inner_text("#main"))
        for hidden in ("nav.tabs", "#gear", "#refresh"):
            self.assertFalse(page.is_visible(hidden), hidden)
        self.assertNotIn("Тест", page.inner_text("header"))                        # имя ученика не светится
        self.assertNotIn("Математика", page.content())
        self.assertEqual(errors, [])

    def test_wrong_password_then_right_password(self):
        auth.set_password(server.AUTH_DIR, PASSWORD)
        _, page, errors = self.open()
        page.wait_for_selector("#lock-form")
        page.fill("#l-pass", "неверный-пароль")
        page.click("button.go")
        page.wait_for_selector(".msg.bad")
        self.assertIn("Неверный пароль", page.inner_text(".msg.bad"))
        page.fill("#l-pass", PASSWORD)
        page.click("button.go")
        page.wait_for_selector("nav.tabs", state="visible")
        page.wait_for_selector("#hello:has-text('Тест')")
        self.assertNotIn(PASSWORD, page.content())                                 # пароль нигде в странице не остался
        self.assertEqual(errors, [])

    def test_lock_is_recognised_even_if_the_web_server_answers_200(self):
        """uhttpd на роутере мог бы отдать код 200 вместе с телом «нужен пароль»: страница должна всё равно показать форму пароля."""
        _, page, errors = self.open()
        page.wait_for_selector("nav.tabs", state="visible")
        page.route("**/cgi-bin/data", lambda route: route.fulfill(status=200, json={"ok": False, "auth": True, "message": "Нужен пароль семьи."}))
        page.evaluate("load()")
        page.wait_for_selector("#lock-form")
        self.assertFalse(page.is_visible("nav.tabs"))
        self.assertEqual(errors, [])

    def test_lock_screen_shows_hint_and_forgot_help(self):
        auth.set_password(server.AUTH_DIR, PASSWORD, "имя кота и год")
        _, page, errors = self.open()
        page.wait_for_selector("#lock-form")
        page.wait_for_selector("text=Подсказка:")
        self.assertIn("имя кота и год", page.inner_text("#main"))
        self.assertNotIn(PASSWORD, page.content())
        self.assertFalse(page.is_visible("text=Службы → Дневник → Пароль семьи"))              # пояснение свёрнуто
        page.click("summary:has-text('Забыли пароль?')")
        self.assertTrue(page.is_visible("text=Службы → Дневник → Пароль семьи"))
        self.assertIn("нельзя", page.inner_text("details"))                                    # со страницы пароль не сбросить: так и написано
        self.assertEqual(errors, [])

    def test_lock_screen_without_hint_has_no_hint_line(self):
        auth.set_password(server.AUTH_DIR, PASSWORD)
        _, page, _ = self.open()
        page.wait_for_selector("#lock-form")
        page.wait_for_timeout(300)
        self.assertNotIn("Подсказка:", page.inner_text("#main"))

    def test_hint_in_settings(self):
        context, page, errors = self.open()
        page.wait_for_selector("nav.tabs", state="visible")
        page.click("#gear")
        page.click("[data-pass=set]")
        page.fill("#p-new", PASSWORD)
        page.fill("#p-new2", PASSWORD)
        page.fill("#p-hint", "тут " + PASSWORD)                                                  # пароль в подсказке: сервер отказывает
        page.click("#pass-form button[type=submit]")
        page.wait_for_selector(".set-card .msg.bad:has-text('не должна содержать')")
        self.assertFalse(auth.enabled(server.AUTH_DIR))
        page.fill("#p-new", PASSWORD)
        page.fill("#p-new2", PASSWORD)
        page.fill("#p-hint", "имя кота")
        page.click("#pass-form button[type=submit]")
        page.wait_for_selector(".set-card .msg.good")
        self.assertEqual(auth.get_hint(server.AUTH_DIR), "имя кота")
        self.assertIn("Подсказка: «имя кота»", page.inner_text(".settings"))
        page.click("[data-pass=hint]")                                                            # поменять только подсказку
        self.assertEqual(page.input_value("#p-hint"), "имя кота")                                  # текущая подсказка подставлена
        page.fill("#p-cur", "не-тот-пароль")
        page.fill("#p-hint", "кличка")
        page.click("#pass-form button[type=submit]")
        page.wait_for_selector(".set-card .msg.bad:has-text('Неверный текущий')")
        self.assertEqual(auth.get_hint(server.AUTH_DIR), "имя кота")
        page.fill("#p-cur", PASSWORD)
        page.fill("#p-hint", "кличка")
        page.click("#pass-form button[type=submit]")
        page.wait_for_selector(".set-card .msg.good:has-text('Подсказка сохранена')")
        self.assertEqual(auth.get_hint(server.AUTH_DIR), "кличка")
        self.assertTrue(auth.check_password(server.AUTH_DIR, PASSWORD))
        self.assertFalse(page.is_visible("#lock-form"))                                           # это устройство остаётся в системе
        self.assertEqual(errors, [])

    def test_empty_password_is_refused_without_a_request(self):
        auth.set_password(server.AUTH_DIR, PASSWORD)
        _, page, _ = self.open()
        page.wait_for_selector("#lock-form")
        page.click("button.go")
        page.wait_for_selector(".msg.bad")
        self.assertIn("Введите пароль", page.inner_text(".msg.bad"))

    def test_device_remembers_the_password_and_other_devices_do_not(self):
        auth.set_password(server.AUTH_DIR, PASSWORD)
        context, page, _ = self.open()
        page.wait_for_selector("#lock-form")
        page.fill("#l-pass", PASSWORD)
        page.click("button.go")
        page.wait_for_selector("nav.tabs", state="visible")
        page.reload()                                                              # после перезагрузки пароль снова не спрашивают
        page.wait_for_selector("nav.tabs", state="visible")
        self.assertFalse(page.is_visible("#lock-form"))
        _, other, _ = self.open()                                                  # другое устройство: свой набор cookie
        other.wait_for_selector("#lock-form")

    def test_password_changed_elsewhere_locks_this_device_on_next_action(self):
        auth.set_password(server.AUTH_DIR, PASSWORD)
        context, page, _ = self.open()
        page.wait_for_selector("#lock-form")
        page.fill("#l-pass", PASSWORD)
        page.click("button.go")
        page.wait_for_selector("nav.tabs", state="visible")
        auth.set_password(server.AUTH_DIR, "совсем-другой-пароль")                  # пароль сменили с другого телефона
        page.reload()
        page.wait_for_selector("#lock-form")                                       # старая cookie больше не подходит
        self.assertFalse(page.is_visible("nav.tabs"))

    def test_set_change_and_disable_from_settings(self):
        context, page, errors = self.open()
        page.wait_for_selector("nav.tabs", state="visible")
        page.click("#gear")
        page.wait_for_selector(".settings")
        self.assertIn("Не задан", page.inner_text(".settings"))
        page.click("[data-pass=set]")
        page.fill("#p-new", "")
        page.fill("#p-new2", "")
        page.click("#pass-form button[type=submit]")
        page.wait_for_selector(".set-card .msg.bad")
        self.assertIn("Введите пароль", page.inner_text(".set-card .msg.bad"))
        self.assertIn("хоть один символ", page.inner_text("#pass-form"))
        self.assertIn("легко подобрать", page.inner_text("#pass-form"))                          # предупреждение есть, запрета нет
        page.fill("#p-new", PASSWORD)
        page.fill("#p-new2", PASSWORD + "1")
        page.click("#pass-form button[type=submit]")
        page.wait_for_selector(".set-card .msg.bad:has-text('не совпадают')")
        self.assertFalse(auth.enabled(server.AUTH_DIR))
        page.fill("#p-new", PASSWORD)
        page.fill("#p-new2", PASSWORD)
        page.click("#pass-form button[type=submit]")
        page.wait_for_selector(".set-card .msg.good")
        self.assertTrue(auth.check_password(server.AUTH_DIR, PASSWORD))
        self.assertIn("Включён", page.inner_text(".settings"))
        self.assertFalse(page.is_visible("#lock-form"))                            # это устройство сразу получило доступ
        _, other, _ = self.open()                                                  # а другое теперь просит пароль
        other.wait_for_selector("#lock-form")
        # смена: нужен текущий пароль
        page.click("[data-pass=change]")
        page.fill("#p-cur", "не-тот-пароль")
        page.fill("#p-new", "новый-пароль-456")
        page.fill("#p-new2", "новый-пароль-456")
        page.click("#pass-form button[type=submit]")
        page.wait_for_selector(".set-card .msg.bad:has-text('Неверный текущий')")
        self.assertTrue(auth.check_password(server.AUTH_DIR, PASSWORD))
        page.fill("#p-cur", PASSWORD)                                              # после ошибки форма очищается: вводим заново
        page.fill("#p-new", "новый-пароль-456")
        page.fill("#p-new2", "новый-пароль-456")
        page.click("#pass-form button[type=submit]")
        page.wait_for_selector(".set-card .msg.good")
        self.assertTrue(auth.check_password(server.AUTH_DIR, "новый-пароль-456"))
        # отключение
        page.click("[data-pass=disable]")
        page.fill("#p-cur", "новый-пароль-456")
        page.click("#pass-form button[type=submit]")
        page.wait_for_selector(".set-card .msg.good")
        self.assertFalse(auth.enabled(server.AUTH_DIR))
        self.assertIn("Не задан", page.inner_text(".settings"))
        self.assertEqual(errors, [])

    def test_logout_on_this_device(self):
        auth.set_password(server.AUTH_DIR, PASSWORD)
        context, page, _ = self.open()
        page.wait_for_selector("#lock-form")
        page.fill("#l-pass", PASSWORD)
        page.click("button.go")
        page.wait_for_selector("nav.tabs", state="visible")
        page.click("#gear")
        page.click("[data-pass=logout]")
        page.wait_for_selector("#lock-form")
        self.assertFalse(page.is_visible("nav.tabs"))
        self.assertIsNone(page.evaluate("localStorage.getItem('cache')"))          # сохранённая копия дневника стёрта с устройства
        page.reload()
        page.wait_for_selector("#lock-form")                                       # и после перезагрузки снова просит пароль


if __name__ == "__main__":
    unittest.main()
