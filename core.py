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
import urllib.parse
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

    def page(self, path):
        """Текст страницы сайта (нужен, чтобы найти номера ученика и школы)."""
        req = urllib.request.Request(self.base + path, headers={"User-Agent": USER_AGENT})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                if "login.dnevnik.ru" in r.geturl():
                    raise NeedLogin("сайт отправил на страницу входа")
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise NeedLogin("сессия истекла") from e
            raise RuntimeError(f"сайт ответил {e.code}") from e

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


# ---------------------------------------------------------------- вход по логину и паролю
LOGIN_URL = "https://login.dnevnik.ru/login/"
RELOGIN_PAUSE = 6 * 3600   # после неудачного входа повторяем не раньше чем через 6 часов: чтобы не заблокировали учётную запись


class LoginFailed(Exception):
    """Не удалось войти. kind: wrong (неверный логин/пароль), captcha, network, unknown."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _attrs(tag):
    return {m.group(1).lower(): (m.group(3) if m.group(3) is not None else (m.group(4) or ""))
            for m in re.finditer(r"""([\w:.-]+)\s*=\s*("([^"]*)"|'([^']*)')""", tag)}


def parse_login_form(page_html):
    """Скрытые поля формы входа (сайт может менять их значения, поэтому берём каждый раз со страницы)."""
    fields = {}
    for tag in re.findall(r"<input\b[^>]*>", page_html, re.I):
        a = _attrs(tag)
        if a.get("type", "").lower() == "hidden" and a.get("name"):
            fields[a["name"]] = a.get("value", "").replace("&amp;", "&")
    return fields


def login_with_password(login, password, login_url=LOGIN_URL, base=BASE, timeout=30):
    """Входит в Дневник.ру обычным логином и паролем (как форма на сайте). Возвращает CookieJar со входом."""
    jar = http.cookiejar.CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(jar)]
    if login_url.startswith("https"):
        handlers.append(urllib.request.HTTPSHandler(context=make_ssl_context()))
    opener = urllib.request.build_opener(*handlers)
    headers = {"User-Agent": USER_AGENT}
    body = ""
    try:
        page = opener.open(urllib.request.Request(login_url, headers=headers), timeout=timeout).read().decode("utf-8", "ignore")
        data = parse_login_form(page)
        data.update({"login": login, "password": password})
        if not data.get("ReturnUrl"):
            data["ReturnUrl"] = base + "/userfeed"
        req = urllib.request.Request(login_url, data=urllib.parse.urlencode(data).encode(),
                                     headers={**headers, "Content-Type": "application/x-www-form-urlencoded", "Referer": login_url})
        with opener.open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, OSError) as e:
        raise LoginFailed("network", "Нет связи с сайтом Дневник.ру. Проверьте интернет на роутере.") from e
    if any(c.name == "DnevnikAuth_a" for c in jar):
        return jar
    if parse_login_form(body).get("Captcha.Id"):
        raise LoginFailed("captcha", "Сайт просит ввести картинку-капчу. Один раз войдите на dnevnik.ru в браузере "
                                     "(введите капчу), потом повторите здесь.")
    if "Ошибка в логине или пароле" in body or 'name="password"' in body:
        raise LoginFailed("wrong", "Неверный логин или пароль.")
    raise LoginFailed("unknown", "Сайт не принял вход. Попробуйте позже.")


def discover_ids(client, today=None):
    """Находит номера ученика и школы на страницах сайта (раньше их показывал браузер)."""
    today = today or date.today()
    html = client.page("/marks")
    persons = list(dict.fromkeys(re.findall(r"childPersonId=(\d{8,})", html)))
    schools = list(dict.fromkeys(re.findall(r"[?&]school=(\d{8,})", html) + re.findall(r"schoolId[=:\"']+(\d{8,})", html)))
    if not persons or not schools:
        raise RuntimeError("не нашёл на сайте номера ученика и школы")
    for pid in persons:               # у школьной учётной записи ученик один; у родителя берём первого ребёнка
        for sid in schools:
            days = client.diary({"personId": pid, "schoolId": sid}, today - timedelta(days=30), today + timedelta(days=7))
            if any(d["lessons"] for d in days):
                return {"personId": pid, "schoolId": sid}
    return {"personId": persons[0], "schoolId": schools[0]}


def setup_login(data_dir, login, password, base=BASE, login_url=None):
    """Проверяет логин и пароль на сайте и, если подошли, сохраняет их (файл закрыт правами 600).
    Возвращает (успех, сообщение для человека). Пароль никуда не печатается."""
    login, password = (login or "").strip(), password or ""
    if not login or not password:
        return False, "Введите логин и пароль."
    if len(login) > 200 or len(password) > 200:
        return False, "Слишком длинный логин или пароль."
    data_dir = Path(data_dir)
    login_url = login_url or (LOGIN_URL if base == BASE else base + "/login/")
    try:
        jar = login_with_password(login, password, login_url, base)
        extra = urlparse(base).hostname if base != BASE else None
        ids = discover_ids(Client(jar, base=base), today=None)
    except LoginFailed as e:
        return False, str(e)
    except NeedLogin:
        return False, "Сайт принял вход, но не открыл дневник. Попробуйте ещё раз."
    except Exception as e:
        return False, f"Вход прошёл, но данные ученика не нашлись ({type(e).__name__}). Напишите разработчику."
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    write_private(data_dir / "credentials.json", json.dumps({"login": login, "password": password}, ensure_ascii=False))
    write_private(data_dir / "session.json", json.dumps(jar_to_list(jar, also=extra), ensure_ascii=False))
    write_private(data_dir / "ids.json", json.dumps(ids))
    try:
        (data_dir / "login-state.json").unlink()
    except OSError:
        pass
    return True, "Вход выполнен. Загружаю дневник, это займёт около минуты."


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


def sync(data_dir, out_path, base=BASE, today=None, session_save_hours=0, config_path=None, version_path=None, login_url=None):
    """Обновляет дневник. Возвращает словарь для страницы. Бросает NeedLogin, если войти не получилось.

    Если сохранённый вход истёк, а на роутере есть логин и пароль (credentials.json), заходит заново сам.

    data_dir: тут лежат session.json, ids.json, history.json, credentials.json (то, что должно пережить перезагрузку)
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
    ids_path, cred_path, state_path = data_dir / "ids.json", data_dir / "credentials.json", data_dir / "login-state.json"
    history = read_json(history_path, {})
    updated_prev = read_json(out_path, {}).get("updatedAt")
    extra = urlparse(base).hostname if base != BASE else None   # у настоящего сайта разрешён только Дневник.ру
    login_url = login_url or (LOGIN_URL if base == BASE else base + "/login/")

    def fail(err, kind):
        status = {"needLogin": kind == "login", "error": str(err), "syncing": False}
        write_public(out_path, public_payload(history, updated_prev, status, cfg, version))

    def relogin():
        """Заходит заново по сохранённому логину и паролю."""
        creds = read_json(cred_path, None)
        if not creds:
            raise NeedLogin("Введите логин и пароль от Дневник.ру.")
        state = read_json(state_path, {})
        failed_at = state.get("failedAt", 0)
        if time.time() - failed_at < RELOGIN_PAUSE and cred_path.stat().st_mtime <= failed_at:
            raise NeedLogin(state.get("message") or "Вход не удался.")    # не долбим сайт неверным паролем
        try:
            jar = login_with_password(creds.get("login", ""), creds.get("password", ""), login_url, base)
        except LoginFailed as e:
            if e.kind == "network":
                raise RuntimeError(str(e)) from e                          # нет интернета: это не повод просить ввести пароль
            write_private(state_path, json.dumps({"failedAt": time.time(), "message": str(e)}, ensure_ascii=False))
            raise NeedLogin(f"{e} Введите логин и пароль заново.") from e
        try:
            state_path.unlink()
        except OSError:
            pass
        return jar

    def fetch(jar, ids):
        client = Client(jar, base=base)
        if not ids:
            ids = discover_ids(client, today)
            write_private(ids_path, json.dumps(ids))
        first = (today - timedelta(days=14)) if history else date(today.year if today.month >= 9 else today.year - 1, 9, 1)
        cur = first - timedelta(days=first.weekday())
        while cur <= today + timedelta(days=21):
            for d in client.diary(ids, cur, cur + timedelta(days=7)):
                history[d["date"]] = d
            cur += timedelta(days=7)

    before = hashlib.sha256(json.dumps(history, sort_keys=True).encode()).hexdigest()
    relogged = False
    try:
        ids = read_json(ids_path, None)
        try:
            jar = load_jar(session_path, also=extra)
            fetch(jar, ids)
        except NeedLogin:
            jar = relogin()
            relogged = True
            fetch(jar, read_json(ids_path, None))
    except NeedLogin as e:
        fail(e, "login")
        raise
    except Exception as e:
        fail(e, "error")
        raise

    if hashlib.sha256(json.dumps(history, sort_keys=True).encode()).hexdigest() != before:
        write_private(history_path, json.dumps(history, ensure_ascii=False))     # пишем, только если что-то изменилось
    age_h = (time.time() - session_path.stat().st_mtime) / 3600 if session_path.exists() else 1e9
    if relogged or age_h >= session_save_hours:
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
    has_login = (data_dir / "session.json").exists() or (data_dir / "credentials.json").exists()
    status = {"needLogin": not has_login, "error": None if has_login else "Введите логин и пароль от Дневник.ру.", "syncing": False}
    write_public(out_path, public_payload(history, updated, status, cfg, version))
