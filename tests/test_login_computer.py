"""Вход через Госуслуги на любом компьютере: передача входа на роутер (без настоящего роутера и браузера)."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import login_computer as lc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SESS = [{"name": "DnevnikAuth_a", "value": "abc", "domain": ".dnevnik.ru", "path": "/", "httpOnly": False, "secure": True, "expires": 1800000000},
        {"name": "dnevnik_sst", "value": "x", "domain": "schools.dnevnik.ru", "httpOnly": True}]
IDS = {"personId": "1000000000001", "schoolId": "1000000000002"}


@unittest.skipIf(shutil.which("python3") is None or shutil.which("sh") is None, "нужны python3 и sh")
class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dn-lc-"))
        self.data = self.tmp / "data"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_remote(self, bundle, sync=False):
        cmd = lc.remote_command(bundle, str(self.data), sync)
        return subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=30)      # так команду выполнит роутер

    def test_files_are_saved_privately(self):
        r = self.run_remote({"session": SESS, "ids": IDS})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads((self.data / "session.json").read_text()), SESS)
        self.assertEqual(json.loads((self.data / "ids.json").read_text()), IDS)
        self.assertEqual(stat.S_IMODE((self.data / "session.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.data.stat().st_mode), 0o700)
        self.assertEqual([p.name for p in self.data.iterdir() if p.name.endswith(".new")], [])

    def test_existing_ids_are_kept_for_the_same_pupil_and_replaced_for_another(self):
        self.data.mkdir()
        (self.data / "ids.json").write_text(json.dumps(dict(IDS, groupId="77")))
        self.assertEqual(self.run_remote({"session": SESS, "ids": IDS}).returncode, 0)
        self.assertEqual(json.loads((self.data / "ids.json").read_text())["groupId"], "77")
        other = {"personId": "5", "schoolId": "6"}
        self.assertEqual(self.run_remote({"session": SESS, "ids": other}).returncode, 0)
        self.assertEqual(json.loads((self.data / "ids.json").read_text()), other)              # чужой группы у другого ученика не остаётся

    def test_garbage_is_refused_and_nothing_changes(self):
        self.data.mkdir()
        (self.data / "session.json").write_text("старый вход")
        for bad in ({"session": [], "ids": IDS}, {"session": "x", "ids": IDS}, {"session": SESS, "ids": {"personId": "a", "schoolId": "1"}},
                    {"session": SESS, "ids": {}}, {"session": [{"name": "a", "value": "b", "domain": ".gosuslugi.ru"}], "ids": IDS},
                    {"session": [{"name": "a"}], "ids": IDS}, {}):
            r = self.run_remote(bad)
            self.assertEqual(r.returncode, 3, bad)
            self.assertIn("ничего не изменено", r.stdout)
        self.assertEqual((self.data / "session.json").read_text(), "старый вход")
        self.assertFalse((self.data / "ids.json").exists())

    def test_command_is_portable_between_shells(self):
        cmd = lc.remote_command({"session": SESS, "ids": IDS})
        self.assertNotIn('"', cmd)                      # кавычки внутри ломаются при передаче из Windows
        self.assertNotIn("\n", cmd)
        self.assertLess(len(cmd), 30000)                 # предел длины команды в Windows 32 КБ
        self.assertTrue(cmd.startswith("python3 -c 'import base64,sys;exec(base64.b64decode(sys.argv[1]))'"))
        long = lc.remote_command({"session": SESS * 15, "ids": IDS})
        self.assertLess(len(long), 30000)

    def test_cyrillic_cookie_values_survive(self):
        sess = [dict(SESS[0], value="значение")]
        self.assertEqual(self.run_remote({"session": sess, "ids": IDS}).returncode, 0)
        self.assertEqual(json.loads((self.data / "session.json").read_text(encoding="utf-8"))[0]["value"], "значение")


@unittest.skipIf(shutil.which("python3") is None or shutil.which("sh") is None, "нужны python3 и sh")
class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dn-lc-main-"))
        self.local = self.tmp / "local"
        self.local.mkdir()
        (self.local / "session.json").write_text(json.dumps(SESS))
        (self.local / "ids.json").write_text(json.dumps(IDS))
        self.patches = [mock.patch.object(lc, "SESSION", self.local / "session.json"), mock.patch.object(lc, "IDS", self.local / "ids.json")]
        for p in self.patches:
            p.start()
        # «ssh» для проверки: запоминает свои ключи и выполняет последнюю строку здесь, как это сделал бы роутер
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "ssh"
        fake.write_text('#!/bin/sh\nF="' + str(self.tmp) + '/ssh-args"\n: > "$F"\nn=$#; i=0\nfor a; do i=$((i+1)); [ $i -lt $n ] && printf "%s\\n" "$a" >> "$F"; last=$a; done\nexec sh -c "$last"\n')
        fake.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_whole_pipeline_without_browser(self):
        remote = self.tmp / "router-data"
        rc = lc.main(["--no-browser", "--router", "192.168.1.1", "--identity", "/k/key", "--ssh-arg=-o", "--ssh-arg=BatchMode=yes", "--remote-data", str(remote)])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads((remote / "session.json").read_text()), SESS)
        args = (self.tmp / "ssh-args").read_text().split("\n")
        self.assertIn("root@192.168.1.1", args)
        self.assertEqual(args[args.index("-i") + 1], "/k/key")
        self.assertIn("BatchMode=yes", args)
        self.assertTrue((self.local / "session.json").exists())                  # без --forget вход остаётся и на этом компьютере

    def test_forget_removes_the_local_copy_only_after_success(self):
        remote = self.tmp / "router-data"
        self.assertEqual(lc.main(["--no-browser", "--remote-data", str(remote), "--forget"]), 0)
        self.assertFalse((self.local / "session.json").exists())
        self.assertFalse((self.local / "ids.json").exists())
        self.assertTrue((remote / "session.json").exists())

    def test_failed_send_keeps_the_local_login_and_says_how_to_retry(self):
        (self.bin / "ssh").write_text("#!/bin/sh\nexit 255\n")
        with mock.patch("builtins.print") as pr:
            rc = lc.main(["--no-browser", "--remote-data", str(self.tmp / "x"), "--forget"])
        self.assertEqual(rc, 255)
        self.assertTrue((self.local / "session.json").exists())                   # с --forget всё равно не стираем, если не передалось
        self.assertIn("--no-browser", " ".join(str(c.args[0]) for c in pr.call_args_list))

    def test_no_send_only_keeps_the_login(self):
        self.assertEqual(lc.main(["--no-browser", "--no-send"]), 0)
        self.assertFalse((self.tmp / "ssh-args").exists())

    def test_missing_login_is_reported(self):
        (self.local / "session.json").unlink()
        self.assertEqual(lc.main(["--no-browser", "--no-send"]), 1)

    def test_missing_ssh_is_explained(self):
        with mock.patch("shutil.which", return_value=None), mock.patch("builtins.print") as pr:
            rc = lc.main(["--no-browser"])
        self.assertEqual(rc, 2)
        self.assertIn("Клиент OpenSSH", " ".join(str(c.args[0]) for c in pr.call_args_list))


class FilesTests(unittest.TestCase):
    def test_windows_launcher_and_requirements(self):
        bat = (ROOT / "login-windows.bat").read_text(encoding="utf-8")
        self.assertIn("tools\\login_computer.py", bat)
        self.assertIn("py -3", bat)
        self.assertIn("pause", bat)
        self.assertIn("playwright", (ROOT / "requirements.txt").read_text())

    def test_script_runs_help(self):
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "login_computer.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--no-browser", r.stdout)


if __name__ == "__main__":
    unittest.main()
