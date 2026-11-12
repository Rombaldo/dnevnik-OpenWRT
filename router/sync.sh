#!/bin/sh
# Обновление дневника на роутере. Запускается по расписанию (cron) и кнопкой «Обновить» на странице.
APP=/root/dnevnik
WWW=/tmp/dnevnik-www
PRIV=/tmp/dnevnik-private          # данные дневника лежат вне папки веб-сервера (их отдаёт cgi-bin/data после проверки пароля)
LOCK=/tmp/dnevnik-sync.lock
LOG=/tmp/dnevnik-sync.log          # в оперативной памяти: флеш-память роутера не изнашиваем

[ -d "$WWW" ] || exit 0
if [ -e "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then exit 0; fi   # уже идёт обновление
echo $$ > "$LOCK"
echo "--- $(date '+%F %T')" >> "$LOG"
python3 "$APP/sync.py" --data "$APP/data" --out "$PRIV/data.json" --save-session-hours 6 >> "$LOG" 2>&1
rm -f "$LOCK"
# журнал не должен расти бесконечно
if [ "$(wc -l < "$LOG")" -gt 300 ]; then tail -n 100 "$LOG" > "$LOG.new" && mv "$LOG.new" "$LOG"; fi
exit 0
