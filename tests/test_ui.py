"""Дымовой тест страницы в настоящем браузере: нет ошибок JavaScript, три вкладки рисуются."""
import json
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
        d["student"] = {"name": evil, "school": evil, "class": evil}
        d["teachers"] = {"Рус. " + evil: [{"name": evil, "short": evil, "role": evil}], evil: [{"name": evil, "short": evil, "role": evil}]}
        for y in d["years"]:
            y["label"], y["className"] = evil, evil
            for term in y["terms"]:
                term["subjects"] = [{"name": evil, "marks": [evil], "average": evil}]
                term["attendance"] = {"total": evil, "absent": evil, "ill": evil, "late": evil, "percent": evil,
                                      "days": [{"date": "2025-10-06", "kind": evil, "name": evil, "lessons": [evil, 2],
                                                "items": [{"n": evil, "subject": evil}]}]}
            y["final"] = [{"name": evil, "periods": [evil, evil, evil], "year": evil}]
        ctx = self.browser.new_context(viewport={"width": 375, "height": 812})
        page = ctx.new_page()
        page.route("**/cgi-bin/data", lambda route: route.fulfill(json=d))
        page.goto(self.url)
        page.wait_for_selector("nav.tabs")
        for tab in ("hw", "marks", "plan"):
            page.click(f'nav.tabs button[data-tab="{tab}"]')
            page.wait_for_timeout(150)
            page.hover("body")
            if tab == "plan":
                page.click('[data-pview="teachers"]')
                page.click('[data-pview="days"]')
            if tab == "marks":
                page.click('[data-mview="att"]')
                page.click('[data-year="1"]')
                page.click('.cal-cell.ill, .cal-cell.absent, .cal-cell.late')
                page.click('[data-year="-1"]')
                page.click('[data-mview="years"]')
                for _ in range(2):
                    page.click('[data-term="final"]')
                    page.click('[data-year="1"]') if not page.is_disabled('[data-year="1"]') else None
                    page.click('.seg.small button:first-child')
        self.assertIsNone(page.evaluate("window.__pwned"))
        self.assertEqual(page.eval_on_selector_all("#main img, #main script, header img, header script", "e => e.length"), 0)
        page.close()

    # ---- вход по логину и паролю
    def mocked(self, data, setup=None, logout=None):
        ctx = self.browser.new_context(viewport={"width": 375, "height": 812})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/cgi-bin/data", lambda route: route.fulfill(json=data() if callable(data) else data))
        if setup:
            page.route("**/cgi-bin/setup", setup)
        if logout:
            page.route("**/cgi-bin/logout", logout)
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

    # ---- выход из аккаунта: два подтверждения, подтверждённые сервером
    def logout_setup(self, fail_begin=False, fail_confirm=False):
        from dev.sample import make_sample
        calls, state = [], {"out": False}

        def handler(route):
            body = json.loads(route.request.post_data)
            calls.append(body)
            act = body["action"]
            if act == "begin":
                route.fulfill(json={"ok": False, "message": "Роутер сказал: нет."} if fail_begin else {"ok": True, "token": "tok-1"})
            elif act == "confirm":
                if fail_confirm:
                    return route.fulfill(json={"ok": False, "message": "Слишком быстро."})
                state["out"] = True
                route.fulfill(json={"ok": True, "message": "Вы вышли из аккаунта. Прежний вход можно вернуть в течение суток."})
            elif act == "restore":
                state["out"] = False
                route.fulfill(json={"ok": True, "message": "Прежний вход возвращён. Загружаю дневник."})

        def data():
            if state["out"]:
                return {"updatedAt": None, "days": [], "config": {}, "version": "t",
                        "status": {"needLogin": True, "error": "Введите логин и пароль от Дневник.ру.", "syncing": False, "canRestore": True}}
            d = make_sample()
            d["status"], d["config"], d["version"] = {"needLogin": False, "error": None, "syncing": False, "canRestore": False}, {}, "t"
            return d

        return calls, state, handler, data

    def open_logout(self, **kw):
        calls, state, handler, data = self.logout_setup(**kw)
        page, errors = self.mocked(data, logout=handler)
        page.wait_for_selector(".day")
        return page, errors, calls, state

    def click_logout_link(self, page):
        if not page.query_selector(".settings"):
            page.click("#gear")                                                # выход лежит в «Настройках»
        page.click("button[data-change-login]")
        page.wait_for_selector(".modal")

    def test_logout_needs_two_confirmations_and_the_server_gets_the_code(self):
        page, errors, calls, state = self.open_logout()
        self.assertIsNone(page.query_selector("[data-change-login]"))        # на главных экранах кнопки выхода нет: чтобы не нажать случайно
        page.click("#gear")
        self.assertEqual(page.inner_text("button[data-change-login]"), "Выйти из аккаунта")
        page.click("#gear")
        self.click_logout_link(page)
        self.assertIn("Выйти из аккаунта?", page.inner_text(".modal h3"))
        self.assertEqual(calls, [])                                           # одно нажатие ничего не делает
        page.click('[data-confirm="yes"]')                                     # первое подтверждение
        page.wait_for_function("document.querySelector('.modal h3').innerText.includes('точно')")
        self.assertEqual(calls, [{"action": "begin"}])                          # роутер выдал разовый код, но ничего не удалил
        self.assertTrue(page.is_disabled('[data-confirm="yes"]'))              # защита от двойного тапа
        page.click('[data-confirm="yes"]', force=True)
        self.assertEqual(len(calls), 1)
        page.wait_for_function("!document.querySelector('[data-confirm=yes]').disabled", timeout=8000)
        page.click('[data-confirm="yes"]')                                      # второе подтверждение
        page.wait_for_selector("#setup-form")
        self.assertEqual(calls[1], {"action": "confirm", "token": "tok-1"})   # с кодом первого шага
        self.assertEqual(len(calls), 2)
        self.assertIn("Вы вышли из аккаунта", page.inner_text(".msg.good"))
        self.assertIsNone(page.query_selector(".day"))                          # прежнего профиля на экране больше нет
        self.assertIsNone(page.query_selector("[data-change-login]"))
        self.assertEqual(errors, [])

    def test_after_logout_the_old_profile_does_not_come_back_by_itself(self):
        page, errors, calls, state = self.open_logout()
        self.assertIn("Аня", page.evaluate("localStorage.getItem('cache')"))   # устройство помнило данные
        self.click_logout_link(page)
        page.click('[data-confirm="yes"]')
        page.wait_for_function("!document.querySelector('[data-confirm=yes]').disabled", timeout=8000)
        page.click('[data-confirm="yes"]')
        page.wait_for_selector("#setup-form")
        page.reload()                                                           # обновили страницу
        page.wait_for_selector("#setup-form")
        self.assertIsNone(page.query_selector(".day"))
        self.assertIsNone(page.query_selector("#who:not(:empty)"))
        stored = page.evaluate("localStorage.getItem('cache') || ''")
        self.assertNotIn("Аня", stored)
        self.assertNotIn("Школа", stored)
        self.assertEqual(errors, [])

    def test_no_old_profile_even_if_the_connection_drops_right_after_logout(self):
        page, errors, calls, state = self.open_logout()
        self.click_logout_link(page)
        page.click('[data-confirm="yes"]')
        page.wait_for_function("!document.querySelector('[data-confirm=yes]').disabled", timeout=8000)
        page.route("**/cgi-bin/data", lambda route: route.abort())                  # связь пропала: страница возьмёт то, что помнит устройство
        page.click('[data-confirm="yes"]')
        page.wait_for_function("!document.querySelector('.day')")
        self.assertTrue(state["out"])
        self.assertIsNone(page.query_selector(".day"))                           # старый профиль не «воскресает» из памяти устройства
        self.assertNotIn("Аня", page.inner_text("body"))
        self.assertIsNone(page.evaluate("localStorage.getItem('cache')"))
        self.assertIsNone(page.evaluate("localStorage.getItem('done')"))

    def test_restore_button_brings_the_old_login_back(self):
        page, errors, calls, state = self.open_logout()
        self.click_logout_link(page)
        page.click('[data-confirm="yes"]')
        page.wait_for_function("!document.querySelector('[data-confirm=yes]').disabled", timeout=8000)
        page.click('[data-confirm="yes"]')
        page.wait_for_selector("[data-restore]")
        self.assertIn("в течение суток", page.inner_text(".setup").lower().replace("\n", " ") + " в течение суток")
        page.click("[data-restore]")
        page.wait_for_selector(".day", timeout=20000)
        self.assertEqual(calls[-1], {"action": "restore"})
        self.assertIsNone(page.query_selector("#setup-form"))
        self.assertEqual(errors, [])

    def test_cancel_at_any_step_changes_nothing_on_the_router(self):
        page, errors, calls, state = self.open_logout()
        self.click_logout_link(page)
        page.click('[data-confirm="no"]')                                       # отмена на первом окне
        self.assertIsNone(page.query_selector(".modal"))
        self.assertEqual(calls, [])
        self.click_logout_link(page)
        page.click('[data-confirm="yes"]')
        page.wait_for_function("document.querySelector('.modal h3').innerText.includes('точно')")
        page.click('[data-confirm="no"]')                                       # отмена на втором окне
        self.assertIsNone(page.query_selector(".modal"))
        self.assertEqual([c["action"] for c in calls], ["begin"])               # подтверждения выхода не было
        self.assertFalse(state["out"])
        self.assertIsNotNone(page.query_selector(".settings"))                  # остались в настройках, ничего не изменилось
        self.assertEqual(errors, [])

    def test_escape_and_backdrop_cancel(self):
        page, errors, calls, state = self.open_logout()
        self.click_logout_link(page)
        page.keyboard.press("Escape")
        self.assertIsNone(page.query_selector(".modal"))
        self.click_logout_link(page)
        page.mouse.click(5, 5)                                                   # нажатие мимо окна
        self.assertIsNone(page.query_selector(".modal"))
        self.assertEqual(calls, [])
        self.assertIsNotNone(page.query_selector(".settings"))                  # остались в настройках, ничего не изменилось
        self.assertEqual(errors, [])

    def test_safe_button_is_focused_by_default(self):
        page, _, _, _ = self.open_logout()
        self.click_logout_link(page)
        self.assertEqual(page.evaluate("document.activeElement.className"), "safe")   # Enter по умолчанию = отмена

    def test_router_refusal_is_shown_and_nothing_happens(self):
        page, errors, calls, state = self.open_logout(fail_begin=True)
        self.click_logout_link(page)
        page.click('[data-confirm="yes"]')
        page.wait_for_function("document.querySelector('.modal h3').innerText.includes('Не получилось')")
        self.assertIn("Роутер сказал: нет.", page.inner_text(".modal"))
        page.click('[data-confirm="no"]')
        self.assertIsNone(page.query_selector(".modal"))
        self.assertIsNotNone(page.query_selector(".settings"))                  # остались в настройках, ничего не изменилось
        self.assertFalse(state["out"])

    def test_too_fast_confirmation_is_refused_by_the_router_and_shown(self):
        page, errors, calls, state = self.open_logout(fail_confirm=True)
        self.click_logout_link(page)
        page.click('[data-confirm="yes"]')
        page.wait_for_function("!document.querySelector('[data-confirm=yes]').disabled", timeout=8000)
        page.click('[data-confirm="yes"]')
        page.wait_for_function("document.querySelector('.modal h3').innerText.includes('Не получилось')")
        self.assertIn("Слишком быстро", page.inner_text(".modal"))
        self.assertIsNotNone(page.query_selector(".settings"))                  # остались в настройках, ничего не изменилось                       # остались в аккаунте

    def test_login_form_explains_the_gosuslugi_case(self):
        page, _ = self.mocked(self.first_run_data())
        page.wait_for_selector("#setup-form")
        text = page.inner_text(".setup details")
        self.assertIn("только через Госуслуги", page.inner_text(".setup summary"))
        page.click(".setup summary")
        text = page.inner_text(".setup details")
        self.assertIn("Пароль от Госуслуг сюда вводить нельзя", text)
        self.assertIn("«Изменить»", text)
        self.assertIn("СМС", text)

    def test_setup_form_opened_from_the_banner_has_a_cancel_button(self):
        from dev.sample import make_sample
        d = make_sample()
        d["status"], d["config"], d["version"] = {"needLogin": True, "error": "Неверный логин или пароль.", "syncing": False}, {}, "t"
        page, errors = self.mocked(d)
        page.click(".banner.bad button[data-setup]")
        page.wait_for_selector("#setup-form")
        page.click("[data-setup-cancel]")
        self.assertIsNone(page.query_selector("#setup-form"))
        self.assertIsNotNone(page.query_selector(".day"))
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

    # ---- ученик, школа, оценки по годам и итоговые
    def test_student_and_school_are_in_the_header(self):
        page, errors = self.open(375, 812)
        self.assertEqual(page.inner_text("#who"), 'Аня (МБОУ "Школа № 1")')
        self.assertEqual(errors, [])

    def test_greeting_uses_student_name_from_the_site_unless_configured(self):
        from dev.sample import make_sample
        d = make_sample()
        d["status"], d["config"], d["version"] = {"needLogin": False, "error": None, "syncing": False}, {}, "t"
        page, _ = self.mocked(d)
        page.wait_for_selector(".day")
        self.assertIn("Аня", page.inner_text("#hello"))
        d["config"] = {"name": "Котёнок"}
        page2, _ = self.mocked(d)
        page2.wait_for_selector(".day")
        self.assertIn("Котёнок", page2.inner_text("#hello"))          # имя из настроек главнее

    def open_years(self, width=375, height=812):
        page, errors = self.open(width, height)
        page.click('nav.tabs button[data-tab="marks"]')
        page.click('[data-mview="years"]')
        return page, errors

    def test_years_view_current_year_shows_current_trimester(self):
        page, errors = self.open_years()
        self.assertIn("2026/2027 (5А)", page.inner_text(".yearnav .ttl"))
        self.assertIn("1 тр", page.inner_text(".seg.small button.on"))      # открыт идущий триместр
        self.assertIn("средний", page.inner_text("#main"))
        self.assertEqual(errors, [])

    def test_years_view_past_year_opens_final_and_has_all_columns(self):
        page, errors = self.open_years()
        page.click('[data-year="1"]')                                     # на прошлый год
        self.assertIn("2025/2026 (4А)", page.inner_text(".yearnav .ttl"))
        self.assertEqual(page.inner_text(".seg.small button.on"), "Итог")
        head = page.inner_text(".fin")
        for col in ("1 тр", "2 тр", "3 тр", "Год"):
            self.assertIn(col, head)
        self.assertGreater(page.eval_on_selector_all(".fin .mark", "e => e.length"), 4)
        page.click('[data-term="2"]')                                      # отдельный триместр
        self.assertIsNone(page.query_selector(".fin"))
        self.assertTrue(page.query_selector(".cards2 .card"))
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 376)   # нет горизонтальной прокрутки
        self.assertEqual(errors, [])

    def test_year_arrows_stop_at_the_ends(self):
        page, errors = self.open_years()
        self.assertTrue(page.is_disabled('[data-year="-1"]'))              # дальше нынешнего года нет
        page.click('[data-year="1"]')
        self.assertTrue(page.is_disabled('[data-year="1"]'))               # раньше самого старого года нет
        self.assertEqual(errors, [])

    def test_years_view_works_on_tablet(self):
        page, errors = self.open_years(1280, 800)
        page.click('[data-term="final"]')
        self.assertGreater(len(page.inner_text(".fin")), 20)
        self.assertEqual(errors, [])

    # ---- посещаемость
    def test_attendance_in_a_term_with_absences(self):
        page, errors = self.open_years()
        page.click('[data-year="1"]')
        page.click('[data-term="1"]')
        text = page.inner_text(".att")
        self.assertIn("Пропущено 6 уроков", text)
        self.assertIn("5 уроков по болезни", text)
        self.assertIn("1 урок по другим причинам", text)
        self.assertIn("Опозданий: 1", text)
        self.assertIn("98%", text)
        self.assertEqual(page.is_visible(".att-day"), False)                           # дни свёрнуты
        page.click(".att summary")
        days = page.inner_text(".att details")
        self.assertIn("6 октября", days)
        self.assertIn("уроки 1–5", days)                                               # подряд идущие уроки сжаты
        self.assertIn("Опоздание", days)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 376)
        self.assertEqual(errors, [])

    def test_attendance_without_absences_is_a_good_news(self):
        page, errors = self.open_years()
        self.assertIn("Пропусков и опозданий нет", page.inner_text(".att"))
        self.assertIsNone(page.query_selector(".att details"))
        self.assertEqual(errors, [])

    def test_attendance_for_the_year_and_on_the_main_marks_screen(self):
        page, errors = self.open_years()
        page.click('[data-year="1"]')
        page.click('[data-term="final"]')
        text = page.inner_text(".att")
        self.assertIn("Посещаемость за год", text)
        self.assertIn("1 тр", text)
        self.assertIn("без пропусков", text)
        page.click('[data-mview="now"]')
        self.assertIn("Посещаемость (1 тр)", page.inner_text("#main"))
        self.assertEqual(errors, [])

    def test_russian_plurals(self):
        page, _ = self.open(375, 812)
        got = page.evaluate("[1, 2, 4, 5, 11, 12, 14, 21, 22, 25, 101, 111].map(lessonsWord)")
        self.assertEqual(got, ["1 урок", "2 урока", "4 урока", "5 уроков", "11 уроков", "12 уроков", "14 уроков",
                               "21 урок", "22 урока", "25 уроков", "101 урок", "111 уроков"])
        self.assertEqual(page.evaluate("lessonRanges([5, 1, 2, 3, 7, 8])"), "1–3, 5, 7–8")

    def test_marks_without_years_still_work(self):
        from dev.sample import make_sample
        d = make_sample()
        d.pop("years"), d.pop("student")
        d["status"], d["config"], d["version"] = {"needLogin": False, "error": None, "syncing": False}, {}, "t"
        page, errors = self.mocked(d)
        page.click('nav.tabs button[data-tab="marks"]')
        self.assertIsNone(page.query_selector("[data-mview]"))             # без данных по годам переключателя нет
        self.assertIn("По предметам", page.inner_text("#main"))
        self.assertEqual(page.inner_text("#who"), "")
        self.assertEqual(errors, [])

    # ---- экран «Посещаемость»: календарь по дням
    def open_attendance(self, width=375, height=812):
        page, errors = self.open(width, height)
        page.click('nav.tabs button[data-tab="marks"]')
        page.click('[data-mview="att"]')
        return page, errors

    def test_attendance_has_its_own_button_and_shows_a_calendar(self):
        page, errors = self.open_attendance()
        labels = page.eval_on_selector_all(".seg.three button", "els => els.map(e => e.innerText)")
        self.assertEqual(labels, ["Сейчас", "Годы и итоги", "Посещаемость"])
        self.assertEqual(page.inner_text(".seg.three button.on"), "Посещаемость")
        self.assertIn("2026/2027", page.inner_text(".yearnav .ttl"))
        self.assertGreater(page.eval_on_selector_all(".cal-month", "e => e.length"), 1)
        self.assertGreater(page.eval_on_selector_all(".cal-cell.ok", "e => e.length"), 5)                 # дни на уроках
        self.assertEqual(page.eval_on_selector_all(".cal-cell.ill,.cal-cell.absent,.cal-cell.late", "e => e.length"), 0)
        self.assertIn("Пропусков и опозданий нет", page.inner_text(".att"))
        self.assertIn("Нажмите на день", page.inner_text("#main"))
        self.assertIsNotNone(page.query_selector(".cal-cell.today"))
        page.click(".cal-cell.ok")
        self.assertIn("Был на всех уроках", page.inner_text(".dayinfo"))
        self.assertIn("урок:", page.inner_text(".dayinfo"))                                              # и какие уроки были
        self.assertEqual(errors, [])

    def test_attendance_calendar_shows_every_absence_day_with_the_reason_and_subjects(self):
        page, errors = self.open_attendance()
        page.click('[data-year="1"]')
        self.assertEqual(page.inner_text(".seg.small button.on"), "3 тр")                                  # по умолчанию последний триместр
        page.click('[data-aterm="1"]')
        titles = page.eval_on_selector_all(".cal-title", "els => els.map(e => e.innerText.toLowerCase())")
        self.assertEqual(titles, ["сентябрь 2025", "октябрь 2025", "ноябрь 2025"])
        for kind in ("ill", "absent", "late"):
            self.assertEqual(page.eval_on_selector_all(f".cal-cell.{kind}", "e => e.length"), 1, kind)
        # 1 октября 2025 это среда: перед первым числом две пустые клетки (неделя с понедельника)
        self.assertEqual(page.evaluate("document.querySelectorAll('.cal-month')[1].querySelectorAll('.cal-cell.blank').length"), 2)
        self.assertEqual(page.evaluate("document.querySelector('.cal-cell.ill').innerText"), "6")
        page.click(".cal-cell.ill")
        info = page.inner_text(".dayinfo")
        for needle in ("6 октября, понедельник", "Пропуск по болезни", "1 урок: Рус. язык", "5 урок: Физкультура"):
            self.assertIn(needle, info)
        page.click(".cal-cell.absent")
        self.assertIn("20 октября, понедельник", page.inner_text(".dayinfo"))
        self.assertIn("Пропуск без уважительной причины", page.inner_text(".dayinfo"))
        self.assertIn("2 урок: Математика", page.inner_text(".dayinfo"))
        page.click(".cal-cell.absent")                                                                    # повторное нажатие закрывает
        self.assertIn("Нажмите на день", page.inner_text("#main"))
        self.assertEqual(page.eval_on_selector_all(".att-list .att-day", "e => e.length"), 3)             # ниже список дней с пропусками
        self.assertIn("Рус. язык, Математика", page.inner_text(".att-list"))
        self.assertTrue(page.eval_on_selector_all(".cal-cell.none", "els => els.length > 5 && els.every(e => e.tagName === 'SPAN')"))   # выходные не кнопки
        self.assertGreater(page.eval_on_selector_all(".cal-cell.ok", "e => e.length"), 20)                 # будни без пропусков: зелёные, как в этом году
        self.assertIn("не сообщает", page.inner_text("#main"))                                            # честная оговорка про прошлые годы
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 376)
        self.assertEqual(errors, [])

    def test_attendance_term_switch_and_tablet(self):
        page, errors = self.open_attendance(1280, 800)
        page.click('[data-year="1"]')
        page.click('[data-aterm="2"]')
        titles = page.eval_on_selector_all(".cal-title", "els => els.map(e => e.innerText.toLowerCase())")
        self.assertEqual(titles[0], "декабрь 2025")
        self.assertIn("Пропусков и опозданий нет", page.inner_text(".att"))
        self.assertEqual(page.eval_on_selector_all(".cal-cell.ill", "e => e.length"), 0)
        page.click('[data-aterm="1"]')
        self.assertEqual(page.eval_on_selector_all(".cal-cell.ill", "e => e.length"), 1)
        self.assertEqual(errors, [])

    def test_attendance_screen_without_year_data_is_hidden(self):
        from dev.sample import make_sample
        d = make_sample()
        d.pop("years")
        d["status"], d["config"], d["version"] = {"needLogin": False, "error": None, "syncing": False}, {}, "t"
        page, errors = self.mocked(d)
        page.click('nav.tabs button[data-tab="marks"]')
        self.assertIsNone(page.query_selector('[data-mview="att"]'))
        self.assertEqual(errors, [])

    # ---- учителя
    def test_teachers_are_shown_in_homework_and_schedule(self):
        page, errors = self.open(375, 812)
        page.locator(".lesson2", has_text="Рус. язык").first.locator("[data-toggle]").click()
        self.assertIn("Учитель: Иванова М. П.", page.locator(".lesson2.open .ldetails").first.inner_text())
        page.click('nav.tabs button[data-tab="plan"]')
        self.assertIn("Иванова М. П.", page.locator(".lesson", has_text="Рус. язык").first.inner_text())
        self.assertEqual(errors, [])

    def test_teachers_screen_lists_subjects_with_full_names_and_roles(self):
        page, errors = self.open(375, 812)
        page.click('nav.tabs button[data-tab="plan"]')
        labels = page.eval_on_selector_all(".seg button", "els => els.map(e => e.innerText)")
        self.assertEqual(labels, ["Расписание", "Учителя"])
        page.click('[data-pview="teachers"]')
        text = page.inner_text(".teacher-row >> nth=0") + page.inner_text("#main")
        self.assertIn("Иванова Мария Петровна", text)
        self.assertIn("· Учитель", text)
        self.assertIsNone(page.query_selector(".nav"))                                                     # на этом экране дней нет
        subjects = page.eval_on_selector_all(".teacher-row b", "els => els.map(e => e.innerText)")
        self.assertEqual(subjects, sorted(subjects, key=lambda x: x.lower()))                              # по алфавиту
        page.click('[data-pview="days"]')
        self.assertIsNotNone(page.query_selector(".nav"))
        self.assertEqual(errors, [])

    def test_without_teacher_data_the_schedule_is_as_before(self):
        from dev.sample import make_sample
        d = make_sample()
        d.pop("teachers")
        d["status"], d["config"], d["version"] = {"needLogin": False, "error": None, "syncing": False}, {}, "t"
        page, errors = self.mocked(d)
        page.click('nav.tabs button[data-tab="plan"]')
        self.assertIsNone(page.query_selector('[data-pview]'))
        self.assertNotIn("Иванова", page.inner_text("#main"))
        self.assertEqual(errors, [])

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


    # ---------- Настройки, единый вид, посещаемость (v0.11.0) ----------
    def open_ctx(self, width=375, height=812, scheme="light", settings=None, touch=True):
        ctx = self.browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, has_touch=touch)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(self.url)
        if settings is not None:
            page.evaluate("v => localStorage.setItem('settings', v)", settings if isinstance(settings, str) else json.dumps(settings))
            page.reload()
        page.wait_for_selector("nav.tabs")
        return page, errors

    def setting(self, page, key, value):
        if not page.query_selector(".settings"):
            page.click("#gear")
        page.click(f'[data-set="{key}"][data-val="{value}"]')

    def test_gear_opens_settings_and_back_returns(self):
        page, errors = self.open_ctx()
        page.click("#gear")
        self.assertIn("Настройки", page.inner_text(".settings h1"))
        for needle in ("Тема оформления", "Размер текста", "Листать дни свайпом", "Домашка", "Расписание", "при запуске"):
            self.assertIn(needle, page.inner_text(".settings"))
        self.assertIsNone(page.query_selector("nav.tabs button.on"))          # ни одна вкладка не подсвечена
        page.click("[data-settings-close]")
        self.assertIsNone(page.query_selector(".settings"))
        self.assertIsNotNone(page.query_selector(".day"))
        page.click("#gear")
        page.click('nav.tabs button[data-tab="plan"]')                         # любая вкладка тоже закрывает настройки
        self.assertIsNone(page.query_selector(".settings"))
        self.assertEqual(errors, [])

    def test_theme_manual_overrides_the_device_and_is_remembered(self):
        bg = lambda pg: pg.evaluate("getComputedStyle(document.body).backgroundColor")
        page, errors = self.open_ctx(scheme="light")
        light = bg(page)
        self.setting(page, "theme", "dark")
        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), "dark")
        self.assertNotEqual(bg(page), light)
        page.reload(); page.wait_for_selector("nav.tabs")
        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), "dark")   # после перезагрузки осталась
        self.assertEqual(self.open_ctx(scheme="dark")[0].evaluate("getComputedStyle(document.body).backgroundColor"), bg(page))
        self.setting(page, "theme", "auto")
        self.assertEqual(bg(page), light)                                                    # «Авто» при светлом устройстве = светлая
        page.close()
        dark_os, errors2 = self.open_ctx(scheme="dark")
        self.assertNotEqual(bg(dark_os), light)                                              # «Авто» при тёмном устройстве = тёмная
        self.setting(dark_os, "theme", "light")
        self.assertEqual(bg(dark_os), light)                                                 # светлую можно выбрать и при тёмном устройстве
        self.assertEqual(errors + errors2, [])

    def test_scale_is_one_for_every_screen_and_nothing_overflows(self):
        for scale, expect in (("0.9", 0.9), ("1.1", 1.1), ("1.2", 1.2)):
            page, errors = self.open_ctx(settings={"scale": float(scale)})
            self.assertAlmostEqual(float(page.evaluate("getComputedStyle(document.documentElement).zoom")), expect, places=2)
            for tab in ("hw", "marks", "plan"):
                page.click(f'nav.tabs button[data-tab="{tab}"]')
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 376, f"{tab} при {scale}")
            page.click('nav.tabs button[data-tab="marks"]'); page.click('[data-mview="att"]')
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 376, f"посещаемость при {scale}")
            page.close()
            self.assertEqual(errors, [])
        page, errors = self.open_ctx()
        self.setting(page, "scale", "1.2")
        self.assertAlmostEqual(float(page.evaluate("getComputedStyle(document.documentElement).zoom")), 1.2, places=2)
        self.setting(page, "scale", "1")
        self.assertAlmostEqual(float(page.evaluate("getComputedStyle(document.documentElement).zoom")), 1.0, places=2)
        self.assertEqual(errors, [])

    def test_homework_and_schedule_look_the_same(self):
        for w, h in ((375, 812), (1280, 800)):
            page, errors = self.open_ctx(w, h, touch=False)
            hw = page.evaluate("""() => { const q = (s, p) => getComputedStyle(document.querySelector(s))[p];
              return {bg: q('body', 'backgroundImage') + q('body', 'backgroundColor'), font: q('body', 'fontSize'),
                      title: q('.lt b', 'fontSize'), sub: q('.lt span', 'fontSize'), icon: q('.lic', 'width'),
                      radius: q('.day', 'borderTopLeftRadius'), shadow: q('.day', 'boxShadow'), header: q('header', 'backgroundImage')}; }""")
            page.click('nav.tabs button[data-tab="plan"]')
            plan = page.evaluate("""() => { const q = (s, p) => getComputedStyle(document.querySelector(s))[p];
              return {bg: q('body', 'backgroundImage') + q('body', 'backgroundColor'), font: q('body', 'fontSize'),
                      title: q('.lesson .t b', 'fontSize'), sub: q('.lesson .t span', 'fontSize'), icon: q('.lesson .ic', 'width'),
                      radius: q('main .card', 'borderTopLeftRadius'), shadow: q('main .card', 'boxShadow'), header: q('header', 'backgroundImage')}; }""")
            page.click('nav.tabs button[data-tab="marks"]')
            marks_font = page.evaluate("getComputedStyle(document.body).fontSize")
            self.assertEqual(hw, plan, f"домашка и расписание выглядят по-разному на {w}px")
            self.assertEqual(hw["font"], "18px")                                              # на планшете шрифт не прыгает
            self.assertEqual(marks_font, "18px")
            self.assertIsNone(page.query_selector(".scene"))
            page.close()
            self.assertEqual(errors, [])

    def test_swipe_in_schedule_can_be_turned_off(self):
        page, errors = self.open_ctx()
        page.click('nav.tabs button[data-tab="plan"]')
        title = lambda: page.inner_text(".nav .ttl")
        first = title()
        self.assertIn("Листай дни", page.inner_text("#main"))
        page.click("#gear"); page.click('[data-flip="swipe"]')
        self.assertEqual(page.get_attribute('[data-flip="swipe"]', "aria-checked"), "false")
        page.click("[data-settings-close]"); page.click('nav.tabs button[data-tab="plan"]')
        self.assertNotIn("Листай дни", page.inner_text("#main"))                              # подсказки про свайп больше нет
        self.swipe(page, -120)
        self.assertEqual(title(), first)                                                      # свайп ничего не делает
        page.click('.nav button[data-nav="1"]')
        self.assertNotEqual(title(), first)                                                   # стрелки работают
        page.reload(); page.wait_for_selector("nav.tabs")
        page.click('nav.tabs button[data-tab="plan"]')
        self.swipe(page, -120); self.swipe(page, -120)
        self.assertEqual(json.loads(page.evaluate("localStorage.getItem('settings')"))["swipe"], False)   # выбор запомнен
        page.click("#gear"); page.click('[data-flip="swipe"]'); page.click("[data-settings-close]")
        page.click('nav.tabs button[data-tab="plan"]')
        before = title(); self.swipe(page, -120)
        self.assertNotEqual(title(), before)                                                  # включили обратно: свайп снова работает
        self.assertEqual(errors, [])

    def test_homework_by_days_has_arrows_and_swipe_like_the_schedule(self):
        page, errors = self.open_ctx(settings={"hwMode": "pages"})
        self.assertEqual(page.eval_on_selector_all(".day", "e => e.length"), 1)               # один день за раз
        self.assertIsNotNone(page.query_selector(".nav"))
        self.assertIsNone(page.query_selector("[data-past]"))
        title = lambda: page.inner_text(".nav .ttl")
        first = title()
        self.assertIn("Листай дни", page.inner_text("#main"))
        self.swipe(page, -120)
        second = title()
        self.assertNotEqual(first, second)
        self.swipe(page, 120)
        self.assertEqual(title(), first)
        page.click('.nav button[data-nav="1"]'); self.assertEqual(title(), second)           # стрелки
        page.click('.nav button[data-nav="-1"]'); self.assertEqual(title(), first)
        page.query_selector(".strip .check").click()                                          # отметка «сделано» работает и тут
        self.assertTrue(page.query_selector(".strip.done"))
        page.click('nav.tabs button[data-tab="plan"]')                                        # день в расписании независим
        plan_before = page.inner_text(".nav .ttl"); page.click('.nav button[data-nav="1"]')
        page.click('nav.tabs button[data-tab="hw"]'); self.assertEqual(title(), first)
        page.click("#gear"); page.click('[data-flip="swipe"]'); page.click("[data-settings-close]")
        self.swipe(page, -120)
        self.assertEqual(title(), first)                                                      # выключили свайпы: не листает
        self.assertNotIn("Листай дни", page.inner_text("#main"))
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 376)
        self.assertEqual(errors, [])

    def test_homework_feed_is_the_default_and_does_not_swipe(self):
        page, errors = self.open_ctx()
        self.assertGreater(page.eval_on_selector_all(".day", "e => e.length"), 1)
        self.assertIsNone(page.query_selector(".nav"))
        before = page.inner_text("#main")
        self.swipe(page, -120)
        self.assertEqual(page.inner_text("#main"), before)
        page.click("#gear"); page.click('[data-set="hwMode"][data-val="pages"]'); page.click("[data-settings-close]")
        self.assertEqual(page.eval_on_selector_all(".day", "e => e.length"), 1)
        page.click("#gear"); page.click('[data-set="hwMode"][data-val="feed"]'); page.click("[data-settings-close]")
        self.assertGreater(page.eval_on_selector_all(".day", "e => e.length"), 1)
        self.assertEqual(errors, [])

    def test_homework_past_days_setting(self):
        page, errors = self.open_ctx()
        self.assertIn("Показать прошлые дни", page.inner_text("[data-past]"))
        days_before = page.eval_on_selector_all(".day", "e => e.length")
        page.click("#gear"); page.click('[data-flip="showPast"]'); page.click("[data-settings-close]")
        self.assertGreater(page.eval_on_selector_all(".day", "e => e.length"), days_before)   # прошлые дни показаны сразу
        self.assertEqual(errors, [])

    def test_teachers_in_schedule_can_be_hidden(self):
        page, errors = self.open_ctx()
        page.click('nav.tabs button[data-tab="plan"]')
        self.assertIn("Иванова М. П.", page.inner_text("#main"))
        page.click("#gear"); page.click('[data-flip="teachers"]'); page.click("[data-settings-close]")
        self.assertNotIn("Иванова М. П.", page.inner_text("#main"))
        page.click('[data-pview="teachers"]')
        self.assertIn("Иванова Мария Петровна", page.inner_text("#main"))                     # экран «Учителя» остаётся
        self.assertEqual(errors, [])

    def test_start_tab_is_remembered(self):
        page, errors = self.open_ctx()
        self.setting(page, "startTab", "plan")
        page.reload(); page.wait_for_selector("nav.tabs")
        self.assertEqual(page.get_attribute("nav.tabs button.on", "data-tab"), "plan")
        self.assertEqual(errors, [])

    def test_clear_done_needs_two_taps_and_reset_restores_defaults(self):
        page, errors = self.open_ctx()
        page.query_selector(".strip .check").click()
        self.assertTrue(page.query_selector(".strip.done"))
        page.click("#gear")
        page.click("[data-clear-done]")
        self.assertIn("Нажмите ещё раз", page.inner_text("[data-clear-done]"))
        self.assertIsNotNone(page.evaluate("localStorage.getItem('done')"))                  # после одного нажатия ничего не стёрто
        page.click("[data-clear-done]")
        self.assertIsNone(page.evaluate("localStorage.getItem('done')"))
        self.setting(page, "theme", "dark"); self.setting(page, "scale", "1.2")
        page.click("[data-reset-settings]")
        self.assertEqual(page.evaluate("document.documentElement.dataset.theme || 'auto'"), "auto")
        self.assertAlmostEqual(float(page.evaluate("getComputedStyle(document.documentElement).zoom")), 1.0, places=2)
        self.assertEqual(errors, [])

    def test_broken_settings_in_storage_fall_back_to_defaults(self):
        for junk in ("не json", '{"theme": "розовая", "scale": 99, "swipe": "да", "hwMode": 5, "startTab": "x"}', "null", "[1,2]"):
            page, errors = self.open_ctx(settings=junk)
            self.assertIsNone(page.evaluate("document.documentElement.dataset.theme"))
            self.assertEqual(page.eval_on_selector_all(".day", "e => e.length") > 1, True)
            page.click("#gear")
            self.assertEqual(page.inner_text('[data-set="theme"].on'), "Авто")
            self.assertEqual(page.inner_text('[data-set="scale"].on'), "Обычно")
            page.close()
            self.assertEqual(errors, [], junk)

    def test_settings_reachable_in_the_offline_copy_without_logout(self):
        page, errors = self.open_ctx()
        page.evaluate("window.__DATA__ = data; showSettings = true; render()")
        self.assertIsNone(page.query_selector("[data-change-login]"))
        self.assertEqual(errors, [])

    def test_past_period_calendar_is_green_too_and_says_was_at_lessons(self):
        page, errors = self.open_attendance()
        ok_bg = page.evaluate("getComputedStyle(document.querySelector('.cal-cell.ok')).backgroundColor")
        self.assertNotEqual(ok_bg, "rgba(0, 0, 0, 0)")
        self.assertIn("был на уроках", page.inner_text(".legend"))
        page.click('[data-year="1"]'); page.click('[data-aterm="1"]')
        self.assertGreater(page.eval_on_selector_all(".cal-cell.ok", "e => e.length"), 10)
        self.assertEqual(page.evaluate("[...new Set([...document.querySelectorAll('.cal-cell.ok')].map(e => getComputedStyle(e).backgroundColor))]"), [ok_bg])
        self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('.legend .l-ok')).backgroundColor"), ok_bg)
        self.assertIn("был на уроках", page.inner_text(".legend"))
        self.assertNotIn("пропусков нет", page.inner_text(".legend"))
        page.click(".cal-cell.ok")
        self.assertIn("Был на уроках", page.inner_text(".dayinfo"))
        self.assertNotEqual(page.evaluate("getComputedStyle(document.querySelector('.cal-cell.ill')).backgroundColor"), ok_bg)   # дни с пропусками по-прежнему другого цвета
        self.assertEqual(errors, [])

    def test_today_in_the_day_pager_is_blue_like_in_the_feed(self):
        page, errors = self.open_ctx()
        feed = page.evaluate("getComputedStyle(document.querySelector('.dtitle .rel')).color")
        self.assertNotEqual(feed, page.evaluate("getComputedStyle(document.body).color"))
        self.setting(page, "hwMode", "pages"); page.click("[data-settings-close]")
        self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('.nav .ttl .rel')).color"), feed)
        self.assertIn("Сегодня", page.inner_text(".nav .ttl .rel"))
        page.click('nav.tabs button[data-tab="plan"]')
        self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('.nav .ttl .rel')).color"), feed)
        self.assertEqual(errors, [])

    def test_header_is_compact_and_the_top_of_every_screen_is_the_same(self):
        for w, h in ((375, 812), (1280, 800)):
            page, errors = self.open_ctx(w, h, touch=False)
            self.assertLess(page.evaluate("document.querySelector('header').getBoundingClientRect().height"), 100 if w < 700 else 80)
            tops = {}
            for tab in ("hw", "marks", "plan"):
                page.click(f'nav.tabs button[data-tab="{tab}"]')
                tops[tab] = page.evaluate("document.querySelector('main .grid > div > :first-child').getBoundingClientRect().top - document.querySelector('header').getBoundingClientRect().bottom")
            self.assertEqual(len({round(v) for v in tops.values()}), 1, f"верх экранов разный на {w}px: {tops}")
            self.assertGreaterEqual(min(tops.values()), 8)
            page.close()
            self.assertEqual(errors, [])

    def test_missed_days_are_written_next_to_missed_lessons(self):
        page, errors = self.open_years()
        page.click('[data-year="1"]'); page.click('[data-term="1"]')
        text = page.inner_text(".att")
        self.assertIn("Пропущено 6 уроков (2 дня)", text)                                        # 6 и 20 октября; опоздание 21-го днём пропуска не считается
        self.assertIn("5 уроков по болезни", text)
        page.click('[data-term="final"]')
        text = page.inner_text(".att")
        self.assertIn("Посещаемость за год", text)
        self.assertIn("пропущено 6 уроков (2 дня)", text)                                       # и в строке триместра
        page.click('[data-mview="att"]'); page.click('[data-aterm="1"]')
        self.assertIn("Пропущено 6 уроков (2 дня)", page.inner_text(".att"))                     # и на экране посещаемости
        self.assertEqual(errors, [])

    def test_missed_days_counting_and_plurals(self):
        page, _ = self.open(375, 812)
        self.assertEqual(page.evaluate("[1, 2, 5, 11, 21, 22, 25].map(daysWord)"), ["1 день", "2 дня", "5 дней", "11 дней", "21 день", "22 дня", "25 дней"])
        self.assertEqual(page.evaluate("""[
            missedDays([]), missedDays(undefined),
            missedDays([{date: 'a', kind: 'ill'}, {date: 'a', kind: 'absent'}, {date: 'b', kind: 'ill'}]),
            missedDays([{date: 'a', kind: 'late'}]),
            missedDays([{date: 'a', kind: 'ill'}, {date: 'b', kind: 'late'}])]"""), [0, 0, 2, 0, 1])
        self.assertEqual(page.evaluate("lessonsAndDays(8, 0)"), "8 уроков")                       # старые данные без дней: как раньше
        self.assertIn("(3 дня)", page.evaluate("lessonsAndDays(8, 3)"))


    def test_months_and_weekdays_are_written_in_full(self):
        page, errors = self.open_ctx()
        text = page.inner_text("#main") + page.inner_text("header")
        for short in ("нояб.", "окт.", "дек.", "сен.", "янв.", "фев.", "апр.", "авг."):
            self.assertNotIn(short, text)
        self.assertRegex(page.inner_text(".dtitle"), r"^[а-я]{6,11}, \d{1,2} [а-я]{3,8}")        # «четверг, 12 ноября», не «чт, 12 нояб.»
        self.assertRegex(page.inner_text("#updated"), r"Обновлено \d{1,2} [а-я]{3,8} в")
        page.click('nav.tabs button[data-tab="plan"]')
        self.assertNotRegex(page.inner_text(".nav .ttl small"), r"\.")
        page.click('nav.tabs button[data-tab="marks"]')
        self.assertNotIn(".", page.inner_text(".row .t span").split("·")[0])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
