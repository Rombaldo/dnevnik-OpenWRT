import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import server

server.DEMO = True  # только выдуманные данные


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_data_json(self):
        code, body = self.get("/data.json")
        d = json.loads(body)
        self.assertEqual(code, 200)
        self.assertTrue(d["days"])
        self.assertIn("status", d)
        self.assertEqual(d["version"], server.VERSION)

    def test_index_served(self):
        code, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("Мой дневник", body)

    def test_private_files_not_served(self):
        for p in ["/data/session.json", "/../data/session.json", "/../server.py", "/..%2fserver.py",
                  "/session.json", "/../config.json", "/../fetcher.py"]:
            self.assertEqual(self.get(p)[0], 404, p)

    def test_attachment_is_served_with_name(self):
        req = urllib.request.Request(self.base + "/files/1.pdf?name=" + urllib.parse.quote("ГЕО Карта.pdf"))
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers["Content-Type"], "application/pdf")
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn(urllib.parse.quote("ГЕО Карта.pdf"), r.headers["Content-Disposition"])
            self.assertTrue(r.read().startswith(b"%PDF"))

    def test_attachment_bad_requests(self):
        for p in ["/files/999.pdf", "/files/1.html", "/files/1.exe", "/files/abc.pdf", "/files/../server.py",
                  "/files/1.pdf/../../server.py", "/files/.pdf"]:
            self.assertEqual(self.get(p)[0], 404, p)

    def test_offline_copy_has_inline_data(self):
        code, body = self.get("/offline.html")
        self.assertEqual(code, 200)
        self.assertNotIn("/*INLINE_DATA*/null", body)
        self.assertIn('"days"', body)

    def test_version_file_matches_changelog(self):
        text = (server.ROOT / "CHANGELOG.md").read_text()
        self.assertIn(f"## {server.VERSION}", text)


if __name__ == "__main__":
    unittest.main()
