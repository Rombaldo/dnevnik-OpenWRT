"""Вход в Дневник.ру через Госуслуги (в окне браузера, вручную) и разовое обновление данных.

Команды:
    python fetcher.py login   - открыть окно браузера, войти через Госуслуги вручную; сохраняет вход и номера ученика
    python fetcher.py sync    - один раз обновить данные (без браузера)

Браузер (Playwright) нужен только для входа. Обновление данных идёт обычными запросами (core.py).
"""
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import core

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SESSION = DATA / "session.json"   # cookie входа (секретно, права 600)
IDS = DATA / "ids.json"           # personId / schoolId
OUT = DATA / "diary.json"         # то, что показывает страница
YANDEX = "/Applications/Yandex.app/Contents/MacOS/Yandex"

NeedLogin = core.NeedLogin


def launch(p, headless):
    """Свой Chromium от Playwright; если его нет - Яндекс.Браузер (он на Chromium)."""
    try:
        return p.chromium.launch(headless=headless, chromium_sandbox=True)   # окно с Госуслугами - в песочнице
    except Exception:
        if os.path.exists(YANDEX):
            return p.chromium.launch(headless=headless, executable_path=YANDEX, chromium_sandbox=True)
        raise


def save_cookies(ctx):
    cookies = [c for c in ctx.cookies() if core.keep_cookie(c)]   # только Дневник.ру, без Госуслуг и Яндекса
    now = time.time()
    for c in cookies:
        if c.get("expires", -1) in (-1, None) or c["expires"] < 0:
            c["expires"] = now + core.COOKIE_LIFETIME  # сессионные куки превращаем в 30-дневные
    core.write_private(SESSION, json.dumps(cookies, ensure_ascii=False))


def login():
    from playwright.sync_api import sync_playwright
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
                # узнаём номера ученика и школы: их сайт сам подставляет в запрос дневника
                with page.expect_request(lambda r: "/api/v2/marks/diary" in r.url, timeout=30000) as req:
                    page.goto("https://dnevnik.ru/marks")
                q = parse_qs(urlparse(req.value.url).query)
                core.write_private(IDS, json.dumps({"personId": q["personId"][0], "schoolId": q["schoolId"][0]}))
                save_cookies(ctx)
                print("Вход сохранён.", flush=True)
                browser.close()
                return 0
        print("Время вышло, вход не выполнен.", flush=True)
        browser.close()
        return 1


def sync():
    return core.sync(DATA, OUT, config_path=ROOT / "config.json", version_path=ROOT / "VERSION")


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
