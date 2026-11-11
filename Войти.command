#!/bin/bash
# Открывает окно браузера: войдите в Дневник.ру через Госуслуги. Окно закроется само.
# Если настроен роутер, вход сразу передаётся на него.
cd "$(dirname "$0")"
.venv/bin/python fetcher.py login || exit 1
if [ -f data/router_key ]; then
  ./router/deploy.sh push && echo "Готово: вход передан на роутер."
fi
echo "Можно закрыть это окно."
