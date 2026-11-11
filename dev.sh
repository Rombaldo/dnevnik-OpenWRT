#!/bin/bash
# Помощник разработчика. Использование: ./dev.sh <команда>
cd "$(dirname "$0")"
PY=.venv/bin/python
case "$1" in
  setup) python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && $PY -m playwright install chromium ;;
  demo)  $PY server.py --demo ;;                                  # http://localhost:8766, выдуманные данные
  test)  $PY -m unittest discover -s tests -t . -v ;;
  qr)    $PY tools/make_qr.py "${@:2}" ;;
  run)   $PY server.py ;;                                         # настоящие данные
  login) $PY fetcher.py login && { [ -f data/router_key ] && ./router/deploy.sh push; true; } ;;   # вход + передача на роутер
  deploy) ./router/deploy.sh "${@:2}" ;;                          # выложить программу на роутер (deploy login - вместе со входом)
  router) ssh -i data/router_key -o BatchMode=yes -o IdentitiesOnly=yes -o UserKnownHostsFile=data/known_hosts root@${ROUTER:-192.168.10.1} 'tail -n 12 /tmp/dnevnik-sync.log; echo; ls -la /root/dnevnik/data' ;;
  sync)  $PY fetcher.py sync ;;
  *) echo "Команды: setup | demo | test | qr [адрес] | run | login | deploy [login] | router | sync" ;;
esac
