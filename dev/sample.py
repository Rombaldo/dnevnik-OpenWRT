"""Выдуманные данные дневника для разработки и тестов. Настоящих данных здесь нет."""
import random
from datetime import date, datetime, timedelta

SLOTS = [("8:00", "8:40"), ("8:50", "9:30"), ("9:40", "10:20"), ("10:40", "11:20"), ("11:30", "12:10"), ("12:20", "13:00")]
WEEK = [  # расписание по дням недели (пн..пт)
    ["РоВ", "Математика", "Рус. язык", "История", "Литература", "Физкультура"],
    ["Рус. язык", "Род.язык", "Математика", "Англ. язык", "ОПД", "Музыка"],
    ["Рус. язык", "Математика", "Биология", "Литература", "История"],
    ["Рус. язык", "Математика", "Литература", "История", "Физкультура", "Англ. язык"],
    ["Рус. язык", "Математика", "Труд", "Труд", "География", "Физкультура"],
]
HOMEWORK = {
    "Рус. язык": ["упр 100, 101", "выучить правило, упр 105", "-"], "Математика": ["№ 3.20 (а, б)", "принести линейку", "без задания"],
    "История": ["Читать параграф 5", "Устно ответить на вопросы"], "Литература": ["Выучить стихотворение", "-"],
    "Англ. язык": ["Учить слова", "Упр. 3 стр. 12"], "Биология": ["Нет домашнего задания"], "География": ["изучить презентацию"],
    "Физкультура": ["Комплекс 2"], "Труд": ["Подготовить листы А4"],
}
SKILL = {"Рус. язык": 3.4, "Математика": 3.6, "История": 3.8, "Литература": 4.0, "Англ. язык": 4.9, "Музыка": 5.0,
         "Физкультура": 4.9, "География": 4.8, "Биология": 4.7, "Труд": 4.9, "ОПД": 5.0, "Род.язык": 3.5, "РоВ": 0}


def make_sample(today=None, seed=7):
    rnd = random.Random(seed)
    today = today or date.today()
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=6)
    days = []
    for i in range(9 * 7):
        d = start + timedelta(days=i)
        if d.weekday() > 4:
            continue
        lessons = []
        for n, subj in enumerate(WEEK[d.weekday()], 1):
            s, e = SLOTS[n - 1]
            marks = []
            if d <= today and SKILL.get(subj) and rnd.random() < 0.35:
                v = max(2, min(5, round(rnd.gauss(SKILL[subj], 0.8))))
                marks.append({"value": str(v), "work": "Ответ на уроке",
                              "created": datetime(d.year, d.month, d.day, 12, 0).isoformat()})
            lessons.append({
                "n": n, "start": s, "end": e, "subject": subj, "subjectId": f"s-{subj}", "groupId": "g1",
                "theme": f"Тема урока ({subj})", "homework": rnd.choice(HOMEWORK.get(subj, ["-"])),
                "files": [{"id": "1", "name": "Презентация. Географические исследования", "ext": "pdf", "size": 5149565,
                           "url": "https://b5.csdnevnik.ru/demo-1.pdf"},
                          {"id": "2", "name": "Карточки к уроку", "ext": "docx", "size": 20480, "url": ""}]
                if subj == "География" else [],
                "canceled": False, "marks": marks,
            })
        days.append({"date": d.isoformat(), "weekday": ["пн", "вт", "ср", "чт", "пт"][d.weekday()], "lessons": lessons})
    return {
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        "days": days,
    }
