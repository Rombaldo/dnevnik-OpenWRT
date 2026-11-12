#!/usr/bin/python3
"""Кнопка «Обновить» на странице. Не чаще раза в 2 минуты. Работает только для тех, у кого есть семейный пароль (если он включён)."""
import json
import os
import subprocess
import sys
import time

HOME_DIR = os.environ.get("DNEVNIK_HOME", "/root/dnevnik")      # другое значение нужно только тестам
LAST = os.environ.get("DNEVNIK_LAST_REFRESH", "/tmp/dnevnik-lastrefresh")
MIN_GAP = 120
sys.path.insert(0, HOME_DIR)
import auth  # noqa: E402


def reply(obj, code=200):
    sys.stdout.write(auth.status_line(code) + "\r\nContent-Type: application/json; charset=utf-8\r\nCache-Control: no-store\r\n\r\n")
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


if not auth.authed(os.path.join(HOME_DIR, "data"), os.environ.get("HTTP_COOKIE")):
    reply(auth.locked_reply()[1], 401)
now = time.time()
try:
    last = float(open(LAST).read() or 0)
except (OSError, ValueError):
    last = 0
if now - last >= MIN_GAP:
    with open(LAST, "w") as f:
        f.write(str(now))
    subprocess.Popen([os.path.join(HOME_DIR, "router", "sync.sh")], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
reply({"ok": True})
