"""Общая часть: разбор данных Дневника и обновление по HTTP без браузера.

Только стандартная библиотека Python, поэтому работает и на Mac, и на роутере.
Вход через Госуслуги делается отдельно, в браузере на Mac (fetcher.py login):
оттуда берутся cookie входа (session.json) и номера ученика/школы (ids.json).
"""
import hashlib
import http.cookiejar
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

BASE = "https://dnevnik.ru"
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
COOKIE_LIFETIME = 30 * 24 * 3600
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
APP_DIR = Path(__file__).resolve().parent


class NeedLogin(Exception):
    """Вход истёк: нужно заново войти через Госуслуги."""


# ---------------------------------------------------------------- разбор данных
def ts(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def hm(h):
    return f"{int(h['startHour'])}:{h['startMinute']}", f"{int(h['endHour'])}:{h['endMinute']}"


def trusted_link(url):
    """Ссылки на файлы берём только с серверов Дневник.ру по защищённому соединению."""
    u = urlparse(url or "")
    h = u.hostname or ""
    return u.scheme == "https" and (h == "dnevnik.ru" or h.endswith(".dnevnik.ru") or h.endswith(".csdnevnik.ru"))


def norm_day(d):
    day = datetime.fromtimestamp(int(d["date"]), tz=timezone.utc).date()
    lessons = []
    for l in d.get("lessons", []):
        if l.get("isEmpty") and not l.get("subject"):
            continue
        start, end = hm(l["hours"]) if l.get("hours") else ("", "")
        hw = l.get("homework") or {}
        files = []
        for a in hw.get("attachments") or []:
            if isinstance(a, dict) and re.fullmatch(r"\d+", str(a.get("fileId") or "")):
                link = a.get("fileLink") or ""
                files.append({
                    "id": str(a["fileId"]),
                    "name": a.get("fileName") or a.get("name") or "файл",
                    "ext": str(a.get("fileExtension") or "").lower().lstrip("."),
                    "size": int(a.get("size") or 0),
                    "url": link if trusted_link(link) else "",   # файл открывается по ссылке, у нас не хранится
                })
        marks = []
        for w in l.get("workMarks") or []:
            for m in w.get("marks") or []:
                marks.append({"value": m.get("value"), "work": w.get("workName"), "created": m.get("createdDate")})
        lessons.append({
            "n": l.get("number"), "start": start, "end": end,
            "subject": (l.get("subject") or {}).get("name", "?"),
            "subjectId": (l.get("subject") or {}).get("id"),
            "groupId": l.get("groupId"),
            "theme": l.get("theme") or "",
            "homework": (hw.get("text") or "").strip(),
            "files": files,
            "canceled": bool(l.get("isCanceled")),
            "marks": marks,
        })
    return {"date": day.isoformat(), "weekday": WEEKDAYS[day.weekday()], "lessons": lessons}


# ---------------------------------------------------------------- cookie входа
def keep_cookie(c, also=None):
    """Нужны только cookie самого Дневника.ру. Всё остальное, что браузер насобирал при входе
    (Госуслуги, Яндекс и т.д.), мы не храним и никуда не отправляем.
    also - ещё один разрешённый адрес (нужен только тестам с поддельным сайтом)."""
    dom = (c.get("domain") if isinstance(c, dict) else c.domain).lstrip(".").lower()
    return dom == "dnevnik.ru" or dom.endswith(".dnevnik.ru") or (also is not None and dom == also)


def load_jar(path, also=None):
    """Читает cookie из session.json (формат Playwright) в обычный CookieJar."""
    p = Path(path)
    if not p.exists():
        raise NeedLogin("нет сохранённого входа")
    jar = http.cookiejar.CookieJar()
    for c in json.loads(p.read_text()):
        if not keep_cookie(c, also):
            continue
        dom = c["domain"]
        exp = int(c["expires"]) if c.get("expires", -1) and c["expires"] > 0 else None
        rest = {"HttpOnly": None} if c.get("httpOnly") else {}
        jar.set_cookie(http.cookiejar.Cookie(
            0, c["name"], c["value"], None, False, dom, True, dom.startswith("."), c.get("path", "/"), True,
            bool(c.get("secure")), exp, False, None, None, rest))
    return jar


def jar_to_list(jar, also=None):
    now = time.time()
    out = []
    for c in jar:
        if not keep_cookie(c, also):
            continue
        out.append({
            "name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
            "expires": c.expires if c.expires else now + COOKIE_LIFETIME,   # сессионные куки делаем «долгими»
            "httpOnly": c.has_nonstandard_attr("HttpOnly"), "secure": bool(c.secure),
        })
    return out


def write_private(path, text):
    """Пишет файл с правами только для владельца (там вход или личные данные)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def make_ssl_context():
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats().get("x509_ca"):    # на некоторых системах нужно подсказать, где сертификаты
        for cand in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
            if os.path.exists(cand):
                ctx.load_verify_locations(cand)
                break
    return ctx


# ---------------------------------------------------------------- запросы к сайту
class Client:
    def __init__(self, jar, base=BASE, timeout=30):
        self.base, self.timeout = base, timeout
        handlers = [urllib.request.HTTPCookieProcessor(jar)]
        if base.startswith("https"):
            handlers.append(urllib.request.HTTPSHandler(context=make_ssl_context()))
        self.opener = urllib.request.build_opener(*handlers)

    def diary(self, ids, start, finish):
        url = (f"{self.base}/api/v2/marks/diary?personId={ids['personId']}&schoolId={ids['schoolId']}"
               f"&startDate={ts(start)}&finishDate={ts(finish)}&timestamp={int(time.time() * 1000)}")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                   "Referer": f"{self.base}/marks"})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                if "login.dnevnik.ru" in r.geturl():
                    raise NeedLogin("сайт отправил на страницу входа")
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise NeedLogin("сессия истекла") from e
            raise RuntimeError(f"дневник ответил {e.code}") from e
        try:
            return [norm_day(d) for d in json.loads(body)["days"]]
        except (ValueError, KeyError) as e:
            raise NeedLogin("сайт вернул не данные дневника (вход, скорее всего, истёк)") from e


# ---------------------------------------------------------------- обновление
def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def public_payload(history, updated_at, status, cfg=None, version=None):
    days = [history[k] for k in sorted(history)]
    out = {"updatedAt": updated_at, "days": days, "status": status, "config": cfg or {}}
    if version:
        out["version"] = version
    return out


def write_public(out_path, payload):
    """Данные для страницы. Кладём туда же копию «для офлайна», если рядом есть index.html."""
    text = json.dumps(payload, ensure_ascii=False)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o644)       # это содержимое страницы: веб-сервер должен его отдавать при любом umask
    os.replace(tmp, out_path)
    index = out_path.parent / "index.html"
    if index.exists():
        blob = text.replace("</", "<\\/")
        tmp = out_path.with_name("offline.html.tmp")
        tmp.write_text(index.read_text(encoding="utf-8").replace("/*INLINE_DATA*/null", blob), encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, out_path.with_name("offline.html"))


def sync(data_dir, out_path, base=BASE, today=None, session_save_hours=0, config_path=None, version_path=None):
    """Обновляет дневник. Возвращает словарь для страницы. Бросает NeedLogin, если вход истёк.

    data_dir: тут лежат session.json, ids.json, history.json (то, что должно пережить перезагрузку)
    out_path: куда писать data.json для страницы (на роутере это оперативная память)
    session_save_hours: как часто перезаписывать session.json (на роутере экономим флеш-память)
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)   # тут вход и личные данные: чужим пользователям компьютера не видно
    today = today or date.today()
    cfg = read_json(config_path, {}) if config_path else {}
    version = Path(version_path).read_text().strip() if version_path and Path(version_path).exists() else None
    history_path, session_path = data_dir / "history.json", data_dir / "session.json"
    history = read_json(history_path, {})
    updated_prev = read_json(out_path, {}).get("updatedAt")

    def fail(err, kind):
        status = {"needLogin": kind == "login", "error": str(err), "syncing": False}
        write_public(out_path, public_payload(history, updated_prev, status, cfg, version))

    try:
        ids = read_json(data_dir / "ids.json", None)
        if not ids:
            raise NeedLogin("нет сведений об ученике (нужен вход)")
        extra = urlparse(base).hostname if base != BASE else None   # у настоящего сайта разрешён только Дневник.ру
        jar = load_jar(session_path, also=extra)
        client = Client(jar, base=base)
        if history:
            first = today - timedelta(days=14)
        else:   # первый запуск: с 1 сентября учебного года
            first = date(today.year if today.month >= 9 else today.year - 1, 9, 1)
        last = today + timedelta(days=21)
        before = hashlib.sha256(json.dumps(history, sort_keys=True).encode()).hexdigest()
        cur = first - timedelta(days=first.weekday())
        while cur <= last:
            for d in client.diary(ids, cur, cur + timedelta(days=7)):
                history[d["date"]] = d
            cur += timedelta(days=7)
    except NeedLogin as e:
        fail(e, "login")
        raise
    except Exception as e:
        fail(e, "error")
        raise

    if hashlib.sha256(json.dumps(history, sort_keys=True).encode()).hexdigest() != before:
        write_private(history_path, json.dumps(history, ensure_ascii=False))     # пишем, только если что-то изменилось
    age_h = (time.time() - session_path.stat().st_mtime) / 3600 if session_path.exists() else 1e9
    if age_h >= session_save_hours:
        write_private(session_path, json.dumps(jar_to_list(jar, also=extra), ensure_ascii=False))
    payload = public_payload(history, datetime.now().isoformat(timespec="seconds"),
                             {"needLogin": False, "error": None, "syncing": False}, cfg, version)
    write_public(out_path, payload)
    return payload


def rebuild(data_dir, out_path, config_path=None, version_path=None):
    """Собирает данные для страницы из сохранённой истории без обращения к сайту (после перезагрузки роутера)."""
    data_dir = Path(data_dir)
    hp = data_dir / "history.json"
    history = read_json(hp, {})
    updated = datetime.fromtimestamp(hp.stat().st_mtime).isoformat(timespec="seconds") if hp.exists() else None
    cfg = read_json(config_path, {}) if config_path else {}
    version = Path(version_path).read_text().strip() if version_path and Path(version_path).exists() else None
    status = {"needLogin": not (data_dir / "session.json").exists(), "error": None, "syncing": False}
    write_public(out_path, public_payload(history, updated, status, cfg, version))
