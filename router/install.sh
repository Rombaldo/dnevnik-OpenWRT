#!/bin/sh
# Установка и обновление «Мой дневник» на роутере с OpenWrt прямо из приватного репозитория GitHub.
#
# Установка на роутер (по SSH), одна команда:
#   wget -qO- https://raw.githubusercontent.com/Rombaldo/dnevnik-OpenWRT/main/router/install.sh | sh
# (если репозиторий приватный, нужен токен GitHub только на чтение: файл /root/.dnevnik-token и ключ
#  --header="Authorization: Bearer $(cat /root/.dnevnik-token)" в wget)
#
# Выбор: с пунктом «Службы → Дневник» в веб-интерфейсе LuCI или без него.
#   без LuCI (по умолчанию):  wget -qO- https://raw.githubusercontent.com/Rombaldo/dnevnik-OpenWRT/main/router/install.sh | sh
#   с LuCI:                   wget -qO- https://raw.githubusercontent.com/Rombaldo/dnevnik-OpenWRT/main/router/install.sh | sh -s -- --luci
#   убрать страницу LuCI:     ... | sh -s -- --no-luci
#   с уведомлениями:          ... | sh -s -- --push       (ставит модуль для отправки уведомлений, около 4 МБ; включаются на телефоне в ⚙)
#   с выходом в интернет:     ... | sh -s -- --remote     (после установки запускает router/remote.sh: Tailscale и постоянный адрес
#                                                          для семьи; нужен пароль семьи, без него в интернет не выставляет;
#                                                          ключи можно сочетать: sh -s -- --luci --remote)
# Если страница LuCI уже стоит, при обновлении без ключей она тоже обновляется.
#
# Обновление до последней версии:   sh /root/dnevnik/router/install.sh
# Конкретная версия:                REF=v0.5.0 sh /root/dnevnik/router/install.sh
#
# Ваши данные (вход, история, config.json) при установке и обновлении не трогаются.
# Для проверки без GitHub: SRC_TARBALL=/tmp/архив.tar.gz sh install.sh
set -e
umask 022

APP=/root/dnevnik
WWW=/tmp/dnevnik-www
TOKEN_FILE=/root/.dnevnik-token
TMP=/tmp/dnevnik-install
# Репозиторий и версия не запоминаются: берётся имя из этого скрипта, если не задано REPO=... и REF=... при запуске
REPO="${REPO:-Rombaldo/dnevnik-OpenWRT}"
REF="${REF:-main}"

LUCI=auto
REMOTE=0
PUSH=0
for arg in "$@"; do
    case "$arg" in
        --luci) LUCI=1 ;;
        --no-luci) LUCI=0 ;;
        --remote) REMOTE=1 ;;
        --push) PUSH=1 ;;
        *) echo "ОШИБКА: неизвестный ключ $arg (бывают --luci, --no-luci, --remote и --push)" >&2; exit 2 ;;
    esac
done

say() { echo "==> $*"; }
die() { echo "ОШИБКА: $*" >&2; rm -rf "$TMP"; exit 1; }

[ "$(id -u)" = "0" ] || die "запускайте от root"
[ -f /etc/openwrt_release ] || die "это не OpenWrt"
OLD=$(cat "$APP/VERSION" 2>/dev/null || echo "нет")
if [ "$LUCI" = "auto" ]; then   # без ключей: раз страница LuCI уже стоит, она обновляется вместе с программой
    if [ -f /www/luci-static/resources/view/dnevnik/status.js ]; then LUCI=1; else LUCI=0; fi
fi

# 1. Python и нужные модули
say "Проверяю Python…"
PKGS="python3-light python3-openssl python3-urllib python3-email python3-codecs"
if command -v apk >/dev/null 2>&1; then
    MISSING=""
    for p in $PKGS; do apk info -e "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"; done
    if [ -n "$MISSING" ]; then say "Ставлю пакеты:$MISSING"; apk update >/dev/null 2>&1 || true; apk add $MISSING || die "не удалось поставить$MISSING"; fi
elif command -v opkg >/dev/null 2>&1; then
    MISSING=""
    for p in $PKGS; do opkg list-installed 2>/dev/null | grep -q "^$p " || MISSING="$MISSING $p"; done
    if [ -n "$MISSING" ]; then say "Ставлю пакеты:$MISSING"; opkg update >/dev/null 2>&1 || true; opkg install $MISSING || die "не удалось поставить$MISSING"; fi
else
    die "не нашёл apk или opkg"
fi
python3 -c "import ssl, hashlib, http.cookiejar, urllib.request, argparse, json" 2>/dev/null || die "в Python не хватает модулей"

# 2. Скачиваю программу
rm -rf "$TMP"; mkdir -p "$TMP"
if [ -n "$SRC_TARBALL" ]; then
    say "Беру архив $SRC_TARBALL"
    cp "$SRC_TARBALL" "$TMP/src.tar.gz"
else
    URL="https://api.github.com/repos/$REPO/tarball/$REF"
    if [ -s "$TOKEN_FILE" ]; then
        say "Скачиваю $REPO ($REF) с GitHub (с токеном)…"
        # токен кладём во временный конфиг (доступ только у root), чтобы он не светился в списке процессов;
        # строгие права действуют только в скобках, на остальные файлы они не влияют
        ( umask 077; printf 'header = Authorization: Bearer %s\nheader = Accept: application/vnd.github+json\n' "$(cat "$TOKEN_FILE")" > "$TMP/wgetrc" )
        WGETRC="$TMP/wgetrc" wget -q -T 30 -O "$TMP/src.tar.gz" "$URL" || die "не скачалось: проверьте токен, имя репозитория ($REPO) и версию ($REF)"
        rm -f "$TMP/wgetrc"
    else
        say "Скачиваю $REPO ($REF) с GitHub…"
        wget -q -T 30 -O "$TMP/src.tar.gz" "$URL" || die "не скачалось ($REPO, $REF). Если репозиторий приватный, нужен токен в $TOKEN_FILE; если публичный, проверьте интернет на роутере"
    fi
fi
[ "$(wc -c < "$TMP/src.tar.gz")" -gt 2000 ] || die "архив подозрительно маленький"
tar tzf "$TMP/src.tar.gz" >/dev/null 2>&1 || die "архив повреждён"
mkdir -p "$TMP/x" && tar xzf "$TMP/src.tar.gz" -C "$TMP/x"
SRC=$(find "$TMP/x" -mindepth 1 -maxdepth 1 -type d | head -n 1)
for f in core.py auth.py devices.py notify.py push.py sync.py VERSION web/index.html router/dnevnik.init router/sync.sh router/refresh.cgi router/setup.cgi router/logout.cgi router/auth.cgi router/data.cgi router/push.cgi router/password.py router/doctor.py router/remote.sh router/push.sh router/setname.sh; do
    [ -f "$SRC/$f" ] || die "в архиве нет файла $f"
done
if [ "$LUCI" = "1" ]; then
    for f in luci/rpcd/luci.dnevnik luci/menu.d/luci-app-dnevnik.json luci/acl.d/luci-app-dnevnik.json luci/htdocs/view/dnevnik/status.js router/luci.sh; do
        [ -f "$SRC/$f" ] || die "в архиве нет файла $f (для страницы LuCI нужна версия 0.12.0 или новее)"
    done
fi
NEW=$(cat "$SRC/VERSION")

# 3. Раскладываю (data/ и config.json не трогаю)
say "Устанавливаю версию $NEW (была: $OLD)…"
mkdir -p "$APP/data"; chmod 700 "$APP/data"
rm -rf "$APP/web" "$APP/router" "$APP/luci"
cp -r "$SRC/web" "$SRC/router" "$APP/"
[ -d "$SRC/luci" ] && cp -r "$SRC/luci" "$APP/"
cp -f "$SRC/core.py" "$SRC/auth.py" "$SRC/devices.py" "$SRC/notify.py" "$SRC/push.py" "$SRC/sync.py" "$SRC/VERSION" "$APP/"
[ -f "$APP/config.json" ] || cp "$SRC/config.example.json" "$APP/config.json"
chmod 755 "$APP"/router/*.sh "$APP"/router/*.cgi "$APP"/router/*.py "$APP"/router/dnevnik.init
[ -f "$APP/luci/rpcd/luci.dnevnik" ] && chmod 755 "$APP/luci/rpcd/luci.dnevnik"
rm -f "$APP/.source"   # старые версии установщика запоминали имя репозитория
chown -R root:root "$APP"

# 4. Служба и расписание
say "Включаю службу и расписание…"
cp "$APP/router/dnevnik.init" /etc/init.d/dnevnik; chmod 755 /etc/init.d/dnevnik
/etc/init.d/dnevnik enable
grep -q "dnevnik/router/sync.sh" /etc/crontabs/root 2>/dev/null || echo "*/20 6-22 * * * /root/dnevnik/router/sync.sh" >> /etc/crontabs/root
/etc/init.d/cron enable >/dev/null 2>&1 || true
/etc/init.d/cron restart >/dev/null 2>&1 || true
/etc/init.d/dnevnik restart 2>&1 | grep -v "Not found" || true
rm -rf "$TMP"

# 4а. Страница в LuCI (по выбору)
LUCI_MSG=""
if [ "$LUCI" = "1" ]; then
    say "Ставлю страницу «Службы → Дневник» в LuCI…"
    LUCI_MSG=$(sh "$APP/router/luci.sh" install 2>&1) || true
elif [ -f /www/luci-static/resources/view/dnevnik/status.js ]; then
    say "Убираю страницу LuCI…"
    sh "$APP/router/luci.sh" remove >/dev/null 2>&1 || true
fi

# 5. Проверяю, что страница действительно открывается
OK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    if wget -q -T 3 -O /dev/null http://127.0.0.1:8765/index.html 2>/dev/null; then OK=1; break; fi
    sleep 2
done
[ "$OK" = "1" ] || die "версия установлена, но страница на порту 8765 не отвечает: смотрите logread | tail"

DOMAIN=$(uci -q get dhcp.@dnsmasq[0].domain); DOMAIN="${DOMAIN:-lan}"
LANIP=$(uci -q get network.lan.ipaddr | cut -d/ -f1)
NAME_OK=0
for i in 1 2 3 4 5; do
    nslookup "dnevnik.$DOMAIN" 127.0.0.1 2>/dev/null | grep -q "$LANIP" && { NAME_OK=1; break; }
    sleep 1
done

say "Готово: версия $NEW."
[ -n "$LUCI_MSG" ] && echo "$LUCI_MSG"
if [ -f "$APP/data/auth.json" ]; then
    echo "    Пароль семьи включён: без него страница не откроется. Забыли пароль: задайте новый в LuCI (Службы → Дневник) или: python3 $APP/router/password.py set 'новый пароль'"
else
    echo "    Пароль семьи не задан: страницу видит любой, у кого есть к ней доступ. Задайте его в «Настройки» → «Пароль для семьи» (обязательно, если открываете страницу вне дома)."
fi
if [ ! -s "$APP/data/credentials.json" ] && [ ! -s "$APP/data/session.json" ]; then
    echo "    Осталось один раз ввести логин и пароль от Дневник.ру: откройте страницу ниже на телефоне и заполните форму."
fi
if [ "$NAME_OK" = "1" ]; then
    echo "    Страница: http://dnevnik.$DOMAIN   (запасной адрес: http://$LANIP:8765)"
else
    echo "    Страница: http://$LANIP:8765   (имя dnevnik.$DOMAIN пока не заработало: выключите на телефоне «Частный DNS»/«Защищённый DNS» или пользуйтесь адресом с цифрами)"
fi

# 6. Проверка, что всё работает (только показывает замечания; ничего не меняет)
echo
say "Проверяю, что всё работает…"
python3 "$APP/router/doctor.py" --summary || true
echo "    Полная проверка в любой момент: python3 $APP/router/doctor.py   (или кнопка в LuCI: Службы → Дневник)"

# 6а. Уведомления (по желанию, ключ --push)
PUSH_CODE=0
if [ "$PUSH" = "1" ]; then
    echo
    say "Ставлю модуль уведомлений…"
    sh "$APP/router/push.sh" install || PUSH_CODE=$?
fi

# 7. Выход в интернет (по желанию, ключ --remote)
if [ "$REMOTE" = "1" ]; then
    echo
    say "Настраиваю выход в интернет (Tailscale)…"
    sh "$APP/router/remote.sh" install
    exit $?
else
    echo
    echo "    Чтобы дневник открывался не только дома: wget -qO- https://raw.githubusercontent.com/$REPO/$REF/router/install.sh | sh -s -- --remote"
    [ "$PUSH" = "1" ] || echo "    Уведомления о новых оценках и домашке на телефон (по желанию): sh $APP/router/push.sh"
fi
