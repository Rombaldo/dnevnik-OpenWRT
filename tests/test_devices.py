"""Кто заходит: название устройства, откуда, журнал входов, «последний заход»."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import devices

UA = {
    "iphone": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.7 Mobile/15E148 Safari/604.1",
    "iphone_chrome": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/130.0 Mobile/15E148 Safari/604.1",
    "ipad": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "android": "Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "android_k": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36",
    "samsung": "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/23.0 Chrome/115.0.0.0 Mobile Safari/537.36",
    "firefox_android": "Mozilla/5.0 (Android 14; Mobile; rv:131.0) Gecko/131.0 Firefox/131.0",
    "yandex": "Mozilla/5.0 (Linux; arm_64; Android 13; SM-A536E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 YaBrowser/24.9 Mobile Safari/537.36",
    "huawei": "Mozilla/5.0 (Linux; Android 12; BKY-W09; HMSCore 6.12) AppleWebKit/537.36 (KHTML, like Gecko) HuaweiBrowser/15.0 Chrome/114.0 Mobile Safari/537.36",
    "edge": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "chrome_win": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "mac_safari": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "linux_chrome": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "firefox_linux": "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
}


class LabelTests(unittest.TestCase):
    def test_known_devices(self):
        want = {"iphone": "iPhone · Safari", "iphone_chrome": "iPhone · Chrome", "ipad": "iPad · Safari", "android": "Android (SM-X710) · Chrome",
                "android_k": "Android · Chrome", "samsung": "Android (SM-S918B) · Samsung Internet", "firefox_android": "Android · Firefox",
                "yandex": "Android (SM-A536E) · Яндекс Браузер", "huawei": "Android (BKY-W09) · Huawei Browser", "edge": "Windows · Edge",
                "chrome_win": "Windows · Chrome", "mac_safari": "Mac · Safari", "linux_chrome": "Linux · Chrome", "firefox_linux": "Linux · Firefox"}
        for k, w in want.items():
            self.assertEqual(devices.device_label(UA[k]), w, k)

    def test_unknown_and_garbage(self):
        for bad in (None, "", 5, [], "curl/8.0", "<script>alert(1)</script>"):
            self.assertIn("·", devices.device_label(bad))
        self.assertEqual(devices.device_label("curl/8.0"), "Устройство · браузер")

    def test_label_never_contains_markup_from_the_user_agent(self):
        label = devices.device_label("Mozilla/5.0 (Linux; Android 14; <img src=x onerror=1>) Chrome/1")
        self.assertNotIn("<", label)

    def test_truncated_user_agent_does_not_crash(self):
        self.assertTrue(devices.device_label(UA["iphone"][:40]))
        self.assertTrue(devices.device_label(UA["android"][:25]))


class WhereTests(unittest.TestCase):
    def test_places(self):
        for addr, want in (("192.168.10.147", "home"), ("10.0.0.5", "home"), ("172.16.3.4", "home"), ("169.254.1.1", "home"), ("127.0.0.1", "internet"), ("::1", "internet"),
                           ("100.109.6.85", "tailscale"), ("8.8.8.8", "internet"), ("", "unknown"), (None, "unknown"), ("мусор", "unknown"), ("fd7a:115c:a1e0::1", "home")):
            self.assertEqual(devices.where_from(addr), want, addr)


class CookieTests(unittest.TestCase):
    def test_id_and_cookie(self):
        did = devices.new_dev_id()
        self.assertRegex(did, r"^[0-9a-f]{12}$")
        self.assertNotEqual(did, devices.new_dev_id())
        self.assertEqual(devices.dev_id_from_cookie(f"a=1; dnevnik_dev={did}; b=2"), did)
        for bad in (None, "", "dnevnik_dev=zzz", "dnevnik_dev=abc", "x_dnevnik_dev=" + did, "dnevnik_dev=" + did + "00", "dnevnik_dev=ABCDEFABCDEF"):
            self.assertIsNone(devices.dev_id_from_cookie(bad), bad)
        c = devices.dev_cookie_header(did)
        self.assertIn("HttpOnly", c)
        self.assertIn("SameSite=Lax", c)
        self.assertNotIn("Secure", c)
        self.assertIn("Secure", devices.dev_cookie_header(did, secure=True))


class LoginLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_is_recorded_without_address_or_user_agent(self):
        e = devices.record_login(self.dir, True, UA["iphone"], "192.168.10.147", "a" * 12, now=1000)
        self.assertEqual((e["ok"], e["dev"], e["where"], e["ts"]), (True, "iPhone · Safari", "home", 1000))
        raw = (self.dir / "logins.json").read_text()
        self.assertNotIn("192.168", raw)
        self.assertNotIn("Mozilla", raw)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / "logins.json").st_mode), 0o600)

    def test_newest_first_and_capped(self):
        for i in range(devices.MAX_LOGINS + 10):
            devices.record_login(self.dir, True, UA["iphone"], "127.0.0.1", f"{i:012x}", now=1000 + i)
        log = devices.recent_logins(self.dir, 500)
        self.assertEqual(len(log), devices.MAX_LOGINS)
        self.assertEqual(log[0]["ts"], 1000 + devices.MAX_LOGINS + 9)
        self.assertEqual(len(devices.recent_logins(self.dir, 5)), 5)

    def test_repeated_failures_from_one_device_merge_into_one_row(self):
        did = "b" * 12
        for i in range(5):
            devices.record_login(self.dir, False, UA["android"], "127.0.0.1", did, now=1000 + i * 30)
        log = devices.recent_logins(self.dir)
        self.assertEqual((len(log), log[0]["n"], log[0]["ok"]), (1, 5, False))
        devices.record_login(self.dir, True, UA["android"], "127.0.0.1", did, now=1200)                # успешный вход всегда отдельной строкой
        devices.record_login(self.dir, False, UA["android"], "127.0.0.1", did, now=1210)
        self.assertEqual([e["ok"] for e in devices.record_login.__globals__["recent_logins"](self.dir)], [False, True, False])

    def test_failures_from_another_device_or_much_later_are_separate(self):
        devices.record_login(self.dir, False, UA["android"], "127.0.0.1", "a" * 12, now=1000)
        devices.record_login(self.dir, False, UA["android"], "127.0.0.1", "b" * 12, now=1001)
        devices.record_login(self.dir, False, UA["android"], "127.0.0.1", "b" * 12, now=1001 + devices.MERGE_FAILS + 1)
        self.assertEqual(len(devices.recent_logins(self.dir)), 3)

    def test_failed_since_counts_merged_attempts(self):
        for i in range(4):
            devices.record_login(self.dir, False, UA["iphone"], "127.0.0.1", "c" * 12, now=5000 + i)
        devices.record_login(self.dir, False, UA["iphone"], "127.0.0.1", "d" * 12, now=100)
        devices.record_login(self.dir, True, UA["iphone"], "127.0.0.1", "d" * 12, now=5100)
        self.assertEqual(devices.failed_since(self.dir, 4000), 4)
        self.assertEqual(devices.failed_since(self.dir, 0), 5)
        self.assertEqual(devices.failed_since(self.dir / "нет", 0), 0)

    def test_clear_and_garbage(self):
        devices.record_login(self.dir, True, UA["iphone"], "127.0.0.1", "a" * 12)
        devices.clear_logins(self.dir)
        devices.clear_logins(self.dir)                                                       # повторно безвредно
        self.assertEqual(devices.recent_logins(self.dir), [])
        (self.dir / "logins.json").write_text("{не json")
        self.assertEqual(devices.recent_logins(self.dir), [])
        devices.record_login(self.dir, True, UA["iphone"], "127.0.0.1", "a" * 12)            # битый файл не мешает писать
        (self.dir / "logins.json").write_text('{"a": 1}')
        self.assertEqual(devices.recent_logins(self.dir), [])
        (self.dir / "logins.json").write_text('[1, "x", {"ts": 5, "ok": false, "n": 2}]')
        self.assertEqual(devices.failed_since(self.dir, 0), 2)


class SeenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "seen.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_visit_is_written_and_repeat_within_five_minutes_is_not(self):
        self.assertTrue(devices.touch(self.path, "a" * 12, UA["iphone"], "127.0.0.1", now=1000))
        m = self.path.stat().st_mtime_ns
        self.assertFalse(devices.touch(self.path, "a" * 12, UA["iphone"], "127.0.0.1", now=1000 + devices.SEEN_EVERY - 1))
        self.assertEqual(self.path.stat().st_mtime_ns, m)                                      # лишней записи нет
        self.assertTrue(devices.touch(self.path, "a" * 12, UA["iphone"], "127.0.0.1", now=1000 + devices.SEEN_EVERY))
        row = devices.seen_list(self.path)[0]
        self.assertEqual((row["dev"], row["where"], row["ts"], row["first"]), ("iPhone · Safari", "internet", 1000 + devices.SEEN_EVERY, 1000))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_a_changed_browser_label_is_written_at_once(self):
        devices.touch(self.path, "a" * 12, UA["android"], "127.0.0.1", now=1000)
        self.assertTrue(devices.touch(self.path, "a" * 12, UA["linux_chrome"], "127.0.0.1", now=1001))

    def test_no_id_means_no_tracking(self):
        self.assertFalse(devices.touch(self.path, None, UA["iphone"], "127.0.0.1"))
        self.assertFalse(self.path.exists())

    def test_list_is_newest_first_and_capped(self):
        for i in range(devices.MAX_SEEN + 20):
            devices.touch(self.path, f"{i:012x}", UA["iphone"], "127.0.0.1", now=1000 + i)
        rows = devices.seen_list(self.path)
        self.assertEqual(len(rows), devices.MAX_SEEN)
        self.assertEqual(rows[0]["ts"], 1000 + devices.MAX_SEEN + 19)
        self.assertGreaterEqual(rows[0]["ts"], rows[-1]["ts"])

    def test_garbage_files(self):
        self.path.write_text("{не json")
        self.assertEqual(devices.seen_list(self.path), [])
        self.assertTrue(devices.touch(self.path, "a" * 12, UA["iphone"], "127.0.0.1", now=5))
        self.path.write_text("[1,2]")
        self.assertEqual(devices.seen_list(self.path), [])
        self.assertEqual(devices.seen_list(self.path.parent / "нет"), [])


if __name__ == "__main__":
    unittest.main()
