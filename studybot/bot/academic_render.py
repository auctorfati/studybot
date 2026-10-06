"""Сообщения модуля 5: знакомство с термином, «Термин», «Разбор предложения», итог, контекст."""

from __future__ import annotations

import json
import sqlite3

from ..english.academic_session import TermFeedback, TextFeedback
from ..sessions.planner import Step
from .ui import Button, Reply

END = Button("Закончить", "end")


def text_reply(conn: sqlite3.Connection, step: Step) -> Reply:
    from ..english.academic import numbered, paragraph_of
    p, sid = step.payload, step.id
    r = conn.execute("SELECT title, fields_json FROM records WHERE code = ?", (p["code"],)).fetchone()
    f = json.loads(r["fields_json"])
    stop = [[Button("Закончить", "end")]]
    if p["phase"] == "read":
        head = f"Модуль 5 · Текст {p['code']}" + (" · повтор" if p.get("stage") else "") + f" — {r['title']}"
        src = f"{f.get('Ист', '')} Лицензия: {f.get('Лицензия', '')}".strip()
        text = (f"{head}\n\n{f.get('Текст', '')}\n\n{src}\n\n"
                "Прочитай без словаря и нажми «Прочитал»: бот засечёт время.")
        return Reply(text, [[Button("Прочитал", f"txr:{sid}")]] + stop)
    if p["phase"] == "q":
        qs = numbered(f.get("Вопросы", ""))
        q = qs[p["qi"]] if p["qi"] < len(qs) else ""
        return Reply(f"Вопрос {p['qi'] + 1} из {len(qs)}: {q}\nОтветь по-русски.", stop)
    if p["phase"] == "sentence":
        return Reply("Переведи предложение:\n" + f.get("Предложение", ""), stop)
    if p["phase"] == "paragraph":
        return Reply("Письменный перевод абзаца, словарь можно:\n\n"
                     + paragraph_of(f.get("Текст", ""), f.get("Абзац", "")), stop)
    return Reply("Перескажи по-русски главное из текста, голосом или текстом, до двух минут.", stop)


def step_reply(conn: sqlite3.Connection, step: Step) -> Reply:
    p, sid = step.payload, step.id
    if step.kind == "text":
        return text_reply(conn, step)
    t = conn.execute("SELECT * FROM terms WHERE id = ?", (p["term_id"],)).fetchone()
    extra = []
    if t["context"]:
        extra.append(Button("Контекст", f"tctx:{sid}"))
    if json.loads(t["notes_json"]):
        extra.append(Button("Заметка", f"tnote:{sid}"))
    if p["kind"] == "intro":
        head = "Модуль 5 · Новый термин" + (f" · {t['section']}" if t["section"] else "")
        lines = [head, f"{t['term']} — {t['meaning']}"]
        alts = json.loads(t["alt_json"])
        if alts:
            lines.append("Ещё переводят: " + " / ".join(alts))
        if t["sound"]:
            lines.append(f"Чтение: {t['sound']}")
        lines += ["", t["sentence"], t["translation"]]
        return Reply("\n".join(lines), [[Button("Готово", f"tdone:{sid}"), END]] + ([extra] if extra else []))
    if p["kind"] == "parse":
        text = (f"Модуль 5 · Разбор предложения\n{t['sentence']}\n\nНайди подлежащее и сказуемое и переведи "
                f"предложение. Одним сообщением.")
    else:
        text = f"Модуль 5 · Термин\n{t['sentence']}\n\nЧто здесь значит «{t['term']}»? Напиши по-русски."
    return Reply(text, [[Button("Не знаю", f"idk:{sid}"), END]])


def feedback_reply(fb: TermFeedback) -> Reply:
    row = []
    if fb.has_context:
        row.append(Button("Контекст", f"tctx:{fb.step_id}"))
    if fb.has_note:
        row.append(Button("Заметка", f"tnote:{fb.step_id}"))
    return Reply("\n".join(fb.lines) or "Записано.", [row] if row else [])


def context_reply(conn: sqlite3.Connection, term_id: int) -> Reply:
    t = conn.execute("SELECT term, context, source_ref, translation FROM terms WHERE id = ?", (term_id,)).fetchone()
    if not t["context"]:
        return Reply(f"Абзаца статьи нет. Перевод предложения: {t['translation']}")
    return Reply(f"Контекст «{t['term']}» ({t['source_ref']}):\n\n{t['context']}")


def note_reply(conn: sqlite3.Connection, term_id: int) -> Reply:
    codes = json.loads(conn.execute("SELECT notes_json FROM terms WHERE id = ?", (term_id,)).fetchone()[0])
    if not codes:
        return Reply("Заметки к этому термину нет.")
    marks = ",".join("?" * len(codes))
    rows = {r["code"]: r for r in conn.execute(
        f"SELECT code, title, body FROM notes WHERE track = 'en' AND code IN ({marks})", codes)}
    return Reply("\n\n".join(f"{c} — {rows[c]['title']}\n{rows[c]['body']}" for c in codes if c in rows)
                 or "Заметки к этому термину ещё не загружены.")


def exam_reply(conn: sqlite3.Connection, step: Step) -> Reply:
    from ..english.academic import numbered
    p, sid = step.payload, step.id
    r = conn.execute("SELECT fields_json, title FROM records WHERE code = ?", (p["code"],)).fetchone()
    fields = json.loads(r["fields_json"])
    f = fields["parts"][p["part"]]
    head = f"Модуль 5 · Прогон {p['code']} ({fields.get('Статус', '').rstrip('.')}) · Часть {p['part']}. {f['title']}"
    stop = [[Button("Закончить", "end")]]
    if p["part"] == "1":
        return Reply(f"{head}\n{f.get('Объём', '')}\n\n{f.get('Текст', '')}\n\nПереведи письменно на русский "
                     f"одним сообщением. Словарь можно, машинный перевод нет.", stop)
    if p["phase"] == "read":
        tail = ("Прочитай без словаря и нажми «Прочитал»: дальше текст закрыт." if p["part"] == "2"
                else "Прочитай и нажми «Прочитал»: дальше вопросы, текст можно смотреть.")
        return Reply(f"{head}\n{f.get('Объём', '')}\n\n{f.get('Текст', '')}\n\n{tail}",
                     [[Button("Прочитал", f"exr:{sid}")]] + stop)
    if p["part"] == "2":
        return Reply("Текст закрыт — не возвращайся к нему. Передай содержание по-русски, до двух минут, "
                     "голосом или текстом.", stop)
    if p["part"] == "3":
        q = next((h["content"] for h in reversed(p.get("history", [])) if h["role"] == "assistant"), "")
        return Reply(f"{head}: пять–семь минут голосом.\n\n{q}", stop)
    if p["phase"] == "q":
        qs = numbered(f.get("Вопросы", ""))
        k = len(p.get("answers", []))
        return Reply(f"Вопрос {k + 1} из {len(qs)}: {qs[k] if k < len(qs) else ''}", stop)
    return Reply("Перескажи статью по-английски за две минуты на оборотах блока 7, голосом или текстом.", stop)


SPEECH_TITLE = {"diary": "Дневник", "explain": "Объяснение", "compare": "Сравнение", "advice": "Совет по ситуации",
                "agree": "Согласен или нет", "why": "Почему?", "roleplay": "Ролевая игра", "discussion": "Обсуждение",
                "talk": "Разговор без подготовки"}
CARD_FIELDS = {"explain": ("Задание",), "compare": ("Задание",), "advice": ("Ситуация",),
               "agree": ("Утверждение",), "why": ("Утверждение",), "roleplay": ("Роль бота",),
               "discussion": ("Роль бота", "Тема", "Позиция бота"), "talk": ("Тема",)}


def listen_reply(step: Step, audio) -> Reply:
    from ..english.listening import parts, question_key, text_key
    p, sid = step.payload, step.id
    stop = [Button("Закончить", "end")]
    head = ("Критерий выхода модуля 6, слушание" if p.get("exit") else
            ("Пересказ на слух" if p["format"] == "story" else "Длинное слушание")) + f" · {p['record']}"
    qs, _, _ = parts(p["card"])
    if p.get("phase", "listen") == "listen":
        return Reply(f"{head}\nСлушай без текста, сколько нужно раз. Потом нажми «Прослушал».",
                     [[Button("Прослушал", f"lsn:{sid}"), Button("Медленнее", f"slowl:{text_key(p['record'])}")],
                      stop], audio=audio)
    if p["phase"] == "q":
        k = p["qi"]
        lines = []
        if k == 1 and p["card"].get("Слова"):
            lines.append("Слова: " + p["card"]["Слова"])
        lines.append(f"Вопрос {k} из {len(qs)} — звуком. Ответь голосом.")
        return Reply("\n".join(lines), [[Button("Медленнее", f"slowl:{question_key(p['record'], k)}")], stop],
                     audio=audio)
    return Reply("Перескажи запись по-английски, до двух минут, голосом. Текст откроется после пересказа.",
                 [stop])


def speech_reply(step: Step) -> Reply:
    from ..english.speech import TASK_TEXT
    p = step.payload
    lines = [f"Свободная речь · {SPEECH_TITLE[p['format']]}" + (f" · {p['record']} {p['title']}" if p["record"] else ""),
             TASK_TEXT[p["format"]]]
    for k in CARD_FIELDS.get(p["format"], ()):
        if p["card"].get(k):
            lines.append(f"{k}: {p['card'][k]}")
    if p["history"]:
        lines += ["", p["history"][-1]["content"]]
    if p["turns"]:
        lines.append("Не знаешь, как сказать, — напиши «как сказать: …» по-русски.")
    return Reply("\n".join(lines), [[Button("Закончить", "end")]])


def exit_speech_reply(step: Step, live: bool = False) -> Reply:
    """live — реплики собеседника только звуком (озвучка во время сессии): текст реплики не показывается."""
    p = step.payload
    lines = [f"Критерий выхода модуля {p['module']}, {p['title']}.", p["criteria"]]
    card = p.get("card") or {}
    for k in ("topic", "Задание", "Утверждение", "Роль бота", "Тема", "Позиция бота", "Первый вопрос"):
        if card.get(k) and not (live and k == "Первый вопрос"):
            lines.append(f"{'Тема' if k == 'topic' else k}: {card[k]}")
    if p["mode"] == "mono":
        lines.append("Голосом, одной записью.")
    elif p["mode"] == "talk" and p["history"] and live:
        lines += ["", "Собеседник говорит голосом. Слушай и отвечай голосом."]
        return Reply("\n".join(lines), [[Button("Закончить", "end")]], live_text=p["history"][-1]["content"])
    elif p["mode"] == "talk" and p["history"]:
        lines += ["", p["history"][-1]["content"]]
    return Reply("\n".join(lines), [[Button("Закончить", "end")]])
