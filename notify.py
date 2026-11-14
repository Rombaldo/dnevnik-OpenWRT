"""Что нового в дневнике: сравнивает данные с прошлым обновлением и находит события для уведомлений.

События: новая оценка, новая домашка, отмена и изменение урока, пропуск или опоздание. Первое обновление после установки событий не даёт
(запоминается исходное состояние), иначе пришли бы все прежние оценки разом. Отдельно от отправки: здесь только «что изменилось»; отправка в push.py.

Состояние лежит в data/notify-state.json (хеши и короткие поля, права 600), последние события в data/events.json (для раздела «Что нового»).
Модуль без внешних зависимостей: на роутере его запускает sync.py после каждого обновления с сайта.
"""
import hashlib
import json
import os
import re
import time
from datetime import date
from pathlib import Path

STATE, EVENTS = "notify-state.json", "events.json"
VERSION = 1
KEEP_EVENTS = 40
GROUP_OVER = 3          # больше стольких событий одного вида за раз: одно общее сообщение
FLOOD = 15              # столько новых оценок разом: похоже на перезагрузку данных, шлём одно общее
TYPES = ("mark", "homework", "schedule", "attendance")
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
NO_HW = re.compile(r"^(без\s+(домашнего\s+)?задани[яе]\.?|нет\s+домашнего\s+задания\.?|-|—)$", re.I)     # как на странице: «задания нет»


def rus_date(iso):
    try:
        y, m, d = (int(x) for x in str(iso)[:10].split("-"))
        return f"{d} {MONTHS[m - 1]}"
    except (ValueError, IndexError):
        return str(iso)


def _h(text):
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:10]


def _write_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def homework_text(lesson):
    """Текст домашки для сравнения; пусто, если задания нет."""
    text = (lesson.get("homework") or "").strip()
    if NO_HW.match(text):
        text = ""
    files = ", ".join(str(f.get("name", "")) for f in (lesson.get("files") or []) if isinstance(f, dict))
    return (text + (f" [файлы: {files}]" if files else "")).strip()


def facts(payload):
    """Всё, что может породить событие, из данных страницы (в них и оценки, и домашка, и расписание, и пропуски)."""
    marks, hw, sched, att = {}, {}, {}, {}
    for d in payload.get("days") or []:
        day = d.get("date")
        if not day:
            continue
        lessons = []
        for l in d.get("lessons") or []:
            subj, n = l.get("subject") or "?", l.get("n")
            lessons.append([n, subj, l.get("start") or "", l.get("end") or "", bool(l.get("canceled"))])
            for m in l.get("marks") or []:
                key = _h(f"{day}|{n}|{subj}|{m.get('work')}|{m.get('created')}|{m.get('value')}")
                marks[key] = {"d": day, "s": subj, "v": str(m.get("value") if m.get("value") is not None else "").strip(), "w": m.get("work") or ""}
            text = homework_text(l)
            if text and not l.get("canceled"):
                hw[f"{day}|{n}|{subj}"] = {"h": _h(text), "t": text, "d": day, "s": subj, "n": n}
        sched[day] = lessons
    for y in payload.get("years") or []:
        if not y.get("current"):
            continue
        for t in y.get("terms") or []:
            for e in ((t.get("attendance") or {}).get("days") or []):
                key = _h(f"{e.get('date')}|{e.get('kind')}|{','.join(str(x) for x in (e.get('lessons') or []))}")
                att[key] = {"d": e.get("date"), "k": e.get("kind"), "name": e.get("name") or "", "l": list(e.get("lessons") or [])}
    return {"marks": marks, "hw": hw, "sched": sched, "att": att}


def compact(f, today):
    """То, что запоминаем между обновлениями: хеши, а для будущих дней ещё и расписание."""
    return {"v": VERSION,
            "marks": sorted(f["marks"]),
            "hw": {k: v["h"] for k, v in f["hw"].items() if v["d"] >= today},
            "sched": {d: v for d, v in f["sched"].items() if d >= today},
            "att": sorted(f["att"])}


def _ranges(nums):
    a = sorted(int(x) for x in nums if str(x).lstrip("-").isdigit())
    out, i = [], 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[j + 1] == a[j] + 1:
            j += 1
        out.append(f"{a[i]}–{a[j]}" if j > i else str(a[i]))
        i = j + 1
    return ", ".join(out)


def _event(kind, key, title, body, now):
    return {"id": _h(f"{kind}|{key}"), "type": kind, "title": title, "body": body, "ts": int(now), "tag": kind, "url": "./"}


def detect(old, f, today, now=None):
    """События: что есть сейчас и чего не было в прошлый раз."""
    now = now if now is not None else time.time()
    ev = []
    # оценки
    new_marks = [k for k in f["marks"] if k not in set(old.get("marks") or [])]
    if len(new_marks) > FLOOD:
        ev.append(_event("mark", "flood" + str(len(new_marks)), f"Новых оценок: {len(new_marks)}", "Откройте дневник, чтобы посмотреть.", now))
    else:
        for k in new_marks:
            m = f["marks"][k]
            ev.append(_event("mark", k, f"Новая оценка: {m['v']}" if m["v"] else "Новая оценка", f"{m['s']} · {m['w'] or 'оценка'} ({rus_date(m['d'])})", now))
    # домашка (только на сегодня и позже: прошлое нам не интересно)
    old_hw = old.get("hw") or {}
    for key, v in f["hw"].items():
        if v["d"] >= today and old_hw.get(key) != v["h"]:
            text = re.sub(r"\s+", " ", v["t"])
            ev.append(_event("homework", key + v["h"], f"Новая домашка: {v['s']}", f"на {rus_date(v['d'])}: {text[:110]}{'…' if len(text) > 110 else ''}", now))
    # расписание: отмена и изменение для сегодняшнего дня и будущих, только если день уже был в прошлый раз
    old_sched = old.get("sched") or {}
    for day, lessons in sorted(f["sched"].items()):
        if day < today or day not in old_sched:
            continue
        old_l = {(x[0], x[1]): x for x in old_sched[day]}
        newly_canceled = []
        for n, subj, start, end, canceled in lessons:
            prev = old_l.get((n, subj))
            if canceled and prev and not prev[4]:
                newly_canceled.append((n, subj))
                ev.append(_event("schedule", f"c|{day}|{n}|{subj}", f"Урок отменён: {subj}", f"{rus_date(day)}, {n} урок", now))
        was = {tuple(x[:4]) for x in old_sched[day] if not x[4]}
        now_set = {(n, s, st, e) for n, s, st, e, c in lessons if not c}
        gone = {x for x in was - now_set if (x[0], x[1]) not in newly_canceled}
        added = now_set - was
        if gone or added:
            parts = [f"{n}. {s} {st}" for n, s, st, e in sorted(added, key=lambda x: str(x[0]))][:3]
            ev.append(_event("schedule", f"m|{day}|{sorted(map(str, gone | added))}", f"Изменилось расписание на {rus_date(day)}",
                             ("Теперь: " + "; ".join(parts)) if parts else "Уроки изменились, откройте расписание.", now))
    # пропуски и опоздания
    old_att = set(old.get("att") or [])
    for k, a in f["att"].items():
        if k in old_att:
            continue
        late = a["k"] == "late"
        lessons = f"уроки {_ranges(a['l'])}" if len(a["l"]) > 1 else (f"урок {_ranges(a['l'])}" if a["l"] else "")
        ev.append(_event("attendance", k, "Опоздание" if late else (a["name"] or "Пропуск"), ", ".join(x for x in (rus_date(a["d"]), lessons) if x), now))
    return group(ev, now)


LABEL = {"mark": ("Новых оценок", "Откройте «Оценки»."), "homework": ("Новой домашки", "Откройте «Домашка»."),
         "schedule": ("Изменений в расписании", "Откройте «Расписание»."), "attendance": ("Новых отметок о посещаемости", "Откройте «Оценки» → «Посещаемость».")}


def group(events, now):
    """Если событий одного вида много, одно сообщение вместо россыпи."""
    out = []
    for kind in TYPES:
        same = [e for e in events if e["type"] == kind]
        if len(same) <= GROUP_OVER:
            out += same
        else:
            title, tail = LABEL[kind]
            first = "; ".join(e["title"] + (f" ({e['body'].split(' · ')[0]})" if kind == "mark" else "") for e in same[:3])
            out.append(_event(kind, "group|" + "|".join(e["id"] for e in same), f"{title}: {len(same)}", f"{first}… {tail}", now))
    return out


def process(data_dir, payload, today=None, now=None, deliver="auto"):
    """Вызывается после каждого обновления с сайта. Возвращает события. Сбой в отправке дневник не ломает."""
    data_dir = Path(data_dir)
    today = today or date.today().isoformat()
    now = now if now is not None else time.time()
    old = _read(data_dir / STATE)
    f = facts(payload)
    new_state = compact(f, today)
    events = detect(old, f, today, now) if isinstance(old, dict) and old.get("v") == VERSION else []     # первый раз: только запоминаем
    if new_state != old:
        _write_private(data_dir / STATE, json.dumps(new_state, ensure_ascii=False))
    if events:
        log = (_read(data_dir / EVENTS, []) or [])
        seen = {e.get("id") for e in log}
        log = ([e for e in events if e["id"] not in seen] + log)[:KEEP_EVENTS]
        _write_private(data_dir / EVENTS, json.dumps(log, ensure_ascii=False))
        if deliver == "auto":
            try:
                import push
                push.deliver(data_dir, events, now=now)
            except Exception as e:                         # нет модуля, нет подписок, сеть: событие остаётся в «Что нового»
                print(f"notify: отправка пропущена ({type(e).__name__})")
        elif callable(deliver):
            deliver(data_dir, events)
    return events


def recent(data_dir, limit=20):
    return (_read(Path(data_dir) / EVENTS, []) or [])[:limit]
