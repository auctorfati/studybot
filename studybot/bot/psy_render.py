"""Сообщения психологии: задание, ответ карточки, итог, часть разбора, меню «Разборы»."""

from __future__ import annotations

import json
import sqlite3

from ..psychology.session import PsyFeedback
from ..psychology.track import AREA_ORDER, area_rank
from ..sessions.planner import Step
from .ui import Button, Reply

AREA_TITLE = {"PATO": "Патопсихология", "PSY": "Психопатология", "NEURO": "Нейропсихология",
              "KLIN": "Классификации", "PSAN": "Психоаналитическая диагностика", "REL": "Психология отношений",
              "SEX": "Сексология"}
PURPOSE = {"probe": "Проверка темы", "new": "Новое", "review": "Повторение"}
KIND = {"card": "Карточка", "open": "Вопрос по механизмам", "distinction": "Различение", "vignette": "Виньетка"}
END = Button("Закончить", "end")
ROW = 3


def _topic_head(conn: sqlite3.Connection, code: str) -> str:
    row = conn.execute("SELECT title FROM topics WHERE code = ?", (code,)).fetchone()
    return f"{code} «{row[0]}»" if row else code


def step_reply(conn: sqlite3.Connection, step: Step) -> Reply:
    p, sid = step.payload, step.id
    if step.kind == "digest":
        return digest_part_reply(conn, p["topic"], p["part"] or 1, step_id=sid, reason=p.get("reason", ""))
    head = f"Психология · {PURPOSE[p['purpose']]} · {_topic_head(conn, p['topic'])}"
    kind = KIND[p["kind"]]
    if p["kind"] == "card" and p["answer_mode"] == "self":
        text = f"{head}\n{kind}: {p['prompt']}\n\nВспомни ответ и открой его. Можно и набрать — тогда проверю."
        return Reply(text, [[Button("Показать ответ", f"pshow:{sid}")], [Button("Не знаю", f"idk:{sid}"), END]])
    if p["kind"] == "card":
        text = f"{head}\n{kind} — набери ответ: {p['prompt']}"
    elif p["kind"] == "vignette":
        qs = "\n".join(f"{k}. {q}" for k, q in enumerate(p.get("questions") or [], start=1))
        text = (f"{head}\n{kind}.\n{p['prompt']}\n\nВопросы:\n{qs}\n\nОтветь на вопросы одним сообщением. "
                f"Угол наставника — следующим шагом.")
    else:
        text = f"{head}\n{kind}: {p['prompt']}\n\nОтветь своими словами."
    return Reply(text, [[Button("Не знаю", f"idk:{sid}"), END]])


def shown_reply(answer: str, sid: int) -> Reply:
    return Reply(f"Ответ: {answer}\n\nКак было?", self_buttons(sid))


def self_buttons(sid: int) -> list[list[Button]]:
    return [[Button("Не знал", f"pself:{sid}:0"), Button("С трудом", f"pself:{sid}:1"),
             Button("Знал", f"pself:{sid}:2")]]


def feedback_reply(fb: PsyFeedback) -> Reply:
    if fb.ask:
        buttons = self_buttons(fb.step_id) if fb.ask_buttons == "self" else [[END]]
        return Reply("\n".join(fb.lines + [fb.ask]), buttons)
    lines = list(fb.lines)
    if fb.deferred:
        lines.append(f"Проверка недоступна ({fb.deferred}): позиция отложена до следующей сессии без оценки.")
    row = []
    if fb.review_id and fb.has_digest:
        row.append(Button("Почему так", f"pwhy:{fb.step_id}"))
    if fb.review_id and fb.has_source:
        row.append(Button("Источник", f"src:{fb.step_id}"))
    if fb.disputable and fb.review_id:
        row.append(Button("Оспорить", f"dispute:{fb.review_id}"))
    buttons = [row] if row else []
    if fb.topic_event == "closed" and fb.has_digest:
        buttons.append([Button("Разбор темы", f"dg:{fb.topic}:1")])
    return Reply("\n".join(lines) or "Записано.", buttons)


# разборы

def digest_part_reply(conn: sqlite3.Connection, topic: str, num: int, step_id: int | None = None,
                      reason: str = "") -> Reply:
    d = conn.execute("SELECT title, parts FROM digests WHERE topic_code = ?", (topic,)).fetchone()
    part = conn.execute("SELECT * FROM digest_parts WHERE topic_code = ? AND num = ?", (topic, num)).fetchone()
    if d is None or part is None:
        return Reply("Разбора этой темы нет.", [[Button("К разборам", "dm")]])
    text = f"Разбор {topic} «{d['title']}» — часть {num} из {d['parts']}: {part['title']}\n\n{part['body']}"
    if step_id is not None:                     # чтение внутри занятия
        last = num >= d["parts"]
        if num == 1 and reason == "intro":
            text = f"Новая тема. Сначала разбор, потом вопросы.\n\n{text}"
        return Reply(text, [[Button("Готово" if last else "Дальше", f"dgn:{step_id}"),
                             Button("Отложить", f"dgl:{step_id}")]])
    nav = []
    if num > 1:
        nav.append(Button("←", f"dg:{topic}:{num - 1}"))
    if num < d["parts"]:
        nav.append(Button("→", f"dg:{topic}:{num + 1}"))
    grp = conn.execute("SELECT area, grp FROM topics WHERE code = ?", (topic,)).fetchone()
    back = f"dm:b:{grp['area']}:{grp['grp']}" if grp else "dm"
    return Reply(text, ([nav] if nav else []) + [[Button("К темам", back)]])


def part_for_item(conn: sqlite3.Connection, topic: str, code: str) -> int:
    """Часть разбора, в строке «Позиции» которой стоит позиция; иначе «Коротко»."""
    for r in conn.execute("SELECT num, positions_json FROM digest_parts WHERE topic_code = ? ORDER BY num", (topic,)):
        if code in json.loads(r["positions_json"]):
            return r["num"]
    return 1


def _rows(buttons: list[Button], width: int = ROW) -> list[list[Button]]:
    return [buttons[i:i + width] for i in range(0, len(buttons), width)]


def menu(conn: sqlite3.Connection, arg: str) -> Reply:
    """«Разборы»: области → блоки карты → темы (прочитанные отмечены) → чтение с «Коротко»."""
    parts = [x for x in arg.split(":") if x] if arg else []
    if not parts:
        rows = conn.execute("SELECT area, count(*) AS n, sum(r.status = 'read') AS done FROM digests d "
                            "LEFT JOIN digest_reads r ON r.topic_code = d.topic_code GROUP BY area").fetchall()
        if not rows:
            return Reply("Разборов нет: пришли файлы разборов документом после банков.")
        rows = sorted(rows, key=lambda r: area_rank(r["area"]))
        buttons = [[Button(f"{AREA_TITLE.get(r['area'], r['area'])} — {r['done'] or 0} из {r['n']}",
                           f"dm:a:{r['area']}")] for r in rows]
        return Reply("Разборы тем: выбери область. Число — прочитано из всех.", buttons)
    if parts[0] == "a" and len(parts) >= 2:
        area = parts[1]
        grps = conn.execute("SELECT t.grp, min(t.sort_key) AS k FROM topics t JOIN digests d ON d.topic_code = t.code "
                            "WHERE t.area = ? GROUP BY t.grp ORDER BY k", (area,)).fetchall()
        buttons = _rows([Button(f"Блок {g['grp']}", f"dm:b:{area}:{g['grp']}") for g in grps])
        return Reply(f"{AREA_TITLE.get(area, area)}: выбери блок карты.", buttons + [[Button("Назад", "dm")]])
    if parts[0] == "b" and len(parts) >= 3:
        area, grp = parts[1], parts[2]
        rows = conn.execute(
            "SELECT t.code, t.title, r.status FROM topics t JOIN digests d ON d.topic_code = t.code "
            "LEFT JOIN digest_reads r ON r.topic_code = t.code WHERE t.area = ? AND t.grp = ? ORDER BY t.sort_key",
            (area, grp)).fetchall()
        buttons = []
        for r in rows:
            mark = " · прочитан" if r["status"] == "read" else ""
            title = f"{r['code']} {r['title']}"
            title = title if len(title) <= 34 else title[:33].rstrip() + "…"
            buttons.append([Button(title + mark, f"dg:{r['code']}:1")])
        return Reply(f"{AREA_TITLE.get(area, area)}, блок {grp}: темы.", buttons + [[Button("Назад", f"dm:a:{area}")]])
    return menu(conn, "")
