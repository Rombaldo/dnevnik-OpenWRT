"""Домашний сервер: раз в N минут обновляет дневник и отдаёт страницу по Wi-Fi.

Запуск:  .venv/bin/python server.py
Открыть: http://<адрес-Mac>:8765  (адрес печатается при запуске)
"""
import json
import re
import socket
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import fetcher

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DEMO = "--demo" in sys.argv   # выдуманные данные, без входа и без обращений к сайту
PORT = 8766 if DEMO else 8765
VERSION = (Path(__file__).resolve().parent / "VERSION").read_text().strip()
INTERVAL = 20 * 60          # обновление раз в 20 минут
NIGHT = (23, 6)             # ночью не ходим на сайт
MIN_MANUAL_GAP = 120        # кнопка «Обновить» не чаще раза в 2 минуты

state = {"needLogin": not fetcher.SESSION.exists(), "error": None, "lastTry": 0, "syncing": False}
wake = threading.Event()


def worker():
    while True:
        now = datetime.now()
        night = now.hour >= NIGHT[0] or now.hour < NIGHT[1]
        due = time.time() - state["lastTry"] >= INTERVAL
        if (due and not night and not state["needLogin"]) or (wake.is_set() and not state["needLogin"]):
            wake.clear()
            state["syncing"] = True
            state["lastTry"] = time.time()
            try:
                fetcher.sync()
                state["error"] = None
            except fetcher.NeedLogin as e:
                state["needLogin"] = True
                state["error"] = str(e)
                print("Нужно войти заново:", e, flush=True)
            except Exception as e:  # сеть упала и т.п. - покажем старые данные
                state["error"] = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            finally:
                state["syncing"] = False
        # вход могли обновить командой login - подхватываем
        if state["needLogin"] and fetcher.SESSION.exists():
            if fetcher.SESSION.stat().st_mtime > state["lastTry"]:
                state["needLogin"] = False
                wake.set()
        time.sleep(5)


def read_data():
    if DEMO:
        from dev.sample import make_sample
        d = make_sample()
        d["status"] = {"needLogin": False, "error": None, "syncing": False}
        d["config"] = {"name": "Тест"}
        d["version"] = VERSION
        return d
    d = json.loads(fetcher.OUT.read_text()) if fetcher.OUT.exists() else {"updatedAt": None, "days": []}
    d["status"] = {"needLogin": state["needLogin"], "error": state["error"], "syncing": state["syncing"]}
    cfg = ROOT / "config.json"   # необязательные настройки: {"name": "Катя"}
    try:
        d["config"] = json.loads(cfg.read_text()) if cfg.exists() else {}
    except Exception:
        d["config"] = {}
    d["version"] = VERSION
    return d


MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml", ".png": "image/png"}


FILE_RE = re.compile(r"/files/(\d+)\.([a-z0-9]+)")
FILE_MIME = {
    "pdf": "application/pdf", "txt": "text/plain; charset=utf-8", "rtf": "application/rtf",
    "doc": "application/msword", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint", "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odt": "application/vnd.oasis.opendocument.text",
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/refresh":
            if time.time() - state["lastTry"] >= MIN_MANUAL_GAP and not state["needLogin"]:
                wake.set()
            return self.send(200, json.dumps({"ok": True}), MIME[".json"])
        self.send(404, "нет", "text/plain; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/data.json":
            return self.send(200, json.dumps(read_data(), ensure_ascii=False), MIME[".json"])
        if path == "/offline.html":  # страница с данными внутри: можно сохранить и открыть без сети
            html = (WEB / "index.html").read_text()
            blob = json.dumps(read_data(), ensure_ascii=False).replace("</", "<\\/")
            html = html.replace("/*INLINE_DATA*/null", blob)
            return self.send(200, html, MIME[".html"],
                             {"Content-Disposition": 'attachment; filename="dnevnik-offline.html"'})
        m = FILE_RE.fullmatch(path)
        if m:   # вложения к домашке: только файлы, которые сами скачали, и только известных типов
            fid, ext = m.group(1), m.group(2)
            if ext not in FILE_MIME:
                return self.send(404, "не найдено", "text/plain; charset=utf-8")
            if DEMO:
                from dev.sample import SAMPLE_PDF
                body = SAMPLE_PDF if (fid, ext) == ("1", "pdf") else None
            else:
                fp = fetcher.FILES / f"{fid}.{ext}"
                body = fp.read_bytes() if fp.is_file() else None
            if body is None:
                return self.send(404, "не найдено", "text/plain; charset=utf-8")
            raw = parse_qs(urlparse(self.path).query).get("name", [f"{fid}.{ext}"])[0]
            name = re.sub(r'[\x00-\x1f"\\/:*?<>|]+', " ", raw).strip()[:120] or f"{fid}.{ext}"
            if not name.lower().endswith("." + ext):
                name += "." + ext
            return self.send(200, body, FILE_MIME[ext], {
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"})
        if path == "/":
            path = "/index.html"
        f = (WEB / path.lstrip("/")).resolve()
        if WEB not in f.parents or not f.is_file():
            return self.send(404, "не найдено", "text/plain; charset=utf-8")
        self.send(200, f.read_bytes(), MIME.get(f.suffix, "application/octet-stream"))


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    if DEMO:
        print(f"ДЕМО-режим (выдуманные данные): http://localhost:{PORT}", flush=True)
    else:
        threading.Thread(target=worker, daemon=True).start()
        wake.set()  # сразу обновимся при запуске
    print(f"Дневник {VERSION} запущен: http://{lan_ip()}:{PORT}  (на этом Mac: http://localhost:{PORT})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
