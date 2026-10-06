"""Модули английского: какие открыты для нового (карта пути, раздел 9, пункт 2).

Разговорная ветка идёт по порядку 1 → 2 → 3 → 4 → 6: модуль открывается, когда сдан
критерий выхода предыдущего; повторения прошлых модулей идут дальше в общей очереди.
Модуль 1 открыт с запуска. Модуль 5 — академическая ветка — со своей очередью
(часть 2 доработок); в лестницу разговорной ветки его фразы здесь не попадают.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..clock import to_iso

CONVERSATION = ("EN1", "EN2", "EN3", "EN4", "EN6")
NEXT = {"EN1": "EN2", "EN2": "EN3", "EN3": "EN4", "EN4": "EN6"}


class Modules:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def _row(self, area: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM en_modules WHERE area = ?", (area,)).fetchone()

    def is_open(self, area: str) -> bool:
        """Открыт для нового: модуль 1 — пока не закрыт; другие — открыты и не закрыты."""
        row = self._row(area)
        if area == "EN1":
            return row is None or row["closed_at"] is None
        return row is not None and row["opened_at"] is not None and row["closed_at"] is None

    def open_areas(self) -> list[str]:
        return [a for a in CONVERSATION if self.is_open(a)]

    def current(self) -> str | None:
        areas = self.open_areas()
        return areas[0] if areas else None

    def open(self, area: str, now: datetime) -> None:
        self.conn.execute("INSERT INTO en_modules (area, opened_at) VALUES (?, ?) ON CONFLICT(area) DO UPDATE SET "
                          "opened_at = COALESCE(opened_at, excluded.opened_at)", (area, to_iso(now)))

    def on_closed(self, area: str, now: datetime) -> str | None:
        """Модуль закрыт по критерию: открыть следующий. Возвращает открытый модуль."""
        nxt = NEXT.get(area)
        if nxt:
            self.open(nxt, now)
        return nxt

    def sql_filter(self, alias: str = "i") -> tuple[str, list[str]]:
        areas = self.open_areas() or ["EN1"]
        return f"{alias}.area IN ({','.join('?' * len(areas))})", areas
