#!/usr/bin/python3
"""Семейный пароль: вход, выход, задать, сменить, отключить, узнать состояние. Запускается веб-сервером роутера (uhttpd).
Пароли нигде не печатаются и не пишутся в журнал."""
import json
import os
import sys

HOME_DIR = os.environ.get("DNEVNIK_HOME", "/root/dnevnik")      # другое значение нужно только тестам
sys.path.insert(0, HOME_DIR)
import auth  # noqa: E402

DATA = os.path.join(HOME_DIR, "data")
THROTTLE = os.environ.get("DNEVNIK_AUTH_THROTTLE", "/tmp/dnevnik-auth-throttle")   # в оперативной памяти: после перезагрузки счётчик неудач обнуляется
sys.stdout.reconfigure(encoding="utf-8")


def reply(obj, code=200, cookie=None):
    head = auth.status_line(code) + "\r\nContent-Type: application/json; charset=utf-8\r\nCache-Control: no-store\r\n"
    if cookie:
        head += "Set-Cookie: %s\r\n" % cookie
    sys.stdout.write(head + "\r\n" + json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


if os.environ.get("REQUEST_METHOD") != "POST":
    reply({"ok": False, "message": "Нужен запрос POST."}, 405)
try:
    size = int(os.environ.get("CONTENT_LENGTH") or 0)
    if not 0 < size <= 2048:
        raise ValueError
    payload = json.loads(sys.stdin.buffer.read(size).decode("utf-8"))
    assert isinstance(payload, dict)
except Exception:
    reply({"ok": False, "message": "Не удалось прочитать запрос."}, 400)

secure = (os.environ.get("HTTP_X_FORWARDED_PROTO", "").lower() == "https" or os.environ.get("HTTPS", "").lower() == "on"
          or auth.is_secure_host(os.environ.get("HTTP_HOST")))
code, body, cookie = auth.handle(DATA, payload, os.environ.get("HTTP_COOKIE"), THROTTLE, secure=secure)
reply(body, code, cookie)
