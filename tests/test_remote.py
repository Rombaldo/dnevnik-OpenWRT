"""Выход в интернет (router/remote.sh) и помощник «Проверить всё» (router/doctor.py): без настоящего роутера, с подставными командами."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REMOTE = ROOT / "router" / "remote.sh"
DOCTOR = ROOT / "router" / "doctor.py"

# Файл службы Tailscale с OpenWrt 25.12 (как есть, без нашей правки)
INIT_ORIGINAL = """#!/bin/sh /etc/rc.common
USE_PROCD=1
START=80

export TS_NO_LOGS_NO_SUPPORT=true

start_service() {
  config_load tailscale
  procd_open_instance
  procd_set_param command /usr/sbin/tailscaled

  # Starting with v1.48.1 ENV variable is required to enable use of iptables / nftables.
  procd_set_param env TS_DEBUG_FIREWALL_MODE="$fw_mode"

  # Disable logging to log.tailscale.com
  procd_append_param env TS_NO_LOGS_NO_SUPPORT=true

  procd_append_param command --port "$port"
  procd_close_instance
}
"""
# Так выглядел файл на роутере после ручной правки (до появления remote.sh)
INIT_MANUAL = INIT_ORIGINAL.replace("""  procd_append_param env TS_NO_LOGS_NO_SUPPORT=true
""", """  procd_append_param env TS_NO_LOGS_NO_SUPPORT=true
  # Через Clash: прямой доступ к серверам Tailscale замедлен (добавлено вручную)
  procd_append_param env HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890 NO_PROXY=127.0.0.1,localhost,192.168.0.0/16
""")

FAKE_TAILSCALE = r"""#!/bin/sh
# Подставной tailscale. Состояние лежит в $FAKE: state, funnel (текст), funnel_mode (ok|needs), up_mode (ok|stuck|needs_proxy), calls
echo "tailscale $*" >> "$FAKE/calls"
case "$1" in
  status)
    [ -f "$FAKE/state" ] || exit 1
    printf '{"BackendState": "%s", "Self": {"DNSName": "dnevnik.example.ts.net."}}\n' "$(cat "$FAKE/state")" ;;
  up)
    printf 'To authenticate, visit:\n\n\thttps://login.tailscale.com/a/abc123\n'
    mode=$(cat "$FAKE/up_mode" 2>/dev/null || echo ok)
    if [ "$mode" = "needs_proxy" ] && grep -q dnevnik-proxy "$DNEVNIK_TS_INIT" 2>/dev/null; then mode=ok; fi
    sleep 1
    [ "$mode" = "ok" ] && echo Running > "$FAKE/state" ;;
  funnel)
    case "$2" in
      status) cat "$FAKE/funnel" 2>/dev/null || echo "No serve config" ;;
      --bg)
        if [ "$(cat "$FAKE/funnel_mode" 2>/dev/null || echo ok)" = "ok" ]; then
          printf 'Available on the internet:\n\nhttps://dnevnik.example.ts.net/\n|-- proxy http://127.0.0.1:%s\n\nFunnel started and running in the background.\n' "$3"
          printf '# Funnel on:\n#     - https://dnevnik.example.ts.net\n\nhttps://dnevnik.example.ts.net (Funnel on)\n|-- / proxy http://127.0.0.1:%s\n' "$3" > "$FAKE/funnel"
        else
          printf '\nFunnel is not enabled on your tailnet.\nTo enable, visit:\n\n         https://login.tailscale.com/f/funnel?node=abc\n'
          sleep 8
        fi ;;
      --https=443) rm -f "$FAKE/funnel" ;;
    esac ;;
  version) echo "1.104.1" ;;
esac
"""
FAKE_SERVICE = r"""#!/bin/sh
echo "service $*" >> "$FAKE/calls"
case "$1" in
  start|restart) [ -f "$FAKE/state" ] && [ "$(cat "$FAKE/state")" = Running ] || echo NeedsLogin > "$FAKE/state" ;;
esac
"""
FAKE_APK = r"""#!/bin/sh
echo "apk $*" >> "$FAKE/calls"
[ "$1" = add ] && cp "$FAKE/tailscale-stub" "$BIN/tailscale" && chmod 755 "$BIN/tailscale"
exit 0
"""
FAKE_DF = r"""#!/bin/sh
printf 'Filesystem 1K-blocks Used Available Use%% Mounted on\noverlay 94720 40000 %s 70%% /overlay\n' "$(cat "$FAKE/free_kb" 2>/dev/null || echo 30000)"
"""
FAKE_NETSTAT = r"""#!/bin/sh
[ -f "$FAKE/proxy_listen" ] && echo "tcp 0 0 127.0.0.1:7890 0.0.0.0:* LISTEN"
exit 0
"""
FAKE_PS = "#!/bin/sh\nexit 0\n"


def put(path, text, mode=0o755):
    path.write_text(text)
    path.chmod(mode)


class RemoteBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dn-remote-"))
        self.bin, self.fake, self.home = self.tmp / "bin", self.tmp / "fake", self.tmp / "home"
        for d in (self.bin, self.fake, self.home / "data", self.home / "router"):
            d.mkdir(parents=True)
        self.init = self.tmp / "init-tailscale"
        self.init.write_text(INIT_ORIGINAL)
        put(self.tmp / "service", FAKE_SERVICE)
        put(self.fake / "tailscale-stub", FAKE_TAILSCALE)
        put(self.bin / "apk", FAKE_APK)
        put(self.bin / "df", FAKE_DF)
        put(self.bin / "netstat", FAKE_NETSTAT)
        put(self.bin / "ps", FAKE_PS)
        (self.fake / "free_kb").write_text("30000")
        (self.home / "data" / "auth.json").write_text("{}")                              # пароль семьи задан
        shutil.copy(DOCTOR, self.home / "router" / "doctor.py")
        self.have_tailscale(True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def have_tailscale(self, yes):
        target = self.bin / "tailscale"
        if yes:
            shutil.copy(self.fake / "tailscale-stub", target)
            target.chmod(0o755)
        elif target.exists():
            target.unlink()

    def set(self, name, value):
        (self.fake / name).write_text(value)

    def calls(self):
        f = self.fake / "calls"
        return f.read_text().splitlines() if f.exists() else []

    def run_remote(self, *args, machine=False, **env):
        e = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE": str(self.fake), "BIN": str(self.bin), "DNEVNIK_HOME": str(self.home),
             "DNEVNIK_TS_INIT": str(self.init), "DNEVNIK_TS_SVC": str(self.tmp / "service"), "DNEVNIK_ALLOW_NONROOT": "1",
             "DNEVNIK_TMP": str(self.tmp), "DNEVNIK_LOGIN_WAIT": "6", "DNEVNIK_STEP": "1", "DNEVNIK_RESTART_WAIT": "0",
             "DNEVNIK_PORT": "8765", "DNEVNIK_FREE_PATH": str(self.tmp), "DNEVNIK_FREE_KB": "30000", "HOME": str(self.tmp)}
        if machine:
            e["DNEVNIK_MACHINE"] = "1"
        e.update({k: str(v) for k, v in env.items()})
        r = subprocess.run(["sh", str(REMOTE), *args], capture_output=True, text=True, env=e, timeout=90)
        return r.returncode, r.stdout + r.stderr


class RemoteInstallTests(RemoteBase):
    def test_syntax(self):
        self.assertEqual(subprocess.run(["sh", "-n", str(REMOTE)]).returncode, 0)

    def test_clean_install_end_to_end(self):
        self.have_tailscale(False)
        code, out = self.run_remote(machine=True)
        self.assertEqual(code, 0, out)
        self.assertEqual([c for c in self.calls() if c.startswith("apk add")], ["apk add tailscale"])
        self.assertIn("service enable", self.calls())
        self.assertIn("https://login.tailscale.com/a/abc123", out)                      # ссылка показана человеку
        self.assertIn("Connect device", out)                                            # и сказано, что нажать
        self.assertIn("@@LOGIN_URL https://login.tailscale.com/a/abc123", out)          # и отдана программе установки на компьютере
        self.assertIn("@@ADDRESS https://dnevnik.example.ts.net/", out)
        self.assertIn("Готово. Адрес дневника из интернета", out)
        self.assertIn("ИТОГ", out)                                                       # в конце запущена проверка
        self.assertEqual(self.init.read_text(), INIT_ORIGINAL)                           # прокси без необходимости не включали

    def test_machine_lines_only_when_asked(self):
        self.have_tailscale(False)
        code, out = self.run_remote()
        self.assertEqual(code, 0, out)
        self.assertNotIn("@@", out)

    def test_refuses_to_expose_without_a_password(self):
        (self.home / "data" / "auth.json").unlink()
        code, out = self.run_remote(machine=True)
        self.assertEqual(code, 2)
        self.assertIn("без пароля нельзя", out)
        self.assertIn("password.py set", out)
        self.assertIn("@@NEEDS password", out)
        self.assertEqual([c for c in self.calls() if "funnel" in c or c.startswith("apk")], [])     # ничего не ставили и не открывали

    def test_not_enough_space_is_a_clear_message_not_a_broken_install(self):
        self.have_tailscale(False)
        self.set("free_kb", "8000")
        code, out = self.run_remote(machine=True)
        self.assertEqual(code, 2)
        self.assertIn("свободно 7 МБ", out)
        self.assertIn("необязательный", out)
        self.assertEqual([c for c in self.calls() if c.startswith("apk")], [])

    def test_funnel_needs_a_click_in_the_account_then_second_run_finishes(self):
        self.set("funnel_mode", "needs")
        code, out = self.run_remote(machine=True)
        self.assertEqual(code, 2, out)
        self.assertIn("login.tailscale.com/f/funnel?node=abc", out)
        self.assertIn("Enable Funnel", out)
        self.assertIn("публичный журнал сертификатов", out)                             # честно сказано, что имя станет публичным
        self.assertIn("@@NEEDS funnel_enable https://login.tailscale.com/f/funnel?node=abc", out)
        self.set("funnel_mode", "ok")                                                    # человек нажал кнопку
        code, out = self.run_remote()
        self.assertEqual(code, 0, out)
        self.assertIn("https://dnevnik.example.ts.net/", out)

    def test_already_configured_changes_nothing(self):
        self.set("state", "Running")
        self.set("funnel", "# Funnel on:\n\nhttps://dnevnik.example.ts.net (Funnel on)\n|-- / proxy http://127.0.0.1:8765\n")
        code, out = self.run_remote()
        self.assertEqual(code, 0, out)
        self.assertIn("Tailscale уже установлен", out)
        self.assertIn("Funnel уже включён", out)
        c = self.calls()
        self.assertEqual([x for x in c if x.startswith(("apk", "tailscale up", "tailscale funnel --bg", "service restart"))], [])
        self.assertEqual(self.init.read_text(), INIT_ORIGINAL)

    def test_funnel_for_another_port_is_not_taken_for_ours(self):
        self.set("state", "Running")
        self.set("funnel", "https://dnevnik.example.ts.net (Funnel on)\n|-- / proxy http://127.0.0.1:8799\n")     # например, пробная страница
        code, out = self.run_remote()
        self.assertEqual(code, 0, out)
        self.assertTrue(any(c.startswith("tailscale funnel --bg 8765") for c in self.calls()))


class RemoteProxyTests(RemoteBase):
    def test_login_that_hangs_switches_to_the_proxy_when_one_is_listening(self):
        """Тот самый случай с нашим роутером: вход проходит, а Tailscale не оживает, пока трафик не пустить через Clash."""
        self.set("up_mode", "needs_proxy")
        self.set("proxy_listen", "1")
        code, out = self.run_remote(machine=True)
        self.assertEqual(code, 0, out)
        self.assertIn("Tailscale не подключился напрямую", out)
        self.assertIn("Прокси для Tailscale включён: 127.0.0.1:7890", out)
        self.assertEqual(len([c for c in self.calls() if c.startswith("tailscale up")]), 2)      # вторая попытка уже через прокси
        text = self.init.read_text()
        self.assertIn("HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890 NO_PROXY=127.0.0.1,localhost,192.168.0.0/16", text)
        self.assertEqual(text.count("# >>> dnevnik-proxy"), 1)
        self.assertTrue(Path(str(self.init) + ".bak-dnevnik").exists())
        self.assertEqual(Path(str(self.init) + ".bak-dnevnik").read_text(), INIT_ORIGINAL)       # исходный файл сохранён

    def test_hanging_login_without_a_proxy_fails_with_a_useful_message(self):
        self.set("up_mode", "stuck")
        code, out = self.run_remote()
        self.assertEqual(code, 1)
        self.assertIn("Tailscale не подключился", out)
        self.assertIn("doctor.py", out)
        self.assertEqual(self.init.read_text(), INIT_ORIGINAL)

    def test_proxy_is_not_enabled_when_nothing_listens_on_its_port(self):
        self.set("up_mode", "needs_proxy")                                                      # прокси нужен, но порт никто не слушает
        code, out = self.run_remote()
        self.assertEqual(code, 1)
        self.assertNotIn("dnevnik-proxy", self.init.read_text())

    def test_proxy_on_off_is_idempotent_and_exact(self):
        code, out = self.run_remote("proxy", "on")
        self.assertEqual(code, 0, out)
        once = self.init.read_text()
        lines = once.splitlines()
        i = next(n for n, l in enumerate(lines) if "TS_NO_LOGS_NO_SUPPORT" in l and "procd_append_param" in l)
        self.assertIn("# >>> dnevnik-proxy", lines[i + 1])                                       # сразу после нужной строки
        self.assertIn("procd_append_param env HTTPS_PROXY=http://127.0.0.1:7890", lines[i + 2])
        self.assertIn("# <<< dnevnik-proxy", lines[i + 3])
        self.assertIn("export TS_NO_LOGS_NO_SUPPORT=true", once)                                  # верхнюю строку export не трогали
        self.run_remote("proxy", "on")
        self.assertEqual(self.init.read_text(), once)                                            # повторно: без дублей
        self.run_remote("proxy", "on", "127.0.0.1:7891")
        t = self.init.read_text()
        self.assertEqual(t.count("HTTPS_PROXY"), 1)
        self.assertIn("127.0.0.1:7891", t)
        self.assertNotIn(":7890", t)
        code, out = self.run_remote("proxy", "off")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.init.read_text(), INIT_ORIGINAL)                                   # вернулось ровно как было
        self.assertGreaterEqual(len([c for c in self.calls() if c == "service restart"]), 3)

    def test_manual_edit_from_before_is_migrated_not_duplicated(self):
        self.init.write_text(INIT_MANUAL)
        code, out = self.run_remote("proxy", "on")
        self.assertEqual(code, 0, out)
        t = self.init.read_text()
        self.assertEqual(t.count("HTTPS_PROXY"), 1)
        self.assertNotIn("добавлено вручную", t)
        self.assertEqual(t.count("# >>> dnevnik-proxy"), 1)
        self.run_remote("proxy", "off")
        self.assertEqual(self.init.read_text(), INIT_ORIGINAL)

    def test_bad_proxy_address_is_refused(self):
        for bad in ("127.0.0.1:7890;rm -rf /", "a b", "$(reboot)", "127.0.0.1:7890\nx"):
            code, out = self.run_remote("proxy", "on", bad)
            self.assertEqual(code, 1, bad)
            self.assertEqual(self.init.read_text(), INIT_ORIGINAL, bad)

    def test_proxy_needs_the_tailscale_init_file(self):
        self.init.unlink()
        code, out = self.run_remote("proxy", "on")
        self.assertEqual(code, 1)
        self.assertIn("сначала установите Tailscale", out)

    def test_status_and_unknown_commands(self):
        self.set("state", "Running")
        self.run_remote("proxy", "on")
        self.set("funnel", "https://dnevnik.example.ts.net (Funnel on)\n|-- / proxy http://127.0.0.1:8765\n")
        code, out = self.run_remote("status")
        self.assertEqual(code, 0, out)
        for needle in ("Tailscale: Running", "Прокси для Tailscale: 127.0.0.1:7890", "Адрес из интернета: https://dnevnik.example.ts.net"):
            self.assertIn(needle, out)
        self.assertEqual(self.run_remote("bogus")[0], 1)
        self.assertEqual(self.run_remote("funnel", "on")[0], 1)
        self.assertEqual(self.run_remote("proxy")[0], 1)
        code, _ = self.run_remote("funnel", "off")
        self.assertEqual(code, 0)
        self.assertFalse((self.fake / "funnel").exists())

    def test_doctor_reads_what_remote_sh_wrote(self):
        self.run_remote("proxy", "on")
        env = dict(os.environ, DNEVNIK_TS_INIT=str(self.init))
        out = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, r'{ROOT / 'router'}'); import doctor; print(doctor.proxy_info())"],
                             capture_output=True, text=True, env=env).stdout
        self.assertIn("127.0.0.1:7890", out)


class DoctorTests(RemoteBase):
    """Помощник: подставные Tailscale и сервер дневника, проверяем вердикты и подсказки."""

    def setUp(self):
        super().setUp()
        self.page = self.tmp / "private"
        self.page.mkdir()
        (self.page / "data.json").write_text(json.dumps({"updatedAt": "2099-01-01T00:00:00", "days": [{}] * 5, "status": {"needLogin": False}}))
        (self.home / "data" / "session.json").write_text("[]")
        (self.home / "VERSION").write_text("9.9.9\n")
        (self.tmp / "crontab").write_text("*/20 6-22 * * * /root/dnevnik/router/sync.sh\n")
        shutil.copy(ROOT / "auth.py", self.home / "auth.py")
        self.srv = None

    def tearDown(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
        super().tearDown()

    def serve(self, routes):
        """Маленький сервер вместо дневника на роутере: routes {путь: код}."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                code = routes.get(self.path.split("?")[0], 404)
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b"{}")
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self.srv.server_address[1]

    def doctor(self, *args, port=None, **env):
        e = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE": str(self.fake), "DNEVNIK_HOME": str(self.home), "DNEVNIK_PRIV": str(self.page),
             "DNEVNIK_TMP": str(self.tmp), "DNEVNIK_TS_INIT": str(self.init), "DNEVNIK_CRON": str(self.tmp / "crontab"),
             "DNEVNIK_PORT": str(port or 1), "DNEVNIK_SITE": f"http://127.0.0.1:{port or 1}/", "DNEVNIK_FREE_KB": "30000", "HOME": str(self.tmp)}
        e.update({k: str(v) for k, v in env.items()})
        r = subprocess.run([sys.executable, str(self.home / "router" / "doctor.py"), *args], capture_output=True, text=True, env=e, timeout=60)
        return r.returncode, r.stdout

    def good_port(self):
        return self.serve({"/index.html": 200, "/cgi-bin/data": 401})

    def test_all_good_at_home_without_remote_access(self):
        self.have_tailscale(False)
        code, out = self.doctor(port=self.good_port())
        self.assertEqual(code, 0, out)
        self.assertIn("✓ Страница дневника на роутере", out)
        self.assertIn("· Выход в интернет (Tailscale): не установлен", out)
        self.assertIn("--remote", out)                                                  # сказано, как включить
        self.assertNotIn("✗", out)

    def test_dead_web_server_is_a_failure_with_a_fix(self):
        self.have_tailscale(False)
        code, out = self.doctor(port=None)
        self.assertEqual(code, 1)
        self.assertIn("✗ Страница дневника на роутере", out)
        self.assertIn("/etc/init.d/dnevnik restart", out)
        self.assertIn("есть проблемы", out)

    def test_data_in_the_open_folder_is_a_failure(self):
        self.have_tailscale(False)
        port = self.serve({"/index.html": 200, "/cgi-bin/data": 200, "/data.json": 200})
        code, out = self.doctor(port=port)
        self.assertEqual(code, 1)
        self.assertIn("Данные в открытой папке", out)

    def test_exposed_to_the_internet_without_password_is_a_failure(self):
        self.set("state", "Running")
        self.set("funnel", "https://dnevnik.example.ts.net (Funnel on)\n|-- / proxy http://127.0.0.1:%d\n" % 1)
        (self.home / "data" / "auth.json").unlink()
        code, out = self.doctor(port=self.good_port(), DNEVNIK_PORT=1)
        self.assertEqual(code, 1, out)
        self.assertIn("✗ Пароль семьи: НЕ задан, а страница открыта из интернета", out)

    def test_no_password_at_home_is_only_a_note(self):
        self.have_tailscale(False)
        (self.home / "data" / "auth.json").unlink()
        code, out = self.doctor(port=self.good_port())
        self.assertEqual(code, 0)
        self.assertIn("! Пароль семьи: не задан", out)

    def test_tailscale_states(self):
        port = self.good_port()
        self.set("state", "NeedsLogin")
        code, out = self.doctor(port=port)
        self.assertEqual(code, 1)
        self.assertIn("✗ Tailscale: нужно войти в аккаунт", out)
        self.assertIn("Connect device", out)
        self.set("state", "Running")
        code, out = self.doctor(port=port)
        self.assertEqual(code, 0, out)
        self.assertIn("✓ Tailscale: подключён", out)
        self.assertIn("! Адрес из интернета (Funnel): не включён", out)

    def test_proxy_that_nobody_listens_on_is_reported(self):
        self.set("state", "Running")
        self.run_remote("proxy", "on", "127.0.0.1:1")                                    # порт, где ничего нет
        code, out = self.doctor(port=self.good_port())
        self.assertEqual(code, 1)
        self.assertIn("ничего не слушает", out)
        self.assertIn("remote.sh proxy off", out)

    def test_needs_login_and_stale_data(self):
        self.have_tailscale(False)
        (self.page / "data.json").write_text(json.dumps({"updatedAt": "2020-01-01T00:00:00", "days": [{}], "status": {"needLogin": True}}))
        code, out = self.doctor(port=self.good_port())
        self.assertEqual(code, 1)
        self.assertIn("✗ Вход в Дневник.ру: нужно войти заново", out)
        self.assertIn("! Свежесть данных", out)

    def test_push_states(self):
        self.have_tailscale(False)
        port = self.good_port()
        code, out = self.doctor(port=port)
        self.assertIn("Уведомления", out)                                                 # статус виден всегда (модуль в тестовой среде есть или нет)
        (self.home / "data" / "push.json").write_text(json.dumps({"subs": {"a": {}, "b": {}}, "last": {"sent": 3, "failed": 0}}))
        code, out = self.doctor("--json", port=port)
        push = next(r for r in json.loads(out)["results"] if r["id"] == "push")
        if "подключено устройств: 2" in push["detail"]:                                   # модуль cryptography есть в этой среде
            self.assertEqual(push["status"], "ok")
            self.assertIn("доставлено 3", push["detail"])
            (self.home / "data" / "push.json").write_text(json.dumps({"subs": {"a": {}}, "last": {"sent": 0, "failed": 1}, "last_error": "служба пушей ответила 403"}))
            code, out = self.doctor("--json", port=port)
            push = next(r for r in json.loads(out)["results"] if r["id"] == "push")
            self.assertEqual(push["status"], "warn")
            self.assertIn("403", push["detail"])
            self.assertIn("Проверить уведомление", push["fix"])
        else:
            self.assertEqual(push["status"], "info")
            self.assertIn("push.sh", push["detail"])

    def test_failed_logins_in_the_journal(self):
        self.have_tailscale(False)
        port = self.good_port()
        now = int(time.time())
        (self.home / "data" / "logins.json").write_text(json.dumps([{"ts": now - 100, "ok": False, "n": 3, "dev": "x", "where": "home", "did": "a"}]))
        code, out = self.doctor(port=port)
        self.assertIn("· Неверные попытки входа: за сутки: 3", out)
        self.assertEqual(code, 0)
        (self.home / "data" / "logins.json").write_text(json.dumps([{"ts": now - 100, "ok": False, "n": 12, "dev": "x", "where": "internet", "did": "a"},
                                                                    {"ts": now - 90000, "ok": False, "n": 99, "dev": "x", "where": "internet", "did": "b"}]))
        code, out = self.doctor(port=port)
        self.assertIn("! Неверные попытки входа: за сутки: 12", out)                           # вчерашние (99) не считаются
        self.assertIn("подбирает пароль", out)
        (self.home / "data" / "logins.json").write_text("{не json")
        self.assertIn("ИТОГ", self.doctor(port=port)[1])

    def test_push_without_module_is_only_a_note(self):
        self.have_tailscale(False)
        code, out = self.doctor(port=self.good_port())
        self.assertEqual(code, 0)
        self.assertNotIn("✗ Уведомления", out)

    def release(self, text):
        f = self.tmp / "openwrt_release"
        f.write_text(f"DISTRIB_ID='OpenWrt'\nDISTRIB_RELEASE='{text}'\n")
        return str(f)

    def test_openwrt_versions(self):
        self.have_tailscale(False)
        port = self.good_port()
        code, out = self.doctor(port=port, DNEVNIK_OPENWRT_RELEASE=self.release("25.12.5"))
        self.assertNotIn("Версия OpenWrt", out)                                           # проверенная версия: без заметок
        for ok in ("24.10.1", "23.05.3", "22.03.5", "21.02.7"):
            code, out = self.doctor(port=port, DNEVNIK_OPENWRT_RELEASE=self.release(ok))
            self.assertIn("· Версия OpenWrt", out, ok)
            self.assertIn("проверялась только на 25.12", out, ok)
            self.assertEqual(code, 0, ok)                                                 # просто заметка, не проблема
        code, out = self.doctor(port=port, DNEVNIK_OPENWRT_RELEASE=self.release("19.07.10"))
        self.assertIn("! Версия OpenWrt", out)
        self.assertIn("не поддерживается", out)
        code, out = self.doctor(port=port, DNEVNIK_OPENWRT_RELEASE=self.release("SNAPSHOT"))
        self.assertIn("· Версия OpenWrt", out)

    def test_not_enough_space(self):
        self.have_tailscale(False)
        code, out = self.doctor(port=self.good_port(), DNEVNIK_FREE_KB=2000)
        self.assertEqual(code, 1)
        self.assertIn("✗ Свободное место", out)
        code, out = self.doctor(port=self.good_port(), DNEVNIK_FREE_KB=9000)
        self.assertIn("! Место для выхода в интернет (Tailscale)", out)                  # для дневника хватает, для Tailscale нет
        self.assertEqual(code, 0)

    def test_no_internet_is_a_failure(self):
        self.have_tailscale(False)
        code, out = self.doctor(port=self.good_port(), DNEVNIK_SITE="http://127.0.0.1:1/")
        self.assertEqual(code, 1)
        self.assertIn("✗ Связь роутера с dnevnik.ru: нет", out)

    def test_summary_shows_only_notes(self):
        self.have_tailscale(False)
        (self.home / "data" / "auth.json").unlink()
        code, out = self.doctor("--summary", port=self.good_port())
        self.assertNotIn("✓", out)
        self.assertIn("! Пароль семьи", out)
        self.assertIn("замечания", out)

    def test_json_output(self):
        self.set("state", "Running")
        self.set("funnel", "https://dnevnik.example.ts.net (Funnel on)\n|-- / proxy http://127.0.0.1:1\n")
        code, out = self.doctor("--json", port=self.good_port())
        d = json.loads(out)
        self.assertEqual(d["address"], "https://dnevnik.example.ts.net/")
        self.assertIn(d["summary"]["verdict"], ("ok", "notes", "problems"))
        ids = {r["id"] for r in d["results"]}
        self.assertTrue({"system", "web", "cron", "login", "internet", "family", "tailscale", "funnel"} <= ids)

    def test_report_contains_no_private_data(self):
        """Отчёт уходит другому человеку: в нём не должно быть адресов, логинов, паролей."""
        self.set("state", "Running")
        self.set("funnel", "https://dnevnik.secret-net.ts.net (Funnel on)\n|-- / proxy http://127.0.0.1:1\n")
        (self.home / "data" / "session.json").write_text(json.dumps([{"name": "DnevnikAuth_a", "value": "СЕКРЕТНАЯ-КУКА"}]))
        (self.tmp / "dnevnik-sync.log").write_text("--- 2026-11-12 10:00:00\nok: 57 дней, 2026-11-12T10:00:03\nTraceback: имя ученика Иван Иванов\n")
        code, out = self.doctor("--report", port=self.good_port())
        for private in ("secret-net", "СЕКРЕТНАЯ-КУКА", "Иван", "ts.net", "http://127.0.0.1", str(self.tmp)):
            self.assertNotIn(private, out, private)
        self.assertIn("Отчёт «Мой дневник»", out)
        self.assertIn("ok: 57 дн", out)                                                  # безопасные строки журнала оставлены
        self.assertIn("9.9.9", out)

    def test_never_writes_anything(self):
        self.have_tailscale(False)
        before = sorted(p.relative_to(self.tmp).as_posix() for p in self.tmp.rglob("*"))
        self.doctor(port=self.good_port())
        after = sorted(p.relative_to(self.tmp).as_posix() for p in self.tmp.rglob("*"))
        self.assertEqual([a for a in after if a not in before and not a.endswith(".pyc") and "__pycache__" not in a], [])

    def test_a_crashing_check_does_not_kill_the_report(self):
        self.have_tailscale(False)
        (self.page / "data.json").write_text("{не json")
        code, out = self.doctor(port=self.good_port())
        self.assertIn("ИТОГ", out)


if __name__ == "__main__":
    unittest.main()


class PushShTests(RemoteBase):
    """router/push.sh: ставит модуль уведомлений, если места хватает; подставные apk, df и python3."""
    PUSH = ROOT / "router" / "push.sh"

    def setUp(self):
        super().setUp()
        self.py = self.tmp / "fakepy"
        # подставной python3: «import cryptography» удаётся, только если есть файл-флаг; остальное пишет в журнал
        put(self.py, '#!/bin/sh\necho "py $*" >> "$FAKE/calls"\ncase "$*" in *"import cryptography"*) [ -f "$FAKE/have_crypto" ] && exit 0 || exit 1;; esac\n'
                     'case "$*" in *json.load*) echo "${FAKE_DEVICES:-0}"; exit 0;; esac\ncat >/dev/null\nexit 0\n')

    def run_push(self, *args, **env):
        e = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE": str(self.fake), "BIN": str(self.bin), "DNEVNIK_HOME": str(self.home), "DNEVNIK_PY": str(self.py),
             "DNEVNIK_FREE_PATH": str(self.tmp)}
        e.update({k: str(v) for k, v in env.items()})
        r = subprocess.run(["sh", str(self.PUSH), *args], capture_output=True, text=True, env=e, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def test_syntax(self):
        self.assertEqual(subprocess.run(["sh", "-n", str(self.PUSH)]).returncode, 0)

    def test_installs_the_module_and_prepares_keys(self):
        put(self.bin / "apk", '#!/bin/sh\necho "apk $*" >> "$FAKE/calls"\n[ "$2" = python3-cryptography ] && touch "$FAKE/have_crypto"\nexit 0\n')
        code, out = self.run_push()
        self.assertEqual(code, 0, out)
        self.assertIn("apk add python3-cryptography", self.calls())
        self.assertTrue(any("import push" in c or c.startswith("py -") for c in self.calls()))     # ключи подготовлены
        self.assertIn("Готово. Включите уведомления на телефоне", out)

    def test_install_that_does_not_help_fails_with_a_message(self):
        code, out = self.run_push()                                                       # подставной apk ничего не меняет: модуль так и не появился
        self.assertEqual(code, 1, out)
        self.assertIn("Python его не видит", out)

    def test_module_already_there_changes_nothing_and_prepares_keys(self):
        (self.fake / "have_crypto").write_text("1")
        code, out = self.run_push()
        self.assertEqual(code, 0, out)
        self.assertIn("уже установлен", out)
        self.assertEqual([c for c in self.calls() if c.startswith("apk")], [])
        self.assertIn("Проверить уведомление", out)                                         # объяснено, что делать на телефоне
        self.assertIn("https", out)

    def test_not_enough_space_is_a_clear_message(self):
        self.set("free_kb", "5000")
        code, out = self.run_push()
        self.assertEqual(code, 2)
        self.assertIn("свободно 4 МБ", out)
        self.assertEqual([c for c in self.calls() if c.startswith("apk")], [])

    def test_status_and_unknown_command(self):
        code, out = self.run_push("status")
        self.assertIn("не установлен", out)
        (self.fake / "have_crypto").write_text("1")
        code, out = self.run_push("status", FAKE_DEVICES=3)
        self.assertIn("Модуль уведомлений: установлен", out)
        self.assertEqual(self.run_push("bogus")[0], 1)

    def test_requires_installed_program(self):
        shutil.rmtree(self.home / "router")
        code, out = self.run_push()
        self.assertEqual(code, 1)
        self.assertIn("дневник не установлен", out)
