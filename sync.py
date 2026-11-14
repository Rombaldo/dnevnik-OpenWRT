"""Одно обновление дневника без браузера. Запускается на роутере по расписанию (cron) и на Mac.

    python3 sync.py --data DIR --out FILE [--save-session-hours N]

DIR  - где лежат session.json, ids.json, history.json
FILE - куда писать data.json для страницы
"""
import argparse
import sys
from pathlib import Path

import core

APP = Path(__file__).resolve().parent

ap = argparse.ArgumentParser()
ap.add_argument("--data", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--save-session-hours", type=float, default=0)
ap.add_argument("--rebuild", action="store_true", help="только собрать страницу из сохранённой истории")
args = ap.parse_args()

if args.rebuild:
    core.rebuild(args.data, args.out, config_path=APP / "config.json", version_path=APP / "VERSION")
    print("rebuilt")
    sys.exit(0)

try:
    o = core.sync(args.data, args.out, session_save_hours=args.save_session_hours,
                  config_path=APP / "config.json", version_path=APP / "VERSION")
    print(f"ok: {len(o['days'])} дней, {o['updatedAt']}")
    try:                                   # что нового: оценки, домашка, отмены, пропуски; сбой уведомлений обновление не ломает
        import notify
        events = notify.process(args.data, o)
        if events:
            print(f"notify: событий {len(events)}")
    except Exception as e:
        print(f"notify: {type(e).__name__}: {e}")
except core.NeedLogin as e:
    print(f"NEED_LOGIN: {e}")
    sys.exit(2)
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(1)
