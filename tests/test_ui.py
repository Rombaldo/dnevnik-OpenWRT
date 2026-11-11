"""Дымовой тест страницы в настоящем браузере: нет ошибок JavaScript, три вкладки рисуются."""
import threading
import unittest
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

    def open(self, width, height):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(self.url)
        page.wait_for_selector("nav.tabs")
        return page, errors

    def check_tabs(self, width, height):
        page, errors = self.open(width, height)
        self.assertIn("Тест", page.inner_text("#hello"))   # имя из demo-настроек
        for tab, needle in [("hw", "Домашка"), ("marks", "Как у тебя дела"), ("plan", "урок")]:
            page.click(f'nav.tabs button[data-tab="{tab}"]')
            page.wait_for_timeout(150)
            text = page.inner_text("#main")
            self.assertTrue(len(text) > 50, f"вкладка {tab} пустая")
            if tab == "marks":
                self.assertIn(needle, text)
                self.assertIn("Рейтинг", text)
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
        card = page.query_selector(".hw")
        if card:
            before = page.inner_text("#hero .small")
            card.query_selector(".check").click()
            page.wait_for_timeout(150)
            self.assertNotEqual(before, page.inner_text("#hero .small"))
        page.close()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
