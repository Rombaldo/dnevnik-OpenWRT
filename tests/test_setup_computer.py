"""Установка с компьютера (tools/setup_computer.py): без настоящего роутера, SSH и браузера."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import setup_computer as sc  # noqa: E402

TRICKY = ["простой пароль", "it's", "a'b'c", '"двойные"', "$(touch /tmp/pwned-by-test)", "`touch /tmp/pwned-by-test`", "a;b&&c|d", "тире - и\\обратный", "a b\tc", "😀эмодзи", "x" * 128]


class QuotingTests(unittest.TestCase):
    def test_every_string_survives_the_shell_unchanged_and_nothing_is_executed(self):
        marker = Path("/tmp/pwned-by-test")
        marker.unlink(missing_ok=True)
        for text in TRICKY:
            r = subprocess.run(["sh", "-c", f"printf %s {sc.q(text)}"], capture_output=True)
            self.assertEqual(r.stdout.decode("utf-8"), text, text)
        self.assertFalse(marker.exists(), "строка из пароля была выполнена как команда")

    def test_newline_is_kept_inside_quotes(self):
        quoted = sc.q("a\nb")
        r = subprocess.run(["sh", "-c", f"printf %s {quoted}"], capture_output=True)
        self.assertEqual(r.stdout.decode(), "a\nb")


class ScriptTests(unittest.TestCase):
    """Сценарий для роутера запускаем у себя, подменив wget и python3: смотрим порядок шагов и что именно передаётся."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dn-setup-"))
        self.bin, self.app, self.log = self.tmp / "bin", self.tmp / "app", self.tmp / "log"
        (self.app / "router").mkdir(parents=True)
        self.bin.mkdir()
        # wget отдаёт «установщик»: сам читает stdin (как мог бы настоящий) и пишет в журнал
        self.put(self.bin / "wget", '#!/bin/sh\necho "wget $*" >> "$LOG"\nprintf \'echo "установщик: ключи $*"\\necho "установщик" >> "$LOG"\\n\'\n')
        self.put(self.bin / "python3", '#!/bin/sh\n{ echo "python3 $*"; echo "ENV_PW=$DNEVNIK_NEW_PASSWORD"; echo "ENV_HINT=$DNEVNIK_NEW_HINT"; } >> "$LOG"\ncat >/dev/null\nexit ${PY_RC:-0}\n')
        self.put(self.app / "router" / "remote.sh", '#!/bin/sh\necho "remote.sh $*" >> "$LOG"\ncat >/dev/null\necho "@@ADDRESS https://x.example/"\nexit ${REMOTE_RC:-0}\n')
        self.script_env = {"PATH": f"{self.bin}:/usr/bin:/bin", "LOG": str(self.log), "HOME": str(self.tmp)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, path, text):
        path.write_text(text)
        path.chmod(0o755)

    def run_script(self, **kw):
        env = kw.pop("env", {})
        text = sc.build_script(app=str(self.app), **kw)
        # так роутер получает сценарий: через stdin оболочки sh -s
        r = subprocess.run(["sh", "-s"], input=text.encode(), capture_output=True, env=dict(os.environ, **self.script_env, **env), timeout=30)
        return r.returncode, r.stdout.decode("utf-8"), (self.log.read_text() if self.log.exists() else "")

    def test_full_scenario_runs_every_step_in_order_even_though_commands_read_stdin(self):
        code, out, log = self.run_script(luci=True, remote=True, password="секрет", hint="кот")
        self.assertEqual(code, 0, out)
        steps = [l.split()[0] for l in log.splitlines() if l.split()[0] in ("wget", "установщик", "python3", "remote.sh")]
        self.assertEqual(steps[0:2], ["wget", "установщик"])
        self.assertIn("remote.sh install", log)
        self.assertIn("doctor.py --summary", log)                                        # последний шаг не съеден чтением stdin
        self.assertIn("@@DONE", out)
        self.assertLess(out.index("@@ADDRESS"), out.index("@@DONE"))
        self.assertIn("установщик: ключи --luci", out)

    def test_password_goes_through_environment_never_through_arguments(self):
        code, out, log = self.run_script(luci=False, remote=False, password="секрет-123", hint="имя кота")
        self.assertEqual(code, 0, out)
        py = [l for l in log.splitlines() if l.startswith("python3 ")]
        self.assertTrue(py[0].endswith("password.py set-env"), py)
        for line in py:
            self.assertNotIn("секрет-123", line)                                         # в аргументах процесса пароля нет
        self.assertIn("ENV_PW=секрет-123", log)
        self.assertIn("ENV_HINT=имя кота", log)
        self.assertNotIn("секрет-123", out)                                              # и в выводе тоже
        self.assertIn("--no-luci", out)

    def test_tricky_password_arrives_intact_and_executes_nothing(self):
        marker = Path("/tmp/pwned-by-test")
        marker.unlink(missing_ok=True)
        for pw in TRICKY:
            if "\n" in pw:
                continue
            self.log.unlink(missing_ok=True)
            code, out, log = self.run_script(luci=False, remote=False, password=pw, hint="h'int")
            self.assertEqual(code, 0, pw)
            self.assertIn(f"ENV_PW={pw}", log, pw)
            self.assertIn("ENV_HINT=h'int", log)
        self.assertFalse(marker.exists())

    def test_no_password_means_no_password_step(self):
        code, out, log = self.run_script(luci=True, remote=False, password="")
        self.assertEqual(code, 0)
        self.assertNotIn("set-env", log)
        self.assertNotIn("remote.sh", log)

    def test_failed_install_stops_and_says_so(self):
        self.put(self.bin / "wget", "#!/bin/sh\necho 'exit 7'\n")
        code, out, log = self.run_script(luci=True, remote=True, password="x")
        self.assertEqual(code, 1)
        self.assertIn("@@FAILED install 7", out)
        self.assertNotIn("set-env", log)                                                 # дальше не пошли
        self.assertNotIn("@@DONE", out)

    def test_failed_password_stops_before_going_to_the_internet(self):
        code, out, log = self.run_script(luci=True, remote=True, password="x", env={"PY_RC": "1"})
        self.assertEqual(code, 1)
        self.assertIn("@@FAILED password", out)
        self.assertNotIn("remote.sh", log)                                               # без пароля в интернет не выставляем

    def test_remote_needing_a_click_is_not_a_failure_of_the_script(self):
        code, out, log = self.run_script(luci=True, remote=True, password="x", env={"REMOTE_RC": "2"})
        self.assertIn("@@REMOTE_CODE 2", out)
        self.assertIn("@@DONE", out)

    def test_script_has_machine_mode_and_no_secrets_in_echo(self):
        text = sc.build_script(password="СЕКРЕТ", hint="")
        self.assertIn("export DNEVNIK_MACHINE=1", text)
        self.assertEqual([l for l in text.splitlines() if l.startswith("echo") and "СЕКРЕТ" in l], [])


class LineTests(unittest.TestCase):
    def feed(self, lines):
        out, shown, opened = sc.Outcome(), [], []
        for l in lines:
            sc.handle_line(l, out, opened.append, shown.append)
        return out, shown, opened

    def test_machine_lines_are_not_shown_and_normal_ones_are(self):
        out, shown, _ = self.feed(["==> Ставлю", "@@ADDRESS https://a.example/", "обычная строка", "@@DONE"])
        self.assertEqual(shown, ["==> Ставлю", "обычная строка"])
        self.assertEqual((out.address, out.done), ("https://a.example/", True))

    def test_login_link_opens_in_the_browser(self):
        out, shown, opened = self.feed(["@@LOGIN_URL https://login.tailscale.com/a/xyz"])
        self.assertEqual(opened, ["https://login.tailscale.com/a/xyz"])
        self.assertTrue(any("Connect device" in s for s in shown))

    def test_funnel_enable_opens_its_link_and_is_remembered(self):
        out, shown, opened = self.feed(["@@NEEDS funnel_enable https://login.tailscale.com/f/funnel?node=abc"])
        self.assertEqual(opened, ["https://login.tailscale.com/f/funnel?node=abc"])
        self.assertTrue(any("Enable Funnel" in s for s in shown))
        self.assertTrue(out.needs[0].startswith("funnel_enable"))

    def test_failure_and_remote_code(self):
        out, _, _ = self.feed(["@@FAILED install 3", "@@REMOTE_CODE 2"])
        self.assertEqual((out.failed, out.remote_code), ("install 3", 2))

    def test_browser_failure_does_not_break_the_flow(self):
        out, shown = sc.Outcome(), []
        sc.handle_line("@@LOGIN_URL https://x", out, mock.Mock(side_effect=RuntimeError("нет браузера")), shown.append)
        self.assertEqual(out.login_url, "https://x")


class RunSshTests(unittest.TestCase):
    def test_runs_ssh_with_the_script_on_stdin_and_parses_output(self):
        tmp = Path(tempfile.mkdtemp(prefix="dn-ssh-"))
        try:
            fake = tmp / "ssh"
            fake.write_text(f'#!/bin/sh\necho "$@" > {tmp}/args\ncat > {tmp}/stdin\necho "==> шаг"\necho "@@LOGIN_URL https://l.example/a"\necho "@@ADDRESS https://addr.example/"\necho "@@DONE"\nexit 0\n')
            fake.chmod(0o755)
            shown, opened = [], []
            out = sc.run_ssh("192.168.1.1", "echo hi\n", opener=opened.append, say=shown.append, ssh=str(fake))
            self.assertEqual((out.address, out.done, out.code, opened), ("https://addr.example/", True, 0, ["https://l.example/a"]))
            args = (tmp / "args").read_text()
            for needle in ("root@192.168.1.1", "sh -s", "StrictHostKeyChecking=accept-new", "ServerAliveInterval=30"):
                self.assertIn(needle, args)
            self.assertEqual((tmp / "stdin").read_text(), "echo hi\n")                    # сценарий пришёл через stdin, а не в аргументах
            self.assertEqual(shown[0], "==> шаг")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ssh_failure_is_reported_by_exit_code(self):
        tmp = Path(tempfile.mkdtemp(prefix="dn-ssh-"))
        try:
            fake = tmp / "ssh"
            fake.write_text("#!/bin/sh\ncat >/dev/null\necho 'Permission denied'\nexit 255\n")
            fake.chmod(0o755)
            out = sc.run_ssh("1.2.3.4", "x\n", opener=lambda u: None, say=lambda s: None, ssh=str(fake))
            self.assertEqual((out.code, out.done), (255, False))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FlowTests(unittest.TestCase):
    """Весь разговор с человеком: подставные ответы и подставной SSH."""

    def run_flow(self, answers, secrets, outcome=None, argv=(), login_tool=None):
        said, opened, calls = [], [], []
        ans, sec = list(answers), list(secrets)

        def runner(router, script, opener=None, say=None):
            calls.append((router, script))
            return outcome or self.ok_outcome()
        code = sc.main(list(argv), ask=lambda prompt: (said.append("? " + prompt), ans.pop(0))[1], ask_secret=lambda prompt: (said.append("? " + prompt), sec.pop(0))[1],
                       runner=runner, opener=opened.append, say=said.append, login_tool=login_tool)
        return code, said, opened, calls

    def ok_outcome(self, address="https://dnevnik.example.ts.net/"):
        o = sc.Outcome()
        o.done, o.address, o.code = True, address, 0
        return o

    def test_happy_path(self):
        code, said, opened, calls = self.run_flow(["192.168.1.1", "", "", "имя кота", "1"], ["семья-2026", "семья-2026"])
        self.assertEqual(code, 0)
        router, script = calls[0]
        self.assertEqual(router, "192.168.1.1")
        self.assertIn("--luci", script)
        self.assertIn("remote.sh install", script)
        self.assertIn("DNEVNIK_NEW_PASSWORD='семья-2026'", script)
        self.assertIn("DNEVNIK_NEW_HINT='имя кота'", script)
        text = "\n".join(said)
        self.assertIn("Дневник из интернета: https://dnevnik.example.ts.net/", text)
        self.assertIn("http://192.168.1.1:8765", text)
        self.assertEqual(opened, ["http://192.168.1.1:8765"])                              # открыли страницу для входа в Дневник.ру
        self.assertNotIn("семья-2026", text)                                              # пароль человеку обратно не печатается

    def test_default_router_comes_from_the_gateway(self):
        with mock.patch.object(sc, "guess_router", return_value="10.0.0.1"):
            code, said, _, calls = self.run_flow(["", "", "н", "3"], [""])
        self.assertEqual(calls[0][0], "10.0.0.1")
        self.assertTrue(any("[10.0.0.1]" in s for s in said))

    def test_password_mismatch_asks_again(self):
        code, said, _, calls = self.run_flow(["1.1.1.1", "", "", "", "3"], ["a1", "a2", "верный", "верный"])
        self.assertIn("Пароли не совпали", "\n".join(said))
        self.assertIn("DNEVNIK_NEW_PASSWORD='верный'", calls[0][1])

    def test_internet_access_without_password_is_not_allowed(self):
        code, said, _, calls = self.run_flow(["1.1.1.1", "", "", "", "3"], ["", "пароль", "пароль"])
        self.assertIn("Для доступа из интернета пароль нужен", "\n".join(said))
        self.assertIn("DNEVNIK_NEW_PASSWORD='пароль'", calls[0][1])

    def test_home_only_without_password_is_allowed(self):
        code, said, _, calls = self.run_flow(["1.1.1.1", "", "н", "3"], [""])
        script = calls[0][1]
        self.assertNotIn("remote.sh", script)
        self.assertNotIn("set-env", script)

    def test_options_from_the_command_line_skip_the_questions(self):
        code, said, _, calls = self.run_flow(["3"], [""], argv=["--router", "9.9.9.9", "--no-luci", "--no-remote"])
        self.assertEqual(calls[0][0], "9.9.9.9")
        self.assertIn("--no-luci", calls[0][1])
        self.assertFalse(any("Адрес роутера" in s for s in said))

    def test_failed_install_returns_error_and_says_how_to_retry(self):
        o = sc.Outcome()
        o.failed, o.code = "install 1", 1
        code, said, _, _ = self.run_flow(["1.1.1.1", "", "н"], [""], outcome=o)
        self.assertEqual(code, 1)
        text = "\n".join(said)
        self.assertIn("Установка не завершилась", text)
        self.assertIn("сделанное она пропустит", text)

    def test_connection_failure_without_done_marker_is_an_error(self):
        o = sc.Outcome()
        o.code = 255
        code, said, _, _ = self.run_flow(["1.1.1.1", "", "н"], [""], outcome=o)
        self.assertEqual(code, 1)

    def test_funnel_click_pending_is_explained(self):
        o = self.ok_outcome(address=None)
        o.needs = ["funnel_enable https://login.tailscale.com/f/funnel?node=abc"]
        code, said, _, _ = self.run_flow(["1.1.1.1", "", "", "", "3"], ["п", "п"], outcome=o)
        self.assertEqual(code, 0)
        self.assertIn("ждёт вашего действия в браузере", "\n".join(said))

    def test_gosuslugi_choice_runs_the_login_tool_for_this_router(self):
        got = []
        code, _, opened, _ = self.run_flow(["5.5.5.5", "", "н", "2"], [""], login_tool=lambda r: (got.append(r), 0)[1])
        self.assertEqual(got, ["5.5.5.5"])
        self.assertEqual(opened, [])

    def test_skip_login(self):
        said, opened, sec = [], [], [""]
        code = sc.main(["--router", "1.1.1.1", "--no-luci", "--no-remote", "--skip-login"], ask=lambda p: "", ask_secret=lambda p: sec.pop(0),
                       runner=lambda r, s, opener=None, say=None: self.ok_outcome(), opener=opened.append, say=said.append)
        self.assertEqual(code, 0)
        self.assertFalse(any("Как вы входите" in s for s in said))


class GuessRouterTests(unittest.TestCase):
    def guess(self, which, text):
        def fake_run(cmd, **kw):
            if cmd[0] == which:
                return mock.Mock(stdout=text.encode("utf-8"))
            raise OSError
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            return sc.guess_router()

    def test_mac(self):
        self.assertEqual(self.guess("route", "   route to: default\n    gateway: 192.168.0.1\n  interface: en0\n"), "192.168.0.1")

    def test_linux(self):
        self.assertEqual(self.guess("ip", "default via 192.168.10.1 dev eth0 proto dhcp\n"), "192.168.10.1")

    def test_windows_english_and_russian(self):
        self.assertEqual(self.guess("ipconfig", "   Default Gateway . . . . . . . . . : 192.168.1.1\n"), "192.168.1.1")
        self.assertEqual(self.guess("ipconfig", "   Основной шлюз. . . . . . . . . : 192.168.100.1\n"), "192.168.100.1")

    def test_unknown_gives_none(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError):
            self.assertIsNone(sc.guess_router())


class EnvironmentTests(unittest.TestCase):
    def test_everything_present_means_no_problems(self):
        self.assertEqual(sc.check_environment(which=lambda n: "/usr/bin/ssh", system="Linux", pyver=(3, 11)), [])
        self.assertEqual(sc.check_environment(which=lambda n: "C:/ssh.exe", system="Windows", winver=(10, 0), pyver=(3, 12)), [])

    def test_windows_7_without_ssh_gets_the_git_bash_way_out(self):
        for ver in ((6, 1), (6, 2), (6, 3)):                                   # Windows 7, 8, 8.1
            p = sc.check_environment(which=lambda n: None, system="Windows", winver=ver, pyver=(3, 8))
            self.assertEqual(len(p), 1, ver)
            for needle in ("Windows (7 или 8)", "Git for Windows", "Git Bash", "Python 3.8", "PuTTY", "Госуслуги"):
                self.assertIn(needle, p[0])

    def test_windows_10_without_ssh_is_told_where_to_switch_it_on(self):
        p = sc.check_environment(which=lambda n: None, system="Windows", winver=(10, 0), pyver=(3, 11))
        self.assertIn("Клиент OpenSSH", p[0])
        self.assertNotIn("Git for Windows", p[0])

    def test_windows_7_with_git_ssh_can_continue(self):
        self.assertEqual(sc.check_environment(which=lambda n: "C:/Git/usr/bin/ssh.exe", system="Windows", winver=(6, 1), pyver=(3, 8)), [])

    def test_mac_and_linux_without_ssh(self):
        for system in ("Darwin", "Linux"):
            p = sc.check_environment(which=lambda n: None, system=system, pyver=(3, 11))
            self.assertIn("openssh-client", p[0])

    def test_too_old_python_is_reported(self):
        p = sc.check_environment(which=lambda n: "ssh", system="Linux", pyver=(3, 6))
        self.assertIn("Python 3.7", p[0])

    def test_old_windows_detection(self):
        self.assertTrue(sc.old_windows("Windows", (6, 1)))
        self.assertFalse(sc.old_windows("Windows", (10, 0)))
        self.assertFalse(sc.old_windows("Darwin", (6, 1)))

    def test_main_stops_before_asking_anything_when_the_environment_is_not_ready(self):
        said, asked = [], []
        code = sc.main([], ask=lambda p: asked.append(p) or "", ask_secret=lambda p: asked.append(p) or "", say=said.append, env_check=lambda: ["нет ssh"])
        self.assertEqual(code, 2)
        self.assertEqual(asked, [])
        self.assertIn("нет ssh", said)

    def test_the_tool_uses_only_syntax_old_pythons_understand(self):
        """Windows 7 держит не новее Python 3.8: в программе установки нет синтаксиса новее."""
        import ast
        tree = ast.parse((ROOT / "tools" / "setup_computer.py").read_text(encoding="utf-8"), feature_version=(3, 8))
        self.assertTrue(tree.body)


class LauncherTests(unittest.TestCase):
    def test_windows_launcher(self):
        bat = (ROOT / "setup-windows.bat").read_text(encoding="utf-8")
        self.assertIn("tools\\setup_computer.py", bat)
        self.assertIn("chcp 65001", bat)
        self.assertIn("pause", bat)

    def test_mac_launcher_is_executable_and_runs_the_tool(self):
        f = ROOT / "Установить-на-роутер.command"
        self.assertTrue(os.access(f, os.X_OK))
        text = f.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/bash"))
        self.assertIn("tools/setup_computer.py", text)
        self.assertEqual(subprocess.run(["bash", "-n", str(f)]).returncode, 0)

    def test_tool_is_executable_and_compiles(self):
        self.assertTrue(os.access(ROOT / "tools" / "setup_computer.py", os.X_OK))
        subprocess.run([sys.executable, "-m", "py_compile", str(ROOT / "tools" / "setup_computer.py")], check=True)


if __name__ == "__main__":
    unittest.main()
