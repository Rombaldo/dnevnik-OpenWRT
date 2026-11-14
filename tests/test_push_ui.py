"""Уведомления на странице (⚙ → «Уведомления») и сервис-воркер: с подставными браузерными API и ответами роутера."""
import base64
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import server

server.DEMO = True
ROOT = Path(__file__).resolve().parent.parent
KEY = base64.urlsafe_b64encode(b"\x04" + bytes(range(64))).decode().rstrip("=")

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None

# Подставные API браузера: настоящие подписки на пуши без телефона не оформить
FAKES = """
(() => {
  window.__push = { reqs: [] };
  const mkSub = () => ({ endpoint: "https://fcm.googleapis.com/fcm/send/test-device",
    toJSON() { return { endpoint: this.endpoint, keys: { p256dh: "BAAA", auth: "AAAA" } }; },
    async unsubscribe() { window.__push.reqs.push(["unsubscribe"]); reg.pushManager.sub = null; return true; } });
  const reg = { pushManager: { sub: window.__preSub ? mkSub() : null,
    async getSubscription() { return this.sub; },
    async subscribe(o) { window.__push.reqs.push(["subscribe", o.userVisibleOnly, o.applicationServerKey.length]); this.sub = mkSub(); return this.sub; } } };
  const sw = { async register(url, o) { window.__push.reqs.push(["register", url, o && o.scope]); return reg; }, ready: Promise.resolve(reg), async getRegistration() { return reg; } };
  Object.defineProperty(navigator, "serviceWorker", { value: sw, configurable: true });
  window.PushManager = function PushManager() {};
  const asked = window.__asked || "granted";
  window.Notification = { permission: "default", async requestPermission() { window.__push.reqs.push(["permission"]); window.Notification.permission = asked; return asked; } };
})();
"""


@unittest.skipIf(sync_playwright is None, "Playwright не установлен")
class PushUiBase(unittest.TestCase):
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
        cls.httpd.server_close()

    def open(self, pre="", answers=None, events=None, ua=None, fakes=True, settings=True):
        """pre: JS, выполняемый до подмен (например, window.__preSub = true)."""
        ctx = self.browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, **({"user_agent": ua} if ua else {}))
        page = ctx.new_page()
        self.errors, self.requests = [], []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" and "status of 40" not in m.text else None)
        ans = {"config": {"ok": True, "ready": True, "publicKey": KEY}, "get": {"ok": True, "subscribed": False, "prefs": None},
               "subscribe": None, "update": None, "test": {"ok": True, "message": "Отправлено. Уведомление придёт на телефон в течение минуты."},
               "unsubscribe": {"ok": True}, "events": {"ok": True, "events": events if events is not None else []}}
        ans.update(answers or {})

        def handler(route):
            body = json.loads(route.request.post_data)
            self.requests.append(body)
            a = ans.get(body["action"])
            if body["action"] == "subscribe" and a is None:
                a = {"ok": True, "id": "x", "prefs": body.get("prefs") or {"types": {"mark": True, "homework": True, "schedule": True, "attendance": True}, "quiet": {"on": False, "from": "22:00", "to": "07:00"}}}
            if body["action"] == "update" and a is None:
                a = {"ok": True, "prefs": body.get("prefs") or {"types": {"mark": True, "homework": True, "schedule": True, "attendance": True}, "quiet": {"on": False, "from": "22:00", "to": "07:00"}}, "name": body.get("name", "")}
            status = 401 if (a or {}).get("auth") else 200
            route.fulfill(status=status, json=a if a is not None else {"ok": True})
        page.route("**/cgi-bin/push", handler)
        if fakes:
            page.add_init_script(pre + FAKES)
        elif pre:
            page.add_init_script(pre)
        page.goto(self.url)
        page.wait_for_selector("nav.tabs")
        if settings:
            page.click("#gear")
            page.wait_for_selector(".set-card:has(h2:has-text('Уведомления'))")
        return page

    def actions(self):
        return [r["action"] for r in self.requests]

    def js_reqs(self, page):
        return page.evaluate("window.__push.reqs")

    def enable(self, page):
        page.click("button[data-push=enable]")
        page.wait_for_selector("button[data-pf=mark]")

    def card_text(self, page):
        return page.inner_text(".set-card:has(h2:has-text('Уведомления'))")


class EnableTests(PushUiBase):
    def test_enable_flow_asks_permission_registers_subscribes_and_tells_the_router(self):
        page = self.open()
        self.assertIn("шифруется", self.card_text(page))                                    # сказано про приватность
        self.enable(page)
        self.assertEqual([r[0] for r in self.js_reqs(page)], ["permission", "register", "subscribe"])
        self.assertEqual(self.js_reqs(page)[1][1:], ["sw.js", "./"])
        self.assertEqual(self.js_reqs(page)[2][1:], [True, 65])                              # userVisibleOnly и ключ отправителя из 65 байт
        acts = self.actions()
        self.assertLess(acts.index("config"), acts.index("subscribe"))
        sub = [r for r in self.requests if r["action"] == "subscribe"][0]
        self.assertEqual(sub["subscription"]["endpoint"], "https://fcm.googleapis.com/fcm/send/test-device")
        self.assertIn("keys", sub["subscription"])
        self.assertTrue(sub["ua"])
        self.assertIn("Уведомления включены", self.card_text(page))
        for label in ("Новые оценки", "Новая домашка", "Отмена и перенос уроков", "Пропуски и опоздания", "Тихие часы", "Проверить уведомление", "Выключить на этом устройстве"):
            self.assertIn(label, self.card_text(page))
        self.assertEqual(self.errors, [])

    def test_permission_denied_stops_before_anything_is_sent(self):
        page = self.open(pre="window.__asked = 'denied';")
        page.click("button[data-push=enable]")
        page.wait_for_selector(".set-card .msg.bad")
        self.assertIn("запрещены", self.card_text(page))
        self.assertNotIn("config", self.actions())
        self.assertNotIn("register", [r[0] for r in self.js_reqs(page)])
        self.assertTrue(page.is_visible("button[data-push=enable]"))                          # можно попробовать снова

    def test_router_without_the_module_explains_what_to_install(self):
        page = self.open(answers={"config": {"ok": True, "ready": False, "publicKey": "", "message": "На роутере не установлен модуль уведомлений. Выполните: sh /root/dnevnik/router/push.sh"}})
        page.click("button[data-push=enable]")
        page.wait_for_selector(".set-card .msg.bad")
        self.assertIn("push.sh", self.card_text(page))
        self.assertNotIn("subscribe", [r[0] for r in self.js_reqs(page)])

    def test_router_refusing_the_subscription_undoes_it_in_the_browser(self):
        page = self.open(answers={"subscribe": {"ok": False, "message": "Уже подключено 20 устройств: отключите ненужные."}})
        page.click("button[data-push=enable]")
        page.wait_for_selector(".set-card .msg.bad")
        self.assertIn("20 устройств", self.card_text(page))
        self.assertIn(["unsubscribe"], self.js_reqs(page))                                   # в браузере подписку отменили, чтобы не висела
        self.assertTrue(page.is_visible("button[data-push=enable]"))

    def test_browser_error_is_reported_in_plain_words(self):
        page = self.open(pre="window.__boom = true;")
        page.evaluate("() => { navigator.serviceWorker.register = async () => { throw new Error('нет сервис-воркера'); }; }")
        page.click("button[data-push=enable]")
        page.wait_for_selector(".set-card .msg.bad")
        self.assertIn("нет сервис-воркера", self.card_text(page))
        self.assertIn("экрана «Домой»", self.card_text(page))

    def test_locked_router_shows_the_password_screen(self):
        page = self.open(answers={"config": {"ok": False, "auth": True, "message": "Нужен пароль семьи."}})
        page.click("button[data-push=enable]")
        page.wait_for_selector("#lock-form")


class DeviceNameTests(PushUiBase):
    def test_name_is_sent_with_the_subscription(self):
        page = self.open()
        page.fill("input[data-pname]", "Планшет Ани")
        self.enable(page)
        sub = [r for r in self.requests if r["action"] == "subscribe"][0]
        self.assertEqual(sub["name"], "Планшет Ани")
        self.assertGreater(len(sub["ua"]), 20)                                               # полная строка браузера: по ней роутер называет устройство
        self.assertEqual(page.input_value("input[data-pname]"), "Планшет Ани")              # и в списке настроек имя осталось

    def test_no_name_means_no_name_field_in_the_request(self):
        page = self.open()
        self.enable(page)
        sub = [r for r in self.requests if r["action"] == "subscribe"][0]
        self.assertNotIn("name", sub)

    def test_name_is_saved_when_editing_finishes(self):
        page = self.open()
        self.enable(page)
        page.fill("input[data-pname]", "Телефон мамы")
        page.press("input[data-pname]", "Tab")
        for _ in range(50):
            if any(r["action"] == "update" and r.get("name") == "Телефон мамы" for r in self.requests):
                break
            page.wait_for_timeout(100)
        upd = [r for r in self.requests if r["action"] == "update" and "name" in r][-1]
        self.assertEqual(upd["name"], "Телефон мамы")
        self.assertNotIn("prefs", upd)                                                       # меняется только имя, настройки не трогаются

    def test_existing_name_comes_from_the_router(self):
        page = self.open(pre="window.__preSub = true;", answers={"get": {"ok": True, "subscribed": True, "name": "Планшет Ани", "prefs": {"types": {"mark": True, "homework": True, "schedule": True, "attendance": True}, "quiet": {"on": False, "from": "22:00", "to": "07:00"}}}})
        page.wait_for_selector("input[data-pname]")
        self.assertEqual(page.input_value("input[data-pname]"), "Планшет Ани")

    def test_the_field_is_limited_to_thirty_characters_and_not_executed(self):
        page = self.open()
        page.fill("input[data-pname]", '<img src=x onerror="window.__pwned=1">' + "я" * 40)
        self.assertEqual(page.get_attribute("input[data-pname]", "maxlength"), "30")
        self.assertIsNone(page.evaluate("window.__pwned"))
        self.assertEqual(page.eval_on_selector_all(".set-card img", "e => e.length"), 0)


class ExistingStateTests(PushUiBase):
    def test_device_already_subscribed_shows_its_settings_from_the_router(self):
        prefs = {"types": {"mark": True, "homework": False, "schedule": True, "attendance": False}, "quiet": {"on": True, "from": "21:00", "to": "06:30"}}
        page = self.open(pre="window.__preSub = true;", answers={"get": {"ok": True, "subscribed": True, "prefs": prefs}})
        page.wait_for_selector("button[data-pf=mark]")
        self.assertEqual(page.eval_on_selector_all("button[data-pf]", "e => e.map(x => x.getAttribute('aria-checked'))"), ["true", "false", "true", "false"])
        self.assertEqual(page.input_value("input[data-pq=from]"), "21:00")
        self.assertEqual(page.input_value("input[data-pq=to]"), "06:30")
        self.assertFalse(page.is_visible("button[data-push=enable]"))

    def test_stale_browser_subscription_unknown_to_the_router_is_replaced(self):
        page = self.open(pre="window.__preSub = true;", answers={"get": {"ok": True, "subscribed": False, "prefs": None}})
        page.wait_for_selector("button[data-push=enable]")
        page.click("button[data-push=enable]")
        page.wait_for_selector("button[data-pf=mark]")
        kinds = [r[0] for r in self.js_reqs(page)]
        self.assertLess(kinds.index("unsubscribe"), kinds.index("subscribe"))                 # старую отменили, оформили новую


class SettingsTests(PushUiBase):
    def enabled(self, **kw):
        page = self.open(**kw)
        self.enable(page)
        return page

    def test_toggling_a_type_saves_to_the_router(self):
        page = self.enabled()
        page.click("button[data-pf=homework]")
        page.wait_for_function("document.querySelector('button[data-pf=homework]').getAttribute('aria-checked') === 'false'")
        upd = [r for r in self.requests if r["action"] == "update"]
        self.assertEqual(len(upd), 1)
        self.assertFalse(upd[0]["prefs"]["types"]["homework"])
        self.assertTrue(upd[0]["prefs"]["types"]["mark"])
        self.assertTrue(upd[0]["endpoint"].startswith("https://fcm.googleapis.com/"))

    def test_quiet_hours_switch_and_times_are_saved(self):
        page = self.enabled()
        self.assertFalse(page.is_visible("input[data-pq=from]"))
        page.click("button[data-pq=on]")
        page.wait_for_selector("input[data-pq=from]")
        page.fill("input[data-pq=from]", "21:30")
        page.press("input[data-pq=from]", "Tab")
        page.fill("input[data-pq=to]", "07:45")
        page.press("input[data-pq=to]", "Tab")
        page.wait_for_function("typeof pushState !== 'undefined' && pushState.prefs && pushState.prefs.quiet.to === '07:45'")
        last = [r for r in self.requests if r["action"] == "update"][-1]
        self.assertEqual(last["prefs"]["quiet"], {"on": True, "from": "21:30", "to": "07:45"})

    def test_test_button_shows_the_router_answer(self):
        page = self.enabled()
        page.click("button[data-push=test]")
        page.wait_for_selector(".set-card .msg.good:has-text('Отправлено')")
        self.assertIn("test", self.actions())
        self.requests.clear()
        page2 = self.open(answers={"test": {"ok": False, "message": "Не получилось отправить: нет связи со службой уведомлений."}})
        self.enable(page2)
        page2.click("button[data-push=test]")
        page2.wait_for_selector(".set-card .msg.bad:has-text('нет связи')")

    def test_disable_unsubscribes_on_the_router_and_in_the_browser(self):
        page = self.enabled()
        page.click("button[data-push=disable]")
        page.wait_for_selector("button[data-push=enable]")
        self.assertIn("unsubscribe", self.actions())
        self.assertIn(["unsubscribe"], self.js_reqs(page))
        self.assertIn("выключены", self.card_text(page))
        self.assertEqual(self.errors, [])

    def test_settings_card_is_inside_the_page_without_horizontal_scroll(self):
        page = self.enabled()
        page.click("button[data-pq=on]")
        page.wait_for_selector("input[data-pq=from]")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)


class UnsupportedTests(PushUiBase):
    def test_http_home_address_explains_that_https_is_needed(self):
        page = self.open(pre="Object.defineProperty(window, 'isSecureContext', {value: false});", fakes=False)
        page.wait_for_function("typeof pushState !== 'undefined' && pushState.loaded")
        self.assertIn("https://", self.card_text(page))
        self.assertFalse(page.is_visible("button[data-push=enable]"))

    def test_iphone_in_safari_tab_is_told_to_add_the_page_to_the_home_screen(self):
        ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        page = self.open(pre="delete window.PushManager;", ua=ua, fakes=False)
        page.wait_for_function("typeof pushState !== 'undefined' && pushState.loaded")
        text = self.card_text(page)
        self.assertIn("экран «Домой»", text)
        self.assertIn("iOS 16.4", text)

    def test_unknown_browser_without_push(self):
        page = self.open(pre="delete window.PushManager;", fakes=False)
        page.wait_for_function("typeof pushState !== 'undefined' && pushState.loaded")
        self.assertIn("не умеет показывать уведомления", self.card_text(page))

    def test_whats_new_is_shown_even_where_push_is_not_supported(self):
        ev = [{"id": "a", "type": "mark", "title": "Новая оценка: 5", "body": "Математика · Ответ на уроке (12 ноября)", "ts": 1_800_000_000}]
        page = self.open(pre="delete window.PushManager;", fakes=False, events=ev)
        page.wait_for_function("typeof pushState !== 'undefined' && pushState.loaded")
        self.assertIn("Что нового", self.card_text(page))
        self.assertIn("Новая оценка: 5", self.card_text(page))


class EventsListTests(PushUiBase):
    def test_recent_events_are_listed_newest_first_with_time(self):
        ev = [{"id": str(i), "type": "mark", "title": f"Новая оценка: {i}", "body": "Математика", "ts": 1_800_000_000 + i * 60} for i in range(5, 0, -1)]
        page = self.open(events=ev)
        page.wait_for_selector(".evrow")
        rows = page.eval_on_selector_all(".evrow", "e => e.map(x => x.innerText)")
        self.assertEqual(len(rows), 5)
        self.assertIn("Новая оценка: 5", rows[0])
        self.assertRegex(rows[0], r"\d+ [а-я]+, \d\d:\d\d")

    def test_empty_list_says_nothing_new(self):
        page = self.open(events=[])
        page.wait_for_function("typeof pushState !== 'undefined' && pushState.loaded")
        self.assertIn("Пока ничего нового", self.card_text(page))

    def test_event_text_is_not_executed(self):
        evil = '<img src=x onerror="window.__pwned=1">'
        page = self.open(events=[{"id": "x", "type": "mark", "title": evil, "body": evil, "ts": 1_800_000_000}])
        page.wait_for_selector(".evrow")
        self.assertEqual(page.eval_on_selector_all(".set-card img", "e => e.length"), 0)
        self.assertIsNone(page.evaluate("window.__pwned"))
        self.assertIn("<img", page.inner_text(".evrow"))


class ServiceWorkerTests(PushUiBase):
    """sw.js выполняем с подставным self: что он показывает и что делает по нажатию."""

    def run_sw(self, script):
        page = self.browser.new_page()
        page.set_content("<body></body>")
        code = (ROOT / "web" / "sw.js").read_text(encoding="utf-8")
        return page.evaluate("""async ([code, script]) => {
          const L = {}, shown = [], opened = [], log = [];
          const self_ = { addEventListener: (t, f) => { L[t] = f; }, skipWaiting() { log.push("skip"); }, windows: [],
            clients: { claim() { log.push("claim"); return Promise.resolve(); }, matchAll: async () => self_.windows, openWindow: async (u) => { opened.push(u); } },
            registration: { showNotification: (t, o) => { shown.push([t, o]); return Promise.resolve(); } } };
          new Function("self", code)(self_);
          const fire = async (type, ev) => { const ps = []; ev.waitUntil = (p) => ps.push(p); L[type](ev); await Promise.all(ps); };
          await new Function("fire", "self_", "shown", "opened", "log", "L", "return (async () => {" + script + "})()")(fire, self_, shown, opened, log, L);
          return { shown, opened, log, events: Object.keys(L) };
        }""", [code, script])

    def test_listens_to_exactly_what_it_needs(self):
        r = self.run_sw("")
        self.assertEqual(sorted(r["events"]), ["activate", "install", "notificationclick", "push"])

    def test_push_shows_the_notification_from_the_router_message(self):
        r = self.run_sw("""await fire("push", { data: { json: () => ({ title: "Новая оценка: 5", body: "Математика", tag: "abc", url: "./" }) } });""")
        title, opts = r["shown"][0]
        self.assertEqual((title, opts["body"], opts["tag"], opts["icon"], opts["data"]["url"], opts["lang"]), ("Новая оценка: 5", "Математика", "abc", "icon.png", "./", "ru"))

    def test_every_push_shows_something_even_if_the_payload_is_broken_or_missing(self):
        """Браузеры (особенно Safari на iPhone) отзывают подписку, если на пуш не показали уведомление."""
        for ev in ('{ data: { json: () => { throw new Error("плохой json"); }, text: () => "просто текст" } }', "{}", '{ data: { json: () => ({}) } }'):
            r = self.run_sw(f'await fire("push", {ev});')
            self.assertEqual(len(r["shown"]), 1, ev)
            self.assertTrue(r["shown"][0][0])
        r = self.run_sw('await fire("push", { data: { json: () => { throw 1; }, text: () => "просто текст" } });')
        self.assertEqual(r["shown"][0][1]["body"], "просто текст")

    def test_click_focuses_the_open_diary_or_opens_it(self):
        r = self.run_sw("""let closed = 0, focused = 0;
          self_.windows = [{ focus: () => { focused++; return Promise.resolve(); } }];
          await fire("notificationclick", { notification: { close: () => { closed++; }, data: { url: "./" } } });
          log.push("closed" + closed, "focused" + focused);
          self_.windows = [];
          await fire("notificationclick", { notification: { close: () => { closed++; }, data: { url: "./?x=1" } } });
          await fire("notificationclick", { notification: { close: () => { closed++; } } });""")
        self.assertIn("closed1", r["log"])
        self.assertIn("focused1", r["log"])
        self.assertEqual(r["opened"], ["./?x=1", "./"])

    def test_it_never_touches_the_network_or_caches(self):
        text = (ROOT / "web" / "sw.js").read_text(encoding="utf-8")
        for bad in ("fetch(", "caches.", "addEventListener(\"fetch\""):
            self.assertNotIn(bad, text)
        self.assertTrue((ROOT / "web" / "icon.png").exists())


class StaticTests(unittest.TestCase):
    def test_sw_is_served_by_the_dev_server_and_listed_for_the_router(self):
        self.assertTrue((ROOT / "web" / "sw.js").exists())
        init = (ROOT / "router" / "dnevnik.init").read_text(encoding="utf-8")
        self.assertIn('cp -f "$APP"/web/* "$WWW"/', init)                                    # весь web/ копируется, sw.js вместе с ним


if __name__ == "__main__":
    unittest.main()
