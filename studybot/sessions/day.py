"""Учёт дня: счётчик, засчитанный день, выбор трека по недобору."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from ..enums import Track
from ..settings import Settings


@dataclass
class DayStatus:
    day: date
    norm_min: int
    en_sec: int
    psy_sec: int
    voice_sec: int
    counted: bool
    skipped: bool
    psy_enabled: bool

    @property
    def total_min(self) -> int:
        return (self.en_sec + self.psy_sec) // 60

    @property
    def left_min(self) -> int:
        return max(0, self.norm_min - self.total_min)

    @property
    def norm_done(self) -> bool:
        return self.total_min >= self.norm_min

    def counter(self) -> str:
        """«сегодня 35 из 60, английский 20, психология 15»."""
        text = f"сегодня {self.total_min} из {self.norm_min}, английский {self.en_sec // 60}"
        if self.psy_enabled or self.psy_sec:
            text += f", психология {self.psy_sec // 60}"
        return text


class DayBook:
    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self.conn = conn
        self.settings = settings

    def ensure(self, day: date) -> None:
        self.conn.execute("INSERT OR IGNORE INTO days (study_date) VALUES (?)", (day.isoformat(),))

    def add_time(self, day: date, track: Track | str, sec: int, voice_sec: int = 0) -> None:
        self.ensure(day)
        col = "sec_en" if Track(track) == Track.EN else "sec_psy"
        self.conn.execute(f"UPDATE days SET {col} = {col} + ?, sec_voice = sec_voice + ? WHERE study_date = ?",
                          (sec, voice_sec, day.isoformat()))
        self._recount(day)

    def add_review(self, day: date, track: Track | str) -> None:
        self.ensure(day)
        col = "reviews_en" if Track(track) == Track.EN else "reviews_psy"
        self.conn.execute(f"UPDATE days SET {col} = {col} + 1 WHERE study_date = ?", (day.isoformat(),))

    def reviews_done(self, day: date, track: Track | str) -> int:
        col = "reviews_en" if Track(track) == Track.EN else "reviews_psy"
        row = self.conn.execute(f"SELECT {col} FROM days WHERE study_date = ?", (day.isoformat(),)).fetchone()
        return row[0] if row else 0

    def _recount(self, day: date) -> None:
        """День засчитан при 10 минутах, из них английского не меньше пяти."""
        row = self.conn.execute("SELECT sec_en, sec_psy FROM days WHERE study_date = ?",
                                (day.isoformat(),)).fetchone()
        ok = (row["sec_en"] + row["sec_psy"] >= self.settings.get("day_min_total") * 60
              and row["sec_en"] >= self.settings.get("day_min_en") * 60)
        self.conn.execute("UPDATE days SET counted = ? WHERE study_date = ?", (int(ok), day.isoformat()))

    def skip(self, day: date) -> None:
        """Кнопка «не сегодня»: день закрыт без упрёков."""
        self.ensure(day)
        self.conn.execute("UPDATE days SET skipped = 1 WHERE study_date = ?", (day.isoformat(),))

    def set_speech_held(self, day: date, held: bool) -> None:
        self.ensure(day)
        self.conn.execute("UPDATE days SET speech_held = ? WHERE study_date = ?", (int(held), day.isoformat()))

    def status(self, day: date) -> DayStatus:
        self.ensure(day)
        r = self.conn.execute("SELECT * FROM days WHERE study_date = ?", (day.isoformat(),)).fetchone()
        return DayStatus(day, self.settings.norm_min(day), r["sec_en"], r["sec_psy"], r["sec_voice"],
                         bool(r["counted"]), bool(r["skipped"]), self.settings.psy_active())

    def choose_track(self, day: date, short: bool = False) -> Track:
        """Предмет следующего блока — тот, что сильнее отстаёт от своей доли нормы.

        Короткая (пятиминутная) сессия английская, пока английский минимум дня не набран.
        """
        st = self.status(day)
        shares = self.settings.track_shares()
        if shares["psy"] == 0:
            return Track.EN
        if short and st.en_sec < self.settings.get("day_min_en") * 60:
            return Track.EN
        norm = st.norm_min * 60
        deficit_en = shares["en"] * norm - st.en_sec
        deficit_psy = shares["psy"] * norm - st.psy_sec
        return Track.PSY if deficit_psy > deficit_en else Track.EN
