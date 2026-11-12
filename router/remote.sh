#!/bin/sh
# Выход в интернет для «Мой дневник»: Tailscale + Funnel. Даёт постоянный адрес вида https://dnevnik.<ваша-сеть>.ts.net,
# который открывается с любого телефона и компьютера без установки приложений. Бесплатно.
#
#   sh /root/dnevnik/router/remote.sh                 настроить всё до конца (можно запускать повторно: сделанное пропускается)
#   sh /root/dnevnik/router/remote.sh status          что сейчас включено
#   sh /root/dnevnik/router/remote.sh proxy on [адрес:порт]   Tailscale ходит к своему серверу через прокси (по умолчанию 127.0.0.1:7890)
#   sh /root/dnevnik/router/remote.sh proxy off       вернуть прямое подключение
#   sh /root/dnevnik/router/remote.sh funnel off      убрать дневник из интернета
#
# Что делает по шагам (если шаг уже сделан, пропускает):
#   1. проверяет, что дневник установлен, хватает места и задан пароль семьи (без пароля в интернет не выставляет);
#   2. ставит пакет tailscale и запускает службу;
#   3. просит войти в ваш аккаунт Tailscale (бесплатный) по ссылке: на странице нужно нажать «Connect device»;
#   4. если после входа Tailscale не заработал, а на роутере есть прокси (SSClash и подобные), включает прокси для Tailscale (см. ниже);
#   5. включает Funnel на дневник и печатает адрес.
# Шаги 3 и 5 требуют ваших действий в браузере: скрипт скажет, что нажать, и его нужно запустить ещё раз.
#
# ПРОКСИ ДЛЯ TAILSCALE (почему и зачем). У некоторых провайдеров Tailscale входит в аккаунт, но дальше «зависает» и роутер остаётся
# в состоянии «NeedsLogin»: регистрация проходит, а длинное соединение за картой сети не отвечает. Помогает подать трафик Tailscale
# в локальный прокси (у нас это SSClash/Clash, порт 7890): в запуск службы /etc/init.d/tailscale добавляется строка
#     HTTPS_PROXY=http://127.0.0.1:7890  HTTP_PROXY=http://127.0.0.1:7890  NO_PROXY=127.0.0.1,localhost,192.168.0.0/16
# Побочный эффект: если Clash остановить, Tailscale потеряет связь. Обновление пакета tailscale может затереть эту строку:
# тогда выполните «remote.sh proxy on» ещё раз. Копия исходного файла: /etc/init.d/tailscale.bak-dnevnik.
#
# Коды выхода: 0 всё готово; 1 ошибка; 2 нужно ваше действие (скрипт написал какое), потом запустите его снова.
APP="${DNEVNIK_HOME:-/root/dnevnik}"
TS_INIT="${DNEVNIK_TS_INIT:-/etc/init.d/tailscale}"   # файл службы, в который добавляется прокси
TS_SVC="${DNEVNIK_TS_SVC:-$TS_INIT}"                   # то, чем службу запускают и перезапускают (отдельно только для тестов)
PORT="${DNEVNIK_PORT:-8765}"
TS_HOST="${DNEVNIK_TS_HOSTNAME:-dnevnik}"
LOGIN_WAIT="${DNEVNIK_LOGIN_WAIT:-600}"       # сколько секунд ждать, пока вы войдёте в аккаунт Tailscale
STEP="${DNEVNIK_STEP:-3}"                     # как часто смотреть состояние, секунд
MIN_FREE_KB="${DNEVNIK_MIN_FREE_KB:-15000}"   # Tailscale на диске занимает около 10 МБ (пакет 26 МБ), с запасом
PROXY_DEFAULT="127.0.0.1:7890"
TMPD="${DNEVNIK_TMP:-/tmp}"
MACHINE="${DNEVNIK_MACHINE:-}"                # если задано, печатаем ещё и строки «@@…» для программы установки на компьютере

say()  { echo "==> $*"; }
warn() { echo "!!  $*"; }
die()  { echo "ОШИБКА: $*" >&2; exit 1; }
mark() { [ -n "$MACHINE" ] && echo "@@$*"; return 0; }

ts_installed() { command -v tailscale >/dev/null 2>&1; }
backend_state() { tailscale status --json 2>/dev/null | sed -n 's/.*"BackendState": *"\([A-Za-z]*\)".*/\1/p' | head -n 1; }
listening() { netstat -ltn 2>/dev/null | grep -q ":$1 "; }

free_kb() {
    p=/overlay; [ -d "$p" ] || p=/
    df -k "$p" 2>/dev/null | tail -n 1 | awk '{print $(NF-2)}'
}

# ---------- прокси для Tailscale ----------
proxy_current() { sed -n 's/.*HTTPS_PROXY=http:\/\/\([^ ]*\).*/\1/p' "$TS_INIT" 2>/dev/null | head -n 1; }

proxy_clear() {
    [ -f "$TS_INIT" ] || return 0
    # свой блок и ручную правку прежних версий (строка с HTTPS_PROXY и её комментарий); без «sed -i»: он по-разному работает в BusyBox и BSD
    sed -e '/# >>> dnevnik-proxy/,/# <<< dnevnik-proxy/d' -e '/HTTPS_PROXY=http/d' -e '/# Через Clash: прямой доступ/d' "$TS_INIT" > "$TS_INIT.new" \
        && cat "$TS_INIT.new" > "$TS_INIT"     # cat, а не mv: права и владелец файла службы сохраняются
    rm -f "$TS_INIT.new"
}

proxy_on() {
    hp="${1:-$PROXY_DEFAULT}"
    case "$hp" in *[!0-9A-Za-z.:-]*|"") die "адрес прокси должен быть вида 127.0.0.1:7890" ;; esac
    [ -f "$TS_INIT" ] || die "нет файла $TS_INIT: сначала установите Tailscale (запустите remote.sh без параметров)"
    [ -f "$TS_INIT.bak-dnevnik" ] || cp "$TS_INIT" "$TS_INIT.bak-dnevnik"
    proxy_clear
    line=$(grep -n 'procd_append_param env TS_NO_LOGS_NO_SUPPORT' "$TS_INIT" | head -n 1 | cut -d: -f1)
    [ -n "$line" ] || die "не нашёл в $TS_INIT место для строки прокси (файл от другой версии Tailscale)"
    {
        head -n "$line" "$TS_INIT"
        echo "  # >>> dnevnik-proxy (добавлено программой «Дневник»: Tailscale ходит к серверу через прокси, см. README)"
        echo "  procd_append_param env HTTPS_PROXY=http://$hp HTTP_PROXY=http://$hp NO_PROXY=127.0.0.1,localhost,192.168.0.0/16"
        echo "  # <<< dnevnik-proxy"
        tail -n +"$((line + 1))" "$TS_INIT"
    } > "$TS_INIT.new" && cat "$TS_INIT.new" > "$TS_INIT"
    rm -f "$TS_INIT.new"
    say "Прокси для Tailscale включён: $hp"
    "$TS_SVC" restart >/dev/null 2>&1
    sleep "${DNEVNIK_RESTART_WAIT:-5}"
}

proxy_off() {
    [ -f "$TS_INIT" ] || { say "Tailscale не установлен, прокси отключать нечего."; return 0; }
    proxy_clear
    say "Прокси для Tailscale отключён. Перезапускаю службу…"
    "$TS_SVC" restart >/dev/null 2>&1
}

# ---------- шаги ----------
check_prereq() {
    [ "$(id -u)" = "0" ] || [ -n "$DNEVNIK_ALLOW_NONROOT" ] || die "запускайте от root"
    [ -d "$APP/router" ] || die "дневник не установлен ($APP не найден): сначала выполните команду установки из README"
    command -v apk >/dev/null 2>&1 || command -v opkg >/dev/null 2>&1 || die "не нашёл apk или opkg (это не OpenWrt?)"
}

check_password() {
    if [ ! -f "$APP/data/auth.json" ]; then
        echo
        warn "Пароль семьи не задан. Выставлять дневник в интернет без пароля нельзя: оценки увидит любой, у кого есть ссылка."
        echo "    Задайте пароль одним из способов и запустите эту команду снова:"
        echo "      • в LuCI: Службы → Дневник → Пароль семьи;"
        echo "      • на странице дневника: ⚙ → Пароль для семьи;"
        echo "      • по SSH:  python3 $APP/router/password.py set 'пароль' 'подсказка'"
        mark "NEEDS password"
        exit 2
    fi
}

install_tailscale() {
    if ts_installed; then say "Tailscale уже установлен."; return 0; fi
    kb=$(free_kb)
    if [ -n "$kb" ] && [ "$kb" -lt "$MIN_FREE_KB" ] 2>/dev/null; then
        warn "На роутере свободно $((kb / 1024)) МБ, а для Tailscale нужно около $((MIN_FREE_KB / 1024)) МБ."
        echo "    Освободите место (удалите ненужные пакеты или большие файлы) либо подключите флешку."
        echo "    Дневник и без выхода в интернет работает дома; это шаг необязательный."
        mark "NEEDS space"
        exit 2
    fi
    say "Ставлю пакет tailscale…"
    if command -v apk >/dev/null 2>&1; then
        apk update >/dev/null 2>&1 || true
        apk add tailscale || die "не удалось поставить пакет tailscale"
    else
        opkg update >/dev/null 2>&1 || true
        opkg install tailscale || die "не удалось поставить пакет tailscale"
    fi
    ts_installed || die "после установки команда tailscale не найдена"
}

start_service() {
    [ -x "$TS_SVC" ] || die "нет службы $TS_SVC"
    "$TS_SVC" enable >/dev/null 2>&1
    if [ -z "$(backend_state)" ]; then
        say "Запускаю службу Tailscale…"
        "$TS_SVC" start >/dev/null 2>&1
        i=0; while [ -z "$(backend_state)" ] && [ $i -lt 20 ]; do sleep 1; i=$((i + 1)); done
    fi
    [ -n "$(backend_state)" ] || die "служба Tailscale не запустилась: посмотрите logread | grep tailscaled"
}

wait_running() {
    waited=0
    while [ "$waited" -lt "$1" ]; do
        [ "$(backend_state)" = "Running" ] && return 0
        sleep "$STEP"; waited=$((waited + STEP))
    done
    [ "$(backend_state)" = "Running" ]
}

login() {
    LOG="$TMPD/dnevnik-ts-up.log"; rm -f "$LOG"
    ( tailscale up --accept-dns=false --accept-routes=false --hostname="$TS_HOST" --reset --timeout="${LOGIN_WAIT}s" > "$LOG" 2>&1 < /dev/null & )
    url=""; i=0
    while [ $i -lt 30 ]; do
        url=$(grep -o 'https://login.tailscale.com/[^ ]*' "$LOG" 2>/dev/null | head -n 1)
        [ -n "$url" ] && break
        [ "$(backend_state)" = "Running" ] && return 0
        sleep 1; i=$((i + 1))
    done
    if [ -z "$url" ]; then
        warn "Tailscale не выдал ссылку для входа. Скорее всего, нет связи с его сервером."
        [ -s "$LOG" ] && sed 's/^/    /' "$LOG"
        return 1
    fi
    echo
    echo "ТРЕБУЕТСЯ ВАШЕ ДЕЙСТВИЕ: войдите в аккаунт Tailscale (бесплатный, можно через Google или Apple)."
    echo "  1. Откройте ссылку в браузере:  $url"
    echo "  2. Войдите (или зарегистрируйтесь)."
    echo "  3. На странице нажмите кнопку «Connect device»: одного входа мало."
    echo "  Скрипт ждёт до $((LOGIN_WAIT / 60)) минут и продолжит сам."
    mark "LOGIN_URL $url"
    wait_running "$LOGIN_WAIT"
}

ensure_login() {
    [ "$(backend_state)" = "Running" ] && { say "Tailscale уже подключён к вашей сети."; return 0; }
    login && { say "Вход выполнен."; return 0; }
    # Вошли (или не успели), а Tailscale не заработал. Если на роутере есть прокси, пробуем через него (так у нас и заработало).
    if [ -z "$(proxy_current)" ] && listening "${PROXY_DEFAULT##*:}"; then
        echo
        warn "Tailscale не подключился напрямую. На роутере работает прокси ($PROXY_DEFAULT): пробую подать в него трафик Tailscale."
        warn "Это нужно там, где провайдер мешает связи с сервером Tailscale. Подробности: комментарий в начале remote.sh и README."
        proxy_on "$PROXY_DEFAULT"
        login && { say "Вход выполнен (через прокси)."; return 0; }
    fi
    echo
    die "Tailscale не подключился. Что проверить: связь роутера с интернетом; нажали ли «Connect device»; вывод «python3 $APP/router/doctor.py»; журнал: logread | grep tailscaled"
}

funnel_url() { tailscale funnel status 2>/dev/null | sed -n 's/^\(https:\/\/[^ ]*\) (Funnel on).*/\1/p' | head -n 1 | sed 's|/*$|/|'; }
funnel_on_for_us() { tailscale funnel status 2>/dev/null | grep -q "proxy http://127.0.0.1:$PORT"; }

enable_funnel() {
    if funnel_on_for_us && [ -n "$(funnel_url)" ]; then say "Funnel уже включён."; return 0; fi
    FL="$TMPD/dnevnik-funnel.log"; rm -f "$FL"
    ( tailscale funnel --bg "$PORT" > "$FL" 2>&1 < /dev/null & )
    i=0
    while [ $i -lt 20 ]; do
        grep -q "Available on the internet" "$FL" 2>/dev/null && break
        grep -q 'login.tailscale.com/f/' "$FL" 2>/dev/null && break
        sleep 1; i=$((i + 1))
    done
    if grep -q 'login.tailscale.com/f/' "$FL" 2>/dev/null; then
        for p in $(ps | grep '[t]ailscale funnel' | awk '{print $1}'); do kill "$p" 2>/dev/null; done
        url=$(grep -o 'https://login.tailscale.com/[^ ]*' "$FL" | head -n 1)
        echo
        echo "ТРЕБУЕТСЯ ВАШЕ ДЕЙСТВИЕ: разрешите Funnel в вашем аккаунте Tailscale (один раз)."
        echo "  1. Откройте ссылку в браузере:  $url"
        echo "  2. Нажмите «Enable Funnel». Заодно включатся HTTPS-сертификаты:"
        echo "     имя вида dnevnik.<ваша-сеть>.ts.net навсегда попадёт в публичный журнал сертификатов (больше ничего о вас там не будет)."
        echo "  3. Запустите эту команду ещё раз: sh $APP/router/remote.sh"
        mark "NEEDS funnel_enable $url"
        exit 2
    fi
    grep -q "Available on the internet" "$FL" 2>/dev/null || { [ -s "$FL" ] && sed 's/^/    /' "$FL"; die "не удалось включить Funnel"; }
    say "Funnel включён."
}

show_status() {
    if ! ts_installed; then echo "Tailscale: не установлен"; return 0; fi
    echo "Tailscale: $(backend_state)"
    p=$(proxy_current); [ -n "$p" ] && echo "Прокси для Tailscale: $p" || echo "Прокси для Tailscale: нет (прямое подключение)"
    u=$(funnel_url)
    if [ -n "$u" ]; then echo "Адрес из интернета: $u"; else echo "Адрес из интернета: Funnel не включён"; fi
}

finish() {
    u=$(funnel_url)
    [ -n "$u" ] || die "Funnel не включился: запустите «sh $APP/router/remote.sh status»"
    echo
    say "Готово. Адрес дневника из интернета:"
    echo
    echo "    $u"
    echo
    echo "  Проверьте: выключите на телефоне Wi-Fi, откройте адрес через мобильный интернет и введите пароль семьи."
    echo "  Этот адрес знаете только вы: давайте его только семье. У каждой семьи свой аккаунт Tailscale и свой адрес."
    mark "ADDRESS $u"
    if [ -f "$APP/router/doctor.py" ]; then
        echo
        say "Итоговая проверка:"
        python3 "$APP/router/doctor.py" --summary || true     # результат проверки только показываем: на успех настройки он не влияет
    fi
}

case "${1:-install}" in
    install)
        check_prereq
        check_password
        install_tailscale
        start_service
        ensure_login
        enable_funnel
        finish ;;
    status) show_status ;;
    proxy)
        case "${2:-}" in
            on) proxy_on "${3:-}" ;;
            off) proxy_off ;;
            *) die "используйте: remote.sh proxy on [адрес:порт]  или  remote.sh proxy off" ;;
        esac ;;
    funnel)
        [ "${2:-}" = "off" ] || die "используйте: remote.sh funnel off"
        tailscale funnel --https=443 off >/dev/null 2>&1; say "Дневник убран из интернета." ;;
    *) die "неизвестная команда: $1 (бывают: install, status, proxy on|off, funnel off)" ;;
esac
