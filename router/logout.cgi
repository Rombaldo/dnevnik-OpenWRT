#!/usr/bin/python3
"""Выход из аккаунта со страницы. Два шага: begin (первое подтверждение, выдаёт код) и confirm (второе, с кодом).
Вход и данные при выходе не стираются, а прячутся в резервную копию на сутки: restore возвращает их.
Запускается веб-сервером роутера (uhttpd)."""
import json
import os
import subprocess
import sys
import time

HOME_DIR = os.environ.get("DNEVNIK_HOME", "/root/dnevnik")      # другое значение нужно только тестам
OUT_FILE = os.environ.get("DNEVNIK_OUT", "/tmp/dnevnik-private/data.json")
GAP_FILE = os.environ.get("DNEVNIK_LOGOUT_GAP", "/tmp/dnevnik-logout-last")
sys.path.insert(0, HOME_DIR)
import auth  # noqa: E402
import core  # noqa: E402

DATA = os.path.join(HOME_DIR, "data")
sys.stdout.reconfigure(encoding="utf-8")


def reply(obj, code=200):
    sys.stdout.write(auth.status_line(code) + "\r\nContent-Type: application/json; charset=utf-8\r\nCache-Control: no-cache\r\n\r\n")
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


if os.environ.get("REQUEST_METHOD") != "POST":
    reply({"ok": False, "message": "Нужен запрос POST."}, 405)
if not auth.authed(DATA, os.environ.get("HTTP_COOKIE")):      # включён семейный пароль, а запрос пришёл без него
    reply(auth.locked_reply()[1], 401)
try:
    size = int(os.environ.get("CONTENT_LENGTH") or 0)
    if not 0 < size <= 1024:
        raise ValueError
    payload = json.loads(sys.stdin.buffer.read(size).decode("utf-8"))
    assert isinstance(payload, dict)
except Exception:
    reply({"ok": False, "message": "Не удалось прочитать запрос."}, 400)

now = time.time()                       # не чаще одного запроса в секунду
try:
    last = float(open(GAP_FILE).read() or 0)
except (OSError, ValueError):
    last = 0
if now - last < 1:
    reply({"ok": False, "message": "Подождите секунду и повторите."}, 429)
with open(GAP_FILE, "w") as f:
    f.write(str(now))


def refresh_page():
    core.rebuild(DATA, OUT_FILE, config_path=os.path.join(HOME_DIR, "config.json"), version_path=os.path.join(HOME_DIR, "VERSION"))


action = payload.get("action")
if action == "begin":
    reply({"ok": True, "token": core.begin_logout(DATA)})
elif action == "confirm":
    ok, message = core.confirm_logout(DATA, str(payload.get("token") or ""))
    if ok:
        refresh_page()
    reply({"ok": ok, "message": message})
elif action == "restore":
    ok, message = core.restore_logout(DATA)
    if ok:
        refresh_page()
        subprocess.Popen([os.path.join(HOME_DIR, "router", "sync.sh")], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    reply({"ok": ok, "message": message})
else:
    reply({"ok": False, "message": "Неизвестное действие."}, 400)
