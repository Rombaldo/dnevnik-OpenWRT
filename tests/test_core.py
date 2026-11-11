import http.server
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

import core


# ---------------------------------------------------------------- разбор данных
def raw_day(lessons, day=(2026, 11, 11)):
    ts = int(datetime(*day, tzinfo=timezone.utc).timestamp())
    return {"date": str(ts), "lessons": lessons}


def raw_lesson(**kw):
    base = {
        "number": 1, "hours": {"startHour": "8", "startMinute": "00", "endHour": "8", "endMinute": "40"},
        "isCanceled": False, "isEmpty": False, "theme": "Тема", "groupId": "g",
        "subject": {"id": "s1", "name": "Рус. язык"},
        "homework": {"text": "  упр 1  ", "attachments": []},
        "workMarks": [],
    }
    base.update(kw)
    return base


class NormDayTests(unittest.TestCase):
    def test_basic_fields(self):
        d = core.norm_day(raw_day([raw_lesson()]))
        self.assertEqual(d["date"], "2026-11-11")
        self.assertEqual(d["weekday"], "ср")
        l = d["lessons"][0]
        self.assertEqual((l["n"], l["start"], l["end"]), (1, "8:00", "8:40"))
        self.assertEqual((l["subject"], l["subjectId"]), ("Рус. язык", "s1"))
        self.assertEqual(l["homework"], "упр 1")

    def test_marks_are_flattened(self):
        wm = [{"workName": "Ответ", "marks": [{"value": "5", "createdDate": "2026-11-11T12:00:00"}]}]
        l = core.norm_day(raw_day([raw_lesson(workMarks=wm)]))["lessons"][0]
        self.assertEqual(l["marks"], [{"value": "5", "work": "Ответ", "created": "2026-11-11T12:00:00"}])

    def test_empty_lesson_without_subject_is_skipped(self):
        self.assertEqual(core.norm_day(raw_day([raw_lesson(isEmpty=True, subject=None)]))["lessons"], [])

    def test_canceled_flag(self):
        self.assertTrue(core.norm_day(raw_day([raw_lesson(isCanceled=True)]))["lessons"][0]["canceled"])

    def test_attachments_become_links(self):
        att = {"text": "см. файл", "attachments": [
            {"fileId": "42", "fileName": "Карта", "fileExtension": "PDF", "size": 1000, "fileLink": "https://b5.csdnevnik.ru/x.pdf?a=1"},
            {"fileId": "43", "fileName": "Чужой", "fileExtension": "pdf", "size": 1, "fileLink": "https://evil.example.com/a.pdf"},
            {"fileName": "без номера"},   # без числового id не берём
        ]}
        files = core.norm_day(raw_day([raw_lesson(homework=att)]))["lessons"][0]["files"]
        self.assertEqual(files[0], {"id": "42", "name": "Карта", "ext": "pdf", "size": 1000,
                                    "url": "https://b5.csdnevnik.ru/x.pdf?a=1"})
        self.assertEqual(files[1]["url"], "")     # ссылку с чужого сервера не отдаём
        self.assertEqual(len(files), 2)

    def test_missing_homework(self):
        l = core.norm_day(raw_day([raw_lesson(homework=None)]))["lessons"][0]
        self.assertEqual((l["homework"], l["files"]), ("", []))

    def test_trusted_link(self):
        self.assertTrue(core.trusted_link("https://b5.csdnevnik.ru/a.pdf?x=1"))
        self.assertTrue(core.trusted_link("https://dnevnik.ru/f"))
        for bad in ["http://b5.csdnevnik.ru/a.pdf", "https://evil.example.com/a.pdf", "https://csdnevnik.ru.evil.com/a",
                    "file:///etc/passwd", "javascript:alert(1)", "", None]:
            self.assertFalse(core.trusted_link(bad), bad)


# ---------------------------------------------------------------- обновление с поддельным сайтом
class FakeSite(http.server.BaseHTTPRequestHandler):
    good_token = "good"
    requests = 0
    login_posts = 0
    only_school = None          # если задано, дневник отдаёт уроки только для этой школы
    captcha = False             # форма входа просит капчу
    password = "secret"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=()):
        raw = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def login_form(self, error=""):
        captcha_id = "abc123" if FakeSite.captcha else ""
        return (f'<form class="login" method="POST"><input type="hidden" name="exceededAttempts" value="False">'
                f'<input type="hidden" name="ReturnUrl" value=""><input type="hidden" name="FingerprintId" value="">'
                f'<input type="hidden" name="Captcha.Id" value="{captcha_id}"><input type="text" name="login">'
                f'<input type="password" name="password"></form>{error}')

    def do_POST(self):
        FakeSite.login_posts += 1
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        if self.path.startswith("/login/") and form.get("login") == ["kate"] and form.get("password") == [FakeSite.password] and not FakeSite.captcha:
            self._send(302, "", headers=[("Location", "/userfeed"),
                                         ("Set-Cookie", f"DnevnikAuth_a={FakeSite.good_token}; Path=/; HttpOnly"),
                                         ("Set-Cookie", "dnevnik_sst=fresh; Path=/; HttpOnly")])
        else:
            self._send(200, self.login_form('<div class="error">Ошибка в логине или пароле. Проверьте правильность заполнения полей.</div>'))

    def do_GET(self):
        FakeSite.requests += 1
        path = self.path.split("?")[0]
        if path == "/login/":
            return self._send(200, self.login_form())
        if path == "/userfeed":
            return self._send(200, "<html>лента</html>")
        authed = f"DnevnikAuth_a={FakeSite.good_token}" in (self.headers.get("Cookie") or "")
        if path == "/marks":
            if not authed:
                return self._send(401, "<html>login</html>")
            return self._send(200, '<a href="/statistics?childPersonId=1111111111">r</a> '
                                   '<a href="/v2/homework?school=3333333333">x</a> <a href="/v2/homework?school=2222222222">y</a>')
        if "/api/v2/marks/diary" not in self.path or not authed:
            return self._send(401, "<html>login</html>")
        if FakeSite.only_school and f"schoolId={FakeSite.only_school}" not in self.path:
            return self._send(200, json.dumps({"days": []}), "application/json")
        att = {"text": "читать", "attachments": [{"fileId": "7", "fileName": "ГЕО", "fileExtension": "pdf", "size": 5,
                                                  "fileLink": "https://b5.csdnevnik.ru/geo.pdf"}]}
        body = json.dumps({"days": [raw_day([raw_lesson(homework=att)]), raw_day([raw_lesson()], day=(2026, 11, 12))]}).encode()
        self._send(200, body, "application/json", headers=[("Set-Cookie", "dnevnik_sst=rotated; Path=/; Max-Age=86400; HttpOnly")])


class SyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        FakeSite.good_token = "good"
        FakeSite.only_school, FakeSite.captcha, FakeSite.password, FakeSite.login_posts = None, False, "secret", 0
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data, self.www = root / "data", root / "www"
        self.data.mkdir(), self.www.mkdir()
        (self.www / "index.html").write_text("<script>window.__DATA__ = /*INLINE_DATA*/null;</script>")
        (self.data / "ids.json").write_text(json.dumps({"personId": "1", "schoolId": "2"}))
        self.write_session("good")
        self.out = self.www / "data.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_session(self, token):
        cookies = [
            {"name": "DnevnikAuth_a", "value": token, "domain": "127.0.0.1", "path": "/", "expires": time.time() + 86400 * 14, "httpOnly": True},
            {"name": "dnevnik_sst", "value": "old", "domain": "127.0.0.1", "path": "/", "expires": time.time() + 3600, "httpOnly": True},
        ]
        (self.data / "session.json").write_text(json.dumps(cookies))

    def run_sync(self, **kw):
        return core.sync(self.data, self.out, base=self.base, today=date(2026, 11, 11), **kw)

    def test_successful_sync_writes_everything(self):
        payload = self.run_sync()
        self.assertEqual(payload["status"], {"needLogin": False, "error": None, "syncing": False})
        saved = json.loads(self.out.read_text())
        self.assertEqual([d["date"] for d in saved["days"]], ["2026-11-11", "2026-11-12"])
        self.assertEqual(saved["days"][0]["lessons"][0]["files"][0]["url"], "https://b5.csdnevnik.ru/geo.pdf")
        self.assertTrue((self.data / "history.json").exists())
        offline = (self.www / "offline.html").read_text()
        self.assertIn('"days"', offline)
        self.assertNotIn("/*INLINE_DATA*/null", offline)

    def test_rotated_cookie_is_saved_and_files_are_private(self):
        self.run_sync()
        names = {c["name"]: c["value"] for c in json.loads((self.data / "session.json").read_text())}
        self.assertEqual(names["dnevnik_sst"], "rotated")
        self.assertEqual(names["DnevnikAuth_a"], "good")
        for f in ("session.json", "history.json"):
            self.assertEqual(oct(os.stat(self.data / f).st_mode)[-3:], "600", f)

    def test_expired_login_keeps_old_data_and_flags_it(self):
        first = self.run_sync()
        FakeSite.good_token = "other"                      # сайт перестал узнавать вход
        with self.assertRaises(core.NeedLogin):
            self.run_sync()
        saved = json.loads(self.out.read_text())
        self.assertTrue(saved["status"]["needLogin"])
        self.assertEqual(len(saved["days"]), 2)           # старые данные остались
        self.assertEqual(saved["updatedAt"], first["updatedAt"])

    def test_no_session_means_login_needed(self):
        (self.data / "session.json").unlink()
        with self.assertRaises(core.NeedLogin):
            self.run_sync()
        self.assertTrue(json.loads(self.out.read_text())["status"]["needLogin"])

    def test_flash_writes_are_limited(self):
        self.run_sync()
        old = time.time() - 3600
        for f in ("history.json", "session.json"):
            os.utime(self.data / f, (old, old))
        self.run_sync(session_save_hours=6)
        self.assertAlmostEqual(os.stat(self.data / "history.json").st_mtime, old, delta=2)   # данные не менялись - не пишем
        self.assertAlmostEqual(os.stat(self.data / "session.json").st_mtime, old, delta=2)   # вход свежий - не пишем

    def test_page_files_are_readable_by_web_server_under_strict_umask(self):
        import stat
        old = os.umask(0o077)           # как было на роутере из-за чужого umask
        try:
            self.run_sync()
        finally:
            os.umask(old)
        for name in ("data.json", "offline.html"):
            mode = stat.S_IMODE(os.stat(self.www / name).st_mode)
            self.assertTrue(mode & 0o004, f"{name}: права {oct(mode)}, веб-сервер его не отдаст")
        # а вход и история при этом закрыты
        self.assertEqual(stat.S_IMODE(os.stat(self.data / "session.json").st_mode), 0o600)

    def test_rebuild_after_reboot_uses_saved_history(self):
        self.run_sync()
        self.out.unlink()                                   # как после перезагрузки роутера: оперативная память пуста
        core.rebuild(self.data, self.out)
        saved = json.loads(self.out.read_text())
        self.assertEqual(len(saved["days"]), 2)
        self.assertTrue(saved["updatedAt"])
        self.assertFalse(saved["status"]["needLogin"])
        (self.data / "session.json").unlink()
        core.rebuild(self.data, self.out)
        self.assertTrue(json.loads(self.out.read_text())["status"]["needLogin"])

    def test_server_error_keeps_data_and_reports(self):
        self.run_sync()
        bad = core.Client.diary
        core.Client.diary = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("дневник ответил 500"))
        try:
            with self.assertRaises(RuntimeError):
                self.run_sync()
        finally:
            core.Client.diary = bad
        saved = json.loads(self.out.read_text())
        self.assertFalse(saved["status"]["needLogin"])
        self.assertIn("500", saved["status"]["error"])
        self.assertEqual(len(saved["days"]), 2)


class PasswordLoginTests(unittest.TestCase):
    """Вход по логину и паролю (без Госуслуг и без Mac)."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        FakeSite.good_token, FakeSite.only_school, FakeSite.captcha, FakeSite.password, FakeSite.login_posts = "good", None, False, "secret", 0
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data, self.out = self.root / "data", self.root / "www" / "data.json"

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, **kw):
        return core.sync(self.data, self.out, base=self.base, today=date(2026, 11, 11), **kw)

    def setup(self, login="kate", password="secret"):
        return core.setup_login(self.data, login, password, base=self.base)

    # --- форма входа
    def test_login_success_returns_session(self):
        jar = core.login_with_password("kate", "secret", self.base + "/login/", self.base)
        self.assertIn("DnevnikAuth_a", [c.name for c in jar])

    def test_wrong_password_is_reported(self):
        with self.assertRaises(core.LoginFailed) as cm:
            core.login_with_password("kate", "nope", self.base + "/login/", self.base)
        self.assertEqual(cm.exception.kind, "wrong")
        self.assertIn("Неверный логин или пароль", str(cm.exception))

    def test_captcha_is_reported_clearly(self):
        FakeSite.captcha = True
        with self.assertRaises(core.LoginFailed) as cm:
            core.login_with_password("kate", "secret", self.base + "/login/", self.base)
        self.assertEqual(cm.exception.kind, "captcha")

    def test_no_connection_is_not_a_wrong_password(self):
        with self.assertRaises(core.LoginFailed) as cm:
            core.login_with_password("kate", "secret", "http://127.0.0.1:9/login/", "http://127.0.0.1:9")
        self.assertEqual(cm.exception.kind, "network")

    def test_hidden_fields_are_parsed(self):
        html = '<input type="hidden" name="a.b" value="x&amp;y"><input name="t" type="text"><input value="9" name="z" type="hidden">'
        self.assertEqual(core.parse_login_form(html), {"a.b": "x&y", "z": "9"})

    def test_password_is_never_printed_in_messages(self):
        for pw in ("nope-pass-123", ""):
            ok, msg = self.setup(password=pw)
            self.assertFalse(ok)
            self.assertNotIn("nope-pass-123", msg)

    # --- номера ученика и школы
    def test_ids_are_found_and_the_right_school_is_picked(self):
        FakeSite.only_school = "2222222222"          # у второй школы уроков нет
        jar = core.login_with_password("kate", "secret", self.base + "/login/", self.base)
        ids = core.discover_ids(core.Client(jar, base=self.base), today=date(2026, 11, 11))
        self.assertEqual(ids, {"personId": "1111111111", "schoolId": "2222222222"})

    # --- настройка со страницы
    def test_setup_saves_login_and_loads_data(self):
        ok, msg = self.setup()
        self.assertTrue(ok, msg)
        for f in ("credentials.json", "session.json", "ids.json"):
            self.assertEqual(oct(os.stat(self.data / f).st_mode)[-3:], "600", f)
        self.assertEqual(json.loads((self.data / "credentials.json").read_text()), {"login": "kate", "password": "secret"})
        payload = self.sync()
        self.assertEqual(len(payload["days"]), 2)

    def test_failed_setup_saves_nothing_and_keeps_old_login(self):
        self.assertTrue(self.setup()[0])
        before = (self.data / "credentials.json").read_text()
        ok, msg = self.setup(password="wrong")
        self.assertFalse(ok)
        self.assertEqual((self.data / "credentials.json").read_text(), before)     # рабочий вход не затёрт

    def test_setup_rejects_empty_and_huge_input(self):
        self.assertFalse(self.setup("", "x")[0])
        self.assertFalse(self.setup("kate", "")[0])
        self.assertFalse(self.setup("k" * 500, "x")[0])
        self.assertFalse((self.data / "credentials.json").exists())

    # --- автоматический вход при обновлении
    def test_sync_logs_in_by_itself_when_session_expired(self):
        self.assertTrue(self.setup()[0])
        (self.data / "session.json").write_text("[]")           # вход «истёк»
        payload = self.sync()
        self.assertFalse(payload["status"]["needLogin"])
        names = {c["name"] for c in json.loads((self.data / "session.json").read_text())}
        self.assertIn("DnevnikAuth_a", names)                    # новый вход сохранён

    def test_sync_logs_in_with_only_a_password_and_no_session(self):
        self.assertTrue(self.setup()[0])
        (self.data / "session.json").unlink()
        (self.data / "ids.json").unlink()                        # даже номера ученика находит сам
        payload = self.sync()
        self.assertEqual(len(payload["days"]), 2)
        self.assertTrue((self.data / "ids.json").exists())

    def test_without_login_the_page_asks_for_it(self):
        with self.assertRaises(core.NeedLogin):
            self.sync()
        saved = json.loads(self.out.read_text())
        self.assertTrue(saved["status"]["needLogin"])
        self.assertIn("логин и пароль", saved["status"]["error"])

    def test_wrong_saved_password_is_not_retried_in_a_loop(self):
        self.assertTrue(self.setup()[0])
        FakeSite.password = "changed-on-site"                   # пароль на сайте сменили
        (self.data / "session.json").write_text("[]")
        with self.assertRaises(core.NeedLogin):
            self.sync()
        posts = FakeSite.login_posts
        with self.assertRaises(core.NeedLogin):
            self.sync()                                          # второй раз сайт не трогаем: чтобы не заблокировали
        self.assertEqual(FakeSite.login_posts, posts)
        self.assertTrue(json.loads(self.out.read_text())["status"]["needLogin"])
        # человек ввёл новый пароль: повтор разрешён
        FakeSite.password = "new-pass"
        self.assertTrue(self.setup(password="new-pass")[0])
        self.assertFalse(self.sync()["status"]["needLogin"])

    def test_no_internet_on_relogin_is_an_error_not_a_login_request(self):
        self.assertTrue(self.setup()[0])
        (self.data / "session.json").write_text("[]")
        with self.assertRaises(RuntimeError):
            core.sync(self.data, self.out, base=self.base, today=date(2026, 11, 11), login_url="http://127.0.0.1:9/login/")
        self.assertFalse(json.loads(self.out.read_text())["status"]["needLogin"])

    def test_rebuild_with_saved_password_does_not_ask_for_login(self):
        self.assertTrue(self.setup()[0])
        (self.data / "session.json").unlink()
        core.rebuild(self.data, self.out)
        self.assertFalse(json.loads(self.out.read_text())["status"]["needLogin"])


if __name__ == "__main__":
    unittest.main()
