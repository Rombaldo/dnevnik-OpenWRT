import unittest
from datetime import datetime, timezone

import fetcher


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
        d = fetcher.norm_day(raw_day([raw_lesson()]))
        self.assertEqual(d["date"], "2026-11-11")
        self.assertEqual(d["weekday"], "ср")
        l = d["lessons"][0]
        self.assertEqual((l["n"], l["start"], l["end"]), (1, "8:00", "8:40"))
        self.assertEqual(l["subject"], "Рус. язык")
        self.assertEqual(l["subjectId"], "s1")
        self.assertEqual(l["homework"], "упр 1")  # пробелы обрезаются

    def test_marks_are_flattened(self):
        wm = [{"workName": "Ответ", "marks": [{"value": "5", "createdDate": "2026-11-11T12:00:00"}]}]
        l = fetcher.norm_day(raw_day([raw_lesson(workMarks=wm)]))["lessons"][0]
        self.assertEqual(l["marks"], [{"value": "5", "work": "Ответ", "created": "2026-11-11T12:00:00"}])

    def test_empty_lesson_without_subject_is_skipped(self):
        d = fetcher.norm_day(raw_day([raw_lesson(isEmpty=True, subject=None)]))
        self.assertEqual(d["lessons"], [])

    def test_missing_homework_and_attachments(self):
        l = fetcher.norm_day(raw_day([raw_lesson(homework=None)]))["lessons"][0]
        self.assertEqual((l["homework"], l["files"]), ("", []))
        att = {"text": "см. файл", "attachments": [{"fileName": "a.pdf"}]}
        l = fetcher.norm_day(raw_day([raw_lesson(homework=att)]))["lessons"][0]
        self.assertEqual(l["files"], ["a.pdf"])

    def test_canceled_flag(self):
        l = fetcher.norm_day(raw_day([raw_lesson(isCanceled=True)]))["lessons"][0]
        self.assertTrue(l["canceled"])


class SessionFileTests(unittest.TestCase):
    def test_session_cookies_get_expiry(self):
        class Ctx:
            def cookies(self):
                return [{"name": "a", "value": "1", "expires": -1}, {"name": "b", "value": "2", "expires": 4102444800}]

        import json, os, tempfile
        from pathlib import Path
        old = fetcher.SESSION, fetcher.DATA
        with tempfile.TemporaryDirectory() as t:
            fetcher.DATA, fetcher.SESSION = Path(t), Path(t) / "session.json"
            fetcher.save_cookies(Ctx())
            saved = json.loads(fetcher.SESSION.read_text())
            self.assertGreater(saved[0]["expires"], 0)           # сессионная кука стала «долгой»
            self.assertEqual(saved[1]["expires"], 4102444800)    # постоянная осталась как была
            self.assertEqual(oct(os.stat(fetcher.SESSION).st_mode)[-3:], "600")  # файл только для владельца
        fetcher.SESSION, fetcher.DATA = old


if __name__ == "__main__":
    unittest.main()
