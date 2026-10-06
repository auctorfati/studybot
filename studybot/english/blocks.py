"""Контрольная точка блока (программа модуля 1, раздел 10).

Десять подсказок с русского голосом (зачёт от восьми), мини-монолог
на четыре-пять предложений по теме блока, пять собственных вопросов,
восемь из десяти вопросов блока поняты на слух. Проводится в выходные,
когда все фразы блока введены. Блок без зачёта не закрывается, следующий
открывается всё равно. Монолог и собственные вопросы проверяет флагман
(слой К5); здесь — состав, пороги и учёт попыток.
"""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from ..clock import StudyCalendar, to_iso
from .graph import QUESTION, phrase_kind

RU_COUNT, RU_PASS = 10, 8
HEAR_COUNT, HEAR_SHARE = 10, 0.8
OWN_QUESTIONS = 5


@dataclass
class ControlPlan:
    area: str
    block: str
    ru_items: list[int]              # с русского голосом
    hearing_items: list[int]         # вопросы блока на слух
    monologue_item: int | None       # единица «по смыслу» блока, если есть
    own_questions: int = OWN_QUESTIONS
    topic: str = ""


@dataclass
class ControlResult:
    ru_correct: int
    ru_total: int
    hearing_correct: int
    hearing_total: int
    questions_ok: int
    monologue_ok: bool
    details: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        ru_need = RU_PASS if self.ru_total >= RU_COUNT else -(-self.ru_total * RU_PASS // RU_COUNT)
        hear_ok = self.hearing_total == 0 or self.hearing_correct >= self.hearing_total * HEAR_SHARE
        return (self.ru_correct >= ru_need and hear_ok
                and self.questions_ok >= OWN_QUESTIONS and self.monologue_ok)


class Blocks:
    def __init__(self, conn: sqlite3.Connection, calendar: StudyCalendar) -> None:
        self.conn = conn
        self.calendar = calendar

    def any_day(self) -> bool:
        row = self.conn.execute("SELECT value_json FROM settings WHERE key = 'cp_any_day'").fetchone()
        return True if row is None else bool(json.loads(row[0]))

    def tried_today(self, day: date) -> bool:
        """Контрольная точка уже шла сегодня: хотя бы одно её задание отвечено."""
        return self.conn.execute(
            "SELECT 1 FROM session_steps st JOIN sessions s ON s.id = st.session_id "
            "WHERE s.study_date = ? AND st.slot = 'control' AND st.status = 'done' LIMIT 1",
            (day.isoformat(),)).fetchone() is not None

    def due_for_control(self, day: date) -> tuple[str, str] | None:
        """Самый ранний блок, готовый к контрольной точке: в любой день (настройка cp_any_day)
        или только в выходные; не чаще раза в день."""
        if not (self.any_day() or self.calendar.is_weekend(day)) or self.tried_today(day):
            return None
        row = self.conn.execute(
            "SELECT area, block FROM en_blocks WHERE introduced_at IS NOT NULL AND cp_passed_at IS NULL "
            "ORDER BY area, CAST(block AS INTEGER) LIMIT 1").fetchone()
        return (row[0], row[1]) if row else None

    def plan(self, area: str, block: str, seed: int | None = None) -> ControlPlan:
        rnd = random.Random(seed)
        rows = self.conn.execute(
            "SELECT i.id, i.kind, i.answer, i.extra_json, COALESCE(s.stage, 0) AS stage FROM items i "
            "LEFT JOIN item_state s ON s.item_id = i.id WHERE i.track = 'en' AND i.archived = 0 "
            "AND i.area = ? AND i.unit = ? ORDER BY i.sort_key", (area, block)).fetchall()
        phrases = [r for r in rows if r["kind"] == "phrase" and r["stage"] >= 2
                   and not json.loads(r["extra_json"]).get("wildcard")]
        # сначала фразы с более высокой ступенью: проверяется то, что должно держаться
        pool = sorted(phrases, key=lambda r: (-r["stage"], rnd.random()))
        ru = [r["id"] for r in pool[:RU_COUNT]]
        questions = [r["id"] for r in phrases if phrase_kind(r["answer"]) == QUESTION]
        rnd.shuffle(questions)
        mono = next((r["id"] for r in rows if r["kind"] == "assembly"), None)
        title = self.conn.execute(
            "SELECT extra_json FROM items WHERE id = ?", (mono,)).fetchone() if mono else None
        return ControlPlan(area, block, ru, questions[:HEAR_COUNT], mono,
                           topic=json.loads(title[0]).get("meaning", "") if title else "")

    def record(self, plan: ControlPlan, result: ControlResult, now: datetime) -> bool:
        self.conn.execute("INSERT OR IGNORE INTO en_blocks (area, block) VALUES (?, ?)",
                          (plan.area, plan.block))
        summary = {"ru": [result.ru_correct, result.ru_total],
                   "hearing": [result.hearing_correct, result.hearing_total],
                   "questions": result.questions_ok, "monologue": result.monologue_ok,
                   "passed": result.passed, **result.details}
        self.conn.execute(
            "UPDATE en_blocks SET cp_attempts = cp_attempts + 1, cp_last_json = ?, "
            "cp_passed_at = CASE WHEN ? THEN ? ELSE cp_passed_at END WHERE area = ? AND block = ?",
            (json.dumps(summary, ensure_ascii=False), int(result.passed), to_iso(now),
             plan.area, plan.block))
        return result.passed
