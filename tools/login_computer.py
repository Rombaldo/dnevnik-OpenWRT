#!/usr/bin/env python3
"""Войти в Дневник.ру через Госуслуги на любом компьютере (Windows, Mac, Linux) и передать вход на роутер.

Запуск (из папки программы):
    Windows:   py -3 tools\\login_computer.py        (или двойной щелчок по login-windows.bat)
    Mac/Linux: python3 tools/login_computer.py

Что происходит:
  1. при первом запуске сама готовит окружение (Python-пакет Playwright и браузер Chromium, около 200 МБ, один раз);
  2. открывает окно браузера: вы входите через Госуслуги сами (пароль мы не видим и не сохраняем);
  3. по SSH передаёт на роутер только вход в Дневник.ру (cookie dnevnik.ru) и номера ученика: роутер спросит свой пароль (тот же,
     что для входа в его веб-интерфейс), и сразу обновляет дневник.
"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SESSION, IDS = DATA / "session.json", DATA / "ids.json"
DEFAULT_ROUTER = "192.168.10.1"
DEFAULT_REMOTE_DATA = "/root/dnevnik/data"

# Эта программа выполняется на роутере (Python там уже стоит). Проверяет присланное и кладёт файлы с правами 600.
REMOTE_CODE = r'''
import json, os, sys, base64, subprocess
d = sys.argv[2]
bundle = json.loads(base64.b64decode(sys.argv[3]).decode("utf-8"))
sess, ids = bundle.get("session"), bundle.get("ids")
ok = isinstance(sess, list) and len(sess) > 0 and all(
    isinstance(c, dict) and isinstance(c.get("name"), str) and isinstance(c.get("value"), str)
    and str(c.get("domain", "")).lstrip(".").endswith("dnevnik.ru") for c in sess)
ok = ok and isinstance(ids, dict) and str(ids.get("personId", "")).isdigit() and str(ids.get("schoolId", "")).isdigit()
if not ok:
    print("ОШИБКА: присланный вход выглядит неправильно, на роутере ничего не изменено.")
    sys.exit(3)
os.makedirs(d, mode=0o700, exist_ok=True)
os.chmod(d, 0o700)
def put(name, text):
    p = os.path.join(d, name)
    tmp = p + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, text.encode("utf-8"))
    os.close(fd)
    os.replace(tmp, p)
try:
    old = json.load(open(os.path.join(d, "ids.json")))
    if str(old.get("personId")) == str(ids["personId"]) and str(old.get("schoolId")) == str(ids["schoolId"]):
        ids = dict(old, **ids)
except Exception:
    pass
put("session.json", json.dumps(sess, ensure_ascii=False))
put("ids.json", json.dumps(ids))
print("Вход сохранён на роутере.")
if sys.argv[4] == "sync" and os.path.exists("/root/dnevnik/router/sync.sh"):
    subprocess.Popen(["/root/dnevnik/router/sync.sh"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    print("Обновление запущено.")
'''


def remote_command(bundle, data_dir=DEFAULT_REMOTE_DATA, sync=True):
    """Одна строка для роутера. В ней нет кавычек внутри, поэтому одинаково работает из Windows, Mac и Linux."""
    code = base64.b64encode(REMOTE_CODE.encode("utf-8")).decode()
    payload = base64.b64encode(json.dumps(bundle, ensure_ascii=False).encode("utf-8")).decode()
    return f"python3 -c 'import base64,sys;exec(base64.b64decode(sys.argv[1]))' {code} {data_dir} {payload} {'sync' if sync else 'nosync'}"


# ---------- подготовка окружения ----------
def venv_python():
    return ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def have_playwright():
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def bootstrap_and_rerun(argv):
    """Первый запуск: создаёт .venv, ставит Playwright и Chromium, и запускает себя заново уже в нём."""
    py = venv_python()
    print("Готовлю окружение для входа (один раз, нужен интернет, несколько минут)…", flush=True)
    if not py.exists():
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
    # без закрепления версии: свежий Playwright ставится и на самые новые версии Python (закреплённая версия нужна только разработке)
    subprocess.check_call([str(py), "-m", "pip", "install", "--quiet", "playwright"])
    subprocess.check_call([str(py), "-m", "playwright", "install", "chromium"])
    env = dict(os.environ, DNEVNIK_BOOTSTRAPPED="1")
    return subprocess.call([str(py), str(Path(__file__).resolve())] + argv, env=env)


def ensure_chromium():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ok = Path(p.chromium.executable_path).exists()
    if not ok:
        print("Скачиваю браузер для входа (один раз)…", flush=True)
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])


# ---------- вход и передача ----------
def read_bundle():
    return {"session": json.loads(SESSION.read_text(encoding="utf-8")), "ids": json.loads(IDS.read_text(encoding="utf-8"))}


def send(bundle, router, user, identity, extra, data_dir, sync):
    if not shutil.which("ssh"):
        print("Не найдена программа ssh. В Windows 10/11 её включают так: Параметры → Приложения → Дополнительные компоненты →\n"
              "Добавить компонент → «Клиент OpenSSH». Потом запустите команду снова: войти заново не придётся, вход уже сохранён\n"
              f"в {SESSION}.")
        return 2
    cmd = ["ssh", "-o", "ConnectTimeout=15"] + (["-i", identity] if identity else []) + list(extra) + [f"{user}@{router}", remote_command(bundle, data_dir, sync)]
    print(f"Передаю вход на роутер {router}. Если спросит «yes/no», напишите yes, потом введите пароль роутера "
          "(при вводе символы не показываются).", flush=True)
    return subprocess.call(cmd)


def wait_for_diary(router, seconds=120):
    """True: дневник загрузился. False: не дождались. None: страница закрыта семейным паролем, проверить нельзя."""
    url = f"http://{router}:8765/cgi-bin/data"
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(4)
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                d = json.loads(r.read().decode("utf-8"))
            if d.get("days") and not (d.get("status") or {}).get("needLogin"):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return None
        except Exception:
            pass
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Войти через Госуслуги на компьютере и передать вход на роутер")
    ap.add_argument("--router", default=DEFAULT_ROUTER, help=f"адрес роутера (по умолчанию {DEFAULT_ROUTER})")
    ap.add_argument("--user", default="root")
    ap.add_argument("--identity", help="файл SSH-ключа, если вы входите на роутер по ключу, а не по паролю")
    ap.add_argument("--ssh-arg", action="append", default=[], help="дополнительный ключ для ssh (можно несколько раз)")
    ap.add_argument("--remote-data", default=DEFAULT_REMOTE_DATA, help="папка данных на роутере (нужна только для проверок)")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер, передать уже сохранённый вход (data/session.json)")
    ap.add_argument("--no-send", action="store_true", help="только войти и сохранить вход на этом компьютере")
    ap.add_argument("--forget", action="store_true", help="после передачи удалить вход с этого компьютера (для чужого компьютера)")
    args = ap.parse_args(argv)

    if not args.no_browser:
        if not have_playwright():
            if os.environ.get("DNEVNIK_BOOTSTRAPPED"):
                print("Не удалось подготовить окружение: пакет playwright не установился.")
                return 1
            return bootstrap_and_rerun(sys.argv[1:] if argv is None else list(argv))
        ensure_chromium()
        sys.path.insert(0, str(ROOT))
        import fetcher
        code = fetcher.login()
        if code:
            return code
    if not SESSION.exists() or not IDS.exists():
        print(f"Нет сохранённого входа ({SESSION}). Запустите без ключа --no-browser.")
        return 1
    if args.no_send:
        print(f"Вход сохранён в {SESSION}. На роутер не передавался.")
        return 0
    rc = send(read_bundle(), args.router, args.user, args.identity, args.ssh_arg, args.remote_data, args.remote_data == DEFAULT_REMOTE_DATA)
    if rc != 0:
        print(f"Передать не получилось (код {rc}). Вход остался на этом компьютере: {SESSION}. Проверьте адрес роутера и пароль и запустите\n"
              "команду снова с ключом --no-browser, входить через Госуслуги заново не нужно.")
        return rc
    if args.remote_data == DEFAULT_REMOTE_DATA:
        print("Жду, пока дневник загрузится (до 2 минут)…", flush=True)
        done = wait_for_diary(args.router)
        print("Готово: дневник загрузился." if done else
              "Вход передан. Страница закрыта паролем семьи, поэтому проверить загрузку отсюда нельзя: откройте http://dnevnik.lan и введите пароль."
              if done is None else
              "Вход передан. Если страница http://dnevnik.lan ещё пустая, подождите пару минут и нажмите «Обновить».")
    if args.forget:
        for p in (SESSION, IDS):
            try:
                p.unlink()
            except OSError:
                pass
        print("Вход удалён с этого компьютера.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
