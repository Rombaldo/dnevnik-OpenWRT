import http.server
import json
import os
import tempfile
import threading
import time
import unittest
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

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeSite.requests += 1
        if "/api/v2/marks/diary" not in self.path or f"DnevnikAuth_a={FakeSite.good_token}" not in (self.headers.get("Cookie") or ""):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"<html>login</html>")
            return
        att = {"text": "читать", "attachments": [{"fileId": "7", "fileName": "ГЕО", "fileExtension": "pdf", "size": 5,
                                                  "fileLink": "https://b5.csdnevnik.ru/geo.pdf"}]}
        body = json.dumps({"days": [raw_day([raw_lesson(homework=att)]), raw_day([raw_lesson()], day=(2026, 11, 12))]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "dnevnik_sst=rotated; Path=/; Max-Age=86400; HttpOnly")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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


if __name__ == "__main__":
    unittest.main()
