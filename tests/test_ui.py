"""Дымовой тест страницы в настоящем браузере: нет ошибок JavaScript, три вкладки рисуются."""
import threading
import unittest
from pathlib import Path
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

    def test_untrusted_text_is_escaped(self):
        """Тексты приходят от учителей и с сайта: теги в них не должны выполняться."""
        evil = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>'
        from dev.sample import make_sample
        d = make_sample()
        for day in d["days"]:
            for l in day["lessons"]:
                l["homework"], l["theme"], l["subject"], l["start"] = evil, evil, "Рус. " + evil, evil
                l["n"] = evil
                l["marks"] = [{"value": evil, "work": evil, "created": "2026-11-10T10:00:00"}]
                l["files"] = [{"id": "1", "name": evil, "ext": evil, "size": 1, "url": 'https://b5.csdnevnik.ru/a"onmouseover="window.__pwned=1'}]
        d["status"] = {"needLogin": False, "error": None, "syncing": False}
        d["config"] = {"name": evil}
        d["version"] = evil
        ctx = self.browser.new_context(viewport={"width": 375, "height": 812})
        page = ctx.new_page()
        page.route("**/data.json", lambda route: route.fulfill(json=d))
        page.goto(self.url)
        page.wait_for_selector("nav.tabs")
        for tab in ("hw", "marks", "plan"):
            page.click(f'nav.tabs button[data-tab="{tab}"]')
            page.wait_for_timeout(150)
            page.hover("body")
        self.assertIsNone(page.evaluate("window.__pwned"))
        self.assertEqual(page.eval_on_selector_all("#main img, #main script, header img, header script", "e => e.length"), 0)
        page.close()

    # ---- вход по логину и паролю
    def mocked(self, data, setup=None):
        ctx = self.browser.new_context(viewport={"width": 375, "height": 812})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/data.json", lambda route: route.fulfill(json=data() if callable(data) else data))
        if setup:
            page.route("**/cgi-bin/setup", setup)
        page.goto(self.url)
        return page, errors

    def first_run_data(self, error="Введите логин и пароль от Дневник.ру."):
        return {"updatedAt": None, "days": [], "status": {"needLogin": True, "error": error, "syncing": False}, "config": {}, "version": "t"}

    def test_first_run_shows_login_form(self):
        page, errors = self.mocked(self.first_run_data())
        page.wait_for_selector("#setup-form")
        self.assertEqual(page.get_attribute("#s-pass", "type"), "password")
        self.assertEqual(page.get_attribute("#s-login", "autocomplete"), "username")
        text = page.inner_text("#main")
        self.assertIn("логин и пароль", text.lower())
        self.assertNotIn("ещё загружаются", text)
        self.assertEqual(errors, [])

    def test_wrong_password_message_and_password_is_not_kept(self):
        import json as _json
        posted = []

        def setup(route):
            posted.append(_json.loads(route.request.post_data))
            route.fulfill(json={"ok": False, "message": "Неверный логин или пароль."})

        page, errors = self.mocked(self.first_run_data(), setup)
        page.wait_for_selector("#setup-form")
        page.fill("#s-login", "kate")
        page.fill("#s-pass", "supersecret-123")
        page.click("button.go")
        page.wait_for_selector(".msg.bad")
        self.assertIn("Неверный логин или пароль", page.inner_text(".msg.bad"))
        self.assertEqual(posted, [{"login": "kate", "password": "supersecret-123"}])
        self.assertEqual(page.input_value("#s-login"), "kate")          # логин не надо вводить заново
        self.assertEqual(page.input_value("#s-pass"), "")                # пароль стёрт
        self.assertNotIn("supersecret-123", page.content())              # и нигде в странице не остался
        self.assertEqual(errors, [])

    def test_successful_login_shows_the_diary(self):
        from dev.sample import make_sample
        state = {"in": False}

        def setup(route):
            state["in"] = True
            route.fulfill(json={"ok": True, "message": "Вход выполнен. Загружаю дневник."})

        def data():
            if state["in"]:
                d = make_sample()
                d["status"] = {"needLogin": False, "error": None, "syncing": False}
                d["config"], d["version"] = {}, "t"
                return d
            return self.first_run_data()

        page, errors = self.mocked(data, setup)
        page.wait_for_selector("#setup-form")
        page.fill("#s-login", "kate")
        page.fill("#s-pass", "x")
        page.click("button.go")
        page.wait_for_selector(".day", timeout=20000)                   # сам перешёл к дневнику
        self.assertEqual(page.query_selector("#setup-form"), None)
        self.assertEqual(errors, [])

    def test_banner_offers_relogin_when_old_data_exists(self):
        from dev.sample import make_sample
        d = make_sample()
        d["status"] = {"needLogin": True, "error": "Неверный логин или пароль. Введите логин и пароль заново.", "syncing": False}
        d["config"], d["version"] = {}, "t"
        page, errors = self.mocked(d)
        page.wait_for_selector(".banner.bad button[data-setup]")
        self.assertIn("Неверный логин или пароль", page.inner_text(".banner.bad"))
        page.click(".banner.bad button[data-setup]")
        page.wait_for_selector("#setup-form")
        self.assertIsNotNone(page.query_selector(".day"))                # старые данные остаются под формой
        self.assertEqual(errors, [])

    def test_change_login_link_when_everything_works(self):
        page, errors = self.open(375, 812)
        page.click("button.linkbtn[data-setup]")
        page.wait_for_selector("#setup-form")
        self.assertEqual(page.input_value("#s-pass"), "")
        page.click("button[data-setup]") if page.query_selector("button[data-setup]") else None
        self.assertEqual(errors, [])

    def test_offline_copy_has_no_login_form(self):
        ctx = self.browser.new_context()
        page = ctx.new_page()
        from dev.sample import make_sample
        import json as _json
        html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()
        d = make_sample()
        d["status"] = {"needLogin": True, "error": "x", "syncing": False}
        page.set_content(html.replace("/*INLINE_DATA*/null", _json.dumps(d).replace("</", "<\\/")))
        page.wait_for_selector(".day")
        self.assertIsNone(page.query_selector("#setup-form"))
        self.assertIsNone(page.query_selector("[data-setup]"))

    def test_attachment_link_and_lesson_details(self):
        page, errors = self.open(375, 812)
        link = page.query_selector("a.file")
        self.assertIsNotNone(link, "ссылка на вложение не нарисована")
        self.assertEqual(link.get_attribute("href"), "https://b5.csdnevnik.ru/demo-1.pdf")   # прямая ссылка на файл
        self.assertEqual(link.get_attribute("target"), "_blank")
        self.assertIn("noopener", link.get_attribute("rel"))
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
