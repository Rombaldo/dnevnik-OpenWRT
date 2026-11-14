"""Что нового: события для уведомлений (оценки, домашка, отмена и изменение уроков, пропуски)."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import notify

TODAY = "2026-11-12"
TOMORROW = "2026-11-13"
YESTERDAY = "2026-11-11"


def lesson(n, subject, start="8:00", end="8:40", marks=(), homework="", canceled=False, files=()):
    return {"n": n, "subject": subject, "start": start, "end": end, "canceled": canceled, "homework": homework, "files": list(files), "marks": list(marks), "theme": ""}


def mark(value, work="Ответ на уроке", created="2026-11-12T10:00:00"):
    return {"value": value, "work": work, "created": created}


def day(date, *lessons):
    return {"date": date, "weekday": "чт", "lessons": list(lessons)}


def payload(days, absences=(), current=True):
    return {"days": list(days), "years": [{"current": current, "terms": [{"number": 1, "attendance": {"days": list(absences)}}]}]}


class NotifyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_(self, p, **kw):
        return notify.process(self.dir, p, today=kw.pop("today", TODAY), now=kw.pop("now", 1_800_000_000), deliver=lambda d, e: self.sent.append(list(e)), **kw)

    def base(self, p):
        self.assertEqual(self.run_(p), [])                          # первый раз: только запомнили
        return p


class BaselineTests(NotifyBase):
    def test_first_run_gives_no_events_even_with_lots_of_data(self):
        p = payload([day(TODAY, lesson(1, "Математика", marks=[mark("5")], homework="Упр. 5"))], absences=[{"date": YESTERDAY, "kind": "absent", "name": "Пропуск", "lessons": [1]}])
        self.assertEqual(self.run_(p), [])
        self.assertEqual(self.sent, [])
        st = self.dir / "notify-state.json"
        self.assertTrue(st.exists())
        self.assertEqual(stat.S_IMODE(os.stat(st).st_mode), 0o600)                   # состояние закрыто от других пользователей

    def test_unchanged_data_gives_no_events_and_does_not_rewrite_the_file(self):
        p = self.base(payload([day(TODAY, lesson(1, "Математика", marks=[mark("5")], homework="Упр. 5"))]))
        st = self.dir / "notify-state.json"
        before = st.stat().st_mtime_ns
        self.assertEqual(self.run_(p), [])
        self.assertEqual(st.stat().st_mtime_ns, before)                               # флеш роутера зря не пишем
        self.assertEqual(self.sent, [])

    def test_state_from_another_version_is_a_new_baseline(self):
        p = self.base(payload([day(TODAY, lesson(1, "Математика"))]))
        (self.dir / "notify-state.json").write_text(json.dumps({"v": 0, "marks": []}))
        p2 = payload([day(TODAY, lesson(1, "Математика", marks=[mark("2")]))])
        self.assertEqual(self.run_(p2), [])                                           # без событий, но состояние обновлено
        self.assertEqual(json.loads((self.dir / "notify-state.json").read_text())["v"], notify.VERSION)

    def test_garbage_state_and_payload_do_not_crash(self):
        (self.dir / "notify-state.json").write_text("{не json")
        self.assertEqual(self.run_(payload([])), [])
        for bad in ({}, {"days": None}, {"days": [{"date": None}], "years": None}, {"days": [{"date": TODAY, "lessons": [{"marks": [{}]}]}]}, {"days": [{"date": TODAY, "lessons": None}]}):
            self.run_(bad)


class MarkTests(NotifyBase):
    def test_new_mark_makes_one_event_with_subject_work_and_date(self):
        self.base(payload([day(TODAY, lesson(1, "Математика", marks=[mark("4", "Диктант", "2026-11-12T09:00:00")]))]))
        ev = self.run_(payload([day(TODAY, lesson(1, "Математика", marks=[mark("4", "Диктант", "2026-11-12T09:00:00"), mark("5", "Ответ на уроке", "2026-11-12T10:00:00")]))]))
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["type"], "mark")
        self.assertEqual(ev[0]["title"], "Новая оценка: 5")
        self.assertEqual(ev[0]["body"], "Математика · Ответ на уроке (12 ноября)")
        self.assertEqual(self.sent[0], ev)                                           # и передано в отправку

    def test_same_mark_is_not_announced_twice(self):
        self.base(payload([day(TODAY, lesson(1, "Математика"))]))
        p = payload([day(TODAY, lesson(1, "Математика", marks=[mark("5")]))])
        self.assertEqual(len(self.run_(p)), 1)
        self.assertEqual(self.run_(p), [])

    def test_two_marks_for_one_lesson_are_two_events(self):
        self.base(payload([day(TODAY, lesson(1, "Литература"))]))
        ev = self.run_(payload([day(TODAY, lesson(1, "Литература", marks=[mark("4", created="a"), mark("3", "Диктант", created="b")]))]))
        self.assertEqual(sorted(e["title"] for e in ev), ["Новая оценка: 3", "Новая оценка: 4"])

    def test_non_numeric_and_missing_values(self):
        self.base(payload([day(TODAY, lesson(1, "Физкультура"))]))
        ev = self.run_(payload([day(TODAY, lesson(1, "Физкультура", marks=[{"value": "зачёт", "work": "Норматив", "created": "x"}, {"value": None, "work": None, "created": "y"}]))]))
        self.assertEqual(sorted(e["title"] for e in ev), ["Новая оценка", "Новая оценка: зачёт"])

    def test_many_marks_are_grouped_into_one_message(self):
        self.base(payload([day(TODAY, lesson(1, "Математика"))]))
        marks = [mark(str(i % 3 + 3), created=f"t{i}") for i in range(5)]
        ev = self.run_(payload([day(TODAY, lesson(1, "Математика", marks=marks))]))
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["title"], "Новых оценок: 5")

    def test_a_flood_of_marks_looks_like_a_data_reload_and_is_one_summary(self):
        self.base(payload([day(TODAY, lesson(1, "Математика"))]))
        marks = [mark("5", created=f"t{i}") for i in range(notify.FLOOD + 5)]
        ev = self.run_(payload([day(TODAY, lesson(1, "Математика", marks=marks))]))
        self.assertEqual([e["title"] for e in ev], [f"Новых оценок: {notify.FLOOD + 5}"])


class HomeworkTests(NotifyBase):
    def test_new_homework_for_today_or_later(self):
        self.base(payload([day(TOMORROW, lesson(2, "Литература"))]))
        ev = self.run_(payload([day(TOMORROW, lesson(2, "Литература", homework="Выучить стихотворение наизусть"))]))
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["title"], "Новая домашка: Литература")
        self.assertEqual(ev[0]["body"], "на 13 ноября: Выучить стихотворение наизусть")

    def test_changed_text_is_announced_again_but_same_text_is_not(self):
        self.base(payload([day(TOMORROW, lesson(2, "Литература", homework="стр. 10"))]))
        self.assertEqual(self.run_(payload([day(TOMORROW, lesson(2, "Литература", homework="стр. 10"))])), [])
        self.assertEqual(len(self.run_(payload([day(TOMORROW, lesson(2, "Литература", homework="стр. 10-12"))]))), 1)

    def test_past_days_homework_is_ignored(self):
        self.base(payload([day(YESTERDAY, lesson(1, "Математика"))]))
        self.assertEqual(self.run_(payload([day(YESTERDAY, lesson(1, "Математика", homework="Задали задним числом"))])), [])

    def test_no_homework_phrases_are_not_homework(self):
        self.base(payload([day(TOMORROW, lesson(1, "Музыка"))]))
        for phrase in ("без задания", "Без домашнего задания.", "нет домашнего задания", "-", "—", "   "):
            self.assertEqual(self.run_(payload([day(TOMORROW, lesson(1, "Музыка", homework=phrase))])), [], phrase)

    def test_files_only_homework_counts(self):
        self.base(payload([day(TOMORROW, lesson(1, "География"))]))
        ev = self.run_(payload([day(TOMORROW, lesson(1, "География", files=[{"name": "Карточки"}]))]))
        self.assertEqual(len(ev), 1)
        self.assertIn("Карточки", ev[0]["body"])

    def test_canceled_lessons_homework_is_ignored(self):
        self.base(payload([day(TOMORROW, lesson(1, "География"))]))
        ev = self.run_(payload([day(TOMORROW, lesson(1, "География", homework="Параграф 3", canceled=True))]))
        self.assertEqual([e["type"] for e in ev], ["schedule"])                       # отмена объявлена, а домашка отменённого урока нет

    def test_a_new_day_with_homework_is_news(self):
        self.base(payload([day(TODAY, lesson(1, "Математика"))]))
        ev = self.run_(payload([day(TODAY, lesson(1, "Математика")), day(TOMORROW, lesson(1, "Математика", homework="Упр. 7"))]))
        self.assertEqual([e["title"] for e in ev], ["Новая домашка: Математика"])

    def test_long_text_is_shortened(self):
        self.base(payload([day(TOMORROW, lesson(1, "История"))]))
        ev = self.run_(payload([day(TOMORROW, lesson(1, "История", homework="слово " * 80))]))
        self.assertLessEqual(len(ev[0]["body"]), 130)
        self.assertTrue(ev[0]["body"].endswith("…"))

    def test_many_homeworks_are_grouped(self):
        self.base(payload([day(TOMORROW, *[lesson(i, f"Предмет{i}") for i in range(1, 7)])]))
        ev = self.run_(payload([day(TOMORROW, *[lesson(i, f"Предмет{i}", homework=f"задание {i}") for i in range(1, 7)])]))
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["title"], "Новой домашки: 6")


class ScheduleTests(NotifyBase):
    def test_newly_canceled_lesson(self):
        self.base(payload([day(TOMORROW, lesson(3, "Физкультура"), lesson(4, "Музыка"))]))
        ev = self.run_(payload([day(TOMORROW, lesson(3, "Физкультура", canceled=True), lesson(4, "Музыка"))]))
        self.assertEqual([(e["type"], e["title"], e["body"]) for e in ev], [("schedule", "Урок отменён: Физкультура", "13 ноября, 3 урок")])

    def test_already_canceled_lesson_is_not_news(self):
        self.base(payload([day(TOMORROW, lesson(3, "Физкультура", canceled=True))]))
        self.assertEqual(self.run_(payload([day(TOMORROW, lesson(3, "Физкультура", canceled=True))])), [])

    def test_time_change_is_a_schedule_change(self):
        self.base(payload([day(TOMORROW, lesson(2, "Математика", "9:00", "9:40"))]))
        ev = self.run_(payload([day(TOMORROW, lesson(2, "Математика", "10:00", "10:40"))]))
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["title"], "Изменилось расписание на 13 ноября")
        self.assertIn("2. Математика 10:00", ev[0]["body"])

    def test_replacement_of_a_subject(self):
        self.base(payload([day(TOMORROW, lesson(2, "Математика"))]))
        ev = self.run_(payload([day(TOMORROW, lesson(2, "Литература"))]))
        self.assertEqual([e["title"] for e in ev], ["Изменилось расписание на 13 ноября"])
        self.assertIn("Литература", ev[0]["body"])

    def test_cancellation_alone_is_not_also_reported_as_a_change(self):
        self.base(payload([day(TOMORROW, lesson(2, "Математика"))]))
        ev = self.run_(payload([day(TOMORROW, lesson(2, "Математика", canceled=True))]))
        self.assertEqual([e["title"] for e in ev], ["Урок отменён: Математика"])

    def test_new_day_appearing_is_not_a_change(self):
        self.base(payload([day(TODAY, lesson(1, "Математика"))]))
        self.assertEqual(self.run_(payload([day(TODAY, lesson(1, "Математика")), day(TOMORROW, lesson(1, "Литература", canceled=True))])), [])

    def test_past_days_are_ignored(self):
        self.base(payload([day(YESTERDAY, lesson(1, "Математика"))]))
        self.assertEqual(self.run_(payload([day(YESTERDAY, lesson(1, "Математика", canceled=True))])), [])

    def test_state_does_not_keep_past_days(self):
        self.base(payload([day(YESTERDAY, lesson(1, "Математика")), day(TOMORROW, lesson(1, "Математика"))]))
        st = json.loads((self.dir / "notify-state.json").read_text())
        self.assertEqual(list(st["sched"]), [TOMORROW])


class AttendanceTests(NotifyBase):
    def absence(self, kind="absent", name="Пропуск без уважительной причины", lessons=(1,), date=TODAY):
        return {"date": date, "kind": kind, "name": name, "lessons": list(lessons)}

    def test_new_absence_and_late(self):
        self.base(payload([day(TODAY, lesson(1, "Математика"))]))
        ev = self.run_(payload([day(TODAY, lesson(1, "Математика"))], absences=[self.absence("ill", "Пропуск по болезни", (1, 2, 3, 5)), self.absence("late", "Опоздание", (1,), YESTERDAY)]))
        titles = {e["title"]: e["body"] for e in ev}
        self.assertEqual(titles["Пропуск по болезни"], "12 ноября, уроки 1–3, 5")
        self.assertEqual(titles["Опоздание"], "11 ноября, урок 1")

    def test_existing_absence_is_not_news(self):
        a = self.absence()
        self.base(payload([day(TODAY, lesson(1, "Математика"))], absences=[a]))
        self.assertEqual(self.run_(payload([day(TODAY, lesson(1, "Математика"))], absences=[a])), [])

    def test_only_the_current_year_counts(self):
        self.base(payload([day(TODAY, lesson(1, "Математика"))], current=False))
        self.assertEqual(self.run_(payload([day(TODAY, lesson(1, "Математика"))], absences=[self.absence()], current=False)), [])


class LogTests(NotifyBase):
    def test_events_are_logged_newest_first_without_duplicates_and_capped(self):
        self.base(payload([day(TODAY, lesson(1, "Математика"))]))
        for i in range(notify.KEEP_EVENTS + 10):
            self.run_(payload([day(TODAY, lesson(1, "Математика", marks=[mark("5", created=f"t{j}") for j in range(i + 1)]))]), now=1_800_000_000 + i)
        log = notify.recent(self.dir, 100)
        self.assertEqual(len(log), notify.KEEP_EVENTS)
        self.assertEqual(len({e["id"] for e in log}), len(log))
        self.assertGreaterEqual(log[0]["ts"], log[-1]["ts"])
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / "events.json").st_mode), 0o600)
        self.assertEqual(len(notify.recent(self.dir, 5)), 5)

    def test_missing_log_is_empty(self):
        self.assertEqual(notify.recent(self.dir / "нет"), [])

    def test_delivery_failure_does_not_break_the_update(self):
        self.base(payload([day(TODAY, lesson(1, "Математика"))]))

        def boom(d, e):
            raise RuntimeError("сеть")
        p = payload([day(TODAY, lesson(1, "Математика", marks=[mark("5")]))])
        with self.assertRaises(RuntimeError):                                          # свой deliver ошибку пробрасывает: так видно в тестах
            notify.process(self.dir, p, today=TODAY, deliver=boom)
        self.assertEqual(len(notify.recent(self.dir)), 1)                              # а событие уже записано в «Что нового»

    def test_auto_delivery_without_the_push_module_is_silent(self):
        self.base(payload([day(TODAY, lesson(1, "Математика"))]))
        p = payload([day(TODAY, lesson(1, "Математика", marks=[mark("5")]))])
        ev = notify.process(self.dir, p, today=TODAY, deliver="auto")                  # push.py ещё нет или нет подписок: тишина, не ошибка
        self.assertEqual(len(ev), 1)


class FormatTests(unittest.TestCase):
    def test_russian_dates(self):
        self.assertEqual(notify.rus_date("2026-01-05"), "5 января")
        self.assertEqual(notify.rus_date("2026-12-31"), "31 декабря")
        self.assertEqual(notify.rus_date("мусор"), "мусор")
        self.assertEqual(notify.rus_date(None), "None")

    def test_lesson_ranges(self):
        self.assertEqual(notify._ranges([1, 2, 3, 5, 7, 8]), "1–3, 5, 7–8")
        self.assertEqual(notify._ranges([]), "")
        self.assertEqual(notify._ranges(["x", 2]), "2")


if __name__ == "__main__":
    unittest.main()
