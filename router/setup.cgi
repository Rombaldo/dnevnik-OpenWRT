#!/usr/bin/python3
"""Настройка входа со страницы: принимает логин и пароль от Дневник.ру, проверяет их на сайте и сохраняет на роутере.
Запускается веб-сервером роутера (uhttpd). Пароль нигде не печатается."""
import json
import os
import subprocess
import sys
import time

HOME_DIR = os.environ.get("DNEVNIK_HOME", "/root/dnevnik")      # другое значение нужно только тестам
sys.path.insert(0, HOME_DIR)
import core  # noqa: E402

DATA = os.path.join(HOME_DIR, "data")
GAP_FILE = os.environ.get("DNEVNIK_GAP_FILE", "/tmp/dnevnik-setup-last")   # не чаще одной попытки в 10 секунд: чтобы с домашней сети не перебирали пароль
sys.stdout.reconfigure(encoding="utf-8")


def reply(obj, code=200):
    sys.stdout.write("Status: %d\r\nContent-Type: application/json; charset=utf-8\r\nCache-Control: no-cache\r\n\r\n" % code)
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


if os.environ.get("REQUEST_METHOD") != "POST":
    reply({"ok": False, "message": "Нужен запрос POST."}, 405)
try:
    size = int(os.environ.get("CONTENT_LENGTH") or 0)
    if not 0 < size <= 4096:
        raise ValueError
    payload = json.loads(sys.stdin.buffer.read(size).decode("utf-8"))
    assert isinstance(payload, dict)
except Exception:
    reply({"ok": False, "message": "Не удалось прочитать данные формы."}, 400)

now = time.time()
try:
    last = float(open(GAP_FILE).read() or 0)
except (OSError, ValueError):
    last = 0
if now - last < 10:
    reply({"ok": False, "message": "Подождите несколько секунд и попробуйте снова."}, 429)
with open(GAP_FILE, "w") as f:
    f.write(str(now))

ok, message = core.setup_login(DATA, payload.get("login"), payload.get("password"))
if ok:   # сразу загружаем дневник, не дожидаясь расписания
    subprocess.Popen([os.path.join(HOME_DIR, "router", "sync.sh")], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
reply({"ok": ok, "message": message})
