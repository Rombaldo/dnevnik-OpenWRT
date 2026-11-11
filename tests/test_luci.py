"""Тесты страницы «Службы → Дневник» в LuCI: серверная часть (rpcd-плагин), установка файлов, выбор --luci и сама страница."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "luci" / "rpcd" / "luci.dnevnik"
VIEW = ROOT / "luci" / "htdocs" / "view" / "dnevnik" / "status.js"
ACL = ROOT / "luci" / "acl.d" / "luci-app-dnevnik.json"
MENU = ROOT / "luci" / "menu.d" / "luci-app-dnevnik.json"

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None


def load_plugin():
    loader = importlib.machinery.SourceFileLoader("luci_dnevnik", str(PLUGIN))
    spec = importlib.util.spec_from_loader("luci_dnevnik", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class PluginCase(unittest.TestCase):
    """Плагин запускается как на роутере (отдельной программой), но все пути указывают во временную папку."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dnevnik-luci-"))
        self.home, self.www, self.run_dir = self.tmp / "home", self.tmp / "www", self.tmp / "run"
        for d in (self.home / "router", self.home / "data", self.www, self.run_dir):
            d.mkdir(parents=True)
        shutil.copy(ROOT / "core.py", self.home / "core.py")
        (self.home / "VERSION").write_text("0.12.0\n")
        (self.home / "config.json").write_text(json.dumps({"name": "", "other": 5}))
        self.cron = self.tmp / "crontab"
        self.cron.write_text("0 3 * * * /usr/bin/other\n*/20 6-22 * * * /root/dnevnik/router/sync.sh\n")
        (self.www / "data.json").write_text(json.dumps({
            "updatedAt": "2026-11-12T01:29:00", "days": [{}, {}, {}], "student": {"name": "Аня", "school": "Школа 1"},
            "status": {"needLogin": False, "error": None}}))
        sync = self.home / "router" / "sync.sh"
        sync.write_text(f"#!/bin/sh\necho started >> {self.tmp}/sync-ran\n")
        sync.chmod(0o755)
        install = self.home / "router" / "install.sh"
        install.write_text(f"#!/bin/sh\necho \"args: $@\" > {self.tmp}/install-args\nsleep 3\n")
        self.env = dict(os.environ, DNEVNIK_HOME=str(self.home), DNEVNIK_WWW=str(self.www), DNEVNIK_TMP=str(self.run_dir),
                        DNEVNIK_CRON=str(self.cron), DNEVNIK_NO_SERVICES="1", DNEVNIK_DOMAIN="lan",
                        DNEVNIK_VERSION_URL=(self.tmp / "remote-version").as_uri())
        (self.tmp / "remote-version").write_text("0.12.0\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, method, args=None):
        r = subprocess.run([sys.executable, str(PLUGIN), "call", method], input=json.dumps(args or {}), capture_output=True,
                           text=True, env=self.env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)


class PluginTests(PluginCase):
    def test_list_methods_are_in_acl_and_in_the_page(self):
        r = subprocess.run([sys.executable, str(PLUGIN), "list"], capture_output=True, text=True, env=self.env)
        methods = set(json.loads(r.stdout))
        acl = json.loads(ACL.read_text())["luci-app-dnevnik"]
        allowed = set(acl["read"]["ubus"]["luci.dnevnik"]) | set(acl["write"]["ubus"]["luci.dnevnik"])
        self.assertEqual(methods, allowed)                                           # нет метода без права и права без метода
        js = VIEW.read_text()
        for m in methods:
            self.assertIn(f"method: '{m}'", js, m)                                   # страница вызывает только существующие методы
        self.assertEqual(set(acl["read"]["ubus"]["luci.dnevnik"]), {"status", "log", "check_update"})   # чтение только безвредное

    def test_menu_points_to_the_page_file(self):
        menu = json.loads(MENU.read_text())["admin/services/dnevnik"]
        self.assertEqual(menu["action"]["path"], "dnevnik/status")
        self.assertTrue((ROOT / "luci" / "htdocs" / "view" / menu["action"]["path"]).with_suffix(".js").is_file())
        self.assertEqual(menu["depends"]["acl"], ["luci-app-dnevnik"])

    def test_unknown_method_is_refused(self):
        r = subprocess.run([sys.executable, str(PLUGIN), "call", "rm_rf"], input="{}", capture_output=True, text=True, env=self.env)
        self.assertNotEqual(r.returncode, 0)

    def test_status(self):
        s = self.call("status")
        self.assertEqual((s["version"], s["student"], s["school"], s["days"]), ("0.12.0", "Аня", "Школа 1", 3))
        self.assertEqual((s["every"], s["from"], s["to"]), (20, 6, 23))
        self.assertEqual(s["updatedAt"], "2026-11-12T01:29:00")
        self.assertFalse(s["needLogin"] or s["syncing"] or s["updating"] or s["canRestore"])
        self.assertEqual(s["url"], "http://dnevnik.lan")
        self.assertIsInstance(s["nextIn"], int)

    def test_status_without_page_data_does_not_crash(self):
        (self.www / "data.json").unlink()
        s = self.call("status")
        self.assertEqual((s["days"], s["student"], s["updatedAt"]), (0, "", None))
        (self.www / "data.json").write_text("{не json")
        self.assertEqual(self.call("status")["days"], 0)

    def test_status_shows_need_login_and_running_sync(self):
        page = json.loads((self.www / "data.json").read_text())
        page["status"] = {"needLogin": True, "error": "нужен вход"}
        (self.www / "data.json").write_text(json.dumps(page))
        (self.run_dir / "dnevnik-sync.lock").write_text(str(os.getpid()))            # процесс жив
        s = self.call("status")
        self.assertTrue(s["needLogin"] and s["syncing"])
        self.assertEqual(s["error"], "нужен вход")
        (self.run_dir / "dnevnik-sync.lock").write_text("999999999")                 # такого процесса нет
        self.assertFalse(self.call("status")["syncing"])

    def test_next_run_calculation(self):
        p = load_plugin()
        at = lambda h, m, s=0: datetime(2026, 11, 12, h, m, s)
        self.assertEqual(p.next_run_in(20, 6, 23, at(10, 5)), 15 * 60)
        self.assertEqual(p.next_run_in(20, 6, 23, at(10, 20)), 20 * 60)              # ровно в запуск: ждём следующий
        self.assertEqual(p.next_run_in(20, 6, 23, at(1, 29)), 4 * 3600 + 31 * 60)    # ночью: до 6:00
        self.assertEqual(p.next_run_in(20, 6, 23, at(22, 50)), 7 * 3600 + 10 * 60)   # после последнего: до 6:00 завтра
        self.assertEqual(p.next_run_in(60, 6, 23, at(10, 5)), 55 * 60)
        self.assertEqual(p.next_run_in(5, 0, 24, at(23, 58)), 120)

    def test_schedule_is_read_from_cron_even_if_unusual(self):
        p = load_plugin()
        for line, expect in (("*/10 7-21 * * * /root/dnevnik/router/sync.sh", (10, 7, 22)), ("0 6-22 * * * /root/dnevnik/router/sync.sh", (60, 6, 23)),
                             ("17 3 * * 1 /root/dnevnik/router/sync.sh", (20, 6, 23)), ("# */5 6-22 * * * /root/dnevnik/router/sync.sh", (20, 6, 23))):
            self.cron.write_text("0 3 * * * /usr/bin/other\n" + line + "\n")
            p.CRON = str(self.cron)
            self.assertEqual(p.read_schedule(), expect, line)
        self.cron.unlink()
        self.assertEqual(p.read_schedule(), (20, 6, 23))

    def test_set_config_changes_only_our_cron_line_and_the_name(self):
        r = self.call("set_config", {"every": 10, "from": 7, "to": 22, "name": "Катя"})
        self.assertTrue(r["ok"], r)
        lines = self.cron.read_text().splitlines()
        self.assertEqual(lines, ["0 3 * * * /usr/bin/other", "*/10 7-21 * * * /root/dnevnik/router/sync.sh"])
        self.assertEqual(json.loads((self.home / "config.json").read_text()), {"name": "Катя", "other": 5})   # чужие настройки целы
        s = self.call("status")
        self.assertEqual((s["every"], s["from"], s["to"], s["name"]), (10, 7, 22, "Катя"))
        self.assertEqual(self.call("set_config", {"every": 60, "from": 0, "to": 24, "name": ""})["ok"], True)
        self.assertIn("0 0-23 * * * /root/dnevnik/router/sync.sh", self.cron.read_text())
        self.assertEqual(self.cron.read_text().count("sync.sh"), 1)                   # строка одна, не дублируется
        self.assertEqual(oct(self.cron.stat().st_mode & 0o777), "0o600")

    def test_set_config_refuses_wrong_values_and_changes_nothing(self):
        before = (self.cron.read_text(), (self.home / "config.json").read_text())
        for bad in ({"every": 1, "from": 6, "to": 23}, {"every": 20, "from": 23, "to": 6}, {"every": 20, "from": 6, "to": 6},
                    {"every": 20, "from": -1, "to": 5}, {"every": 20, "from": 6, "to": 25}, {"every": "x", "from": 6, "to": 23},
                    {"every": 20, "from": 6}, {"every": "20; rm -rf /", "from": 6, "to": 23}, {}):
            self.assertFalse(self.call("set_config", dict(bad, name="x"))["ok"], bad)
        self.assertEqual((self.cron.read_text(), (self.home / "config.json").read_text()), before)

    def test_name_is_cleaned(self):
        self.call("set_config", {"every": 20, "from": 6, "to": 23, "name": '  <b>Ка"тя</b>\n\x07' + "я" * 80})
        name = json.loads((self.home / "config.json").read_text())["name"]
        self.assertTrue(name.startswith("bКатя/b"), name)
        self.assertNotRegex(name, r'[<>"&\x00-\x1f]')
        self.assertLessEqual(len(name), 40)

    def test_refresh_starts_sync_and_not_more_often_than_every_two_minutes(self):
        r = self.call("refresh")
        self.assertTrue(r["started"], r)
        for _ in range(30):
            if (self.tmp / "sync-ran").exists():
                break
            time.sleep(0.1)
        self.assertTrue((self.tmp / "sync-ran").exists())
        r2 = self.call("refresh")
        self.assertTrue(r2["ok"] and not r2["started"])
        self.assertEqual((self.tmp / "sync-ran").read_text().count("started"), 1)

    def test_check_update(self):
        (self.tmp / "remote-version").write_text("0.12.1\n")
        r = self.call("check_update")
        self.assertEqual((r["ok"], r["newer"], r["latest"], r["current"]), (True, True, "0.12.1", "0.12.0"))
        (self.tmp / "remote-version").write_text("0.12.0\n")
        self.assertFalse(self.call("check_update")["newer"])
        (self.tmp / "remote-version").write_text("0.9.9\n")                              # у нас новее: не предлагаем «обновиться» назад
        self.assertFalse(self.call("check_update")["newer"])
        (self.tmp / "remote-version").write_text("0.13.0\n")
        self.assertTrue(self.call("check_update")["newer"])
        (self.tmp / "remote-version").write_text("<html>вход</html>")
        self.assertFalse(self.call("check_update")["ok"])
        (self.tmp / "remote-version").unlink()
        self.assertFalse(self.call("check_update")["ok"])

    def test_update_runs_installer_with_luci_flag_and_not_twice(self):
        r = self.call("update")
        self.assertTrue(r["ok"], r)
        for _ in range(30):
            if (self.tmp / "install-args").exists():
                break
            time.sleep(0.1)
        self.assertEqual((self.tmp / "install-args").read_text().strip(), "args: --luci")   # страница LuCI при обновлении сохраняется
        self.assertTrue(self.call("status")["updating"])
        self.assertFalse(self.call("update")["ok"])                                        # второй запуск не принимается
        self.assertIn("обновление программы", self.call("log", {"which": "update"})["text"])

    def test_log_returns_the_tail(self):
        (self.run_dir / "dnevnik-sync.log").write_text("\n".join(f"строка {i}" for i in range(100)))
        text = self.call("log", {"which": "sync"})["text"]
        self.assertIn("строка 99", text)
        self.assertNotIn("строка 5\n", text + "\n")
        self.assertLessEqual(len(text.splitlines()), 40)
        self.assertEqual(self.call("log", {"which": "update"})["text"], "")

    def test_logout_needs_the_code_and_the_pause_and_can_be_restored(self):
        (self.home / "data" / "session.json").write_text('{"cookies": []}')
        (self.home / "data" / "ids.json").write_text("{}")
        self.assertFalse(self.call("logout_confirm", {"token": "выдуманный"})["ok"])       # без первого шага не выйти
        self.assertTrue((self.home / "data" / "session.json").exists())
        token = self.call("logout_begin")["token"]
        r = self.call("logout_confirm", {"token": token})                                  # слишком быстро
        self.assertFalse(r["ok"])
        self.assertTrue((self.home / "data" / "session.json").exists())
        token = self.call("logout_begin")["token"]
        time.sleep(2.3)
        self.assertFalse(self.call("logout_confirm", {"token": "чужой"})["ok"])
        self.assertTrue((self.home / "data" / "session.json").exists())
        token = self.call("logout_begin")["token"]
        time.sleep(2.3)
        r = self.call("logout_confirm", {"token": token})
        self.assertTrue(r["ok"], r)
        self.assertFalse((self.home / "data" / "session.json").exists())                   # вход спрятан
        self.assertTrue((self.home / "data" / "backup" / "session.json").exists())         # но не стёрт
        self.assertTrue(self.call("status")["canRestore"])
        self.assertTrue(self.call("logout_restore")["ok"])
        self.assertTrue((self.home / "data" / "session.json").exists())
        self.assertFalse(self.call("status")["canRestore"])

    def test_page_data_is_rebuilt_after_logout(self):
        (self.home / "data" / "session.json").write_text('{"cookies": []}')
        self.call("logout_begin")
        token = self.call("logout_begin")["token"]
        time.sleep(2.3)
        self.call("logout_confirm", {"token": token})
        page = json.loads((self.www / "data.json").read_text())
        self.assertTrue(page["status"]["needLogin"])
        self.assertEqual(page["days"], [])                                                   # прежнего профиля на странице больше нет


class InstallFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dnevnik-luci-inst-"))
        self.root = self.tmp / "rootfs"
        self.home = self.tmp / "home"
        shutil.copytree(ROOT / "luci", self.home / "luci")
        self.env = dict(os.environ, DNEVNIK_HOME=str(self.home), DNEVNIK_ROOTFS=str(self.root))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def luci(self, cmd):
        return subprocess.run(["sh", str(ROOT / "router" / "luci.sh"), cmd], capture_output=True, text=True, env=self.env, timeout=30)

    def files(self):
        return {
            "plugin": self.root / "usr/libexec/rpcd/luci.dnevnik", "menu": self.root / "usr/share/luci/menu.d/luci-app-dnevnik.json",
            "acl": self.root / "usr/share/rpcd/acl.d/luci-app-dnevnik.json", "view": self.root / "www/luci-static/resources/view/dnevnik/status.js",
        }

    def test_install_puts_exactly_four_files_with_right_permissions_and_remove_cleans_up(self):
        r = self.luci("install")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        f = self.files()
        self.assertTrue(all(p.is_file() for p in f.values()), f)
        self.assertEqual(oct(f["plugin"].stat().st_mode & 0o777), "0o755")                  # плагин запускает rpcd
        for k in ("menu", "acl", "view"):
            self.assertEqual(oct(f[k].stat().st_mode & 0o777), "0o644")
        self.assertEqual(len([p for p in self.root.rglob("*") if p.is_file()]), 4)           # больше ничего не трогаем
        self.assertEqual(self.luci("status").stdout.strip(), "installed")
        self.assertEqual(self.luci("install").returncode, 0)                                 # повторная установка безвредна
        self.assertEqual(self.luci("remove").returncode, 0)
        self.assertEqual([p for p in self.root.rglob("*") if p.is_file()], [])
        self.assertEqual(self.luci("status").stdout.strip(), "absent")
        self.assertEqual(self.luci("remove").returncode, 0)                                  # и удаление повторно

    def test_install_refuses_when_files_are_missing_instead_of_half_installing(self):
        (self.home / "luci" / "htdocs" / "view" / "dnevnik" / "status.js").unlink()
        r = self.luci("install")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([p for p in self.root.rglob("*") if p.is_file()] if self.root.exists() else [], [])

    def test_unknown_command(self):
        self.assertEqual(self.luci("что-то").returncode, 2)

    def test_installer_syntax_and_flags(self):
        for script in ("install.sh", "luci.sh", "uninstall.sh"):
            self.assertEqual(subprocess.run(["sh", "-n", str(ROOT / "router" / script)]).returncode, 0, script)
        r = subprocess.run(["sh", str(ROOT / "router" / "install.sh"), "--bogus"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)                                                    # неизвестный ключ: понятная ошибка, ничего не делаем
        self.assertIn("--luci", r.stderr)
        text = (ROOT / "router" / "install.sh").read_text()
        self.assertIn("--no-luci", text)
        self.assertIn('LUCI=auto', text)

    def test_uninstall_also_removes_the_luci_page(self):
        self.assertIn("luci.sh remove", (ROOT / "router" / "uninstall.sh").read_text())

    def test_readme_has_both_install_commands(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("router/install.sh | sh\n", readme)
        self.assertIn("router/install.sh | sh -s -- --luci", readme)
        self.assertIn("Службы → Дневник", readme)


STUB = """
window.calls = []; window.pollFns = [];
window.E = function(tag, attrs, children) {
  if (attrs === undefined || Array.isArray(attrs) || typeof attrs === 'string' || attrs instanceof Node) { children = attrs; attrs = null; }
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (typeof v === 'function') n.addEventListener(k, v);
    else if (k === 'class') n.className = v; else n.setAttribute(k, v);
  }
  const add = (c) => { if (c == null) return; if (Array.isArray(c)) c.forEach(add); else n.appendChild(c instanceof Node ? c : document.createTextNode(String(c))); };
  add(children); return n;
};
window.view = { extend: (o) => o };
window.rpc = { declare: (spec) => (...args) => { window.calls.push([spec.method, args]); return Promise.resolve(window.answer(spec.method, args)); } };
window.poll = { add: (fn, sec) => window.pollFns.push(fn) };
window.ui = {
  showModal: (title, body) => { const old = document.getElementById('modal'); if (old) old.remove(); const m = document.createElement('div'); m.id = 'modal'; m.innerHTML = '<h4></h4>'; m.firstChild.textContent = title;
    [].concat(body).forEach((b) => m.appendChild(b)); document.body.appendChild(m); },
  hideModal: () => { const m = document.getElementById('modal'); if (m) m.remove(); },
  addNotification: () => {}
};
window.mount = async (code) => {
  const cls = new Function('view', 'rpc', 'ui', 'poll', 'E', code)(window.view, window.rpc, window.ui, window.poll, window.E);
  window.cls = cls;
  const data = await cls.load();
  document.body.innerHTML = ''; document.body.appendChild(cls.render(data));
};
undefined;
"""

STATUS = {"version": "0.12.0", "updatedAt": "2026-11-12T01:29:00", "days": 57, "needLogin": False, "error": None, "student": "Аня",
          "school": "Школа 1", "syncing": False, "updating": False, "canRestore": False, "every": 20, "from": 6, "to": 23, "name": "",
          "nextIn": 840, "now": 0, "url": "http://dnevnik.lan", "urlIp": "http://192.168.10.1:8765"}


@unittest.skipIf(sync_playwright is None, "Playwright не установлен")
class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.code = VIEW.read_text()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def open(self, status=None, answers=None):
        page = self.browser.new_page(viewport={"width": 900, "height": 900})
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.set_content("<body></body>")
        page.evaluate(STUB)
        page.evaluate("""([st, answers]) => { window.mockStatus = st; window.extra = answers || {};
            window.answer = (m, a) => (m in window.extra ? window.extra[m] : m === 'status' ? window.mockStatus : m === 'log' ? {text: 'строка журнала'} : {ok: true, message: 'ок'}); }""",
                      [dict(STATUS, **(status or {})), answers])
        page.evaluate("code => window.mount(code)", self.code)
        return page

    def names(self, page):
        return page.evaluate("window.calls.map(c => c[0])")

    def test_shows_state(self):
        page = self.open()
        text = page.inner_text(".dn")
        for needle in ("Работает", "0.12.0", "Аня (Школа 1)", "выполнен", "12 ноября, 01:29", "57", "через 14 мин"):
            self.assertIn(needle, text)
        self.assertEqual(page.get_attribute(".dn-v a", "href"), "http://dnevnik.lan")
        self.assertEqual(page.eval_on_selector_all(".dn-form select", "e => e.map(s => s.value)"), ["20", "6", "23"])
        self.assertIn("строка журнала", page.inner_text(".dn-log"))
        self.assertEqual(page.is_visible("text=Вернуть прежний вход"), False)
        self.assertEqual(self.errors, [])

    def test_states_need_login_syncing_and_restore(self):
        page = self.open({"needLogin": True, "canRestore": True})
        self.assertIn("Нужен вход", page.inner_text(".dn-pill"))
        self.assertTrue(page.is_disabled("text=Выйти из аккаунта"))
        self.assertTrue(page.is_visible("text=Вернуть прежний вход"))
        page.click("text=Вернуть прежний вход")
        self.assertIn("logout_restore", self.names(page))
        page = self.open({"syncing": True})
        self.assertIn("Обновляются данные", page.inner_text(".dn-pill"))
        self.assertTrue(page.is_disabled("text=Обновить сейчас"))
        page = self.open({"error": "сбой"})
        self.assertIn("Не удалось обновить", page.inner_text(".dn-pill"))
        self.assertEqual(self.errors, [])

    def test_server_text_is_not_executed(self):
        page = self.open({"student": '<img src=x onerror="window.pwned=1">', "school": "<b>x</b>", "name": '"><script>window.pwned=1</script>'})
        self.assertIsNone(page.query_selector(".dn img"))
        self.assertIsNone(page.query_selector(".dn b"))
        self.assertIn("<img", page.inner_text(".dn"))
        self.assertIsNone(page.evaluate("window.pwned"))
        self.assertEqual(self.errors, [])

    def test_refresh_button(self):
        page = self.open(answers={"refresh": {"ok": True, "started": True, "message": "Обновление запущено."}})
        page.click("text=Обновить сейчас")
        page.wait_for_selector(".dn-note.ok")
        self.assertIn("Обновление запущено", page.inner_text(".dn-note.ok"))
        page2 = self.open(answers={"refresh": {"ok": True, "started": False, "message": "Обновляли только что."}})
        page2.click("text=Обновить сейчас")
        page2.wait_for_selector(".dn-note.warn")

    def test_check_and_install_update(self):
        page = self.open(answers={"check_update": {"ok": True, "current": "0.12.0", "latest": "0.12.1", "newer": True}})
        self.assertFalse(page.is_visible("text=Обновить до 0.12.1"))
        page.click("text=Проверить новую версию")
        page.wait_for_selector("text=Обновить до 0.12.1")
        self.assertIn("Есть новая версия 0.12.1", page.inner_text(".dn"))
        page.click("text=Обновить до 0.12.1")
        self.assertNotIn("update", self.names(page))                                     # без подтверждения ничего не запускается
        page.click("#modal >> text=Отмена")
        self.assertNotIn("update", self.names(page))
        page.click("text=Обновить до 0.12.1"); page.click("#modal button.cbi-button-action")
        page.wait_for_function("window.calls.some(c => c[0] === 'update')")
        page = self.open(answers={"check_update": {"ok": True, "current": "0.12.0", "latest": "0.12.0", "newer": False}})
        page.click("text=Проверить новую версию")
        page.wait_for_selector("text=последняя версия")
        self.assertFalse(page.is_visible("text=Обновить до"))
        page = self.open(answers={"check_update": {"ok": False, "message": "Нет связи с GitHub."}})
        page.click("text=Проверить новую версию")
        page.wait_for_selector("text=Нет связи с GitHub.")

    def test_save_settings_sends_numbers_and_name(self):
        page = self.open()
        page.select_option(".dn-form select >> nth=0", "10")
        page.select_option(".dn-form select >> nth=1", "7")
        page.select_option(".dn-form select >> nth=2", "21")
        page.fill(".dn-form input", "Катя")
        page.click("text=Сохранить")
        page.wait_for_function("window.calls.some(c => c[0] === 'set_config')")
        self.assertEqual(page.evaluate("window.calls.find(c => c[0] === 'set_config')[1]"), [10, 7, 21, "Катя"])
        page.wait_for_selector(".dn-note.ok")

    def test_save_error_is_shown(self):
        page = self.open(answers={"set_config": {"ok": False, "message": "Часы заданы неверно."}})
        page.click("text=Сохранить")
        page.wait_for_selector(".dn-note.bad")
        self.assertIn("Часы заданы неверно", page.inner_text(".dn-note.bad"))

    def test_logout_needs_two_confirmations_and_a_pause(self):
        page = self.open(answers={"logout_begin": {"ok": True, "token": "tok-9"}, "logout_confirm": {"ok": True, "message": "Вы вышли."}})
        page.click("text=Выйти из аккаунта")
        self.assertIn("Выйти из аккаунта?", page.inner_text("#modal h4"))
        self.assertEqual(self.names(page).count("logout_begin"), 0)                      # от одного нажатия ничего не происходит
        page.click("#modal >> text=Отмена")
        self.assertIsNone(page.query_selector("#modal"))
        page.click("text=Выйти из аккаунта"); page.click("#modal >> text=Да, выйти")
        page.wait_for_selector("text=Вы точно хотите выйти")
        self.assertEqual(self.names(page).count("logout_confirm"), 0)                    # код получен, но выхода ещё нет
        self.assertTrue(page.is_disabled("#modal >> text=Да, выйти из аккаунта"))          # защита от случайного второго нажатия
        page.wait_for_function("!document.querySelector('#modal button.cbi-button-negative').disabled", timeout=6000)
        page.click("#modal >> text=Да, выйти из аккаунта")
        page.wait_for_function("window.calls.some(c => c[0] === 'logout_confirm')")
        self.assertEqual(page.evaluate("window.calls.find(c => c[0] === 'logout_confirm')[1]"), ["tok-9"])
        self.assertEqual(self.errors, [])

    def test_logout_second_step_can_be_cancelled(self):
        page = self.open(answers={"logout_begin": {"ok": True, "token": "t"}})
        page.click("text=Выйти из аккаунта"); page.click("#modal >> text=Да, выйти")
        page.wait_for_selector("text=Вы точно хотите выйти")
        page.click('#modal button >> text="Нет, оставить как есть"')
        self.assertIsNone(page.query_selector("#modal"))
        page.wait_for_timeout(3300)
        self.assertEqual(self.names(page).count("logout_confirm"), 0)

    def test_page_polls_and_survives_a_dead_backend(self):
        page = self.open()
        page.evaluate("window.mockStatus = Object.assign({}, window.mockStatus, {version: '0.12.1', needLogin: true})")
        page.evaluate("window.pollFns[0]()")
        page.wait_for_function("document.querySelector('.dn-pill').textContent === 'Нужен вход'")
        self.assertIn("0.12.1", page.inner_text(".dn"))

    def test_phone_width_has_no_horizontal_scroll(self):
        page = self.open()
        page.set_viewport_size({"width": 375, "height": 800})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 376)


if __name__ == "__main__":
    unittest.main()
