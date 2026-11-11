#!/bin/bash
# Проверка на секреты и личные данные: см. check_secrets.py
#   ./scripts/check-secrets.sh staged | tree | history
exec python3 "$(dirname "$0")/check_secrets.py" "$@"
