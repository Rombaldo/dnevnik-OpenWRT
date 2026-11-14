#!/usr/bin/python3
"""Отдаёт данные дневника странице, но только тому, кто знает семейный пароль (если он включён).
Сами данные лежат вне папки, которую раздаёт веб-сервер (/tmp/dnevnik-private), поэтому прямой ссылкой их не достать."""
import os
import sys

HOME_DIR = os.environ.get("DNEVNIK_HOME", "/root/dnevnik")      # другое значение нужно только тестам
OUT_FILE = os.environ.get("DNEVNIK_OUT", "/tmp/dnevnik-private/data.json")
sys.path.insert(0, HOME_DIR)
import auth  # noqa: E402
import devices  # noqa: E402

SEEN = os.environ.get("DNEVNIK_SEEN", devices.SEEN_DEFAULT)
DATA = os.path.join(HOME_DIR, "data")
extra = ""
if os.environ.get("REQUEST_METHOD", "GET") not in ("GET", "HEAD"):
    code, body = 405, b'{"ok": false}'
else:
    code, body = auth.private_page(DATA, OUT_FILE, os.environ.get("HTTP_COOKIE"))
    if code == 200 and auth.enabled(DATA):                 # «последний заход»: только когда страница закрыта паролем, то есть есть кого различать
        try:
            had = devices.dev_id_from_cookie(os.environ.get("HTTP_COOKIE"))
            did = had or devices.new_dev_id()
            devices.touch(SEEN, did, os.environ.get("HTTP_USER_AGENT"), os.environ.get("REMOTE_ADDR"))
            if not had:                                    # вошедшие до обновления получают метку при первом заходе
                secure = os.environ.get("HTTPS", "").lower() == "on" or auth.is_secure_host(os.environ.get("HTTP_HOST"))
                extra = "Set-Cookie: " + devices.dev_cookie_header(did, secure) + "\r\n"
        except Exception:
            pass
sys.stdout.buffer.write((auth.status_line(code) + "\r\nContent-Type: application/json; charset=utf-8\r\nCache-Control: no-store\r\n" + extra + "\r\n").encode("ascii") + body)
