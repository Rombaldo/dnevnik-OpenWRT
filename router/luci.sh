#!/bin/sh
# Страница «Службы → Дневник» в веб-интерфейсе LuCI (необязательная).
#   luci.sh install   - поставить (нужен LuCI на роутере: пакет luci-base)
#   luci.sh remove    - убрать
#   luci.sh status    - стоит ли
# Файлы берутся из /root/dnevnik/luci. Ничего, кроме четырёх маленьких файлов ниже, не меняется.
APP="${DNEVNIK_HOME:-/root/dnevnik}"
R="${DNEVNIK_ROOTFS:-}"          # префикс нужен только тестам; на роутере пусто
SRC="$APP/luci"
PLUGIN="$R/usr/libexec/rpcd/luci.dnevnik"
MENU="$R/usr/share/luci/menu.d/luci-app-dnevnik.json"
ACL="$R/usr/share/rpcd/acl.d/luci-app-dnevnik.json"
VIEWDIR="$R/www/luci-static/resources/view/dnevnik"

has_luci() { [ -n "$R" ] || { [ -f /www/luci-static/resources/luci.js ] && [ -x /etc/init.d/rpcd ]; }; }

reload_luci() {
    [ -n "$R" ] && return 0
    rm -rf /tmp/luci-indexcache* /tmp/luci-modulecache 2>/dev/null
    /etc/init.d/rpcd reload >/dev/null 2>&1
    for i in 1 2 3 4 5; do ubus list luci.dnevnik >/dev/null 2>&1 && return 0; sleep 1; done
    /etc/init.d/rpcd restart >/dev/null 2>&1                  # плагин не подхватился: перезапуск (LuCI попросит войти заново)
    for i in 1 2 3 4 5; do ubus list luci.dnevnik >/dev/null 2>&1 && return 0; sleep 1; done
    return 1
}

case "$1" in
install)
    if ! has_luci; then
        echo "    LuCI на роутере не найден (нет пакета luci-base): страницу в меню ставить некуда. Дневник работает и без неё."
        exit 0
    fi
    for f in rpcd/luci.dnevnik menu.d/luci-app-dnevnik.json acl.d/luci-app-dnevnik.json htdocs/view/dnevnik/status.js; do
        [ -f "$SRC/$f" ] || { echo "ОШИБКА: нет файла $SRC/$f"; exit 1; }
    done
    mkdir -p "$(dirname "$PLUGIN")" "$(dirname "$MENU")" "$(dirname "$ACL")" "$VIEWDIR"
    cp -f "$SRC/rpcd/luci.dnevnik" "$PLUGIN" && chmod 755 "$PLUGIN"
    cp -f "$SRC/menu.d/luci-app-dnevnik.json" "$MENU" && chmod 644 "$MENU"
    cp -f "$SRC/acl.d/luci-app-dnevnik.json" "$ACL" && chmod 644 "$ACL"
    cp -f "$SRC/htdocs/view/dnevnik/status.js" "$VIEWDIR/status.js" && chmod 644 "$VIEWDIR/status.js"
    reload_luci || { echo "    Страница LuCI поставлена, но роутер её пока не подхватил: перезагрузите роутер или выполните /etc/init.d/rpcd restart"; exit 0; }
    echo "    Страница в LuCI: Службы → Дневник"
    ;;
remove)
    rm -f "$PLUGIN" "$MENU" "$ACL"
    rm -rf "$VIEWDIR"
    has_luci && reload_luci
    exit 0
    ;;
status)
    [ -f "$VIEWDIR/status.js" ] && echo "installed" || echo "absent"
    ;;
*)
    echo "Использование: luci.sh install|remove|status"; exit 2
    ;;
esac
