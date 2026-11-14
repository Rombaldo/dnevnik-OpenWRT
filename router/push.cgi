#!/usr/bin/python3
"""Уведомления: подключить и отключить устройство, настройки, проверка. Работает только за паролем семьи (если он включён).
Запускается веб-сервером роутера (uhttpd). Ключи устройств и настройки лежат в data/push.json (права 600)."""
import json
import os
import sys
import time

HOME_DIR = os.environ.get("DNEVNIK_HOME", "/root/dnevnik")      # другое значение нужно только тестам
GAP_FILE = os.environ.get("DNEVNIK_PUSH_GAP", "/tmp/dnevnik-push-test-last")
sys.path.insert(0, HOME_DIR)
import auth  # noqa: E402

DATA = os.path.join(HOME_DIR, "data")
sys.stdout.reconfigure(encoding="utf-8")


def reply(obj, code=200):
    sys.stdout.write(auth.status_line(code) + "\r\nContent-Type: application/json; charset=utf-8\r\nCache-Control: no-store\r\n\r\n")
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


if os.environ.get("REQUEST_METHOD") != "POST":
    reply({"ok": False, "message": "Нужен запрос POST."}, 405)
if not auth.authed(DATA, os.environ.get("HTTP_COOKIE")):
    reply(auth.locked_reply()[1], 401)
try:
    size = int(os.environ.get("CONTENT_LENGTH") or 0)
    if not 0 < size <= 8192:
        raise ValueError
    payload = json.loads(sys.stdin.buffer.read(size).decode("utf-8"))
    assert isinstance(payload, dict)
except Exception:
    reply({"ok": False, "message": "Не удалось прочитать запрос."}, 400)

if payload.get("action") == "test":                    # не чаще одной проверки в 5 секунд: чтобы кнопкой не завалили телефон
    try:
        last = float(open(GAP_FILE).read() or 0)
    except (OSError, ValueError):
        last = 0
    if time.time() - last < 5:
        reply({"ok": False, "message": "Подождите несколько секунд и попробуйте снова."}, 429)
    with open(GAP_FILE, "w") as f:
        f.write(str(time.time()))

import push  # noqa: E402  (тяжёлую часть, криптографию, push.py подгружает только когда она нужна)
code, body = push.handle(DATA, payload)
reply(body, code)
