"""Читает дневник с dnevnik.ru под сохранённым входом и складывает данные в data/diary.json.

Команды:
    python fetcher.py login   - открыть окно браузера, войти через Госуслуги вручную
    python fetcher.py sync    - один раз обновить данные
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SESSION = DATA / "session.json"   # cookies входа (секретно, права 600)
HISTORY = DATA / "history.json"   # накопленные дни дневника
OUT = DATA / "diary.json"         # то, что показывает страница
IDS = DATA / "ids.json"           # personId / schoolId

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
COOKIE_LIFETIME = 30 * 24 * 3600
YANDEX = "/Applications/Yandex.app/Contents/MacOS/Yandex"


class NeedLogin(Exception):
    pass


def launch(p, headless):
    """Свой Chromium от Playwright; если его нет - Яндекс.Браузер (он на Chromium)."""
    try:
        return p.chromium.launch(headless=headless)
    except Exception:
        if os.path.exists(YANDEX):
            return p.chromium.launch(headless=headless, executable_path=YANDEX)
        raise


def save_cookies(ctx):
    cookies = ctx.cookies()
    now = time.time()
    for c in cookies:
        if c.get("expires", -1) in (-1, None) or c["expires"] < 0:
            c["expires"] = now + COOKIE_LIFETIME  # сессионные куки превращаем в 30-дневные
    DATA.mkdir(exist_ok=True)
    SESSION.write_text(json.dumps(cookies, ensure_ascii=False))
    os.chmod(SESSION, 0o600)


def load_cookies(ctx):
    if not SESSION.exists():
        raise NeedLogin("нет сохранённого входа")
    ctx.add_cookies(json.loads(SESSION.read_text()))


def login():
    DATA.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = launch(p, headless=False)
        ctx = browser.new_context(viewport={"width": 1100, "height": 800}, locale="ru-RU")
        page = ctx.new_page()
        page.goto("https://login.dnevnik.ru/")
        print("Войдите через Госуслуги в открывшемся окне. Жду...", flush=True)
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(3)
            # Проверяем не адрес вкладки (Госуслуги могут открыть новую), а сам вход:
            # залогиненного пускают на ленту, иначе перекидывают на страницу входа.
            try:
                r = ctx.request.get("https://dnevnik.ru/userfeed", max_redirects=0, timeout=15000)
                ok = r.status == 200
            except Exception:
                ok = False
            if ok:
                save_cookies(ctx)
                print("Вход сохранён.", flush=True)
                browser.close()
                return 0
        print("Время вышло, вход не выполнен.", flush=True)
        browser.close()
        return 1


def ts(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def hm(h):
    return f"{int(h['startHour'])}:{h['startMinute']}", f"{int(h['endHour'])}:{h['endMinute']}"


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
                files.append({
                    "id": str(a["fileId"]),
                    "name": a.get("fileName") or a.get("name") or "файл",
                    "ext": str(a.get("fileExtension") or "").lower().lstrip("."),
                    "size": int(a.get("size") or 0),
                    "link": a.get("fileLink") or "",
                })
        marks = []
        for w in l.get("workMarks") or []:
            for m in w.get("marks") or []:
                marks.append({"value": m.get("value"), "work": w.get("workName"),
                              "created": m.get("createdDate")})
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


ALLOWED_EXT = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt", "rtf", "odt",
               "jpg", "jpeg", "png", "gif", "webp"}
MAX_FILE = 30 * 1024 * 1024   # файлы больше 30 МБ не скачиваем
FILES = DATA / "files"


def trusted_link(url):
    u = urlparse(url or "")
    h = u.hostname or ""
    return u.scheme == "https" and (h == "dnevnik.ru" or h.endswith(".dnevnik.ru") or h.endswith(".csdnevnik.ru"))


def file_path(f):
    return FILES / f"{f['id']}.{f['ext']}"


def download_files(ctx, history):
    """Скачивает вложения к домашке (свежие и будущие уроки). Помечает f['ok'], когда файл лежит у нас."""
    FILES.mkdir(parents=True, exist_ok=True)
    cutoff = (date.today() - timedelta(days=14)).isoformat()
    for day in history.values():
        for l in day["lessons"]:
            for f in l.get("files", []):
                if not isinstance(f, dict):
                    continue
                path = file_path(f)
                f["ok"] = path.exists()
                if f["ok"] or day["date"] < cutoff:
                    continue
                if f["ext"] not in ALLOWED_EXT or not trusted_link(f.get("link")) or f.get("size", 0) > MAX_FILE:
                    continue
                try:
                    r = ctx.request.get(f["link"], timeout=90000)
                    body = r.body() if r.ok else b""
                except Exception as e:
                    print(f"файл {f['name']} не скачался: {e}", flush=True)
                    continue
                if body and len(body) <= MAX_FILE:
                    tmp = path.with_suffix(".part")
                    tmp.write_bytes(body)
                    tmp.replace(path)
                    f["ok"] = True
                    print(f"скачан файл: {f['name']}.{f['ext']} ({len(body) // 1024} КБ)", flush=True)


def public_days(history):
    """Дни для страницы: без служебных ссылок на файлы."""
    days = []
    for k in sorted(history):
        d = json.loads(json.dumps(history[k]))
        for l in d["lessons"]:
            l["files"] = [{k2: v for k2, v in f.items() if k2 != "link"} for f in l.get("files", []) if isinstance(f, dict)]
        days.append(d)
    return days


def fetch_range(page, ids, start, finish):
    js = """async ([pid, sid, s, f]) => {
        const r = await fetch(`/api/v2/marks/diary?personId=${pid}&schoolId=${sid}&startDate=${s}&finishDate=${f}&timestamp=${Date.now()}`, {credentials: 'include'});
        if (!r.ok) return {status: r.status};
        return {status: 200, json: await r.json()};
    }"""
    res = page.evaluate(js, [ids["personId"], ids["schoolId"], ts(start), ts(finish)])
    if res["status"] in (401, 403):
        raise NeedLogin("сессия истекла")
    if res["status"] != 200:
        raise RuntimeError(f"дневник ответил {res['status']}")
    return [norm_day(d) for d in res["json"]["days"]]


def sync():
    today = date.today()
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    if history:
        first = today - timedelta(days=14)
    else:  # первый запуск: с 1 сентября учебного года
        year = today.year if today.month >= 9 else today.year - 1
        first = date(year, 9, 1)
    last = today + timedelta(days=21)

    with sync_playwright() as p:
        browser = launch(p, headless=True)
        try:
            ctx = browser.new_context(locale="ru-RU")
            load_cookies(ctx)
            page = ctx.new_page()
            ids = json.loads(IDS.read_text()) if IDS.exists() else None
            if ids is None:
                with page.expect_request(lambda r: "/api/v2/marks/diary" in r.url, timeout=30000) as req:
                    page.goto("https://dnevnik.ru/marks")
                q = parse_qs(urlparse(req.value.url).query)
                ids = {"personId": q["personId"][0], "schoolId": q["schoolId"][0]}
                IDS.write_text(json.dumps(ids))
            else:
                page.goto("https://dnevnik.ru/marks")
            if "login.dnevnik.ru" in page.url or "esia" in page.url:
                raise NeedLogin("страница входа")

            cur = first - timedelta(days=first.weekday())  # понедельник
            while cur <= last:
                for d in fetch_range(page, ids, cur, cur + timedelta(days=7)):
                    history[d["date"]] = d
                cur += timedelta(days=7)
            try:
                download_files(ctx, history)
            except Exception as e:  # вложения не должны ронять обновление дневника
                print("вложения не обработаны:", e, flush=True)
            save_cookies(ctx)
        except PWTimeout as e:
            raise NeedLogin("дневник не отдал данные (скорее всего, вход истёк)") from e
        finally:
            browser.close()

    DATA.mkdir(exist_ok=True)
    HISTORY.write_text(json.dumps(history, ensure_ascii=False))
    days = public_days(history)
    out = {"updatedAt": datetime.now().isoformat(timespec="seconds"), "days": days}
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False))
    tmp.replace(OUT)
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "login":
        sys.exit(login())
    try:
        o = sync()
        print(f"Готово: {len(o['days'])} дней, обновлено {o['updatedAt']}")
    except NeedLogin as e:
        print(f"Нужно войти заново: {e}")
        sys.exit(2)
