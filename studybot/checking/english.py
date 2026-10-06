"""Проверка ответа по английскому.

Порядок: пустой ответ — ошибка; совпадение со списком после нормализации —
зачёт кодом; совпадение с принятым кандидатом — зачёт кодом; вердикт из кэша;
иначе дешёвая модель со строгим JSON. Модель не двигает прогресс: она
возвращает вердикт, лестница решает сама (EnglishTrack.record).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from ..clock import to_iso
from ..english.normalize import normalize
from ..english.track import Task
from ..enums import Judge
from ..llm.client import CHEAP, LLMUnavailable
from ..llm.runner import ModelRunner


@dataclass
class EnCheck:
    correct: bool | None             # None — отложить без оценки
    judge: Judge
    explanation: str = ""
    correction: str = ""
    tag: str | None = None
    cached: bool = False
    disputable: bool = False         # под вердиктом модели — кнопка «оспорить»
    unavailable: str | None = None   # причина, если модель недоступна
    verdict: dict | None = None
    call_ids: list = field(default_factory=list)


class EnglishChecker:
    def __init__(self, conn: sqlite3.Connection, runner: ModelRunner) -> None:
        self.conn = conn
        self.runner = runner

    def _target(self, task: Task) -> sqlite3.Row:
        code = task.meta.get("target", task.code)
        return self.conn.execute(
            "SELECT id, code, prompt, answer, variants_json, notes_json, answer_hash FROM items "
            "WHERE track = 'en' AND code = ?", (code,)).fetchone()

    def _accepted_variant(self, target_id: int, norm: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM variants_pending WHERE item_id = ? AND normalized = ? AND status = 'accepted'",
            (target_id, norm)).fetchone() is not None

    def _cache_key(self, task: Task, target: sqlite3.Row) -> str:
        return f"{target['answer_hash']}:{'meaning' if task.by_meaning else 'exact'}"

    async def check(self, task: Task, answer: str, now: datetime) -> EnCheck:
        text = (answer or "").strip()
        if not text:
            return EnCheck(False, Judge.CODE, explanation="Пустой ответ.")
        if task.code_match(text):
            return EnCheck(True, Judge.CODE)
        target = self._target(task)
        norm = normalize(text)
        if not task.by_meaning and self._accepted_variant(target["id"], norm):
            return EnCheck(True, Judge.CODE)

        key = self._cache_key(task, target)
        row = self.conn.execute(
            "SELECT verdict_json FROM verdict_cache WHERE item_id = ? AND normalized = ? AND answer_hash = ?",
            (target["id"], norm, key)).fetchone()
        if row:
            v = json.loads(row[0])
            return self._from_verdict(v, cached=True)

        if not self.runner.available():
            return EnCheck(None, Judge.CHEAP, unavailable=self.runner.llm.available()[1])
        try:
            result = await self.runner.run("check_en", CHEAP, self._payload(task, target, text))
        except LLMUnavailable as exc:
            return EnCheck(None, Judge.CHEAP, unavailable=str(exc))
        v = result.data
        self.conn.execute(
            "INSERT OR REPLACE INTO verdict_cache (item_id, normalized, answer_hash, verdict_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (target["id"], norm, key, json.dumps(v, ensure_ascii=False), to_iso(now)))
        if v["verdict"] == "acceptable" and not task.by_meaning:
            self.conn.execute(
                "INSERT INTO variants_pending (item_id, normalized, raw_answer, first_seen) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(item_id, normalized) DO UPDATE SET seen_count = seen_count + 1",
                (target["id"], norm, text, to_iso(now)))
        out = self._from_verdict(v, cached=False)
        out.call_ids = result.call_ids
        return out

    @staticmethod
    def _from_verdict(v: dict, cached: bool) -> EnCheck:
        ok = v["verdict"] == "acceptable"
        return EnCheck(ok, Judge.CHEAP, v.get("explanation", ""), v.get("correction", ""),
                       v.get("tag"), cached=cached, disputable=True, verdict=v)

    def _payload(self, task: Task, target: sqlite3.Row, answer: str) -> dict:
        codes = json.loads(target["notes_json"])
        notes = []
        if codes:
            marks = ",".join("?" * len(codes))
            notes = [f"{c} — {t}: {b}" for c, t, b in self.conn.execute(
                f"SELECT code, title, body FROM notes WHERE track = 'en' AND code IN ({marks})", codes)]
        data = {
            "task": task.instruction,
            "russian_prompt": task.prompt_ru or "",
            "shown_english": task.shown_en or "",
            "answer": answer,
            "mode": "voice_transcript" if task.voice else "typed",
            "grammar_notes": notes,
        }
        if task.by_meaning:
            data["check"] = "meaning"
            data["question"] = task.meta.get("question", "")
            data["expected_answers"] = [t for t, _ in task.meta.get("extra_expected", [])]
        else:
            data["check"] = "exact"
            data["target"] = target["answer"]
            data["variants"] = json.loads(target["variants_json"])
        return data


def log_en_error(conn: sqlite3.Connection, check: EnCheck, item_code: str, review_id: int,
                 answer: str, now: datetime, day: str) -> None:
    """Журнал ошибок наполняется только ответами в боте и только вердиктами с меткой."""
    if check.correct is not False or not check.tag:
        return
    item = conn.execute("SELECT id FROM items WHERE track = 'en' AND code = ?", (item_code,)).fetchone()
    conn.execute(
        "INSERT INTO errors (track, item_id, review_id, ts, study_date, tag, answer, explanation, correction) "
        "VALUES ('en', ?, ?, ?, ?, ?, ?, ?, ?)",
        (item[0], review_id, to_iso(now), day, check.tag, answer, check.explanation, check.correction))
