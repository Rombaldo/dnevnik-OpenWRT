#!/usr/bin/python3
"""Помощник «Проверить всё»: проходит по всей системе и говорит, что работает, а что нет и что с этим делать.

    python3 /root/dnevnik/router/doctor.py              полный список с пояснениями
    python3 /root/dnevnik/router/doctor.py --summary    только замечания и итог (так показывает установщик)
    python3 /root/dnevnik/router/doctor.py --report     текст для просьбы о помощи: без паролей, логинов, имён и адресов
    python3 /root/dnevnik/router/doctor.py --json       то же для программ (так читает страница LuCI)

Ничего не меняет и не пишет на роутер. Код выхода: 0 нет проблем (замечания допустимы), 1 есть проблемы.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

HOME = os.environ.get("DNEVNIK_HOME", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PORT = int(os.environ.get("DNEVNIK_PORT", "8765"))
PRIV = os.environ.get("DNEVNIK_PRIV", "/tmp/dnevnik-private")
TMPD = os.environ.get("DNEVNIK_TMP", "/tmp")
TS_INIT = os.environ.get("DNEVNIK_TS_INIT", "/etc/init.d/tailscale")
CRON = os.environ.get("DNEVNIK_CRON", "/etc/crontabs/root")
SITE = os.environ.get("DNEVNIK_SITE", "https://dnevnik.ru")
FREE_PATH = os.environ.get("DNEVNIK_FREE_PATH", "/overlay")
RELEASE_FILE = os.environ.get("DNEVNIK_OPENWRT_RELEASE", "/etc/openwrt_release")
TESTED = (25, 12)                  # на этой версии OpenWrt программа проверена по-настоящему
OLDEST = (21, 2)                   # старше: LuCI и Python устроены иначе, не поддерживается
DATA = os.path.join(HOME, "data")
SYNC_LOG = os.path.join(TMPD, "dnevnik-sync.log")
REMOTE_NEEDS_KB = 15000            # сколько места занимает Tailscale (пакет 26 МБ, на диске после сжатия около 10 МБ, с запасом)
OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"

sys.path.insert(0, HOME)


class R:
    """Один пункт проверки. detail безопасен для отчёта; private (адреса и тому подобное) в отчёт не попадает."""

    def __init__(self, id, status, title, detail="", fix="", private=""):
        self.id, self.status, self.title, self.detail, self.fix, self.private = id, status, title, detail, fix, private

    def as_dict(self, report=False):
        d = {"id": self.id, "status": self.status, "title": self.title, "detail": self.detail, "fix": self.fix}
        if self.private and not report:
            d["private"] = self.private
        return d


def read(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def run(cmd, timeout=8):
    """Запускает команду роутера. None, если команды нет или она не ответила вовремя."""
    if not shutil.which(cmd[0]):
        return None
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def http(url, timeout=8, method="GET"):
    """(код, тело) или (None, текст ошибки). Ответы 4xx и 5xx возвращаются как есть."""
    try:
        req = urllib.request.Request(url, method=method, headers={"User-Agent": "dnevnik-doctor"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def listening(host, port):
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def free_kb():
    fake = os.environ.get("DNEVNIK_FREE_KB")
    if fake:
        return int(fake)
    for p in (FREE_PATH, "/"):
        try:
            st = os.statvfs(p)
            return st.f_bavail * st.f_frsize // 1024
        except OSError:
            continue
    return None


def mb(kb):
    return f"{kb / 1024:.0f} МБ"


# ---------- проверки ----------
def openwrt_note(release):
    """Заметка о версии OpenWrt или None, если это проверенная версия."""
    m = re.match(r"(\d+)\.(\d+)", release or "")
    if not m:
        return R("openwrt", INFO, "Версия OpenWrt", f"{release or 'неизвестна'}: проверялась только 25.12; если что-то не так, пришлите «doctor.py --report»")
    v = (int(m.group(1)), int(m.group(2)))
    if v >= TESTED:
        return None
    if v >= OLDEST:
        return R("openwrt", INFO, "Версия OpenWrt", f"{release}: программа проверялась только на 25.12; на вашей версии должна работать (установщик умеет и apk, и opkg), "
                 "но если что-то не так, пришлите «doctor.py --report»")
    return R("openwrt", WARN, "Версия OpenWrt", f"{release}: старше 21.02, не поддерживается (другие LuCI и Python)",
             "обновите OpenWrt до актуальной версии или используйте другой роутер")


def check_system():
    rel = {}
    for line in read(RELEASE_FILE).splitlines():
        k, _, v = line.partition("=")
        rel[k] = v.strip("'\"")
    version = read(os.path.join(HOME, "VERSION")).strip() or "?"
    arch = (run(["uname", "-m"]) or subprocess.CompletedProcess([], 0, "?", "")).stdout.strip()
    out = [R("system", INFO, "Система", f"программа {version}, OpenWrt {rel.get('DISTRIB_RELEASE', '?')}, {arch}")]
    note = openwrt_note(rel.get("DISTRIB_RELEASE"))
    return out + ([note] if note else [])


def check_space(tailscale_installed):
    kb = free_kb()
    if kb is None:
        return [R("space", WARN, "Свободное место", "не удалось узнать")]
    out = []
    if kb < 3000:
        out.append(R("space", FAIL, "Свободное место", f"{mb(kb)}: мало, программа может не запуститься",
                     "освободите место на роутере (удалите ненужные пакеты) или подключите флешку"))
    else:
        out.append(R("space", OK, "Свободное место", mb(kb)))
    if not tailscale_installed:
        if kb < REMOTE_NEEDS_KB:
            out.append(R("space_remote", WARN, "Место для выхода в интернет (Tailscale)", f"свободно {mb(kb)}, нужно около {mb(REMOTE_NEEDS_KB)}",
                         "освободите место или обойдитесь дневником только дома: выход в интернет необязателен"))
        else:
            out.append(R("space_remote", OK, "Место для выхода в интернет (Tailscale)", f"хватает ({mb(kb)})"))
    return out


def check_web():
    out = []
    code, _ = http(f"http://127.0.0.1:{PORT}/index.html", 5)
    if code == 200:
        out.append(R("web", OK, "Страница дневника на роутере", f"отвечает на порту {PORT}"))
    else:
        return [R("web", FAIL, "Страница дневника на роутере", f"не отвечает на порту {PORT}",
                  "запустите: /etc/init.d/dnevnik restart, затем повторите проверку")]
    code, _ = http(f"http://127.0.0.1:{PORT}/cgi-bin/data", 5)
    if code in (200, 401):
        out.append(R("data", OK, "Выдача данных (cgi-bin/data)", "работает" if code == 200 else "работает, закрыта паролем"))
    else:
        out.append(R("data", FAIL, "Выдача данных (cgi-bin/data)", f"ответ {code}", "переустановите программу: sh /root/dnevnik/router/install.sh"))
    for leak in ("data.json", "offline.html"):                                 # данные не должны лежать в открытой папке
        code, _ = http(f"http://127.0.0.1:{PORT}/{leak}", 5)
        if code == 200:
            out.append(R("leak", FAIL, "Данные в открытой папке", f"{leak} отдаётся без пароля",
                         "обновите программу: sh /root/dnevnik/router/install.sh"))
    return out


def check_name():
    domain = os.environ.get("DNEVNIK_DOMAIN") or "lan"
    r = run(["uci", "-q", "get", "dhcp.@dnsmasq[0].domain"])
    if r and r.stdout.strip():
        domain = r.stdout.strip()
    ip = ""
    r = run(["uci", "-q", "get", "network.lan.ipaddr"])
    if r:
        ip = r.stdout.strip().split("/")[0]
    try:
        got = socket.gethostbyname(f"dnevnik.{domain}")
    except OSError:
        got = ""
    if got and (not ip or got == ip):
        return [R("name", OK, f"Имя dnevnik.{domain}", "указывает на роутер")]
    return [R("name", WARN, f"Имя dnevnik.{domain}", "пока не работает",
              f"откройте дневник по адресу с цифрами (http://{ip or 'адрес-роутера'}:{PORT}) или выключите на телефоне «Частный DNS»; "
              "ещё можно выполнить: sh /root/dnevnik/router/setname.sh", private=f"получено: {got or 'ничего'}")]


def check_cron():
    line = next((l for l in read(CRON).splitlines() if "dnevnik/router/sync.sh" in l and not l.lstrip().startswith("#")), "")
    if not line:
        return [R("cron", FAIL, "Расписание обновлений", "в расписании нет строки обновления",
                  "переустановите программу: sh /root/dnevnik/router/install.sh")]
    r = run(["pidof", "crond"])
    if r is not None and not r.stdout.strip():
        return [R("cron", FAIL, "Расписание обновлений", "служба cron не запущена", "выполните: /etc/init.d/cron enable; /etc/init.d/cron start")]
    return [R("cron", OK, "Расписание обновлений", "настроено: " + " ".join(line.split()[:5]))]


def page_data():
    try:
        return json.loads(read(os.path.join(PRIV, "data.json")) or "{}")
    except ValueError:
        return {}


def check_login_and_sync():
    out = []
    has_login = any(os.path.exists(os.path.join(DATA, n)) for n in ("session.json", "credentials.json"))
    page = page_data()
    status = page.get("status") or {}
    if not has_login:
        out.append(R("login", FAIL, "Вход в Дневник.ру", "ещё не выполнен",
                     "откройте страницу дневника и введите логин и пароль от Дневник.ру (или войдите через Госуслуги на компьютере, см. README)"))
    elif status.get("needLogin"):
        out.append(R("login", FAIL, "Вход в Дневник.ру", "нужно войти заново", "откройте страницу дневника и введите логин и пароль, или повторите вход через Госуслуги на компьютере"))
    else:
        out.append(R("login", OK, "Вход в Дневник.ру", "выполнен"))
    lines = [l for l in read(SYNC_LOG).splitlines() if l and not l.startswith("---")]
    last = lines[-1] if lines else ""
    updated = page.get("updatedAt")
    days = len(page.get("days") or [])
    if updated:
        try:
            age_h = (datetime.now() - datetime.fromisoformat(updated)).total_seconds() / 3600
        except ValueError:
            age_h = None
        if age_h is not None and age_h > 26:
            out.append(R("sync", WARN, "Свежесть данных", f"последнее обновление {age_h:.0f} ч назад",
                         "нажмите «Обновить сейчас» в LuCI или посмотрите журнал: tail /tmp/dnevnik-sync.log"))
        else:
            out.append(R("sync", OK, "Свежесть данных", f"обновлено {age_h:.1f} ч назад, дней в расписании: {days}" if age_h is not None else f"дней в расписании: {days}"))
    elif has_login:
        out.append(R("sync", WARN, "Свежесть данных", "данных пока нет", "подождите пару минут после входа и нажмите «Обновить»; журнал: tail /tmp/dnevnik-sync.log"))
    if last.startswith(("NEED_LOGIN", "ERROR")):
        out.append(R("sync_log", WARN, "Последнее обновление с сайта", last.split(":")[0], "см. журнал: tail /tmp/dnevnik-sync.log"))
    return out


def check_internet():
    code, err = http(SITE, 10)
    if code is not None:
        return [R("internet", OK, "Связь роутера с dnevnik.ru", f"есть (ответ {code})")]
    return [R("internet", FAIL, "Связь роутера с dnevnik.ru", "нет",
              "проверьте интернет на роутере; если сайт у вас открывается только через VPN или прокси, настройте их на роутере", private=err)]


def proxy_info():
    """Прокси для Tailscale, прописанный в службе: (адрес или None, слушает ли там кто-то)."""
    m = re.search(r"HTTPS_PROXY=http://([0-9A-Za-z.\-]+):(\d+)", read(TS_INIT))
    if not m:
        return None, False
    return f"{m.group(1)}:{m.group(2)}", listening(m.group(1), int(m.group(2)))


def tailscale_state():
    r = run(["tailscale", "status", "--json"])
    if r is None or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def funnel_info():
    """(включён ли Funnel на наш порт, адрес или '')."""
    r = run(["tailscale", "funnel", "status"])
    text = (r.stdout + r.stderr) if r else ""
    m = re.search(r"(https://[^\s]+)\s+\(Funnel on\)", text)
    if not m:
        return False, ""
    return (f"proxy http://127.0.0.1:{PORT}" in text), m.group(1).rstrip("/") + "/"


def family_enabled():
    try:
        import auth
        return auth.enabled(DATA)
    except Exception:
        return os.path.exists(os.path.join(DATA, "auth.json"))


def check_remote():
    out = []
    installed = bool(shutil.which("tailscale"))
    if not installed:
        out.append(R("tailscale", INFO, "Выход в интернет (Tailscale)", "не установлен: дневник работает только дома (включить доступ из интернета: install.sh --remote)",
                     "wget -qO- https://raw.githubusercontent.com/Rombaldo/dnevnik-OpenWRT/main/router/install.sh | sh -s -- --remote"))
        return out, installed, False, ""
    st = tailscale_state()
    state = (st or {}).get("BackendState")
    if state == "Running":
        out.append(R("tailscale", OK, "Tailscale", "подключён к вашей сети"))
    elif state == "NeedsLogin":
        out.append(R("tailscale", FAIL, "Tailscale", "нужно войти в аккаунт",
                     "выполните: sh /root/dnevnik/router/remote.sh (покажет ссылку для входа; на странице нажмите Connect device)"))
    elif st is None:
        out.append(R("tailscale", FAIL, "Tailscale", "служба не отвечает", "выполните: /etc/init.d/tailscale restart"))
    else:
        out.append(R("tailscale", WARN, "Tailscale", f"состояние {state}", "подождите минуту и повторите проверку"))
    proxy, alive = proxy_info()
    if proxy and alive:
        out.append(R("proxy", INFO, "Прокси для Tailscale", f"включён ({proxy}), он отвечает",
                     "нужен там, где сервер Tailscale напрямую недоступен; если Clash/прокси остановить, Tailscale потеряет связь"))
    elif proxy:
        out.append(R("proxy", FAIL, "Прокси для Tailscale", f"включён ({proxy}), но по этому адресу ничего не слушает",
                     "запустите Clash/прокси или отключите: sh /root/dnevnik/router/remote.sh proxy off"))
    on, url = funnel_info()
    if on:
        out.append(R("funnel", OK, "Адрес из интернета (Funnel)", "включён", private=url))
    else:
        out.append(R("funnel", WARN, "Адрес из интернета (Funnel)", "не включён",
                     "выполните: sh /root/dnevnik/router/remote.sh", private=url))
    return out, installed, on, url


def check_drops():
    r = run(["logread"], 10)
    n = len([l for l in (r.stdout.splitlines() if r else []) if "tailscaled" in l and "no rules matched" in l])
    if n > 20:
        return [R("drops", INFO, "Сбросы пакетов Tailscale", f"{n} в журнале: часть соединений из интернета может обрываться, браузер обычно повторяет",
                  "известная особенность Tailscale, на работу дневника в браузере не влияет")]
    return []


def check_push():
    """Уведомления: модуль, подключённые устройства, результат последней отправки. Криптографию не загружает, читает только файл."""
    import importlib.util
    have = importlib.util.find_spec("cryptography") is not None
    try:
        st = json.loads(read(os.path.join(DATA, "push.json")) or "{}")
    except ValueError:
        st = {}
    n = len(st.get("subs") or {}) if isinstance(st.get("subs"), dict) else 0
    last = st.get("last") or {}
    if not have:
        return [R("push", INFO, "Уведомления", "не включены (нужен модуль на роутере: sh /root/dnevnik/router/push.sh, около 4 МБ)")]
    if not n:
        return [R("push", INFO, "Уведомления", "модуль установлен, устройств пока нет (на телефоне: ⚙ → «Уведомления» по адресу из интернета)")]
    detail = f"подключено устройств: {n}" + (f"; последняя отправка: доставлено {last.get('sent', 0)}, не вышло {last.get('failed', 0)}" if last else "")
    if st.get("last_error") or last.get("failed"):
        return [R("push", WARN, "Уведомления", detail + (f"; {st['last_error']}" if st.get("last_error") else ""),
                  "проверьте связь роутера; на телефоне ⚙ → «Уведомления» → «Проверить уведомление»; если не помогает, выключите и включите уведомления на телефоне заново")]
    return [R("push", OK, "Уведомления", detail)]


def check_logins():
    """Неверные попытки входа за сутки (из журнала входов). Много попыток: кто-то подбирает пароль."""
    try:
        log = json.loads(read(os.path.join(DATA, "logins.json")) or "[]")
    except ValueError:
        return []
    since = time.time() - 86400
    n = sum(int(e.get("n", 1)) for e in log if isinstance(e, dict) and not e.get("ok") and int(e.get("ts", 0)) >= since) if isinstance(log, list) else 0
    if n >= 10:
        return [R("logins", WARN, "Неверные попытки входа", f"за сутки: {n}; похоже, кто-то подбирает пароль",
                  "задайте пароль подлиннее (LuCI → Службы → Дневник → Пароль семьи); кто и когда пытался, видно в «Устройства и входы»")]
    if n:
        return [R("logins", INFO, "Неверные попытки входа", f"за сутки: {n} (обычно это опечатки)")]
    return []


def run_checks():
    results = []
    results += check_system()
    remote, installed, funnel_on, url = check_remote()
    results += check_space(installed)
    results += check_web()
    results += check_name()
    results += check_cron()
    results += check_login_and_sync()
    results += check_internet()
    family = family_enabled()
    if family:
        results.append(R("family", OK, "Пароль семьи", "включён"))
    elif funnel_on:
        results.append(R("family", FAIL, "Пароль семьи", "НЕ задан, а страница открыта из интернета всем",
                         "срочно задайте пароль: LuCI (Службы → Дневник → Пароль семьи) или python3 /root/dnevnik/router/password.py set 'пароль'"))
    else:
        results.append(R("family", WARN, "Пароль семьи", "не задан: страницу видит любой, кто подключён к вашей сети",
                         "задайте пароль: ⚙ на странице дневника или LuCI (обязательно, если открываете дневник из интернета)"))
    results += remote
    results += check_push()
    results += check_logins()
    results += check_drops()
    return results, url


def summarize(results):
    s = {OK: 0, WARN: 0, FAIL: 0, INFO: 0}
    for r in results:
        s[r.status] += 1
    verdict = "problems" if s[FAIL] else "notes" if s[WARN] else "ok"
    return {"ok": s[OK], "warn": s[WARN], "fail": s[FAIL], "info": s[INFO], "verdict": verdict}


MARK = {OK: "✓", WARN: "!", FAIL: "✗", INFO: "·"}
VERDICT = {"ok": "ИТОГ: всё работает.", "notes": "ИТОГ: работает, но есть замечания (отмечены «!»).", "problems": "ИТОГ: есть проблемы (отмечены «✗»), см. что делать ниже."}


def render(results, summary_only=False, report=False):
    lines = []
    for r in results:
        if summary_only and r.status in (OK, INFO):
            continue
        extra = f" [{r.private}]" if r.private and not report and r.status != INFO else ""
        lines.append(f"{MARK[r.status]} {r.title}: {r.detail}{extra}".rstrip(": "))
        if r.fix and r.status in (WARN, FAIL):
            lines.append(f"    что делать: {r.fix}")
    s = summarize(results)
    lines.append(VERDICT[s["verdict"]])
    return "\n".join(lines)


SAFE_LOG = re.compile(r"^(ok: \d+ дн|NEED_LOGIN|ERROR: \w+)")


def report_text(results):
    """Текст для просьбы о помощи: только состояния, версии и числа."""
    head = [f"Отчёт «Мой дневник», {datetime.now():%Y-%m-%d %H:%M} (без паролей, логинов, имён и адресов)", ""]
    log = [re.sub(r"\d{4}-\d\d-\d\dT[\d:.]+", "<время>", l)[:80] for l in read(SYNC_LOG).splitlines() if SAFE_LOG.match(l)][-5:]
    tail = ["", "Последние строки журнала обновлений:"] + ["  " + l for l in log] if log else []
    return "\n".join(head + [render(results, report=True)] + tail)


def main(argv):
    results, url = run_checks()
    summary = summarize(results)
    if "--json" in argv:
        print(json.dumps({"results": [r.as_dict() for r in results], "summary": summary, "address": url}, ensure_ascii=False))
    elif "--report" in argv:
        print(report_text(results))
    else:
        print(render(results, summary_only="--summary" in argv))
    return 1 if summary["fail"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
