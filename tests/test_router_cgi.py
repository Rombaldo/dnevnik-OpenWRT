"""Скрипт настройки входа для роутера (router/setup.cgi): запускаем так, как его запускает веб-сервер роутера (CGI)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CGI = ROOT / "router" / "setup.cgi"


class SetupCgiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gap = str(Path(self.tmp.name) / "gap")

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, body=None, method="POST", raw=None):
        data = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode()
        env = {"PATH": os.environ["PATH"], "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(data)),
               "DNEVNIK_HOME": self.tmp.name, "DNEVNIK_GAP_FILE": self.gap, "PYTHONPATH": str(ROOT)}
        r = subprocess.run([sys.executable, str(CGI)], input=data, capture_output=True, env=env, timeout=30)
        head, _, payload = r.stdout.decode("utf-8").partition("\r\n\r\n")
        status = int(head.split("Status: ")[1].split()[0])
        return status, json.loads(payload), head, r.stderr.decode()

    def test_get_is_refused(self):
        self.assertEqual(self.call({}, method="GET")[0], 405)

    def test_garbage_is_refused(self):
        self.assertEqual(self.call(raw=b"not json")[0], 400)
        self.assertEqual(self.call(raw=b"[1,2]")[0], 400)

    def test_empty_login_is_rejected_without_touching_the_site(self):
        status, body, head, err = self.call({"login": "", "password": ""})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("Введите логин и пароль", body["message"])
        self.assertIn("application/json", head)
        self.assertFalse((Path(self.tmp.name) / "data" / "credentials.json").exists())

    def test_attempts_are_rate_limited(self):
        self.assertEqual(self.call({"login": "", "password": ""})[0], 200)
        status, body, _, _ = self.call({"login": "", "password": ""})        # сразу второй раз
        self.assertEqual(status, 429)
        self.assertFalse(body["ok"])

    def test_oversized_body_is_refused(self):
        self.assertEqual(self.call(raw=b"x" * 5000)[0], 400)

    def test_password_is_not_echoed(self):
        status, body, head, err = self.call({"login": "a" * 300, "password": "pw-should-not-appear"})
        self.assertNotIn("pw-should-not-appear", json.dumps(body, ensure_ascii=False) + head + err)


LOGOUT_CGI = ROOT / "router" / "logout.cgi"


class LogoutCgiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "data").mkdir()
        (self.home / "router").mkdir()
        sync = self.home / "router" / "sync.sh"
        sync.write_text("#!/bin/sh\ntouch \"$(dirname \"$0\")/sync-was-run\"\n")
        sync.chmod(0o755)
        self.out = self.home / "www" / "data.json"
        self.gap = self.home / "gap"
        for name in ("session.json", "ids.json", "history.json"):
            (self.home / "data" / name).write_text("{}")

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, body=None, method="POST", raw=None, keep_gap=False):
        if not keep_gap and self.gap.exists():
            self.gap.unlink()                                             # в тестах не ждём секунду между запросами
        data = raw if raw is not None else json.dumps(body).encode()
        env = {"PATH": os.environ["PATH"], "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(data)), "DNEVNIK_HOME": str(self.home),
               "DNEVNIK_OUT": str(self.out), "DNEVNIK_LOGOUT_GAP": str(self.gap), "PYTHONPATH": str(ROOT)}
        r = subprocess.run([sys.executable, str(LOGOUT_CGI)], input=data, capture_output=True, env=env, timeout=30)
        head, _, payload = r.stdout.decode("utf-8").partition("\r\n\r\n")
        return int(head.split("Status: ")[1].split()[0]), json.loads(payload)

    def age_pending(self, seconds):
        f = self.home / "data" / "logout-pending.json"
        d = json.loads(f.read_text())
        d["at"] -= seconds
        f.write_text(json.dumps(d))

    def test_bad_requests(self):
        self.assertEqual(self.call({}, method="GET")[0], 405)
        self.assertEqual(self.call(raw=b"not json")[0], 400)
        self.assertEqual(self.call({"action": "format-everything"})[0], 400)
        self.assertEqual(self.call(raw=b"x" * 2000)[0], 400)

    def test_two_steps_are_enforced_by_the_server(self):
        _, begin = self.call({"action": "begin"})
        self.assertTrue(begin["ok"])
        code, quick = self.call({"action": "confirm", "token": begin["token"]})        # второй шаг сразу: отказ
        self.assertFalse(quick["ok"])
        self.assertTrue((self.home / "data" / "session.json").exists())              # вход на месте
        self.assertFalse(self.call({"action": "confirm"})[1]["ok"])                    # без кода

    def test_full_logout_and_restore(self):
        token = self.call({"action": "begin"})[1]["token"]
        self.age_pending(5)
        _, res = self.call({"action": "confirm", "token": token})
        self.assertTrue(res["ok"], res)
        self.assertFalse((self.home / "data" / "session.json").exists())
        page = json.loads(self.out.read_text())                                       # страница сразу пересобрана
        self.assertTrue(page["status"]["needLogin"])
        self.assertTrue(page["status"]["canRestore"])
        self.assertEqual(page["days"], [])
        _, res = self.call({"action": "restore"})
        self.assertTrue(res["ok"], res)
        self.assertTrue((self.home / "data" / "session.json").exists())
        self.assertFalse(json.loads(self.out.read_text())["status"]["needLogin"])
        for _ in range(30):                                                           # после возврата запускается обновление
            if (self.home / "router" / "sync-was-run").exists():
                break
            time.sleep(0.1)
        self.assertTrue((self.home / "router" / "sync-was-run").exists())

    def test_requests_are_rate_limited(self):
        self.assertEqual(self.call({"action": "begin"})[0], 200)
        self.assertEqual(self.call({"action": "begin"}, keep_gap=True)[0], 429)


if __name__ == "__main__":
    unittest.main()
