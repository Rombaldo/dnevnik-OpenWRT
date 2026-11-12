"""Семейный пароль на страницу «Мой дневник».

Один общий пароль на всю семью. Пока он не задан, страница открыта, как раньше (домашняя сеть). Когда задан, без него не отдаются
ни данные дневника, ни кнопки «Обновить» и «Выйти из аккаунта», ни форма входа в Дневник.ру.

Как это устроено:
  * пароль хранится не открытым текстом, а в виде PBKDF2-хеша с солью (файл data/auth.json, права 600);
  * после верного пароля устройство получает cookie dnevnik_auth на 180 дней: подпись HMAC, пароль в ней не лежит;
  * смена или отключение пароля сразу делает недействительными все выданные cookie (меняется секретный ключ подписи);
  * перебор пароля тормозится: после 5 неудач подряд ждать 30 секунд, потом дольше (до 15 минут);
  * если файл auth.json есть, но испорчен, страница остаётся закрытой (пароль можно сбросить по SSH: router/password.py).

Модуль без внешних зависимостей и быстрый: его загружает каждый запрос к роутеру.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

AUTH_FILE = "auth.json"
COOKIE = "dnevnik_auth"
COOKIE_DAYS = 180
MIN_LEN, MAX_LEN = 1, 128   # хоть один символ: так решил хозяин (короткий пароль подобрать легко, особенно если страница открыта из интернета)
SHORT_PASSWORD = 3           # короче этого в подсказке ищем не вхождение пароля, а точное совпадение (иначе из-за одной буквы отказывала бы почти любая подсказка)
HINT_MAX = 100            # подсказка к паролю: видна всем на экране входа, поэтому короткая и без самого пароля
ROUNDS = 100_000
FREE_TRIES = 5            # столько неверных паролей подряд принимается без задержки
BASE_LOCK = 30            # потом ждать 30 секунд, 1 минуту, 2 минуты… не дольше MAX_LOCK
MAX_LOCK = 15 * 60

NEED_PASSWORD = "Нужен пароль семьи."
REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed", 429: "Too Many Requests"}


def status_line(code):
    """Строка «Status: 401 Unauthorized» для CGI. Веб-сервер роутера (uhttpd) без пояснения после кода принимает ответ за 200."""
    return f"Status: {code} {REASONS.get(code, 'Error')}"


# ---------------------------------------------------------------- файл с паролем
def _path(data_dir):
    return Path(data_dir) / AUTH_FILE


def enabled(data_dir):
    """Пароль включён, если файл есть (даже испорченный: тогда страница закрыта, а не открыта всем)."""
    return _path(data_dir).exists()


def _read(data_dir):
    try:
        d = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = isinstance(d, dict) and isinstance(d.get("rounds"), int) and all(isinstance(d.get(k), str) and d[k] for k in ("salt", "hash", "secret"))
    return d if ok else None


def _write_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _hash(password, salt_hex, rounds):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), rounds).hex()


def _clean(password):
    return password.strip() if isinstance(password, str) else ""


def clean_hint(hint):
    """Подсказка без управляющих символов и скобок разметки, не длиннее HINT_MAX."""
    return re.sub(r"[\x00-\x1f\x7f<>]", "", hint).strip()[:HINT_MAX] if isinstance(hint, str) else ""


def hint_problem(password, hint):
    """Подсказку видит любой, кто откроет страницу: в ней не должно быть самого пароля."""
    password, hint = _clean(password).lower(), clean_hint(hint).lower()
    if not hint or not password:
        return None
    leaks = hint == password if len(password) < SHORT_PASSWORD else password in hint
    return "Подсказка не должна содержать сам пароль: её видит каждый, кто откроет страницу." if leaks else None


def set_password(data_dir, password, hint=""):
    """Задаёт или меняет пароль и подсказку к нему. Возвращает (ok, сообщение). Все прежние cookie перестают действовать."""
    password = _clean(password)
    if len(password) < MIN_LEN:
        return False, "Пароль не может быть пустым."
    if len(password) > MAX_LEN:
        return False, f"Пароль слишком длинный (не больше {MAX_LEN} символов)."
    problem = hint_problem(password, hint)
    if problem:
        return False, problem
    salt = secrets.token_hex(16)
    doc = {"rounds": ROUNDS, "salt": salt, "hash": _hash(password, salt, ROUNDS), "secret": secrets.token_hex(32),
           "changed": int(time.time()), "hint": clean_hint(hint)}
    _write_private(_path(data_dir), json.dumps(doc, ensure_ascii=False))
    return True, "Пароль сохранён."


def get_hint(data_dir):
    d = _read(data_dir)
    return clean_hint(d.get("hint")) if d else ""


def changed_at(data_dir):
    d = _read(data_dir)
    return int(d.get("changed") or 0) if d else 0


def set_hint(data_dir, hint, password):
    """Меняет только подсказку. Нужен верный текущий пароль: им же проверяем, что в подсказку он не попал."""
    d = _read(data_dir)
    if not d or not check_password(data_dir, password):
        return False, "Неверный текущий пароль."
    problem = hint_problem(password, hint)
    if problem:
        return False, problem
    d["hint"] = clean_hint(hint)
    _write_private(_path(data_dir), json.dumps(d, ensure_ascii=False))
    return True, "Подсказка сохранена."


def disable(data_dir):
    try:
        _path(data_dir).unlink()
    except FileNotFoundError:
        pass


def check_password(data_dir, password):
    d = _read(data_dir)
    password = _clean(password)
    if not d or not password or len(password) > MAX_LEN:
        return False
    return hmac.compare_digest(_hash(password, d["salt"], d["rounds"]), d["hash"])


# ---------------------------------------------------------------- cookie
def _sign(secret_hex, exp):
    return hmac.new(bytes.fromhex(secret_hex), f"v1:{exp}".encode(), hashlib.sha256).hexdigest()


def make_token(data_dir, now=None):
    d = _read(data_dir)
    if not d:
        return None
    exp = int((now if now is not None else time.time()) + COOKIE_DAYS * 86400)
    return f"{exp}.{_sign(d['secret'], exp)}"


def token_valid(data_dir, token, now=None):
    d = _read(data_dir)
    if not d or not isinstance(token, str) or token.count(".") != 1:
        return False
    exp_s, sig = token.split(".")
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    now = now if now is not None else time.time()
    if exp <= now or exp > now + COOKIE_DAYS * 86400 + 120:
        return False
    return hmac.compare_digest(_sign(d["secret"], exp), sig)


def cookie_value(header):
    """Достаёт наш cookie из строки заголовка Cookie."""
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return None


def authed(data_dir, cookie_header, now=None):
    """Можно ли этому запросу видеть дневник: пароль не включён или cookie верный."""
    if not enabled(data_dir):
        return True
    return token_valid(data_dir, cookie_value(cookie_header), now)


def is_secure_host(host):
    """Пришли ли по https. Веб-сервер роутера не передаёт заголовки X-Forwarded-*, поэтому судим по имени: адрес Tailscale Funnel
    (…ts.net) без номера порта открывается только по https. Домашний http (dnevnik.lan, адрес с портом 8765) под это не попадает:
    браузер не принял бы cookie с флагом Secure, и войти дома было бы нельзя."""
    host = (host or "").strip().lower()
    return host.endswith(".ts.net") and ":" not in host


def set_cookie_header(token, secure=False):
    return f"{COOKIE}={token}; Path=/; Max-Age={COOKIE_DAYS * 86400}; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")


def clear_cookie_header(secure=False):
    return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")


# ---------------------------------------------------------------- защита от перебора
def _throttle_read(path):
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return int(d.get("fails", 0)), float(d.get("until", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0, 0.0


def _throttle_write(path, fails, until):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({"fails": fails, "until": until}), encoding="utf-8")
    except OSError:
        pass


def throttle_wait(path, now=None):
    """Сколько секунд ещё нельзя пробовать пароль (0 - можно)."""
    now = now if now is not None else time.time()
    _, until = _throttle_read(path)
    return max(0, int(until - now + 0.999)) if until > now else 0


def throttle_fail(path, now=None):
    now = now if now is not None else time.time()
    fails, _ = _throttle_read(path)
    fails += 1
    until = 0.0
    if fails >= FREE_TRIES:
        until = now + min(MAX_LOCK, BASE_LOCK * 2 ** (fails - FREE_TRIES))
    _throttle_write(path, fails, until)


def throttle_reset(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def _wait_text(seconds):
    return f"Слишком много неверных попыток. Подождите {seconds} сек." if seconds < 90 else f"Слишком много неверных попыток. Подождите {-(-seconds // 60)} мин."


# ---------------------------------------------------------------- запросы страницы
def handle(data_dir, payload, cookie_header, throttle_file, secure=False, now=None):
    """Один запрос к /cgi-bin/auth. Возвращает (код HTTP, ответ-словарь, заголовок Set-Cookie или None).

    Действия: status, login, logout, set (задать или сменить), disable (отключить).
    Смена и отключение требуют и cookie, и текущий пароль: чтобы забытый открытым телефон не позволил сменить пароль."""
    if not isinstance(payload, dict):
        return 400, {"ok": False, "message": "Не удалось прочитать запрос."}, None
    action = payload.get("action")
    is_on = enabled(data_dir)
    is_in = authed(data_dir, cookie_header, now)

    def need_current():
        """Проверка текущего пароля с защитой от перебора. Возвращает (код, ответ) при отказе, иначе None."""
        wait = throttle_wait(throttle_file, now)
        if wait:
            return 429, {"ok": False, "message": _wait_text(wait)}
        if not check_password(data_dir, payload.get("current")):
            throttle_fail(throttle_file, now)
            return 401, {"ok": False, "message": "Неверный текущий пароль."}
        throttle_reset(throttle_file)
        return None

    if action == "status":      # подсказка открыта всем: она нужна на экране входа, пока пароль ещё не введён
        return 200, {"ok": True, "enabled": is_on, "authed": is_in, "hint": get_hint(data_dir) if is_on else ""}, None
    if action == "logout":
        return 200, {"ok": True}, clear_cookie_header(secure)
    if action == "login":
        if not is_on:
            return 200, {"ok": True, "enabled": False}, None
        wait = throttle_wait(throttle_file, now)
        if wait:
            return 429, {"ok": False, "message": _wait_text(wait)}, None
        if check_password(data_dir, payload.get("password")):
            throttle_reset(throttle_file)
            return 200, {"ok": True, "enabled": True}, set_cookie_header(make_token(data_dir, now), secure)
        throttle_fail(throttle_file, now)
        return 401, {"ok": False, "message": "Неверный пароль."}, None
    if action in ("set", "disable", "hint"):
        if is_on:
            if not is_in:
                return 401, {"ok": False, "auth": True, "message": NEED_PASSWORD}, None
            refusal = need_current()
            if refusal:
                return refusal[0], refusal[1], None
        if action == "disable":
            disable(data_dir)
            throttle_reset(throttle_file)
            return 200, {"ok": True, "enabled": False, "message": "Пароль отключён."}, clear_cookie_header(secure)
        if action == "hint":
            if not is_on:
                return 400, {"ok": False, "message": "Пароль не задан."}, None
            ok, message = set_hint(data_dir, payload.get("hint"), payload.get("current"))
            return (200 if ok else 400), {"ok": ok, "message": message}, None
        ok, message = set_password(data_dir, payload.get("password"), payload.get("hint"))
        if not ok:
            return 400, {"ok": False, "message": message}, None
        throttle_reset(throttle_file)
        return 200, {"ok": True, "enabled": True, "message": message}, set_cookie_header(make_token(data_dir, now), secure)
    return 400, {"ok": False, "message": "Неизвестное действие."}, None


def locked_reply():
    """Ответ тем, кто пришёл без пароля."""
    return 401, {"ok": False, "auth": True, "message": NEED_PASSWORD}


def private_page(data_dir, out_path, cookie_header):
    """Содержимое data.json для страницы, но только тому, кто вправе его видеть. Возвращает (код, байты)."""
    if not authed(data_dir, cookie_header):
        code, body = locked_reply()
        return code, json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        text = Path(out_path).read_text(encoding="utf-8")
    except OSError:
        return 404, b"{}"
    try:                                 # страница узнаёт отсюда, включён ли пароль (для экрана «Настройки»)
        page = json.loads(text)
        page.setdefault("status", {})["family"] = enabled(data_dir)
        page["status"]["familyHint"] = get_hint(data_dir) if enabled(data_dir) else ""
        text = json.dumps(page, ensure_ascii=False)
    except (ValueError, AttributeError, TypeError):
        pass
    return 200, text.encode("utf-8")
