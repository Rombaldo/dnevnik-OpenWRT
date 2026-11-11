"""Проверки безопасности: вход, секреты, чужие тексты на странице."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import core

ROOT = Path(__file__).resolve().parent.parent


def cookie(name, domain, value="v" * 20, **kw):
    c = {"name": name, "value": value, "domain": domain, "path": "/", "expires": 4102444800, "httpOnly": True, "secure": True}
    c.update(kw)
    return c


class CookieTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "session.json"
        self.path.write_text(json.dumps([
            cookie("DnevnikAuth_a", ".dnevnik.ru"),
            cookie("dnevnik_sst", "dnevnik.ru"),
            cookie("esia_session", "esia.gosuslugi.ru"),     # Госуслуги: нам не нужны
            cookie("sid", ".gosuslugi.ru"),
            cookie("_ym_uid", ".yandex.ru"),
            cookie("sneaky", "dnevnik.ru.evil.com"),
            cookie("sneaky2", "evildnevnik.ru"),
        ]))

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_dnevnik_cookies_are_loaded(self):
        names = sorted(c.name for c in core.load_jar(self.path))
        self.assertEqual(names, ["DnevnikAuth_a", "dnevnik_sst"])

    def test_only_dnevnik_cookies_are_saved(self):
        names = sorted(c["name"] for c in core.jar_to_list(core.load_jar(self.path)))
        self.assertEqual(names, ["DnevnikAuth_a", "dnevnik_sst"])

    def test_cookies_go_only_to_dnevnik(self):
        jar = core.load_jar(self.path)

        def sent(url):
            r = urllib.request.Request(url)
            jar.add_cookie_header(r)
            return r.get_header("Cookie")

        self.assertIn("DnevnikAuth_a", sent("https://dnevnik.ru/api/v2/marks/diary"))
        self.assertIsNone(sent("https://b5.csdnevnik.ru/file.pdf"))      # сервер файлов cookie не получает
        self.assertIsNone(sent("https://evil.example.com/"))
        self.assertIsNone(sent("https://dnevnik.ru.evil.com/"))
        self.assertIsNone(sent("http://dnevnik.ru/api"))                 # по незащищённому соединению не отправляем

    def test_login_files_are_private(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "data"
            core.write_private(d / "session.json", "[]")
            self.assertEqual(stat.S_IMODE(os.stat(d / "session.json").st_mode), 0o600)

    def test_tls_is_verified(self):
        ctx = core.make_ssl_context()
        import ssl
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)


@unittest.skipIf(shutil.which("git") is None or shutil.which("bash") is None, "нужны git и bash")
class SecretScannerTests(unittest.TestCase):
    """Сканер секретов проверяем во временном репозитории с выдуманными «секретами»."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "check-secrets.sh", self.repo / "scripts")
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, capture_output=True, text=True)

    def scan(self, mode="staged"):
        r = subprocess.run(["bash", "scripts/check-secrets.sh", mode], cwd=self.repo, capture_output=True, text=True)
        return r.returncode, r.stdout

    def add(self, name, text="x\n"):
        p = self.repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        self.git("add", "-f", name)

    def test_clean_repo_passes(self):
        self.add("a.py", "print('привет')\n")
        self.assertEqual(self.scan()[0], 0)

    def test_private_key_is_caught(self):
        self.add("a.txt", "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nAAAA\n")   # строку собираем частями, чтобы сам тест не выглядел как ключ
        code, out = self.scan()
        self.assertEqual(code, 1)
        self.assertIn("ключ", out)

    def test_github_token_is_caught(self):
        self.add("a.sh", "TOKEN=ghp_" + "A" * 36 + "\n")
        self.add("b.sh", "T=github_pat_" + "B" * 40 + "\n")
        self.assertEqual(self.scan()[0], 1)

    def test_variable_token_is_fine(self):
        self.add("install.sh", 'wget --header="Authorization: Bearer $TOKEN" "$URL"\n')
        self.assertEqual(self.scan()[0], 0)

    def test_data_files_are_caught_by_name(self):
        for name in ["data/diary.json", "session.json", "x/ids.json", "config.json", "k.pem", "data/router_key", "known_hosts", ".env"]:
            with self.subTest(name=name):
                self.git("reset", "-q")
                self.add(name)
                self.assertEqual(self.scan()[0], 1, name)

    def test_login_cookie_in_code_is_caught(self):
        self.add("a.py", 'c = {"DnevnikAuth_a": "' + "a1b2" * 8 + '"}\n')
        self.assertEqual(self.scan()[0], 1)
        self.git("reset", "-q")
        self.add("b.py", "personId = 1000022123456\n")
        self.assertEqual(self.scan()[0], 1)

    def test_history_scan_finds_old_leak(self):
        self.add("a.txt", "TOKEN=ghp_" + "C" * 36 + "\n")
        self.git("commit", "-q", "-m", "leak")
        self.git("rm", "-q", "-f", "a.txt")
        self.git("commit", "-q", "-m", "removed")
        self.assertEqual(self.scan("tree")[0], 0)        # в текущих файлах уже нет
        self.assertEqual(self.scan("history")[0], 1)     # но в истории остался

    def test_project_itself_is_clean(self):
        for mode in ("tree", "history"):
            r = subprocess.run(["bash", "scripts/check-secrets.sh", mode], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
