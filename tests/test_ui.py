"""Дымовой тест страницы в настоящем браузере: нет ошибок JavaScript, три вкладки рисуются."""
import threading
import unittest
from datetime import date
from http.server import ThreadingHTTPServer

import server

server.DEMO = True

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "Playwright не установлен")
class UiSmokeTests(unittest.TestCase):
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

    def open(self, width, height, touch=False):
        page = self.browser.new_context(viewport={"width": width, "height": height}, has_touch=touch).new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(self.url)
        page.wait_for_selector("nav.tabs")
        return page, errors

    def check_tabs(self, width, height):
        page, errors = self.open(width, height)
        self.assertIn("Тест", page.inner_text("#hello"))   # имя из demo-настроек
        for tab, needle in [("hw", "Домашка"), ("marks", "По предметам"), ("plan", "урок")]:
            page.click(f'nav.tabs button[data-tab="{tab}"]')
            page.wait_for_timeout(150)
            text = page.inner_text("#main")
            self.assertTrue(len(text) > 50, f"вкладка {tab} пустая")
            if tab == "marks":
                self.assertIn(needle, text)
                for gone in ("Рейтинг", "Как у тебя дела", "средний балл", "пятёрок"):
                    self.assertNotIn(gone, text)   # эти блоки убраны по просьбе
        # нет горизонтальной прокрутки
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width + 1)
        page.close()
        self.assertEqual(errors, [])

    def test_phone(self):
        self.check_tabs(375, 812)

    def test_tablet(self):
        self.check_tabs(1280, 800)

    def test_done_checkbox_updates_progress(self):
        page, errors = self.open(375, 812)
        before = page.inner_text(".day .dprog b")
        page.query_selector(".strip .check").click()
        page.wait_for_timeout(150)
        self.assertNotEqual(before, page.inner_text(".day .dprog b"))
        page.close()
        self.assertEqual(errors, [])

    def swipe(self, page, dx, dy=10):
        page.evaluate("""([dx, dy]) => {
            const mk = (type, x, y) => { const t = new Touch({identifier: 1, target: document.body, clientX: x, clientY: y});
                return new TouchEvent(type, {touches: type === 'touchend' ? [] : [t], changedTouches: [t], bubbles: true}); };
            document.dispatchEvent(mk('touchstart', 200, 300));
            document.dispatchEvent(mk('touchend', 200 + dx, 300 + dy));
        }""", [dx, dy])
        page.wait_for_timeout(300)

    def test_schedule_swipe(self):
        page, errors = self.open(375, 812, touch=True)
        page.click('nav.tabs button[data-tab="plan"]')
        title = lambda: page.inner_text(".nav .ttl")
        first = title()
        self.swipe(page, -120)             # влево - следующий день
        second = title()
        self.assertNotEqual(first, second)
        self.swipe(page, 120)              # вправо - назад
        self.assertEqual(title(), first)
        self.swipe(page, 10, 200)          # вертикальная прокрутка день не меняет
        self.assertEqual(title(), first)
        self.swipe(page, -20)              # короткое движение тоже не считается свайпом
        self.assertEqual(title(), first)
        page.click('.nav button[data-nav="1"]')   # стрелки продолжают работать
        self.assertNotEqual(title(), first)
        page.close()
        self.assertEqual(errors, [])

    @unittest.skipIf(date.today().weekday() > 4, "сегодня выходной: в демо-данных нет уроков")
    def test_today_is_visible_on_homework(self):
        page, errors = self.open(375, 812)
        titles = page.eval_on_selector_all(".day .dtitle", "els => els.map(e => e.innerText)")
        self.assertTrue(any("Сегодня" in t for t in titles), titles)
        self.assertIn("Сегодня", titles[0])   # и он первый в ленте
        page.close()
        self.assertEqual(errors, [])

    def test_attachment_link_and_lesson_details(self):
        page, errors = self.open(375, 812)
        link = page.query_selector("a.file")
        self.assertIsNotNone(link, "ссылка на вложение не нарисована")
        self.assertIn("files/1.pdf", link.get_attribute("href"))
        self.assertIn("Презентация", link.inner_text())
        self.assertIsNotNone(page.query_selector(".file.off"), "нескачанный файл должен показываться без ссылки")
        # раскрытие урока по нажатию на заголовок
        head = page.query_selector("[data-toggle]")
        head.click()
        self.assertTrue(page.query_selector(".lesson2.open"))
        page.close()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
