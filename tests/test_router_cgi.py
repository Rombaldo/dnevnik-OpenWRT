"""Скрипт настройки входа для роутера (router/setup.cgi): запускаем так, как его запускает веб-сервер роутера (CGI)."""
import json
import os
import subprocess
import sys
import tempfile
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
        status = int(head.split("Status: ")[1].split("\r\n")[0])
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


if __name__ == "__main__":
    unittest.main()
