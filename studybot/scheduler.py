"""Интервалы: фиксированная сетка, одна для обоих треков.

Шаг — индекс в сетке: интервал, который позиция сейчас выжидает.
Вход в сетку — шаг 0 (показ через день). Верно — шаг вперёд, «с трудом» —
тот же шаг заново, срыв — на lapse_steps_back шагов назад (по умолчанию два: с 60 дней на 14,
с 14 на 3), а не к началу: выученное сохраняет часть прочности. Последний шаг не растёт дальше.
Модель прогресс не двигает: сюда приходит уже готовая оценка от кода.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .clock import to_iso
from .enums import Rating
from .settings import Settings


class ScheduleError(RuntimeError):
    pass


@dataclass(frozen=True)
class Change:
    step_before: int | None
    step_after: int | None
    due_before: str | None
    due_after: str | None
    lapse: bool = False
    passed_days: int | None = None   # выдержанный интервал при верном ответе

    @property
    def moved(self) -> bool:
        return self.step_before != self.step_after or self.due_before != self.due_after


class Scheduler:
    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self.conn = conn
        self.settings = settings

    @property
    def grid(self) -> list[int]:
        return self.settings.get("interval_grid")

    def step_of(self, days: int) -> int:
        grid = self.grid
        if days not in grid:
            raise ScheduleError(f"в сетке {grid} нет шага {days} дней")
        return grid.index(days)

    def due_for(self, step: int, day: date) -> date:
        return day + timedelta(days=self.grid[step])

    def _state(self, item_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM item_state WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            raise ScheduleError(f"у позиции {item_id} нет состояния")
        return row

    def ensure_state(self, item_id: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO item_state (item_id) VALUES (?)", (item_id,))

    def enter_grid(self, item_id: int, day: date, now: datetime, step: int = 0) -> Change:
        """Поставить в сетку: ступень 4 английского, выученная карточка, подтверждение среза (шаг 14)."""
        if not 0 <= step < len(self.grid):
            raise ScheduleError(f"шаг {step} вне сетки")
        self.ensure_state(item_id)
        old = self._state(item_id)
        due = self.due_for(step, day).isoformat()
        self.conn.execute(
            "UPDATE item_state SET step = ?, due_date = ?, last_review_at = ? WHERE item_id = ?",
            (step, due, to_iso(now), item_id))
        return Change(old["step"], step, old["due_date"], due)

    def set_due(self, item_id: int, due: date | None) -> None:
        """Показ вне сетки — ступени 2–3 английского, отложенные задания."""
        self.ensure_state(item_id)
        self.conn.execute("UPDATE item_state SET due_date = ? WHERE item_id = ?",
                          (due.isoformat() if due else None, item_id))

    def grade(self, item_id: int, rating: Rating, day: date, now: datetime,
              schedules: bool = True) -> Change:
        """Применить оценку к позиции в сетке.

        schedules=False — ответ учтён, но интервал не двигает
        (текстовый ответ на ступени 4, оспоренный вердикт до разбора).
        """
        st = self._state(item_id)
        if st["step"] is None:
            raise ScheduleError(f"позиция {item_id} не в сетке")
        step, due = st["step"], st["due_date"]
        if not schedules:
            self.conn.execute("UPDATE item_state SET reps = reps + 1, last_review_at = ? WHERE item_id = ?",
                              (to_iso(now), item_id))
            return Change(step, step, due, due)
        last = len(self.grid) - 1
        lapse, passed = False, None
        if rating == Rating.AGAIN:
            new, lapse = max(0, step - int(self.settings.get("lapse_steps_back"))), True
        elif rating == Rating.HARD:
            new = step
        else:
            new, passed = min(step + 1, last), self.grid[step]
        new_due = self.due_for(new, day).isoformat()
        self.conn.execute(
            "UPDATE item_state SET step = ?, due_date = ?, reps = reps + 1, lapses = lapses + ?, "
            "last_review_at = ? WHERE item_id = ?",
            (new, new_due, int(lapse), to_iso(now), item_id))
        return Change(step, new, due, new_due, lapse, passed)
