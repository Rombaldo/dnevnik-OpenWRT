#!/usr/bin/env python3
"""Поставить «Мой дневник» на роутер с компьютера, без терминала на роутере: один запуск, вопросы простыми словами.

Запуск (из папки программы):
    Windows:   двойной щелчок по setup-windows.bat   (или  py -3 tools\\setup_computer.py)
    Mac:       двойной щелчок по «Установить-на-роутер.command»   (или  python3 tools/setup_computer.py)
    Linux:     python3 tools/setup_computer.py

Что происходит:
  1. спрашивает адрес роутера и что нужно (страница в LuCI, доступ из интернета, пароль семьи);
  2. один раз просит пароль роутера (тот же, что для веб-интерфейса) и по SSH ставит программу, задаёт пароль семьи,
     включает доступ из интернета (покажет, где в браузере нажать «Connect device» и «Enable Funnel»: ссылки откроет сам);
  3. помогает войти в Дневник.ру (на странице по логину и паролю или через Госуслуги на этом компьютере);
  4. в конце печатает адреса и итоговую проверку.
Пароль семьи уходит на роутер внутри SSH-соединения и не попадает ни в список процессов, ни в журналы.
"""
import argparse
import getpass
import os
import platform
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "Rombaldo/dnevnik-OpenWRT"
INSTALL_URL = f"https://raw.githubusercontent.com/{REPO}/main/router/install.sh"
REMOTE_APP = "/root/dnevnik"


def q(text):
    """Строка в одинарных кавычках для оболочки роутера: безопасна для любых символов, в том числе кавычек."""
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


WIN_OLD_TEXT = """На этой версии Windows (7 или 8) нет встроенной программы ssh. Выход:
  1. Установите Git for Windows (git-scm.com/download/win): ssh идёт в комплекте.
  2. Откройте «Git Bash» и выполните здесь:  python tools/setup_computer.py
  Если Python ещё нет: последняя версия, работающая на Windows 7, это Python 3.8 (python.org/downloads/release/python-3810/, при установке отметьте «Add Python to PATH»).
  Либо поставьте дневник с любого другого компьютера (Windows 10/11, Mac, Linux) или по SSH из PuTTY (способ Б в README: одна команда).
  Заметка: вход через Госуслуги (tools/login_computer.py) на Windows 7 не работает, сделайте его на другом компьютере или войдите на странице по логину и паролю."""
WIN_NEW_TEXT = """Не найдена программа ssh. В Windows 10/11 её включают так: Параметры → Приложения → Дополнительные компоненты → Добавить компонент → «Клиент OpenSSH».
После этого запустите программу снова."""
NIX_TEXT = """Не найдена программа ssh. Mac: она есть в системе (проверьте, что Терминал запускается). Linux: sudo apt install openssh-client (или аналог вашей системы)."""


def check_environment(which=shutil.which, system=None, winver=None, pyver=None):
    """Что мешает установке, понятными словами. Пустой список: можно продолжать. Параметры нужны тестам."""
    system = system or platform.system()
    pyver = pyver or sys.version_info
    problems = []
    if tuple(pyver[:2]) < (3, 7):
        problems.append("Нужен Python 3.7 или новее (на Windows 7 это Python 3.8).")
    if not which("ssh"):
        if system == "Windows":
            old = (winver or (getattr(sys, "getwindowsversion", lambda: (10,))()))[0] < 10
            problems.append(WIN_OLD_TEXT if old else WIN_NEW_TEXT)
        else:
            problems.append(NIX_TEXT)
    return problems


def old_windows(system=None, winver=None):
    system = system or platform.system()
    return system == "Windows" and (winver or getattr(sys, "getwindowsversion", lambda: (10,))())[0] < 10


def guess_router():
    """Адрес роутера = шлюз по умолчанию этого компьютера. None, если определить не вышло."""
    cmds = [["route", "-n", "get", "default"], ["ip", "route", "show", "default"], ["ipconfig"]]
    pats = [r"gateway:\s*(\d+\.\d+\.\d+\.\d+)", r"default via (\d+\.\d+\.\d+\.\d+)", r"(?:Default Gateway|Основной шлюз)[^\d]*(\d+\.\d+\.\d+\.\d+)"]
    for cmd, pat in zip(cmds, pats):
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=5).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(pat, out)
        if m:
            return m.group(1)
    return None


def build_script(luci=True, remote=True, password="", hint="", install_url=INSTALL_URL, app=REMOTE_APP):
    """Сценарий для роутера. Идёт по SSH через stdin (sh -s), поэтому пароль семьи нигде не светится в списке процессов:
    он передаётся программе password.py переменными окружения, а не аргументами."""
    lines = [
        "export DNEVNIK_MACHINE=1",
        'echo "==> Ставлю «Мой дневник» на роутер…"',
        f"( wget -qO- {q(install_url)} | sh -s -- {'--luci' if luci else '--no-luci'} ) </dev/null",
        'rc=$?; [ "$rc" = "0" ] || { echo "@@FAILED install $rc"; exit 1; }',
    ]
    if password:
        lines += [
            'echo "==> Задаю пароль семьи…"',
            f"DNEVNIK_NEW_PASSWORD={q(password)} DNEVNIK_NEW_HINT={q(hint)} python3 {app}/router/password.py set-env </dev/null",
            'rc=$?; [ "$rc" = "0" ] || { echo "@@FAILED password $rc"; exit 1; }',
        ]
    if remote:
        lines += [
            'echo "==> Настраиваю доступ из интернета…"',
            f"sh {app}/router/remote.sh install </dev/null",
            'echo "@@REMOTE_CODE $?"',
        ]
    lines += [
        'echo "==> Итоговая проверка"',
        f"python3 {app}/router/doctor.py --summary </dev/null",
        'echo "@@DONE"',
    ]
    return "\n".join(lines) + "\n"


class Outcome:
    def __init__(self):
        self.login_url = self.address = None
        self.needs = []
        self.failed = None
        self.remote_code = None
        self.done = False
        self.code = None


def handle_line(line, out, opener, say):
    """Строки «@@…» для программы, остальное показывает человеку."""
    if not line.startswith("@@"):
        say(line)
        return
    kind, _, rest = line[2:].partition(" ")
    if kind == "LOGIN_URL":
        out.login_url = rest.strip()
        say("")
        say(">>> Открываю браузер: войдите в Tailscale и нажмите «Connect device». Если окно не открылось, откройте ссылку выше вручную.")
        try:
            opener(out.login_url)
        except Exception:
            pass
    elif kind == "ADDRESS":
        out.address = rest.strip()
    elif kind == "NEEDS":
        out.needs.append(rest.strip())
        name, _, link = rest.strip().partition(" ")
        if name == "funnel_enable" and link:
            say("")
            say(">>> Открываю браузер: нажмите «Enable Funnel». Потом запустите эту программу ещё раз: сделанное она пропустит.")
            try:
                opener(link)
            except Exception:
                pass
    elif kind == "FAILED":
        out.failed = rest.strip()
    elif kind == "REMOTE_CODE":
        out.remote_code = int(rest.strip() or 0) if rest.strip().isdigit() else None
    elif kind == "DONE":
        out.done = True


def run_ssh(router, script, opener=webbrowser.open, say=print, user="root", ssh="ssh", extra=()):
    """Выполняет сценарий на роутере по SSH и разбирает его вывод. Пароль роутера ssh спрашивает сам, на терминале."""
    cmd = [ssh, "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30", "-o", "StrictHostKeyChecking=accept-new", *extra, f"{user}@{router}", "sh -s"]
    out = Outcome()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    p.stdin.write(script.encode("utf-8"))
    p.stdin.close()
    for raw in p.stdout:
        handle_line(raw.decode("utf-8", "replace").rstrip("\r\n"), out, opener, say)
    out.code = p.wait()
    return out


def yes(ask, text, default=True):
    answer = ask(f"{text} [{'Д/н' if default else 'д/Н'}]: ").strip().lower()
    return default if not answer else answer in ("д", "да", "y", "yes", "1")


def main(argv=None, ask=input, ask_secret=getpass.getpass, runner=run_ssh, opener=webbrowser.open, say=print, login_tool=None, env_check=check_environment):
    ap = argparse.ArgumentParser(description="Поставить «Мой дневник» на роутер с компьютера")
    ap.add_argument("--router", help="адрес роутера (иначе спросит)")
    ap.add_argument("--luci", dest="luci", action="store_true", default=None, help="поставить страницу «Службы → Дневник» в веб-интерфейсе роутера")
    ap.add_argument("--no-luci", dest="luci", action="store_false")
    ap.add_argument("--remote", dest="remote", action="store_true", default=None, help="включить доступ из интернета")
    ap.add_argument("--no-remote", dest="remote", action="store_false")
    ap.add_argument("--skip-login", action="store_true", help="не предлагать войти в Дневник.ру")
    args = ap.parse_args(argv)

    problems = env_check()
    if problems:
        for text in problems:
            say(text)
        say("\nИсправьте это и запустите программу снова.")
        return 2
    say("Установка «Мой дневник» на роутер. Понадобится: роутер с OpenWrt, его пароль (как для веб-интерфейса) и 10–15 минут.\n")
    router = args.router
    if not router:
        guess = guess_router() or "192.168.1.1"
        router = ask(f"Адрес роутера [{guess}]: ").strip() or guess
    luci = args.luci if args.luci is not None else yes(ask, "Поставить страницу настройки в веб-интерфейсе роутера (Службы → Дневник)? Рекомендуется")
    remote = args.remote if args.remote is not None else yes(
        ask, "Открывать дневник из интернета (с любого телефона, без приложений)? Нужен бесплатный аккаунт Tailscale; без этого дневник работает только дома")
    password = hint = ""
    if remote:
        say("\nДоступ из интернета без пароля небезопасен: задайте пароль для семьи (один на всех).")
    else:
        say("")
    while True:
        password = ask_secret("Пароль для семьи (лучше фраза; при вводе символы не видны; Enter — не задавать): ")
        if not password and not remote:
            break
        if not password:
            say("Для доступа из интернета пароль нужен.")
            continue
        if password == ask_secret("Повторите пароль: "):
            break
        say("Пароли не совпали, попробуем ещё раз.")
    if password:
        hint = ask("Подсказка к паролю (её увидит каждый на экране входа, сам пароль писать нельзя; можно пропустить): ").strip()
    say("\nСейчас программа попросит пароль роутера (при вводе символы не видны). Дальше всё пойдёт само; следите за сообщениями.\n")
    out = runner(router, build_script(luci, remote, password, hint), opener=opener, say=say)

    say("")
    if out.failed or not out.done:
        say("Установка не завершилась" + (f" (шаг: {out.failed})" if out.failed else "") + ". Что проверить: адрес и пароль роутера, интернет на роутере.")
        say("Запустите программу ещё раз: сделанное она пропустит. Если не помогло, пришлите вывод выше тому, кто вам помогает.")
        return 1
    if any(n.startswith(("funnel_enable", "space", "password")) for n in out.needs):
        say("Основная часть готова. Доступ из интернета ждёт вашего действия в браузере (описано выше). Сделайте это и запустите программу ещё раз.")
    base = f"http://{router}:{8765}"
    say(f"Дневник дома: {base}  (или http://dnevnik.lan)")
    if out.address:
        say(f"Дневник из интернета: {out.address}")
    if args.skip_login:
        return 0
    say("\nОсталось войти в Дневник.ру. Как вы входите на сайт?")
    say("  1. По логину и паролю: введёте их на странице дневника (самый простой способ)")
    say("  2. Через Госуслуги: войдёте на этом компьютере (программа подготовит браузер, ~200 МБ, один раз)"
        + ("  [на этой Windows не работает: сделайте на другом компьютере]" if old_windows() else ""))
    say("  3. Сделаю позже")
    choice = ask("Номер [1]: ").strip() or "1"
    if choice == "1":
        say(f"Открываю страницу: {base}  Введите там логин и пароль от Дневник.ру.")
        try:
            opener(base)
        except Exception:
            pass
    elif choice == "2":
        tool = login_tool or (lambda r: subprocess.call([sys.executable, str(ROOT / "tools" / "login_computer.py"), "--router", r]))
        return tool(router)
    else:
        say("Хорошо. Когда будете готовы: откройте страницу дневника или запустите tools/login_computer.py (см. README).")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
