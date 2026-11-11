"""Читает дневник с dnevnik.ru под сохранённым входом и складывает данные в data/diary.json.

Команды:
    python fetcher.py login   - открыть окно браузера, войти через Госуслуги вручную
    python fetcher.py sync    - один раз обновить данные
"""
import json
import os
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
            if isinstance(a, dict):
                files.append(a.get("fileName") or a.get("name") or "файл")
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


TREND = {"Up": "up", "Down": "down", "Equal": "same"}


def fetch_rating(page, ids, history):
    """Бесплатная часть рейтинга: направление (вверх/вниз/без изменений). Места сайт отдаёт только за подписку."""
    if not ids.get("groupId"):
        with page.expect_request(lambda r: "/ratings/feed-widget" in r.url, timeout=30000) as req:
            page.goto("https://dnevnik.ru/userfeed")
        q = parse_qs(urlparse(req.value.url).query)
        ids["groupId"] = q["groupId"][0]
        IDS.write_text(json.dumps(ids))
    res = page.evaluate("""async ([g, p]) => {
        const r = await fetch(`/ratings/feed-widget?groupId=${g}&childPersonId=${p}&page=userfeed`, {credentials: 'include'});
        if (!r.ok) return null;
        try { return await r.json(); } catch (e) { return null; }
    }""", [ids["groupId"], ids["personId"]])
    if not res:
        return None
    names = {}
    for d in history.values():
        for l in d["lessons"]:
            if l.get("subjectId"):
                names[l["subjectId"]] = l["subject"]
    t = lambda x: TREND.get((x or {}).get("value"), "same")
    out = {
        "class": t((res.get("commonGroupRatingEvent") or {}).get("trend")),
        "parallel": t((res.get("commonParallelRatingEvent") or {}).get("trend")),
        "subjects": [],
        "top": None,
    }
    for e in res.get("personalRatingBySubjectEvents") or []:
        n = names.get((e.get("subject") or {}).get("id"))
        if n:
            out["subjects"].append({"name": n, "trend": t(e.get("trend"))})
    top = res.get("topRatingEvent")
    if top:
        n = names.get(top.get("subjectId"))
        change = None
        for v in ((top.get("topRankingDynamic") or {}).get("values") or []):
            if v.get("isContextPerson"):
                change = (v.get("averageMarkChange") or {}).get("value")
        if n:
            out["top"] = {"subject": n, "change": (change or "").replace(" ", "")}
    return out


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
                rating = fetch_rating(page, ids, history)
            except Exception as e:  # рейтинг - приятное дополнение, его сбой не должен ронять всё остальное
                print("рейтинг не получен:", e, flush=True)
                rating = None
            save_cookies(ctx)
        except PWTimeout as e:
            raise NeedLogin("дневник не отдал данные (скорее всего, вход истёк)") from e
        finally:
            browser.close()

    DATA.mkdir(exist_ok=True)
    HISTORY.write_text(json.dumps(history, ensure_ascii=False))
    days = [history[k] for k in sorted(history)]
    out = {"updatedAt": datetime.now().isoformat(timespec="seconds"), "days": days, "rating": rating}
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
