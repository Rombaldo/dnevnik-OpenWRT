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
  login) $PY fetcher.py login ;;
  sync)  $PY fetcher.py sync ;;
  *) echo "Команды: setup | demo | test | qr [адрес] | run | login | sync" ;;
esac
