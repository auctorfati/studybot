"""Очередь повторений и лимит нового.

Потолок — 40 повторений в день на трек. Если к началу дня просрочено больше,
сегодня остаются самые просроченные относительно своего интервала, остальные
расходятся по ближайшим дням так, чтобы ни один заполняемый день не превысил
потолок. Долг не копится и ничем не наказывается. rebalance() идемпотентна,
её вызывает сборщик в начале первой сессии дня (слой К6).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .clock import to_iso
from .enums import Track
from .settings import Settings

# Позиция видна: не в архиве, фрагмент есть (или не нужен), стоит в сетке.
VISIBLE = """
    i.track = :track AND i.archived = 0 AND i.fragment_status != 'missing'
    AND s.step IS NOT NULL AND s.due_date IS NOT NULL
    AND (s.deferred_until IS NULL OR s.deferred_until <= :now)
"""


@dataclass(frozen=True)
class DueItem:
    item_id: int
    code: str
    step: int
    due_date: str
    overdue_ratio: float


class ReviewQueue:
    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self.conn = conn
        self.settings = settings

    def cap(self, track: Track | str) -> int:
        return self.settings.get("review_cap_en" if Track(track) == Track.EN else "review_cap_psy")

    def _overdue(self, track: str, day: date, now: datetime) -> list[DueItem]:
        grid = self.settings.get("interval_grid")
        rows = self.conn.execute(
            f"SELECT i.id, i.code, s.step, s.due_date, i.sort_key FROM items i "
            f"JOIN item_state s ON s.item_id = i.id WHERE {VISIBLE} AND s.due_date <= :day",
            {"track": track, "now": to_iso(now), "day": day.isoformat()}).fetchall()
        out = []
        for r in rows:
            late = (day - date.fromisoformat(r["due_date"])).days
            interval = grid[min(r["step"], len(grid) - 1)]
            out.append((DueItem(r["id"], r["code"], r["step"], r["due_date"], late / interval),
                        r["sort_key"]))
        # самые просроченные относительно интервала — первыми; при равенстве — раньше срок, порядок банка
        out.sort(key=lambda x: (-x[0].overdue_ratio, x[0].due_date, x[1]))
        return [d for d, _ in out]

    def load(self, track: str, day: date, now: datetime) -> int:
        """Сколько видимых позиций назначено ровно на этот день."""
        return self.conn.execute(
            f"SELECT count(*) FROM items i JOIN item_state s ON s.item_id = i.id "
            f"WHERE {VISIBLE} AND s.due_date = :day",
            {"track": track, "now": to_iso(now), "day": day.isoformat()}).fetchone()[0]

    def rebalance(self, track: Track | str, day: date, now: datetime) -> int:
        """Оставить на сегодня не больше потолка, излишек разнести. Возвращает число перенесённых."""
        track = Track(track).value
        cap = self.cap(track)
        due = self._overdue(track, day, now)
        if len(due) <= cap:
            return 0
        rest = due[cap:]
        moved, d = 0, day
        free = 0
        for item in rest:
            while free <= 0:
                d += timedelta(days=1)
                free = cap - self.load(track, d, now)
            self.conn.execute("UPDATE item_state SET due_date = ? WHERE item_id = ?",
                              (d.isoformat(), item.item_id))
            free -= 1
            moved += 1
        return moved

    def due_today(self, track: Track | str, day: date, now: datetime) -> list[DueItem]:
        """Повторения на сегодня в порядке срочности, не больше потолка."""
        track = Track(track).value
        return self._overdue(track, day, now)[: self.cap(track)]

    # лимит нового

    def _ensure_day(self, day: date) -> None:
        self.conn.execute("INSERT OR IGNORE INTO days (study_date) VALUES (?)", (day.isoformat(),))

    def new_used(self, track: Track | str, day: date) -> int:
        col = "new_en" if Track(track) == Track.EN else "new_psy_units"
        row = self.conn.execute(f"SELECT {col} FROM days WHERE study_date = ?",
                                (day.isoformat(),)).fetchone()
        return row[0] if row else 0

    def new_budget(self, track: Track | str, day: date) -> int:
        """Сколько нового ещё можно сегодня: фразы для английского, единицы для психологии."""
        if self.settings.is_paused(day):
            return 0
        limit = (self.settings.new_limit_en(day) if Track(track) == Track.EN
                 else self.settings.new_limit_psy(day))
        return max(0, limit - self.new_used(track, day))

    def spend_new(self, track: Track | str, day: date, units: int = 1) -> None:
        col = "new_en" if Track(track) == Track.EN else "new_psy_units"
        self._ensure_day(day)
        self.conn.execute(f"UPDATE days SET {col} = {col} + ? WHERE study_date = ?",
                          (units, day.isoformat()))
