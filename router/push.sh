#!/bin/sh
# Уведомления о новых оценках, домашке, отменах уроков и пропусках. Нужен один пакет для шифрования сообщений: python3-cryptography
# (около 4 МБ на роутере). Остальное уже в программе. Безопасно запускать повторно.
#
#   sh /root/dnevnik/router/push.sh            поставить модуль и подготовить ключи
#   sh /root/dnevnik/router/push.sh status     что установлено и сколько устройств подключено
#
# Включается на телефоне: откройте дневник по АДРЕСУ ИЗ ИНТЕРНЕТА (https; по dnevnik.lan уведомления не работают),
# ⚙ → «Уведомления» → «Включить». На iPhone нужна iOS 16.4 или новее и страница, добавленная на экран «Домой».
#
# Коды выхода: 0 готово; 1 ошибка; 2 не хватает места (дневник и так работает, уведомления не включены).
APP="${DNEVNIK_HOME:-/root/dnevnik}"
PY="${DNEVNIK_PY:-python3}"
MIN_FREE_KB="${DNEVNIK_MIN_FREE_KB:-8000}"        # пакет с зависимостями занимает около 4 МБ, с запасом

say()  { echo "==> $*"; }
die()  { echo "ОШИБКА: $*" >&2; exit 1; }
have() { "$PY" -c 'import cryptography' >/dev/null 2>&1; }
free_kb() { p=/overlay; [ -d "$p" ] || p=/; df -k "$p" 2>/dev/null | tail -n 1 | awk '{print $(NF-2)}'; }

devices() {
    [ -f "$APP/data/push.json" ] || { echo 0; return; }
    "$PY" -c 'import json,sys; print(len((json.load(open(sys.argv[1])).get("subs") or {})))' "$APP/data/push.json" 2>/dev/null || echo 0
}

show_status() {
    if have; then echo "Модуль уведомлений: установлен"; else echo "Модуль уведомлений: не установлен (sh $APP/router/push.sh)"; fi
    echo "Подключено устройств: $(devices)"
}

install_module() {
    [ -d "$APP/router" ] || die "дневник не установлен ($APP не найден): сначала выполните команду установки из README"
    if have; then say "Модуль уведомлений уже установлен."; return 0; fi
    kb=$(free_kb)
    if [ -n "$kb" ] && [ "$kb" -lt "$MIN_FREE_KB" ] 2>/dev/null; then
        echo "!!  На роутере свободно $((kb / 1024)) МБ, а для модуля уведомлений нужно около $((MIN_FREE_KB / 1024)) МБ."
        echo "    Освободите место (удалите ненужные пакеты) либо подключите флешку. Дневник без уведомлений работает как раньше."
        exit 2
    fi
    say "Ставлю python3-cryptography…"
    if command -v apk >/dev/null 2>&1; then
        apk update >/dev/null 2>&1 || true
        apk add python3-cryptography || die "не удалось поставить python3-cryptography"
    elif command -v opkg >/dev/null 2>&1; then
        opkg update >/dev/null 2>&1 || true
        opkg install python3-cryptography || die "не удалось поставить python3-cryptography"
    else
        die "не нашёл apk или opkg (это не OpenWrt?)"
    fi
    have || die "модуль поставился, но Python его не видит"
}

prepare_keys() {
    # ключ отправителя создаётся один раз и больше не меняется (иначе подключённые телефоны пришлось бы подключать заново)
    "$PY" - "$APP" <<'PYEOF' || die "не удалось подготовить ключи уведомлений"
import sys
sys.path.insert(0, sys.argv[1])
import push
code, body = push.handle(sys.argv[1] + "/data", {"action": "config"})
assert body.get("ready"), body
PYEOF
}

case "${1:-install}" in
    install)
        install_module
        prepare_keys
        say "Готово. Включите уведомления на телефоне:"
        echo "    1. Откройте дневник по адресу из интернета (https). Адрес: python3 $APP/router/doctor.py или «Службы → Дневник» в LuCI."
        echo "    2. ⚙ → «Уведомления» → «Включить». На iPhone: iOS 16.4+ и страница на экране «Домой»."
        echo "    3. Нажмите «Проверить уведомление». Выберите, о чём сообщать, и тихие часы." ;;
    status) show_status ;;
    *) die "неизвестная команда: $1 (бывают: install, status)" ;;
esac
