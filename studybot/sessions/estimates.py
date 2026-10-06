"""Оценки длительности форматов.

Сборщик набирает сессию по оценкам, учёт идёт по факту. Первые четыре недели
(calibration_days) медиана фактического времени формата заменяет стартовую
оценку, когда накопилось хотя бы MIN_SAMPLES ответов; дальше оценка берётся
по окну калибровки и не плывёт.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, timedelta

from ..settings import Settings

DEFAULTS = {
    # английский
    "intro": 45, "transform": 25, "ask": 25, "repeat": 20, "hear_answer": 30,
    "hear_repeat": 20, "dictation": 30, "read_answer": 25, "ru_en": 20, "find_error": 30,
    "dialog": 300, "dialog_long": 600, "monologue": 150, "cp_questions": 180, "cp_monologue": 180,
    "exit_monologue": 180, "exit_dialog": 600,
    # психология (слой К9)
    "card": 20, "card_typed": 60, "open": 180, "distinction": 150, "vignette": 480, "digest_part": 120,
    "vignette_mentor": 120,
    # модуль 5 (доработки под карту пути, часть 2)
    "term_intro": 40, "term_term": 25, "term_parse": 90,
    "text": 1200, "text_q": 90, "text_sentence": 150, "text_paragraph": 600, "text_retell": 180,
}
MIN_SAMPLES = 5


class Estimates:
    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self.conn = conn
        self.settings = settings
        self._cache: dict[str, int] = {}

    def window(self) -> tuple[str, str] | None:
        first = self.conn.execute("SELECT min(study_date) FROM reviews").fetchone()[0]
        if first is None:
            return None
        end = date.fromisoformat(first) + timedelta(days=self.settings.get("calibration_days"))
        return first, end.isoformat()

    def get(self, fmt: str) -> int:
        if fmt in self._cache:
            return self._cache[fmt]
        default = DEFAULTS.get(fmt, 30)
        win = self.window()
        value = default
        if win is not None:
            rows = [r[0] for r in self.conn.execute(
                "SELECT raw_sec FROM reviews WHERE format = ? AND raw_sec > 0 AND study_date >= ? "
                "AND study_date < ?", (fmt, *win))]
            if len(rows) >= MIN_SAMPLES:
                value = int(statistics.median(min(r, self.settings.get("task_cap_sec")) for r in rows))
        self._cache[fmt] = max(5, value)
        return self._cache[fmt]

    def reset(self) -> None:
        self._cache.clear()
