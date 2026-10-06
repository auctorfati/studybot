"""Свободная речь модулей 2–6 — один шаблон (карта пути, раздел 2 и раздел 9, пункт 5).

Дневник, объяснение, сравнение, совет, «Согласен или нет», «Почему?», ролевая игра со скрытой
ситуацией, обсуждение и спор, разговор без подготовки. Сначала материал — карточка задания
(ситуация, утверждение, тема); затем речь; в конце модель разбирает её по карточке:
что есть, чего нет, не больше трёх поправок по ядру модуля. В повторение ничего не идёт.
В разговоре можно спросить «как сказать: …» — модель даёт вариант в пределах пройденного.
Доля свободной речи в английском времени по модулям — настройки бота.
Форматы на слух (пересказ истории, длинное слушание) ждут озвучки, как задания «на слух».
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime

from ..clock import to_iso
from .modules import Modules

# формат → (вид записи, ходов разговора, оценка секунд, длинный — только в 30, вечерней и длинной)
FORMATS = {
    "diary": (None, 0, 150, False),
    "explain": ("explain", 0, 180, False),
    "compare": ("compare", 0, 180, False),
    "advice": ("advice", 0, 150, False),
    "agree": ("statement", 0, 120, False),
    "why": ("statement", 3, 300, False),
    "roleplay": ("roleplay", 10, 600, True),
    "discussion": ("discussion", 8, 600, True),
    "talk": ("topic", 8, 600, True),
    "story": ("story", 0, 360, False),          # пересказ на слух — только с озвученной записью
    "listening": ("listening", 0, 900, True),   # длинное слушание модуля 6
}
# с какого модуля формат открывается (карта пути, разделы 3–7)
FROM_MODULE = {"diary": 2, "explain": 3, "compare": 3, "advice": 3, "roleplay": 3, "agree": 4, "why": 4,
               "discussion": 4, "talk": 6, "story": 2, "listening": 6}
SHARE = {"EN2": "speech_share_m2", "EN3": "speech_share_m3", "EN4": "speech_share_m4", "EN6": "speech_share_m6"}
CORE = {"2": "Past Simple, was и were, did, going to и will, Present Continuous",
        "3": "can, should, have to, must, сравнения, how much и how many",
        "4": "Present Perfect и Past Simple, may, might, could, первое условное, связки мнения",
        "6": "Past Continuous, Past Perfect, второе условное, косвенная речь"}

TASK_TEXT = {
    "diary": "Дневник: расскажи по-английски за 30–90 секунд, как прошёл день и что планируешь на завтра.",
    "explain": "Объясни по-английски, как это делается или что это значит, за 60–90 секунд.",
    "compare": "Сравни по-английски — не меньше пяти сравнений; если нужен вывод — с причиной.",
    "advice": "Дай по-английски два совета этому человеку.",
    "agree": "Согласен или нет? Ответь по-английски: позиция, причина и фраза для разговора "
             "(I think…, I'm not sure…, That's true, but…).",
    "why": "Скажи по-английски своё мнение об этом утверждении. Бот трижды спросит «почему» или «а если».",
    "roleplay": "Ролевая игра: выясни вопросами, что у собеседника за ситуация, и дай советы.",
    "discussion": "Обсуждение: у бота своя позиция, он будет её держать и возражать. Держи свою или "
                  "поменяй её с объяснением.",
    "talk": "Разговор без подготовки на тему ниже. Отвечай и сам спрашивай.",
    "story": "Пересказ на слух: послушай историю без текста, ответь на вопросы и перескажи.",
    "listening": "Длинное слушание: запись без текста, десять вопросов звуком, пересказ за две минуты.",
}


class Speech:
    def __init__(self, conn: sqlite3.Connection, settings) -> None:
        self.conn = conn
        self.settings = settings
        self.modules = Modules(conn)

    def module(self) -> str | None:
        cur = self.modules.current()
        return cur if cur and cur != "EN1" else None

    def share(self) -> float:
        m = self.module()
        return float(self.settings.get(SHARE[m])) if m in SHARE else 0.0

    def available(self, long_ok: bool) -> list[str]:
        m = self.module()
        if m is None:
            return []
        n = int(m[2:])
        out = []
        for fmt, (kind, _, _, long_fmt) in FORMATS.items():
            if FROM_MODULE[fmt] > n or (long_fmt and not long_ok):
                continue
            if kind and self.pick(kind, fmt) is None:
                continue
            out.append(fmt)
        return out

    def pick(self, kind: str, fmt: str) -> sqlite3.Row | None:
        """Запись по кругу: из открытых и пройденных модулей, реже использованная, затем по порядку."""
        areas = [a for a in ("EN2", "EN3", "EN4", "EN6") if self.modules.is_open(a) or self._closed(a)]
        if not areas:
            return None
        if fmt in ("story", "listening"):
            from .listening import pick as pick_voiced
            from .rehearsals import Rehearsals
            return pick_voiced(self.conn, Rehearsals(self.conn), kind, areas)
        if fmt == "discussion":
            kinds = ("discussion",)
        else:
            kinds = (kind,)
        marks = ",".join("?" * len(areas))
        return self.conn.execute(
            f"SELECT r.*, COALESCE(s.uses, 0) AS uses FROM records r LEFT JOIN record_state s ON s.code = r.code "
            f"WHERE r.archived = 0 AND r.kind IN ({','.join('?' * len(kinds))}) AND r.area IN ({marks}) "
            f"ORDER BY uses, r.sort_key LIMIT 1", (*kinds, *areas)).fetchone()

    def _closed(self, area: str) -> bool:
        row = self.conn.execute("SELECT closed_at FROM en_modules WHERE area = ?", (area,)).fetchone()
        return bool(row and row[0])

    def used_today(self, day: date) -> set[str]:
        return {json.loads(r[0])["format"] for r in self.conn.execute(
            "SELECT st.payload_json FROM session_steps st JOIN sessions s ON s.id = st.session_id "
            "WHERE s.study_date = ? AND st.kind = 'speech'", (day.isoformat(),))}

    def next_format(self, day: date, long_ok: bool) -> str | None:
        """Формат реже использованный сегодня; дневник — не больше раза в день."""
        avail = self.available(long_ok)
        today = self.used_today(day)
        avail = [f for f in avail if not (f == "diary" and "diary" in today)]
        if not avail:
            return None
        counts = {f: self.conn.execute(
            "SELECT count(*) FROM session_steps WHERE kind = 'speech' AND json_extract(payload_json, '$.format') = ?",
            (f,)).fetchone()[0] for f in avail}
        return min(avail, key=lambda f: (counts[f], list(FORMATS).index(f)))

    def payload(self, fmt: str) -> dict:
        kind, turns, _, _ = FORMATS[fmt]
        rec = self.pick(kind, fmt) if kind else None
        card = json.loads(rec["fields_json"]) if rec else {}
        m = (self.module() or "EN2")[2:]
        return {"format": fmt, "record": rec["code"] if rec else None, "title": rec["title"] if rec else "",
                "card": card, "turns": turns, "history": [], "answers": [], "module": m}

    def mark_used(self, code: str | None, now: datetime) -> None:
        if not code:
            return
        self.conn.execute("INSERT INTO record_state (code, uses, last_at) VALUES (?, 1, ?) ON CONFLICT(code) "
                          "DO UPDATE SET uses = uses + 1, last_at = excluded.last_at", (code, to_iso(now)))
