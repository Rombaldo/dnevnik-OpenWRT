"""Веб-пуш: шифрование по RFC 8291, подпись VAPID, подписки, настройки, тихие часы, отправка (без настоящих служб пушей)."""
import json
import os
import stat
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import notify
import push

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAVE = True
except ImportError:  # pragma: no cover
    HAVE = False

FCM = "https://fcm.googleapis.com/fcm/send/abc123:APA91bExampleToken"
APPLE = "https://web.push.apple.com/QAbCdEf"
MOZ = "https://updates.push.services.mozilla.com/wpush/v2/gAAAAAB"


def b64u(b):
    return push.b64u(b)


def device():
    """Подставной телефон: пара ключей и секрет авторизации, как у настоящей подписки."""
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, pub, os.urandom(16)


def sub_json(endpoint, d):
    key, pub, auth = d
    return {"endpoint": endpoint, "keys": {"p256dh": b64u(pub), "auth": b64u(auth)}}


def decrypt(body, ua_key, ua_pub, auth):
    """Расшифровка на стороне телефона, написанная отдельно от encrypt() прямо по тексту RFC 8291."""
    import hmac as hm
    import hashlib
    salt, rs, idlen = body[:16], struct.unpack(">I", body[16:20])[0], body[20]
    as_pub, ct = body[21:21 + idlen], body[21 + idlen:]
    assert rs == 4096 and idlen == 65
    ecdh = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    H = lambda k, d: hm.new(k, d, hashlib.sha256).digest()
    ikm = H(H(auth, ecdh), b"WebPush: info\x00" + ua_pub + as_pub + b"\x01")
    prk = H(salt, ikm)
    cek, nonce = H(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16], H(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    plain = AESGCM(cek).decrypt(nonce, ct, None)
    assert plain[-1] == 2
    return plain[:-1]


@unittest.skipUnless(HAVE, "нужен пакет cryptography")
class EncryptionTests(unittest.TestCase):
    def test_rfc_8291_worked_example_matches_byte_for_byte(self):
        """Пример из раздела 5 RFC 8291: если совпало, настоящий телефон расшифрует то, что мы отправим."""
        want = ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")
        as_key = ec.derive_private_key(int.from_bytes(push.b64d("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
        got = push.encrypt(b"When I grow up, I want to be a watermelon", "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                           "BTBZMqHH6r4Tts7J_aSIgg", salt=push.b64d("DGv6ra1nlYgDCS1FRnbzlw"), as_private=as_key)
        self.assertEqual(b64u(got), want)
        ua_key = ec.derive_private_key(int.from_bytes(push.b64d("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"), "big"), ec.SECP256R1())
        self.assertEqual(decrypt(got, ua_key, push.b64d("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
                                 push.b64d("BTBZMqHH6r4Tts7J_aSIgg")), b"When I grow up, I want to be a watermelon")

    def test_round_trip_for_different_sizes_and_russian_text(self):
        ua_key, ua_pub, auth = device()
        for text in ("а", "Новая оценка: 5", "я" * 1000, "😀 " * 200):
            body = push.encrypt(text.encode("utf-8"), b64u(ua_pub), b64u(auth))
            self.assertEqual(decrypt(body, ua_key, ua_pub, auth).decode("utf-8"), text)

    def test_every_message_uses_a_new_salt_and_key(self):
        ua_key, ua_pub, auth = device()
        a, b = (push.encrypt(b"x", b64u(ua_pub), b64u(auth)) for _ in range(2))
        self.assertNotEqual(a[:16], b[:16])
        self.assertNotEqual(a[21:86], b[21:86])
        self.assertNotEqual(a, b)

    def test_wrong_keys_fail_to_decrypt(self):
        ua_key, ua_pub, auth = device()
        body = push.encrypt("секрет".encode("utf-8"), b64u(ua_pub), b64u(auth))
        other_key, other_pub, other_auth = device()
        for k, p, a in ((other_key, other_pub, auth), (ua_key, ua_pub, other_auth)):
            with self.assertRaises(Exception):
                decrypt(body, k, p, a)
        self.assertNotIn("секрет".encode(), body)                                       # открытого текста в сообщении нет

    def test_bad_device_keys_raise(self):
        for bad in ("", "AAAA", b64u(b"\x04" + b"\x00" * 64)):
            with self.assertRaises(Exception):
                push.encrypt(b"x", bad, b64u(os.urandom(16)))

    def test_message_json_is_short_and_has_the_fields_the_service_worker_reads(self):
        raw = push.message({"title": "Т" * 500, "body": "Б" * 5000, "id": "abc", "url": "./"})
        d = json.loads(raw)
        self.assertEqual(set(d), {"title", "body", "tag", "url"})
        self.assertLessEqual(len(raw), push.MAX_PAYLOAD)
        self.assertEqual(d["tag"], "abc")


@unittest.skipUnless(HAVE, "нужен пакет cryptography")
class VapidTests(unittest.TestCase):
    def test_keys_are_valid_and_unique(self):
        a, b = push.new_vapid(), push.new_vapid()
        self.assertNotEqual(a["private"], b["private"])
        self.assertEqual(len(push.b64d(a["public"])), 65)
        self.assertEqual(push.b64d(a["public"])[0], 4)
        self.assertEqual(len(push.b64d(a["private"])), 32)

    def test_authorization_header_is_a_valid_signed_jwt_for_the_service_origin_only(self):
        v = push.new_vapid()
        h = push.vapid_authorization(FCM, v, "mailto:a@example.com", now=1_800_000_000)
        self.assertTrue(h.startswith("vapid t="))
        token, _, k = h[len("vapid t="):].partition(", k=")
        self.assertEqual(k, v["public"])
        head, claims, sig = token.split(".")
        self.assertEqual(json.loads(push.b64d(head)), {"typ": "JWT", "alg": "ES256"})
        c = json.loads(push.b64d(claims))
        self.assertEqual(c, {"aud": "https://fcm.googleapis.com", "exp": 1_800_000_000 + 12 * 3600, "sub": "mailto:a@example.com"})   # путь с токеном устройства в подпись не попал
        raw = push.b64d(sig)
        self.assertEqual(len(raw), 64)
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push.b64d(v["public"]))
        pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")), f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))

    def test_tampered_token_does_not_verify(self):
        v = push.new_vapid()
        token = push.vapid_authorization(APPLE, v)[len("vapid t="):].split(", k=")[0]
        head, claims, sig = token.split(".")
        raw = push.b64d(sig)
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push.b64d(v["public"]))
        with self.assertRaises(Exception):
            pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")), f"{head}.{claims}x".encode(), ec.ECDSA(hashes.SHA256()))


class ValidationTests(unittest.TestCase):
    def test_endpoints(self):
        for ok in (FCM, APPLE, MOZ, "https://web.push.apple.com/x", "https://wns2-par02p.notify.windows.com/w/?token=1", "https://android.googleapis.com/gcm/send/x"):
            self.assertTrue(push.endpoint_ok(ok), ok)
        for bad in ("http://fcm.googleapis.com/x", "https://evil.example.com/x", "https://fcm.googleapis.com.evil.com/x", "https://evilfcm.googleapis.com/x",
                    "https://xpush.apple.com/x", "https://fcm.googleapis.com:8443/x", "https://user:pw@fcm.googleapis.com/x", "https://127.0.0.1/x",
                    "https://localhost/x", "ftp://fcm.googleapis.com/x", "", "javascript:alert(1)", "https://fcm.googleapis.com/" + "a" * 1100, "//fcm.googleapis.com/x"):
            self.assertFalse(push.endpoint_ok(bad), bad)

    def test_prefs_are_sanitised(self):
        p = push.clean_prefs({"types": {"mark": False, "bogus": True, "homework": "да"}, "quiet": {"on": 1, "from": "25:99", "to": "06:30", "x": 1}, "evil": "<script>"})
        self.assertEqual(p["types"], {"mark": False, "homework": True, "schedule": True, "attendance": True})
        self.assertEqual(p["quiet"], {"on": True, "from": "22:00", "to": "06:30"})           # плохое время отброшено, прежнее осталось
        self.assertNotIn("evil", p)
        self.assertEqual(push.clean_prefs("мусор"), push.default_prefs())
        self.assertEqual(push.clean_prefs({"types": {"mark": False}}, base={"types": {"mark": True, "homework": False, "schedule": True, "attendance": True}, "quiet": {"on": False, "from": "21:00", "to": "08:00"}})["types"]["homework"], False)

    def test_quiet_hours(self):
        def at(h, m):
            return time.mktime((2026, 11, 12, h, m, 0, 0, 0, -1))
        night = {"quiet": {"on": True, "from": "22:00", "to": "07:00"}}
        for h, m, want in ((21, 59, False), (22, 0, True), (23, 30, True), (0, 0, True), (6, 59, True), (7, 0, False), (12, 0, False)):
            self.assertEqual(push.in_quiet(night, at(h, m)), want, (h, m))
        day = {"quiet": {"on": True, "from": "13:00", "to": "15:00"}}
        self.assertTrue(push.in_quiet(day, at(14, 0)))
        self.assertFalse(push.in_quiet(day, at(15, 0)))
        self.assertFalse(push.in_quiet({"quiet": {"on": False, "from": "00:00", "to": "23:59"}}, at(3, 0)))
        self.assertFalse(push.in_quiet({"quiet": {"on": True, "from": "10:00", "to": "10:00"}}, at(10, 0)))      # одинаковое время: тихих часов нет
        self.assertFalse(push.in_quiet({}, at(3, 0)))


@unittest.skipUnless(HAVE, "нужен пакет cryptography")
class PushBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.sent = []
        self.code = 201
        self.devices = {}

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, url, headers, body, timeout=15):
        if isinstance(self.code, Exception):
            raise self.code
        code = self.code(url) if callable(self.code) else self.code
        self.sent.append((url, headers, body))
        return code, ""

    def add(self, endpoint=FCM, prefs=None):
        d = device()
        self.devices[endpoint] = d
        code, body = push.handle(self.dir, {"action": "subscribe", "subscription": sub_json(endpoint, d), "prefs": prefs, "ua": "Android Chrome"})
        self.assertEqual(code, 200, body)
        return push.sub_id(endpoint)

    def read(self, endpoint, i=-1):
        url, headers, body = [s for s in self.sent if s[0] == endpoint][i]
        key, pub, auth = self.devices[endpoint]
        return json.loads(decrypt(body, key, pub, auth)), headers

    def ev(self, kind="mark", n=1, **kw):
        return [{"id": f"{kind}{i}", "type": kind, "title": f"Заголовок {i}", "body": f"Текст {i}", "tag": kind, "url": "./", **kw} for i in range(n)]


class HandleTests(PushBase):
    def test_config_creates_keys_once_and_stores_them_privately(self):
        code, a = push.handle(self.dir, {"action": "config"})
        self.assertEqual((code, a["ready"]), (200, True))
        self.assertEqual(len(push.b64d(a["publicKey"])), 65)
        self.assertEqual(push.handle(self.dir, {"action": "config"})[1]["publicKey"], a["publicKey"])            # ключ не меняется: иначе подписки сломались бы
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / "push.json").st_mode), 0o600)
        self.assertNotIn("private", json.dumps(a))                                    # закрытый ключ страница не получает

    def test_config_without_the_module_says_what_to_do(self):
        with mock.patch.object(push, "available", return_value=False):
            code, a = push.handle(self.dir, {"action": "config"})
            self.assertFalse(a["ready"])
            self.assertIn("push.sh", a["message"])
            self.assertEqual(push.handle(self.dir, {"action": "test", "endpoint": FCM})[1]["ok"], False)
        self.assertFalse((self.dir / "push.json").exists())

    def test_subscribe_update_get_unsubscribe(self):
        sid = self.add(prefs={"types": {"homework": False}})
        st = push.load(self.dir)
        self.assertEqual(st["subs"][sid]["ua"], "Android Chrome")
        self.assertFalse(st["subs"][sid]["prefs"]["types"]["homework"])
        self.assertTrue(st["subs"][sid]["prefs"]["types"]["mark"])
        code, b = push.handle(self.dir, {"action": "get", "endpoint": FCM})
        self.assertEqual((b["subscribed"], b["prefs"]["types"]["homework"]), (True, False))
        code, b = push.handle(self.dir, {"action": "update", "endpoint": FCM, "prefs": {"types": {"homework": True}, "quiet": {"on": True, "from": "21:30", "to": "07:30"}}})
        self.assertEqual((code, b["prefs"]["quiet"]), (200, {"on": True, "from": "21:30", "to": "07:30"}))
        self.assertEqual(push.handle(self.dir, {"action": "unsubscribe", "endpoint": FCM})[0], 200)
        self.assertEqual(push.handle(self.dir, {"action": "get", "endpoint": FCM})[1]["subscribed"], False)
        self.assertEqual(push.handle(self.dir, {"action": "update", "endpoint": FCM, "prefs": {}})[0], 404)

    def test_subscribing_again_keeps_settings_and_does_not_duplicate(self):
        self.add(prefs={"types": {"mark": False}})
        self.add()                                                                    # то же устройство подписалось заново
        st = push.load(self.dir)
        self.assertEqual(len(st["subs"]), 1)
        self.assertFalse(list(st["subs"].values())[0]["prefs"]["types"]["mark"])

    def test_bad_subscriptions_are_refused(self):
        d = device()
        good = sub_json(FCM, d)
        cases = [{}, {"subscription": "x"}, {"subscription": dict(good, endpoint="https://evil.example.com/x")}, {"subscription": dict(good, endpoint="http://fcm.googleapis.com/x")},
                 {"subscription": {"endpoint": FCM, "keys": {"p256dh": "AAAA", "auth": good["keys"]["auth"]}}}, {"subscription": {"endpoint": FCM, "keys": {"p256dh": good["keys"]["p256dh"], "auth": "AAAA"}}},
                 {"subscription": {"endpoint": FCM}}, {"subscription": {"endpoint": 5, "keys": good["keys"]}}]
        for c in cases:
            code, b = push.handle(self.dir, dict(c, action="subscribe"))
            self.assertEqual(code, 400, c)
            self.assertFalse(b["ok"])
        self.assertEqual(push.load(self.dir)["subs"], {})

    def test_device_limit(self):
        for i in range(push.MAX_SUBS):
            self.add(f"https://fcm.googleapis.com/fcm/send/dev{i}")
        code, b = push.handle(self.dir, {"action": "subscribe", "subscription": sub_json(FCM, device())})
        self.assertEqual(code, 400)
        self.assertIn(str(push.MAX_SUBS), b["message"])
        self.add("https://fcm.googleapis.com/fcm/send/dev0")                          # уже подключённое устройство обновить можно

    def test_user_agent_text_is_cleaned(self):
        d = device()
        push.handle(self.dir, {"action": "subscribe", "subscription": sub_json(FCM, d), "ua": "<script>alert(1)</script> Chrome/120 " + "x" * 200})
        ua = list(push.load(self.dir)["subs"].values())[0]["ua"]
        self.assertNotIn("<", ua)
        self.assertLessEqual(len(ua), 60)

    def test_unknown_and_malformed_requests(self):
        for p in (None, [], "x", {"action": "format"}, {}, {"action": "get"}, {"action": "update", "endpoint": 5}):
            self.assertEqual(push.handle(self.dir, p)[0], 400, p)

    def test_events_action_returns_the_log(self):
        notify._write_private(self.dir / "events.json", json.dumps([{"id": str(i), "title": "t"} for i in range(30)]))
        self.assertEqual(len(push.handle(self.dir, {"action": "events"})[1]["events"]), 20)
        self.assertEqual(len(push.handle(self.dir, {"action": "events", "limit": 5})[1]["events"]), 5)
        self.assertEqual(len(push.handle(self.dir, {"action": "events", "limit": "мусор"})[1]["events"]), 20)

    def test_test_button_sends_even_if_every_type_is_off_and_it_is_quiet_time(self):
        self.add(prefs={"types": {t: False for t in notify.TYPES}, "quiet": {"on": True, "from": "00:00", "to": "23:59"}})
        code, b = push.handle(self.dir, {"action": "test", "endpoint": FCM}, post=self.post, now=time.mktime((2026, 11, 12, 12, 0, 0, 0, 0, -1)))
        self.assertTrue(b["ok"], b)
        msg, _ = self.read(FCM)
        self.assertEqual(msg["title"], "Проверка уведомления")

    def test_test_button_reports_dead_and_unreachable_devices(self):
        self.add()
        self.code = 410
        self.assertIn("отключите и включите", push.handle(self.dir, {"action": "test", "endpoint": FCM}, post=self.post)[1]["message"])
        self.add()
        self.code = ConnectionError("нет сети")
        self.assertIn("нет связи", push.handle(self.dir, {"action": "test", "endpoint": FCM}, post=self.post)[1]["message"])

    def test_concurrent_subscriptions_are_not_lost(self):
        ths = [threading.Thread(target=lambda i=i: push.handle(self.dir, {"action": "subscribe", "subscription": sub_json(f"https://fcm.googleapis.com/fcm/send/t{i}", device())})) for i in range(12)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        self.assertEqual(len(push.load(self.dir)["subs"]), 12)


class DeliverTests(PushBase):
    def test_message_is_encrypted_for_the_device_and_signed(self):
        self.add()
        stats = push.deliver(self.dir, self.ev("mark", 1), post=self.post)
        self.assertEqual(stats["sent"], 1)
        msg, h = self.read(FCM)
        self.assertEqual((msg["title"], msg["body"], msg["url"]), ("Заголовок 0", "Текст 0", "./"))
        self.assertEqual(h["Content-Encoding"], "aes128gcm")
        self.assertEqual(h["TTL"], str(push.TTL))
        self.assertTrue(h["Authorization"].startswith("vapid t="))
        vapid_public = push.load(self.dir)["vapid"]["public"]
        self.assertTrue(h["Authorization"].endswith(f"k={vapid_public}"))
        self.assertNotIn("Заголовок".encode(), self.sent[0][2])                       # в сети открытого текста нет

    def test_each_device_gets_it_encrypted_with_its_own_key(self):
        self.add(FCM)
        self.add(APPLE)
        push.deliver(self.dir, self.ev("mark", 1), post=self.post)
        self.assertEqual({u for u, _, _ in self.sent}, {FCM, APPLE})
        self.assertEqual(self.read(FCM)[0]["title"], "Заголовок 0")
        self.assertEqual(self.read(APPLE)[0]["title"], "Заголовок 0")
        with self.assertRaises(Exception):
            key, pub, auth = self.devices[FCM]
            decrypt([b for u, _, b in self.sent if u == APPLE][0], key, pub, auth)     # чужим ключом не открывается

    def test_events_are_filtered_by_the_device_settings(self):
        self.add(FCM, prefs={"types": {"homework": False, "schedule": False, "attendance": False}})
        self.add(APPLE, prefs={"types": {"mark": False}})
        events = self.ev("mark", 1) + self.ev("homework", 1) + self.ev("schedule", 1) + self.ev("attendance", 1)
        push.deliver(self.dir, events, post=self.post)
        got = {u: sorted(json.dumps(self.read(u, i)[0], ensure_ascii=False) for i in range(len([s for s in self.sent if s[0] == u]))) for u in (FCM, APPLE)}
        self.assertEqual(len([s for s in self.sent if s[0] == FCM]), 1)               # у первого только оценки
        self.assertEqual(len([s for s in self.sent if s[0] == APPLE]), 3)             # у второго всё, кроме оценок

    def test_nothing_to_send_means_no_requests(self):
        self.add(prefs={"types": {t: False for t in notify.TYPES}})
        self.assertEqual(push.deliver(self.dir, self.ev("mark", 2), post=self.post)["sent"], 0)
        self.assertEqual(self.sent, [])

    def test_no_devices_writes_nothing(self):
        self.assertEqual(push.deliver(self.dir, self.ev("mark", 1), post=self.post), {"sent": 0, "failed": 0, "removed": 0, "queued": 0})
        self.assertFalse((self.dir / "push.json").exists())

    def test_more_than_three_events_become_one_digest(self):
        self.add()
        push.deliver(self.dir, self.ev("mark", 6), post=self.post)
        self.assertEqual(len(self.sent), 1)
        msg, _ = self.read(FCM)
        self.assertEqual(msg["title"], "Что нового в дневнике: 6")
        self.assertIn("Заголовок 0", msg["body"])

    def test_quiet_hours_hold_events_and_deliver_them_after(self):
        self.add(prefs={"quiet": {"on": True, "from": "22:00", "to": "07:00"}})
        night, morning = (time.mktime((2026, 11, 12, h, 0, 0, 0, 0, -1)) for h in (23, 8))
        stats = push.deliver(self.dir, self.ev("mark", 3), now=night, post=self.post)
        self.assertEqual((stats["sent"], stats["queued"], self.sent), (0, 3, []))
        self.assertEqual(len(list(push.load(self.dir)["subs"].values())[0]["pending"]), 3)
        stats = push.deliver(self.dir, self.ev("homework", 1), now=morning, post=self.post)    # утром приходит всё накопленное одной сводкой
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(self.read(FCM)[0]["title"], "Что нового в дневнике: 4")
        self.assertEqual(list(push.load(self.dir)["subs"].values())[0]["pending"], [])

    def test_quiet_queue_is_capped(self):
        self.add(prefs={"quiet": {"on": True, "from": "00:00", "to": "23:59"}})
        noon = time.mktime((2026, 11, 12, 12, 0, 0, 0, 0, -1))
        for i in range(10):
            push.deliver(self.dir, [dict(e, id=f"{e['id']}-{i}") for e in self.ev("mark", 5)], now=noon, post=self.post)
        self.assertEqual(len(list(push.load(self.dir)["subs"].values())[0]["pending"]), push.MAX_PENDING)

    def test_gone_devices_are_removed(self):
        for code in (404, 410):
            self.setUp()
            self.add(FCM)
            self.add(APPLE)
            self.code = lambda url, c=code: c if url == FCM else 201
            stats = push.deliver(self.dir, self.ev("mark", 1), post=self.post)
            self.assertEqual((stats["removed"], stats["sent"]), (1, 1), code)
            self.assertEqual(list(s["endpoint"] for s in push.load(self.dir)["subs"].values()), [APPLE])

    def test_temporary_failures_keep_the_message_for_the_next_try(self):
        self.add()
        for code in (429, 500, 503, ConnectionError("сеть")):
            self.code = code
            push.deliver(self.dir, [], post=self.post)
        self.code = 503
        stats = push.deliver(self.dir, self.ev("mark", 1), post=self.post)
        sub = list(push.load(self.dir)["subs"].values())[0]
        self.assertEqual((stats["failed"], len(sub["pending"]), sub["fail"]), (1, 1, 1))
        self.code = 201
        stats = push.deliver(self.dir, [], post=self.post)                           # связь вернулась: отправляется оставшееся
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(list(push.load(self.dir)["subs"].values())[0]["pending"], [])
        self.assertEqual(list(push.load(self.dir)["subs"].values())[0]["fail"], 0)

    def test_network_exception_is_not_fatal(self):
        self.add()
        self.code = TimeoutError("таймаут")
        stats = push.deliver(self.dir, self.ev("mark", 1), post=self.post)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(len(list(push.load(self.dir)["subs"].values())[0]["pending"]), 1)

    def test_permanent_errors_drop_the_message_and_record_why(self):
        self.add()
        self.code = 403
        stats = push.deliver(self.dir, self.ev("mark", 1), post=self.post)
        st = push.load(self.dir)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(list(st["subs"].values())[0]["pending"], [])
        self.assertIn("403", st["last_error"])

    def test_last_result_is_saved_for_the_doctor(self):
        self.add()
        push.deliver(self.dir, self.ev("mark", 1), now=1_800_000_000, post=self.post)
        s = push.status(self.dir)
        self.assertEqual((s["devices"], s["keys"], s["last"]["sent"], s["last"]["ts"]), (1, True, 1, 1_800_000_000))
        self.assertEqual(push.status(self.dir / "нет"), {"devices": 0, "keys": False, "last": {}})

    def test_without_the_module_delivery_raises_for_the_caller_to_handle(self):
        with mock.patch.object(push, "available", return_value=False):
            with self.assertRaises(RuntimeError):
                push.deliver(self.dir, self.ev("mark", 1))

    def test_storage_is_private_and_survives_garbage(self):
        self.add()
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / "push.json").st_mode), 0o600)
        (self.dir / "push.json").write_text("{не json")
        self.assertEqual(push.load(self.dir)["subs"], {})
        (self.dir / "push.json").write_text('{"subs": 5}')
        self.assertEqual(push.load(self.dir)["subs"], {})


class IntegrationTests(PushBase):
    """Весь путь: обновление данных нашло оценку, отправлено на подключённое устройство."""

    def test_notify_process_delivers_through_push(self):
        self.add()
        base = {"days": [{"date": "2026-11-12", "weekday": "чт", "lessons": [{"n": 1, "subject": "Математика", "start": "8:00", "end": "8:40", "canceled": False, "homework": "", "files": [], "marks": [], "theme": ""}]}], "years": []}
        notify.process(self.dir, base, today="2026-11-12", deliver=None)
        base["days"][0]["lessons"][0]["marks"] = [{"value": "5", "work": "Ответ на уроке", "created": "2026-11-12T10:00:00"}]
        with mock.patch.object(push, "default_post", self.post):
            events = notify.process(self.dir, base, today="2026-11-12", now=time.mktime((2026, 11, 12, 12, 0, 0, 0, 0, -1)))
        self.assertEqual(len(events), 1)
        msg, _ = self.read(FCM)
        self.assertEqual((msg["title"], msg["body"]), ("Новая оценка: 5", "Математика · Ответ на уроке (12 ноября)"))


if __name__ == "__main__":
    unittest.main()
