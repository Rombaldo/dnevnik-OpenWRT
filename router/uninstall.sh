#!/bin/sh
# Убирает «Мой дневник» с роутера. Пакеты Python не удаляются (могут быть нужны другим).
#   sh /root/dnevnik/router/uninstall.sh          - убрать программу, вход и данные тоже
#   sh /root/dnevnik/router/uninstall.sh --keep   - оставить папку data (вход и история)
[ -x /root/dnevnik/router/setname.sh ] && /root/dnevnik/router/setname.sh remove      # убрать имя dnevnik.lan и вернуть стартовую страницу роутера
/etc/init.d/dnevnik stop 2>/dev/null; /etc/init.d/dnevnik disable 2>/dev/null
rm -f /etc/init.d/dnevnik
sed -i '/dnevnik/d' /etc/crontabs/root 2>/dev/null; /etc/init.d/cron restart >/dev/null 2>&1
rm -rf /tmp/dnevnik-www /tmp/dnevnik-install /tmp/dnevnik-sync.lock /tmp/dnevnik-sync.log
if [ "$1" = "--keep" ]; then
    find /root/dnevnik -mindepth 1 -maxdepth 1 ! -name data ! -name config.json -exec rm -rf {} +
    echo "Программа убрана, данные оставлены в /root/dnevnik/data"
else
    rm -rf /root/dnevnik; echo "Программа и данные убраны."
fi
