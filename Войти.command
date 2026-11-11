#!/bin/bash
# Открывает окно браузера: войдите в Дневник.ру через Госуслуги. Окно закроется само.
cd "$(dirname "$0")"
.venv/bin/python fetcher.py login
