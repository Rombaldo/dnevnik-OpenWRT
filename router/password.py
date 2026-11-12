#!/usr/bin/python3
"""Семейный пароль из командной строки роутера (по SSH). Нужен, если пароль забыт: со страницы без пароля его не сбросить.

    python3 /root/dnevnik/router/password.py status         включён ли пароль
    python3 /root/dnevnik/router/password.py set 'пароль' ['подсказка']   задать или заменить (все устройства войдут заново)
                                                            подсказку видит каждый на экране входа: сам пароль в ней писать нельзя
    python3 /root/dnevnik/router/password.py off            отключить пароль (страница откроется всем, кто её видит)
"""
import os
import sys

HOME_DIR = os.environ.get("DNEVNIK_HOME", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HOME_DIR)
import auth  # noqa: E402

DATA = os.path.join(HOME_DIR, "data")
args = sys.argv[1:]
if args[:1] == ["status"] and len(args) == 1:
    print("Пароль семьи: включён" if auth.enabled(DATA) else "Пароль семьи: выключен (страница открыта всем, кто её видит)")
elif args[:1] == ["set"] and len(args) in (2, 3):
    ok, message = auth.set_password(DATA, args[1], args[2] if len(args) == 3 else "")
    print(message)
    sys.exit(0 if ok else 1)
elif args == ["off"]:
    auth.disable(DATA)
    print("Пароль отключён.")
else:
    print(__doc__)
    sys.exit(2)
