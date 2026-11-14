"""Веб-пуш: уведомления на телефон, зашифрованные для самого телефона (Google и Apple прочитать текст не могут).

Стандарты: RFC 8030 (доставка), RFC 8291 (шифрование aes128gcm), RFC 8292 (подпись VAPID). Нужен только пакет cryptography
(на роутере: python3-cryptography, ставится router/push.sh); всё остальное из стандартной библиотеки.

Что хранится (data/push.json, права 600): ключи VAPID (подпись отправителя), устройства-подписки и настройки каждого устройства
(какие события, тихие часы), очередь событий на время тихих часов. Подписка принимается только от известных служб пушей
(Google, Apple, Mozilla, Microsoft): иначе роутер можно было бы заставить обращаться к чужим адресам.
"""
import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import devices
import notify

FILE, LOCK = "push.json", "push.lock"
MAX_SUBS = 20                    # устройств на семью
MAX_PENDING = 20                 # событий в очереди одного устройства (тихие часы, сбои сети)
MAX_PAYLOAD = 3000               # байт: запись шифрования 4096, с запасом
TTL = 24 * 3600                  # сколько служба пушей держит сообщение, если телефон выключен
DEFAULT_SUBJECT = "mailto:noreply@example.com"      # контакт отправителя в подписи VAPID (Apple требует его наличия)
ALLOWED_HOSTS = {"fcm.googleapis.com", "android.googleapis.com", "web.push.apple.com", "updates.push.services.mozilla.com"}
ALLOWED_SUFFIXES = (".push.apple.com", ".push.services.mozilla.com", ".notify.windows.com")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PERMANENT = {400, 401, 403, 413}     # повторять бесполезно
GONE = {404, 410}                    # подписка больше не существует


def default_prefs():
    return {"types": {t: True for t in notify.TYPES}, "quiet": {"on": False, "from": "22:00", "to": "07:00"}}


# ---------------------------------------------------------------- криптография
def _crypto():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return hashes, serialization, ec, decode_dss_signature, AESGCM


def available():
    try:
        _crypto()
        return True
    except ImportError:
        return False


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text):
    text = str(text)
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


def new_vapid():
    """Пара ключей отправителя: (закрытый, открытый) в виде строк base64url."""
    hashes, ser, ec, _, _ = _crypto()
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.private_numbers().private_value.to_bytes(32, "big")
    pub = key.public_key().public_bytes(ser.Encoding.X962, ser.PublicFormat.UncompressedPoint)
    return {"private": b64u(raw), "public": b64u(pub)}


def encrypt(payload, ua_public, auth, salt=None, as_private=None):
    """Шифрует сообщение для телефона (RFC 8291, aes128gcm). ua_public и auth: ключи подписки телефона (base64url)."""
    hashes, ser, ec, _, AESGCM = _crypto()
    ua_bytes = b64d(ua_public)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_bytes)
    as_key = as_private or ec.generate_private_key(ec.SECP256R1())
    as_pub = as_key.public_key().public_bytes(ser.Encoding.X962, ser.PublicFormat.UncompressedPoint)
    ecdh = as_key.exchange(ec.ECDH(), ua_key)
    prk_key = _hmac(b64d(auth), ecdh)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_bytes + as_pub + b"\x01")
    salt = salt or os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    ct = AESGCM(cek).encrypt(nonce, bytes(payload) + b"\x02", None)             # 0x02: последняя (и единственная) запись
    return salt + struct.pack(">I", 4096) + bytes([len(as_pub)]) + as_pub + ct


def vapid_authorization(endpoint, vapid, subject=DEFAULT_SUBJECT, now=None):
    """Заголовок Authorization: подпись, по которой служба пушей видит, что сообщение от нашего роутера (RFC 8292)."""
    hashes, ser, ec, decode_dss_signature, _ = _crypto()
    u = urlparse(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(now if now is not None else time.time()) + 12 * 3600, "sub": subject}
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "." + b64u(json.dumps(claims, separators=(",", ":")).encode())
    key = ec.derive_private_key(int.from_bytes(b64d(vapid["private"]), "big"), ec.SECP256R1())
    r, s = decode_dss_signature(key.sign(head.encode(), ec.ECDSA(hashes.SHA256())))
    return f"vapid t={head}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}, k={vapid['public']}"


# ---------------------------------------------------------------- хранение
def _path(data_dir):
    return Path(data_dir) / FILE


@contextlib.contextmanager
def _locked(data_dir):
    """Одновременно пишут и обновление данных (cron), и страница (настройки): блокировка, чтобы не потерять записи."""
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    f = open(Path(data_dir) / LOCK, "a+")
    try:
        try:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        f.close()


def load(data_dir):
    try:
        st = json.loads(_path(data_dir).read_text(encoding="utf-8"))
        assert isinstance(st, dict)
    except (OSError, ValueError, AssertionError):
        st = {}
    st.setdefault("subs", {})
    if not isinstance(st["subs"], dict):
        st["subs"] = {}
    return st


def save(data_dir, st):
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, p)


def ensure_vapid(st):
    v = st.get("vapid")
    if not (isinstance(v, dict) and v.get("private") and v.get("public")):
        st["vapid"] = new_vapid()
    return st["vapid"]


def sub_id(endpoint):
    return hashlib.sha1(endpoint.encode("utf-8")).hexdigest()[:16]


def endpoint_ok(url):
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return (isinstance(url, str) and len(url) <= 1000 and u.scheme == "https" and u.port in (None, 443) and not u.username and not u.password
            and (host in ALLOWED_HOSTS or host.endswith(ALLOWED_SUFFIXES)))


def clean_prefs(p, base=None):
    """Настройки устройства: только известные поля и правильные значения, остальное берётся из прежних или по умолчанию."""
    out = default_prefs() if base is None else json.loads(json.dumps(base))
    if isinstance(p, dict):
        if isinstance(p.get("types"), dict):
            for t in notify.TYPES:
                if t in p["types"]:
                    out["types"][t] = bool(p["types"][t])
        q = p.get("quiet")
        if isinstance(q, dict):
            if "on" in q:
                out["quiet"]["on"] = bool(q["on"])
            for k in ("from", "to"):
                if isinstance(q.get(k), str) and HHMM.match(q[k]):
                    out["quiet"][k] = q[k]
    return out


def in_quiet(prefs, now):
    q = prefs.get("quiet") or {}
    if not q.get("on"):
        return False
    lt = time.localtime(now)
    cur = lt.tm_hour * 60 + lt.tm_min
    a, b = (int(x[:2]) * 60 + int(x[3:]) for x in (q["from"], q["to"]))
    return a <= cur < b if a < b else (cur >= a or cur < b) if a > b else False


SERVICES = (("Apple", ("web.push.apple.com",), (".push.apple.com",)), ("Google", ("fcm.googleapis.com", "android.googleapis.com"), ()),
            ("Mozilla", ("updates.push.services.mozilla.com",), (".push.services.mozilla.com",)), ("Microsoft", (), (".notify.windows.com",)))


def service_name(endpoint):
    host = (urlparse(endpoint).hostname or "").lower()
    for name, exact, suffixes in SERVICES:
        if host in exact or (suffixes and host.endswith(suffixes)):
            return name
    return "неизвестная"


def clean_name(name):
    """Название устройства, которое вводит человек: без разметки и управляющих символов, не длиннее 30."""
    return re.sub(r"[\x00-\x1f\x7f<>&\"]", "", name).strip()[:30] if isinstance(name, str) else ""


def describe(data_dir):
    """Подключённые устройства для списка в LuCI. Адрес устройства (в нём секретный токен) не отдаётся."""
    st = load(data_dir)
    out = []
    for sid, s in st["subs"].items():
        types = [t for t, on in (s.get("prefs") or {}).get("types", {}).items() if on]
        out.append({"id": sid, "name": s.get("name") or "", "label": s.get("label") or devices.device_label(s.get("ua")), "service": service_name(s.get("endpoint", "")),
                    "created": s.get("created") or 0, "pending": len(s.get("pending") or []), "fail": s.get("fail", 0), "types": types,
                    "quiet": bool(((s.get("prefs") or {}).get("quiet") or {}).get("on"))})
    return sorted(out, key=lambda r: r["created"])


def remove_sub(data_dir, sid):
    """Отключает устройство от уведомлений (по номеру из списка). True, если такое было."""
    with _locked(data_dir):
        st = load(data_dir)
        if sid not in st["subs"]:
            return False
        del st["subs"][sid]
        save(data_dir, st)
        return True


def status(data_dir):
    """Для проверки «Проверить всё»: без криптографии, только что записано в файле."""
    st = load(data_dir)
    return {"devices": len(st["subs"]), "keys": bool(st.get("vapid")), "last": st.get("last") or {}}


# ---------------------------------------------------------------- отправка
def default_post(url, headers, body, timeout=15):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, (e.read(300) or b"").decode("utf-8", "replace")


def message(m):
    """Что увидит человек: короткий JSON (его читает сервис-воркер страницы)."""
    data = {"title": str(m.get("title") or "Мой дневник")[:120], "body": str(m.get("body") or "")[:300], "tag": str(m.get("tag") or m.get("id") or "dnevnik")[:40], "url": m.get("url") or "./"}
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return raw if len(raw) <= MAX_PAYLOAD else json.dumps(dict(data, body=data["body"][:100]), ensure_ascii=False).encode("utf-8")


def send_one(sub, m, vapid, subject, now, post):
    """Одно сообщение одному устройству. Возвращает код ответа службы пушей (None: сети нет)."""
    body = encrypt(message(m), sub["p256dh"], sub["auth"])
    headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(TTL), "Urgency": "normal",
               "Authorization": vapid_authorization(sub["endpoint"], vapid, subject, now)}
    try:
        return post(sub["endpoint"], headers, body)[0]
    except Exception:
        return None


def digest(events):
    """Если событий больше трёх, одно сообщение вместо россыпи."""
    if len(events) <= 3:
        return list(events)
    titles = "; ".join(e["title"] for e in events[:3])
    return [{"id": "digest" + str(len(events)), "title": f"Что нового в дневнике: {len(events)}", "body": titles + "…", "url": "./"}]


def deliver(data_dir, events, now=None, post=None, only=None):
    """Рассылает события устройствам с учётом их настроек и тихих часов. Исключение, если нет модуля шифрования."""
    if not available():
        raise RuntimeError("нет пакета cryptography")
    now = now if now is not None else time.time()
    post = post or default_post
    stats = {"sent": 0, "failed": 0, "removed": 0, "queued": 0}
    with _locked(data_dir):
        st = load(data_dir)
        if not st["subs"]:
            return stats
        vapid = ensure_vapid(st)
        subject = st.get("subject") or DEFAULT_SUBJECT
        for sid, sub in list(st["subs"].items()):
            if only and sid != only:
                continue
            prefs = sub.get("prefs") or default_prefs()
            mine = [e for e in events if prefs["types"].get(e.get("type"), True)]
            pending = (sub.get("pending") or []) + mine
            if not pending:
                continue
            if in_quiet(prefs, now) and not only:                # кнопка «Проверить уведомление» (only) тихие часы не учитывает
                sub["pending"] = pending[-MAX_PENDING:]
                stats["queued"] += len(mine)
                continue
            keep = False
            for m in digest(pending):
                code = send_one(sub, m, vapid, subject, now, post)
                if code in (200, 201, 202):
                    stats["sent"] += 1
                    sub["fail"] = 0
                elif code in GONE:
                    del st["subs"][sid]
                    stats["removed"] += 1
                    break
                elif code in PERMANENT:
                    stats["failed"] += 1
                    st["last_error"] = f"служба пушей ответила {code}"
                else:                                      # сеть или временный сбой: повторим при следующем обновлении
                    stats["failed"] += 1
                    sub["fail"] = sub.get("fail", 0) + 1
                    keep = True
                    break
            else:
                pass
            if sid in st["subs"]:
                sub["pending"] = pending[-MAX_PENDING:] if keep else []
        st["last"] = {"ts": int(now), **stats}
        save(data_dir, st)
    return stats


# ---------------------------------------------------------------- запросы страницы
TEST_EVENT = {"id": "test", "type": "test", "title": "Проверка уведомления", "body": "Если вы это видите, уведомления работают.", "tag": "test", "url": "./"}


def handle(data_dir, payload, now=None, post=None):
    """Один запрос к /cgi-bin/push. Возвращает (код HTTP, ответ-словарь). Проверку пароля семьи делает вызывающий."""
    if not isinstance(payload, dict):
        return 400, {"ok": False, "message": "Не удалось прочитать запрос."}
    action = payload.get("action")
    now = now if now is not None else time.time()
    if action == "config":
        if not available():
            return 200, {"ok": True, "ready": False, "publicKey": "",
                         "message": "На роутере не установлен модуль уведомлений. Выполните: sh /root/dnevnik/router/push.sh"}
        with _locked(data_dir):
            st = load(data_dir)
            had = bool(st.get("vapid"))
            vapid = ensure_vapid(st)
            if not had:
                save(data_dir, st)
        return 200, {"ok": True, "ready": True, "publicKey": vapid["public"]}
    if action == "events":
        return 200, {"ok": True, "events": notify.recent(data_dir, max(1, min(int(payload.get("limit") or 20), 40)) if str(payload.get("limit") or "20").isdigit() else 20)}
    endpoint = payload.get("endpoint")
    if action == "subscribe":
        s = payload.get("subscription") if isinstance(payload.get("subscription"), dict) else {}
        endpoint, keys = s.get("endpoint"), s.get("keys") if isinstance(s.get("keys"), dict) else {}
        if not isinstance(endpoint, str) or not endpoint_ok(endpoint):
            return 400, {"ok": False, "message": "Этот адрес службы уведомлений не поддерживается."}
        try:
            ua = b64d(keys.get("p256dh", ""))
            au = b64d(keys.get("auth", ""))
            assert len(ua) == 65 and ua[0] == 4 and len(au) == 16
        except Exception:
            return 400, {"ok": False, "message": "Ключи устройства выглядят неправильно."}
        with _locked(data_dir):
            st = load(data_dir)
            sid = sub_id(endpoint)
            if sid not in st["subs"] and len(st["subs"]) >= MAX_SUBS:
                return 400, {"ok": False, "message": f"Уже подключено {MAX_SUBS} устройств: отключите ненужные."}
            old = st["subs"].get(sid) or {}
            prefs = clean_prefs(payload.get("prefs"), old.get("prefs"))
            ua_full = str(payload.get("ua") or "")[:300]
            name = clean_name(payload["name"]) if "name" in payload else (old.get("name") or "")
            st["subs"][sid] = {"endpoint": endpoint, "p256dh": keys["p256dh"], "auth": keys["auth"], "prefs": prefs, "pending": [],
                               "created": old.get("created") or int(now), "ua": re.sub(r"[^\w ./()-]", "", ua_full)[:60],
                               "label": devices.device_label(ua_full) if ua_full else (old.get("label") or ""), "name": name}
            save(data_dir, st)
        return 200, {"ok": True, "id": sid, "prefs": prefs}
    if not isinstance(endpoint, str):
        return 400, {"ok": False, "message": "Не указано устройство."}
    sid = sub_id(endpoint)
    with _locked(data_dir):
        st = load(data_dir)
        sub = st["subs"].get(sid)
        if action == "get":
            return 200, {"ok": True, "subscribed": bool(sub), "prefs": (sub or {}).get("prefs") or default_prefs(), "name": (sub or {}).get("name") or ""}
        if not sub:
            return 404, {"ok": False, "message": "Это устройство не подключено к уведомлениям."}
        if action == "update":
            if "prefs" in payload:
                sub["prefs"] = clean_prefs(payload.get("prefs"), sub.get("prefs"))
            if "name" in payload:
                sub["name"] = clean_name(payload.get("name"))
            save(data_dir, st)
            return 200, {"ok": True, "prefs": sub["prefs"], "name": sub.get("name") or ""}
        if action == "unsubscribe":
            del st["subs"][sid]
            save(data_dir, st)
            return 200, {"ok": True}
    if action == "test":
        if not available():
            return 200, {"ok": False, "message": "На роутере не установлен модуль уведомлений."}
        stats = deliver(data_dir, [TEST_EVENT], now=now, post=post, only=sid)
        if stats["sent"]:
            return 200, {"ok": True, "message": "Отправлено. Уведомление придёт на телефон в течение минуты."}
        if stats["removed"]:
            return 200, {"ok": False, "message": "Служба уведомлений больше не знает это устройство: отключите и включите уведомления заново."}
        return 200, {"ok": False, "message": "Не получилось отправить: нет связи со службой уведомлений. Попробуйте позже."}
    return 400, {"ok": False, "message": "Неизвестное действие."}
