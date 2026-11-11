#!/bin/sh
# Кнопка «Обновить» на странице. Не чаще раза в 2 минуты.
echo "Content-Type: application/json"
echo "Cache-Control: no-cache"
echo ""
LAST=/tmp/dnevnik-lastrefresh
now=$(date +%s)
last=$(cat "$LAST" 2>/dev/null || echo 0)
if [ $((now - last)) -ge 120 ]; then
  echo "$now" > "$LAST"
  ( /root/dnevnik/router/sync.sh >/dev/null 2>&1 & )
fi
echo '{"ok": true}'
