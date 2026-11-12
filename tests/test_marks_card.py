"""Экран «Оценки»: посещаемость внизу, дни в школе, карточка оценки (за что, учитель, тема, средний балл)."""
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

import server
from dev.sample import make_sample

server.DEMO = True

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None


def days_word(n):
    return "дня" if n % 10 == 1 and n % 100 != 11 else "дней"


def lesson(n, subject, marks=(), theme="", canceled=False):
    return {"n": n, "start": "8:00", "end": "8:40", "subject": subject, "subjectId": f"s-{subject}", "groupId": "g1", "theme": theme,
            "homework": "", "files": [], "canceled": canceled, "marks": list(marks)}


def mark(value, work, created):
    return {"value": value, "work": work, "created": created}


def page_data(days=None, teachers=None, years=None):
    d = make_sample()
    d["status"] = {"needLogin": False, "error": None, "syncing": False}
    d["config"] = {"name": "Тест"}
    d["version"] = "t"
    if days is not None:
        d["days"] = days
    if teachers is not None:
        d["teachers"] = teachers
    if years is not None:
        d["years"] = years
    return d


@unittest.skipIf(sync_playwright is None, "Playwright не установлен")
class MarksUiBase(unittest.TestCase):
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

    def open(self, data=None, tab="marks"):
        page = self.browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True).new_page()
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        if data is not None:
            page.route("**/cgi-bin/data", lambda route: route.fulfill(json=data))
        page.goto(self.url)
        page.wait_for_selector("nav.tabs")
        page.click(f'nav.tabs button[data-tab="{tab}"]')
        page.wait_for_selector("#main h2, #main .empty, #main .day, #main .lesson")
        return page


class AttendanceBottomTests(MarksUiBase):
    def test_attendance_is_at_the_bottom_of_the_current_screen(self):
        page = self.open()
        titles = page.eval_on_selector_all("#main h2, #main .att-title", "e => e.map(x => x.textContent.trim())")
        self.assertEqual(titles[0], "Последние оценки")                                    # сверху больше не посещаемость
        self.assertEqual([t for t in titles if not t.startswith("Посещаемость")], ["Последние оценки", "По предметам"])
        self.assertTrue(titles[-1].startswith("Посещаемость"), titles)                      # внизу
        after = page.evaluate("""() => { const att = document.querySelector('#main .att'), last = [...document.querySelectorAll('#main .cards2')].pop();
            return !!(last.compareDocumentPosition(att) & Node.DOCUMENT_POSITION_FOLLOWING); }""")
        self.assertTrue(after)
        self.assertEqual(self.errors, [])

    def test_attendance_is_at_the_bottom_of_a_term_in_years(self):
        page = self.open()
        page.click('[data-mview="years"]')
        page.wait_for_selector("#main .att")
        after = page.evaluate("""() => { const att = document.querySelector('#main .att'), cards = document.querySelector('#main .cards2');
            return !!(cards.compareDocumentPosition(att) & Node.DOCUMENT_POSITION_FOLLOWING); }""")
        self.assertTrue(after)

    def test_attendance_still_shown_when_a_term_has_no_marks(self):
        data = page_data()
        for y in data["years"]:
            for t in y["terms"]:
                t["subjects"] = []
        page = self.open(data)
        page.click('[data-mview="years"]')
        page.wait_for_selector("#main .att")
        self.assertIn("Посещаемость", page.inner_text("#main"))
        self.assertIn("оценок пока нет", page.inner_text("#main"))


class SchoolDaysTests(MarksUiBase):
    def prepare(self):
        """Идущий триместр с известными пропусками: день 1 пропущен целиком, день 2 частично, день 3 только опоздание."""
        data = page_data()
        cur = next(y for y in data["years"] if y["current"])
        term = cur["terms"][-1]
        school = [d for d in data["days"] if any(not l["canceled"] for l in d["lessons"])]
        today = date.today().isoformat()
        past = [d for d in school if d["date"] <= today]
        d1, d2, d3 = past[0], past[1], past[2]
        term["start"], term["finish"] = school[0]["date"], (date.today() + timedelta(days=30)).isoformat()
        term["attendance"] = {"total": 120, "absent": 0, "ill": 0, "late": 1, "percent": 97, "days": [
            {"date": d1["date"], "kind": "ill", "name": "Пропуск по болезни", "lessons": [l["n"] for l in d1["lessons"]], "items": []},
            {"date": d2["date"], "kind": "absent", "name": "Пропуск без уважительной причины", "lessons": [1], "items": [{"n": 1, "subject": "Математика"}]},
            {"date": d3["date"], "kind": "late", "name": "Опоздание", "lessons": [1], "items": []}]}
        total = len([d for d in school if term["start"] <= d["date"] <= today])             # будущие дни не считаем
        return data, total

    def test_days_in_school_are_counted_from_the_schedule(self):
        data, total = self.prepare()
        page = self.open(data)
        text = page.inner_text("#main .att")
        self.assertIn(f"Дней в школе: {total - 1} из {total} {days_word(total)}".replace(" ", " "), text.replace("\xa0", " ").replace(" ", " "))
        self.assertIn("из них неполных: 1", text)                                         # день с одним пропущенным уроком: в школе был
        self.assertEqual(self.errors, [])

    def test_no_absences_means_all_days(self):
        data, total = self.prepare()
        for y in data["years"]:
            for t in y["terms"]:
                t["attendance"] = dict(t["attendance"], absent=0, late=0, days=[]) if t.get("attendance") else t.get("attendance")
        page = self.open(data)
        text = page.inner_text("#main .att").replace("\xa0", " ")
        self.assertIn(f"Дней в школе: {total} из {total} {days_word(total)}", text)
        self.assertNotIn("неполных", text)

    def test_line_is_also_in_years_and_attendance_screens_and_the_year_card(self):
        data, total = self.prepare()
        page = self.open(data)
        want = f"Дней в школе: {total - 1} из {total}"
        page.click('[data-mview="years"]')
        self.assertIn(want, page.inner_text("#main .att").replace("\xa0", " "))
        page.click('[data-term="final"]')
        self.assertIn(want, page.inner_text("#main").replace("\xa0", " "))                  # карточка «за год» суммирует периоды
        page.click('[data-mview="att"]')
        self.assertIn(want, page.inner_text("#main .att").replace("\xa0", " "))

    def test_past_years_have_no_school_days_line(self):
        """За прошлые годы сайт расписание не отдаёт: лучше не показывать число совсем, чем показать неточное."""
        data, _ = self.prepare()
        page = self.open(data)
        page.click('[data-mview="years"]')
        page.click('[data-year="1"]')
        page.wait_for_selector("#main .att")
        self.assertNotIn("Дней в школе", page.inner_text("#main"))

    def test_plural_forms(self):
        page = self.open()
        got = page.evaluate("[1, 2, 5, 11, 21, 22, 25, 31, 111].map(daysGen)")
        self.assertEqual(got, [days_word(n) for n in (1, 2, 5, 11, 21, 22, 25, 31, 111)])


THEME = "Дроби\n    и проценты"
TEACHERS = {"Математика": [{"name": "Петрова Мария Петровна", "short": "Петрова М. П.", "role": "Учитель"}]}


def math_days(theme=THEME, subject="Математика"):
    return [
        {"date": "2026-10-01", "weekday": "чт", "lessons": [lesson(1, subject, [mark("3", "Ответ на уроке", "2026-10-01T10:00:00")], theme)]},
        {"date": "2026-10-08", "weekday": "чт", "lessons": [lesson(2, subject, [mark("5", "Контрольная работа", "2026-10-08T10:00:00")], "Проценты")]},
        {"date": "2026-10-15", "weekday": "чт", "lessons": [lesson(3, subject, [mark("2", "Домашняя работа", "2026-10-15T10:00:00")], "")]},
    ]


def years_with_average(average, subject="Математика"):
    return [{"year": 2026, "label": "2026/2027", "className": "5А", "periodLabel": "тр", "columns": 3, "current": True, "final": [],
             "terms": [{"number": 1, "name": "1 триместр", "start": "2026-09-01", "finish": "2026-11-30", "attendance": None,
                        "subjects": [{"name": subject, "marks": ["3", "5", "2"], "average": average}] if average else []}]}]


class MarkCardTests(MarksUiBase):
    def card(self, page, work):
        page.click(f'.row[data-mark]:has-text("{work}")')
        page.wait_for_selector("#modal .markmodal")
        return page.inner_text("#modal .markmodal")

    def test_card_shows_what_for_teacher_theme_mark_and_average(self):
        page = self.open(page_data(math_days(), TEACHERS, years_with_average("3,33")))
        text = self.card(page, "Контрольная работа")
        for needle in ("Математика", "Контрольная работа, 8 октября", "Учитель: Петрова Мария Петровна", "Тема урока:", "Проценты", "Оценка", "Средний балл", "3,33"):
            self.assertIn(needle, text)
        self.assertEqual(page.inner_text("#modal .mm-big"), "5")
        self.assertIn("m5", page.get_attribute("#modal .mm-big", "class"))
        self.assertIn("по предмету за этот период", text)                                    # средний балл взят с сайта
        self.assertIn("↑", page.inner_text("#modal .mm-avg"))                                # пятёрка подняла средний балл
        self.assertEqual(self.errors, [])

    def test_trend_arrow_up_down_and_none(self):
        page = self.open(page_data(math_days(), TEACHERS, years_with_average("3,33")))
        self.card(page, "Контрольная работа")
        self.assertIn("↑", page.inner_text("#modal .mm-avg"))
        page.click("#modal [data-mark-close] >> nth=1")
        self.card(page, "Домашняя работа")                                                   # двойка при среднем 4 (3 и 5)
        self.assertIn("↓", page.inner_text("#modal .mm-avg"))
        page.click("#modal [data-mark-close] >> nth=1")
        self.card(page, "Ответ на уроке")                                                    # самая первая оценка: сравнивать не с чем
        self.assertNotRegex(page.inner_text("#modal .mm-avg"), "[↑↓]")

    def test_average_is_computed_when_the_site_gave_none(self):
        page = self.open(page_data(math_days(), TEACHERS, years_with_average(None)))
        text = self.card(page, "Контрольная работа")
        self.assertIn("3,33", page.inner_text("#modal .mm-avg"))                             # (3 + 5 + 2) / 3
        self.assertIn("по всем оценкам по предмету", text)

    def test_card_without_teacher_or_theme_has_no_empty_lines(self):
        days = math_days(theme="")
        days[1]["lessons"][0]["theme"] = ""
        page = self.open(page_data(days, {}, years_with_average("3,33")))
        text = self.card(page, "Контрольная работа")
        self.assertNotIn("Учитель", text)
        self.assertNotIn("Тема урока", text)

    def test_theme_whitespace_is_collapsed(self):
        page = self.open(page_data(math_days(), TEACHERS, years_with_average("3,33")))
        self.card(page, "Ответ на уроке")
        self.assertEqual(page.inner_text("#modal .mm-theme"), "Дроби и проценты")

    def test_card_closes_by_button_cross_escape_and_backdrop(self):
        page = self.open(page_data(math_days(), TEACHERS, years_with_average("3,33")))
        for how in ("button", "cross", "escape", "backdrop"):
            self.card(page, "Контрольная работа")
            if how == "button":
                page.click("#modal .mbtns [data-mark-close]")
            elif how == "cross":
                page.click("#modal .mm-x")
            elif how == "escape":
                page.keyboard.press("Escape")
            else:
                page.mouse.click(5, 5)                                                       # тёмный фон за карточкой
            page.wait_for_function("document.querySelector('#modal').innerHTML.trim() === ''")
        self.assertEqual(self.errors, [])

    def test_marks_in_subject_cards_open_the_card_too_and_keyboard_works(self):
        page = self.open(page_data(math_days(), TEACHERS, years_with_average("3,33")))
        chips = page.query_selector_all('#main .chips [data-mark]')
        self.assertEqual(len(chips), 3)
        self.assertEqual(chips[0].get_attribute("role"), "button")
        self.assertEqual(chips[0].get_attribute("tabindex"), "0")
        page.locator('#main .chips [data-mark]').nth(1).focus()
        page.keyboard.press("Enter")                                                          # с клавиатуры: Enter и пробел
        page.wait_for_selector("#modal .markmodal")
        self.assertEqual(page.inner_text("#modal .mm-big"), "5")
        page.keyboard.press("Escape")
        page.wait_for_function("document.querySelector('#modal').innerHTML.trim() === ''")
        page.locator('#main .chips [data-mark]').nth(2).focus()                               # страница перерисована: элемент ищем заново
        page.keyboard.press(" ")
        page.wait_for_selector("#modal .markmodal")
        self.assertEqual(page.inner_text("#modal .mm-big"), "2")

    def test_term_chips_in_years_view_stay_plain(self):
        """В «Годах и итогах» оценки приходят списком без дат и тем, показывать там нечего: нажатия не нужны."""
        page = self.open(page_data(math_days(), TEACHERS, years_with_average("3,33")))
        page.click('[data-mview="years"]')
        page.wait_for_selector("#main .chips")
        self.assertEqual(page.eval_on_selector_all("#main .chips [data-mark]", "e => e.length"), 0)

    def test_server_text_is_not_executed_in_the_card(self):
        evil = '<img src=x onerror="window.__pwned=1">'
        teachers = {"Математика": [{"name": evil, "short": "x", "role": ""}]}
        days = math_days(theme=evil)
        days[1]["lessons"][0]["marks"][0]["work"] = evil
        page = self.open(page_data(days, teachers, years_with_average("3,33")))
        page.click('.row[data-mark] >> nth=0')
        page.wait_for_selector("#modal .markmodal")
        self.assertEqual(page.eval_on_selector_all("#modal img, #modal script", "e => e.length"), 0)
        self.assertIsNone(page.evaluate("window.__pwned"))
        self.assertIn("<img", page.inner_text("#modal"))                                     # показан как обычный текст
        self.assertEqual(self.errors, [])

    def test_unknown_key_shows_nothing_and_does_not_crash(self):
        page = self.open(page_data(math_days(), TEACHERS, years_with_average("3,33")))
        page.evaluate("markKey = '2020-01-01|9|9'; render()")
        self.assertEqual(page.inner_text("#modal").strip(), "")
        self.assertEqual(self.errors, [])

    def test_card_is_reachable_from_recent_marks_in_other_tabs(self):
        page = self.open(page_data(math_days(), TEACHERS, years_with_average("3,33")), tab="hw")
        if page.query_selector(".aside .row[data-mark]"):                                    # на широком экране «Последние оценки» рядом с домашкой
            page.click(".aside .row[data-mark] >> nth=0")
            page.wait_for_selector("#modal .markmodal")

    def test_card_fits_on_a_small_phone_without_horizontal_scroll(self):
        page = self.open(page_data(math_days(), TEACHERS, years_with_average("3,33")))
        page.set_viewport_size({"width": 320, "height": 640})
        self.card(page, "Контрольная работа")
        box = page.eval_on_selector("#modal .markmodal", "e => { const r = e.getBoundingClientRect(); return [r.left, r.right, innerWidth, r.bottom, innerHeight]; }")
        self.assertGreaterEqual(box[0], 0)
        self.assertLessEqual(box[1], box[2])
        self.assertLessEqual(box[3], box[4] + 1)


def today_days():
    """Сегодня два урока с оценками и домашкой, вчера один: как в обычный учебный день."""
    t = date.today()
    wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

    def day(d, lessons):
        return {"date": d.isoformat(), "weekday": wd[d.weekday()], "lessons": lessons}
    math = lesson(1, "Математика", [mark("5", "Контрольная работа", f"{t.isoformat()}T10:00:00")], "Проценты")
    lit = lesson(2, "Литература", [mark("4", "Ответ на уроке", f"{t.isoformat()}T11:00:00"), mark("3", "Диктант", f"{t.isoformat()}T11:30:00")], "Лермонтов")
    for l in (math, lit):
        l["homework"] = "Упражнение 5"
    y = t - timedelta(days=1)
    return [day(y, [lesson(1, "Математика", [mark("3", "Домашняя работа", f"{y.isoformat()}T10:00:00")], "Дроби")]), day(t, [math, lit])]


class HomeworkMarksTests(MarksUiBase):
    def hw(self):
        teachers = {"Математика": [{"name": "Петрова Мария Петровна", "short": "Петрова М. П.", "role": "Учитель"}]}
        years = years_with_average("4,2")
        years[0]["terms"][0]["start"] = (date.today() - timedelta(days=60)).isoformat()      # сегодняшний день внутри периода
        years[0]["terms"][0]["finish"] = (date.today() + timedelta(days=60)).isoformat()
        return self.open(page_data(today_days(), teachers, years), tab="hw")

    def test_marks_in_homework_open_the_card(self):
        page = self.hw()
        chips = page.query_selector_all(".lhead [data-mark]")
        self.assertEqual(len(chips), 3)                                                       # 5, 4 и 3 у сегодняшних уроков
        page.click(".lhead [data-mark] >> nth=0")
        page.wait_for_selector("#modal .markmodal")
        text = page.inner_text("#modal .markmodal")
        for needle in ("Математика", "Контрольная работа", "Учитель: Петрова Мария Петровна", "Проценты", "4,2"):
            self.assertIn(needle, text)
        self.assertEqual(page.inner_text("#modal .mm-big"), "5")
        self.assertEqual(self.errors, [])

    def test_tapping_a_mark_does_not_expand_the_lesson(self):
        page = self.hw()
        page.click(".lhead [data-mark] >> nth=0")
        page.wait_for_selector("#modal .markmodal")
        page.keyboard.press("Escape")
        self.assertEqual(page.eval_on_selector_all(".lesson2.open", "e => e.length"), 0)      # урок остался свёрнутым

    def test_opening_the_card_keeps_expanded_lessons_and_does_not_redraw_the_page(self):
        page = self.hw()
        page.click(".lesson2 >> nth=1 >> .lt")                                                # раскрываем второй урок (шапка, не оценка)
        self.assertEqual(page.eval_on_selector_all(".lesson2.open", "e => e.length"), 1)
        page.evaluate("document.querySelector('.lesson2').dataset.probe = 'kept'")             # метка: страница под карточкой не пересобирается
        page.click(".lhead [data-mark] >> nth=0")
        page.wait_for_selector("#modal .markmodal")
        page.click("#modal .mm-x")
        page.wait_for_function("document.querySelector('#modal').innerHTML.trim() === ''")
        self.assertEqual(page.eval_on_selector_all(".lesson2.open", "e => e.length"), 1)      # раскрытый урок не свернулся
        self.assertEqual(page.get_attribute(".lesson2 >> nth=0", "data-probe"), "kept")

    def test_second_and_third_marks_of_one_lesson_show_their_own_work(self):
        page = self.hw()
        page.click(".lhead [data-mark] >> nth=2")                                              # у литературы две оценки: 4 и 3
        page.wait_for_selector("#modal .markmodal")
        self.assertIn("Диктант", page.inner_text("#modal .mm-sub"))
        self.assertEqual(page.inner_text("#modal .mm-big"), "3")

    def test_keyboard_works_in_homework(self):
        page = self.hw()
        page.locator(".lhead [data-mark]").nth(1).focus()
        page.keyboard.press("Enter")
        page.wait_for_selector("#modal .markmodal")
        self.assertEqual(page.inner_text("#modal .mm-big"), "4")
        page.keyboard.press("Escape")
        page.wait_for_function("document.querySelector('#modal').innerHTML.trim() === ''")

    def test_marks_in_schedule_open_the_card_too(self):
        page = self.hw()
        page.click('nav.tabs button[data-tab="plan"]')
        page.wait_for_selector("#main .lesson")
        chips = page.query_selector_all(".lesson [data-mark]")
        self.assertEqual(len(chips), 3)
        page.click(".lesson [data-mark] >> nth=0")
        page.wait_for_selector("#modal .markmodal")
        self.assertIn("Математика", page.inner_text("#modal h3"))
        self.assertEqual(self.errors, [])

    def test_plain_lesson_tap_still_expands_in_homework(self):
        page = self.hw()
        page.click(".lesson2 >> nth=0 >> .lt")
        self.assertEqual(page.eval_on_selector_all(".lesson2.open", "e => e.length"), 1)
        self.assertIsNone(page.query_selector("#modal .markmodal"))


if __name__ == "__main__":
    unittest.main()
