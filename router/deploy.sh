#!/bin/bash
# Устанавливает или обновляет «Мой дневник» на роутере (OpenWrt) по SSH-ключу data/router_key.
#   ./router/deploy.sh          - выложить программу и (пере)запустить службу
#   ./router/deploy.sh login    - то же + передать вход (cookie) и номера ученика
#   ./router/deploy.sh push     - только передать новый вход и сразу обновить данные (после входа через Госуслуги)
# Адрес роутера можно поменять: ROUTER=192.168.1.1 ./router/deploy.sh
set -e
cd "$(dirname "$0")/.."
MODE="$1"
ROUTER=${ROUTER:-192.168.10.1}
[ -f data/router_key ] || { echo "Нет ключа data/router_key"; exit 1; }
KH="${KNOWN_HOSTS:-data/known_hosts}"   # отпечаток роутера храним в проекте, а не в ~/.ssh
SSH=(ssh -i data/router_key -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KH" -o ConnectTimeout=10 "root@$ROUTER")

push_login() {
  [ -f data/session.json ] && [ -f data/ids.json ] || { echo "Сначала войдите: ./dev.sh login"; exit 1; }
  echo "    Передаю вход (файлы на роутере закрыты правами 600)…"
  COPYFILE_DISABLE=1 tar --format ustar -cf - -C data session.json ids.json \
    | "${SSH[@]}" 'mkdir -p /root/dnevnik/data && chmod 700 /root/dnevnik/data && cd /root/dnevnik/data && umask 077 && tar xof - && chmod 600 session.json ids.json'
}

if [ "$MODE" = "push" ]; then
  push_login
  "${SSH[@]}" '( /root/dnevnik/router/sync.sh >/dev/null 2>&1 & ); echo "    Обновление запущено."'
  exit 0
fi

[ -f config.json ] || cp config.example.json config.json
echo "1/3 Копирую программу на роутер…"
COPYFILE_DISABLE=1 tar --format ustar -cf - core.py sync.py VERSION config.json web router \
  | "${SSH[@]}" 'mkdir -p /root/dnevnik/data && chmod 700 /root/dnevnik/data && cd /root/dnevnik && tar xof - && chmod 755 router/*.sh router/*.cgi router/dnevnik.init'
[ "$MODE" = "login" ] && push_login

echo "2/3 Включаю службу и расписание…"
"${SSH[@]}" '
chown -R root:root /root/dnevnik
cp /root/dnevnik/router/dnevnik.init /etc/init.d/dnevnik && chmod 755 /etc/init.d/dnevnik
/etc/init.d/dnevnik enable
grep -q "dnevnik/router/sync.sh" /etc/crontabs/root 2>/dev/null || echo "*/20 6-22 * * * /root/dnevnik/router/sync.sh" >> /etc/crontabs/root
/etc/init.d/cron enable >/dev/null 2>&1; /etc/init.d/cron restart >/dev/null 2>&1
/etc/init.d/dnevnik restart 2>&1 | grep -v "Not found" || true   # при первом запуске служба ещё не зарегистрирована, это не ошибка
'
echo "3/3 Готово. Страница: http://$ROUTER:8765"
