#!/usr/bin/python3
"""Отдаёт данные дневника странице, но только тому, кто знает семейный пароль (если он включён).
Сами данные лежат вне папки, которую раздаёт веб-сервер (/tmp/dnevnik-private), поэтому прямой ссылкой их не достать."""
import os
import sys

HOME_DIR = os.environ.get("DNEVNIK_HOME", "/root/dnevnik")      # другое значение нужно только тестам
OUT_FILE = os.environ.get("DNEVNIK_OUT", "/tmp/dnevnik-private/data.json")
sys.path.insert(0, HOME_DIR)
import auth  # noqa: E402

if os.environ.get("REQUEST_METHOD", "GET") not in ("GET", "HEAD"):
    code, body = 405, b'{"ok": false}'
else:
    code, body = auth.private_page(os.path.join(HOME_DIR, "data"), OUT_FILE, os.environ.get("HTTP_COOKIE"))
sys.stdout.buffer.write((auth.status_line(code) + "\r\nContent-Type: application/json; charset=utf-8\r\nCache-Control: no-store\r\n\r\n").encode("ascii") + body)
