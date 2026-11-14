"""Кто заходит в дневник: как называется устройство, откуда зашло, журнал входов и «последний заход».

Устройство определяется по строке браузера (iPhone, Android, Windows… и браузер), откуда зашло: из дома, из интернета (через Funnel роутер
видит запрос как свой собственный, настоящего адреса нет) или через Tailscale. Адреса в журнал не пишутся: только время, тип устройства, откуда и результат.

Хранение:
  data/logins.json        журнал входов по паролю семьи (до 50 записей, неверные попытки подряд сливаются в одну), права 600, на флеш-памяти;
  /tmp/dnevnik-seen.json  «последний заход» каждого устройства, в оперативной памяти (после перезагрузки роутера начинается заново), обновляется не чаще раза в 5 минут.
Устройство опознаётся по случайной метке в cookie dnevnik_dev (она ничего не открывает: пароль в ней не лежит).
"""
import ipaddress
import json
import os
import re
import secrets
import time
from pathlib import Path

LOGINS = "logins.json"
MAX_LOGINS = 50
MAX_SEEN = 100
SEEN_EVERY = 300                 # «последний заход» обновляем не чаще раза в 5 минут: страница запрашивает данные часто, а запись лишняя
MERGE_FAILS = 600                # неверные попытки одного устройства в пределах 10 минут записываются одной строкой со счётчиком
DEV_COOKIE = "dnevnik_dev"
DEV_DAYS = 400
ID_RE = re.compile(r"^[0-9a-f]{12}$")
SEEN_DEFAULT = "/tmp/dnevnik-seen.json"
WHERE_TEXT = {"home": "из дома", "internet": "из интернета", "tailscale": "через Tailscale", "unknown": "неизвестно"}


# ---------------------------------------------------------------- название устройства
def device_label(ua):
    """«iPhone · Safari», «Android (SM-X710) · Chrome» и так далее. Пустая или странная строка: «Устройство · браузер»."""
    u = ua if isinstance(ua, str) else ""
    if "iPhone" in u:
        os_ = "iPhone"
    elif "iPad" in u:
        os_ = "iPad"
    elif "Android" in u:
        m = re.search(r"Android [\d.]+; ([A-Za-z0-9][\w .-]{1,29})[;)]", u)      # модель только из безопасных символов
        model = m.group(1).strip() if m else ""
        os_ = f"Android ({model})" if model and model not in ("K", "Mobile", "Tablet") and not model.startswith(("rv:", "Mobile")) else "Android"
    elif "Windows" in u:
        os_ = "Windows"
    elif "Macintosh" in u or "Mac OS X" in u:
        os_ = "Mac"
    elif "CrOS" in u:
        os_ = "Chromebook"
    elif "Linux" in u or "X11" in u:
        os_ = "Linux"
    else:
        os_ = "Устройство"
    for needle, name in (("EdgA/", "Edge"), ("EdgiOS/", "Edge"), ("Edg/", "Edge"), ("OPR/", "Opera"), ("YaBrowser", "Яндекс Браузер"), ("SamsungBrowser", "Samsung Internet"),
                         ("HuaweiBrowser", "Huawei Browser"), ("FxiOS", "Firefox"), ("Firefox/", "Firefox"), ("CriOS", "Chrome"), ("Chrome/", "Chrome")):
        if needle in u:
            return f"{os_} · {name}"
    if "Safari/" in u and "Version/" in u or "AppleWebKit" in u and "Mobile/" in u:
        return f"{os_} · Safari"
    return f"{os_} · браузер"


def where_from(addr):
    """Откуда пришёл запрос: из дома, из интернета (Funnel приходит с самого роутера) или через Tailscale."""
    try:
        ip = ipaddress.ip_address((addr or "").strip())
    except ValueError:
        return "unknown"
    if ip.is_loopback:
        return "internet"
    if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
        return "tailscale"
    return "home" if ip.is_private or ip.is_link_local else "internet"


# ---------------------------------------------------------------- метка устройства в cookie
def new_dev_id():
    return secrets.token_hex(6)


def dev_id_from_cookie(header):
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == DEV_COOKIE and ID_RE.match(value):
            return value
    return None


def dev_cookie_header(did, secure=False):
    return f"{DEV_COOKIE}={did}; Path=/; Max-Age={DEV_DAYS * 86400}; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")


# ---------------------------------------------------------------- файлы
def _read(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_private(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- журнал входов
def record_login(data_dir, ok, ua, addr, did, now=None):
    """Одна запись о попытке входа. Неверные попытки подряд с одного устройства сливаются (счётчик n)."""
    now = int(now if now is not None else time.time())
    path = Path(data_dir) / LOGINS
    log = _read(path, [])
    if not isinstance(log, list):
        log = []
    entry = {"ts": now, "ok": bool(ok), "dev": device_label(ua), "where": where_from(addr), "did": did, "n": 1}
    if not ok and log and isinstance(log[0], dict) and not log[0].get("ok") and log[0].get("did") == did and now - int(log[0].get("ts", 0)) < MERGE_FAILS:
        log[0]["n"] = int(log[0].get("n", 1)) + 1
        log[0]["ts"] = now
    else:
        log.insert(0, entry)
    _write_private(path, log[:MAX_LOGINS])
    return log[0]


def recent_logins(data_dir, limit=30):
    log = _read(Path(data_dir) / LOGINS, [])
    return [e for e in log if isinstance(e, dict)][:limit] if isinstance(log, list) else []


def failed_since(data_dir, since):
    return sum(int(e.get("n", 1)) for e in recent_logins(data_dir, MAX_LOGINS) if not e.get("ok") and int(e.get("ts", 0)) >= since)


def clear_logins(data_dir):
    try:
        (Path(data_dir) / LOGINS).unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- «последний заход»
def touch(seen_path, did, ua, addr, now=None):
    """Отмечает, что устройство только что открывало дневник. True, если что-то записали (не чаще раза в SEEN_EVERY секунд)."""
    if not did:
        return False
    now = int(now if now is not None else time.time())
    seen = _read(seen_path, {})
    if not isinstance(seen, dict):
        seen = {}
    label, cur = device_label(ua), seen.get(did)
    if isinstance(cur, dict) and now - int(cur.get("ts", 0)) < SEEN_EVERY and cur.get("dev") == label:
        return False
    seen[did] = {"ts": now, "dev": label, "where": where_from(addr), "first": int((cur or {}).get("first", now)) if isinstance(cur, dict) else now}
    if len(seen) > MAX_SEEN:
        for k in sorted(seen, key=lambda k: seen[k].get("ts", 0))[:len(seen) - MAX_SEEN]:
            del seen[k]
    _write_private(seen_path, seen)
    return True


def seen_list(seen_path):
    seen = _read(seen_path, {})
    if not isinstance(seen, dict):
        return []
    rows = [dict(v, did=k) for k, v in seen.items() if isinstance(v, dict)]
    return sorted(rows, key=lambda r: r.get("ts", 0), reverse=True)
