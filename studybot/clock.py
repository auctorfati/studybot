"""Время и учебные сутки.

Весь код берёт «сейчас» только из Clock: так прогон 30 условных дней
на ускоренных часах идёт тем же кодом, что и бот.
В базе время хранится в UTC, дни — датами учебных суток.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime:
        """Текущий момент, aware, в UTC."""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """Управляемые часы для тестов и симуляции."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock: нужно время с часовым поясом")
        self._now = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> datetime:
        self._now += timedelta(**delta)
        return self._now

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FakeClock: нужно время с часовым поясом")
        self._now = moment.astimezone(timezone.utc)


def parse_hhmm(value: str) -> time:
    try:
        hh, mm = value.strip().split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"время должно быть в виде ЧЧ:ММ, получено {value!r}") from exc


@dataclass(frozen=True)
class StudyCalendar:
    """Учебные сутки: с границы (по умолчанию 03:00 МСК) до следующей границы.

    Поздний вечерний блок в 01:30 засчитывается в предыдущий день.
    """

    tz: ZoneInfo
    boundary: time

    @classmethod
    def from_strings(cls, tz_name: str, boundary: str) -> "StudyCalendar":
        return cls(ZoneInfo(tz_name), parse_hhmm(boundary))

    def local(self, moment: datetime) -> datetime:
        if moment.tzinfo is None:
            raise ValueError("ожидается время с часовым поясом")
        return moment.astimezone(self.tz)

    def study_date(self, moment: datetime) -> date:
        loc = self.local(moment)
        if loc.time() < self.boundary:
            return loc.date() - timedelta(days=1)
        return loc.date()

    def day_start(self, day: date) -> datetime:
        """Начало учебных суток как момент в UTC."""
        return datetime.combine(day, self.boundary, self.tz).astimezone(timezone.utc)

    def day_end(self, day: date) -> datetime:
        return self.day_start(day + timedelta(days=1))

    def at_local(self, day: date, hhmm: str | time) -> datetime:
        """Момент «в такое-то местное время учебного дня» (для уведомления 20:30).

        Время раньше границы относится к ночи после календарной даты дня.
        """
        t = parse_hhmm(hhmm) if isinstance(hhmm, str) else hhmm
        cal_day = day + timedelta(days=1) if t < self.boundary else day
        return datetime.combine(cal_day, t, self.tz).astimezone(timezone.utc)

    @staticmethod
    def is_weekend(day: date) -> bool:
        return day.weekday() >= 5

    @staticmethod
    def is_sunday(day: date) -> bool:
        return day.weekday() == 6

    @staticmethod
    def week_start(day: date) -> date:
        """Понедельник недели; неделя для сводки — пн–вс."""
        return day - timedelta(days=day.weekday())


def to_iso(moment: datetime) -> str:
    """Формат хранения моментов в базе: UTC, ISO 8601, секунды."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
