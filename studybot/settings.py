"""Настройки, которые меняются из бота одной командой.

В базе хранится только то, что изменено; всё остальное читается из SPEC.
Психологию можно выключить одной записью psy_enabled.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from .clock import Clock, parse_hhmm, to_iso


class SettingError(ValueError):
    pass


def _time(v: Any) -> str:
    if not isinstance(v, str):
        raise SettingError("нужно время ЧЧ:ММ")
    t = parse_hhmm(v)
    return f"{t.hour:02d}:{t.minute:02d}"


def _int_range(lo: int, hi: int) -> Callable[[Any], int]:
    def check(v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise SettingError("нужно целое число")
        if not lo <= v <= hi:
            raise SettingError(f"допустимо от {lo} до {hi}")
        return v
    return check


def _float_range(lo: float, hi: float) -> Callable[[Any], float]:
    def check(v: Any) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise SettingError("нужно число")
        if not lo <= v <= hi:
            raise SettingError(f"допустимо от {lo} до {hi}")
        return float(v)
    return check


def _bool(v: Any) -> bool:
    if not isinstance(v, bool):
        raise SettingError("нужно да или нет")
    return v


def _choice(*options: str) -> Callable[[Any], str]:
    def check(v: Any) -> str:
        if v not in options:
            raise SettingError(f"допустимо: {', '.join(options)}")
        return v
    return check


def _grid(v: Any) -> list[int]:
    if not isinstance(v, list) or not v or not all(isinstance(x, int) and x > 0 for x in v):
        raise SettingError("нужен список положительных дней")
    if v != sorted(set(v)):
        raise SettingError("интервалы должны строго возрастать")
    if 30 not in v:
        raise SettingError("в сетке нужен шаг 30 дней: по нему закрывается фраза")
    return v


def _opt_date(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, str):
        try:
            return date.fromisoformat(v).isoformat()
        except ValueError as exc:
            raise SettingError("нужна дата ГГГГ-ММ-ДД") from exc
    raise SettingError("нужна дата или пусто")


@dataclass(frozen=True)
class Spec:
    default: Any
    check: Callable[[Any], Any]
    title: str


SPEC: dict[str, Spec] = {
    # Ритм (слой 3)
    "notify_time": Spec("20:30", _time, "время вечернего уведомления"),
    "norm_weekday_min": Spec(90, _int_range(10, 240), "норма в будни, минут"),
    "norm_weekend_min": Spec(120, _int_range(10, 300), "норма в выходные, минут"),
    "day_min_total": Spec(10, _int_range(5, 60), "минимум для засчитанного дня, минут"),
    "day_min_en": Spec(5, _int_range(0, 60), "из минимума — английский, минут"),
    "sunday_long_min": Spec(40, _int_range(20, 90), "воскресная длинная сессия, минут"),
    "mode": Spec("voice", _choice("voice", "text"), "режим: голос или только текст"),
    "pause_until": Spec(None, _opt_date, "пауза до даты включительно"),
    # Треки и доли
    "psy_enabled": Spec(True, _bool, "психология подключена (выкл — только английский)"),
    "psy_digest_first": Spec(True, _bool, "психология: разбор темы до первой проверки"),
    # Лимиты нового
    "new_en_weekday": Spec(8, _int_range(0, 40), "новых фраз в будни"),
    "new_en_weekend": Spec(10, _int_range(0, 40), "новых фраз в выходные"),
    "new_psy_weekday": Spec(12, _int_range(0, 60), "новых единиц психологии в будни"),
    "new_psy_weekend": Spec(15, _int_range(0, 60), "новых единиц психологии в выходные"),
    # Повторения
    "review_cap_en": Spec(40, _int_range(5, 200), "потолок повторений английского в день"),
    "review_cap_psy": Spec(40, _int_range(5, 200), "потолок повторений психологии в день"),
    "interval_grid": Spec([1, 3, 7, 14, 30, 60, 120, 240], _grid, "сетка интервалов, дней"),
    "lapse_steps_back": Spec(2, _int_range(1, 8), "срыв: на сколько шагов сетки назад (8 — с самого начала)"),
    "probe_confirm_days": Spec(14, _int_range(1, 60),
                               "срез пройден: карточки ядра встают в повторение с этого шага"),
    # Самооценка карточек психологии
    "selfcheck_every": Spec(5, _int_range(1, 20), "каждая N-я карточка набирается текстом"),
    "selfcheck_every_strict": Spec(2, _int_range(1, 20), "то же при расхождении самооценки"),
    "selfcheck_mismatch_share": Spec(1 / 3, _float_range(0.05, 1.0),
                                     "доля расхождений за неделю, после которой набор чаще"),
    # Учёт времени
    "task_cap_sec": Spec(240, _int_range(30, 900), "предел активного времени задания, секунд"),
    "idle_pause_sec": Spec(600, _int_range(60, 3600), "без ответа столько — пауза, секунд"),
    "speech_hold_min": Spec(6, _int_range(0, 20), "речевых заданий придержать на вечер, минут"),
    "read_sec_per_1000": Spec(60, _int_range(10, 300), "чтение: секунд на 1 000 знаков текста"),
    "why_cap_sec": Spec(120, _int_range(30, 600), "«Почему так?»: предел учёта чтения, секунд"),
    # Модуль 5 — академическая ветка (банк модуля 5, раздел 1)
    "new_terms": Spec(5, _int_range(0, 30), "модуль 5: новых терминов в день"),
    "new_acad_phrases": Spec(3, _int_range(0, 10), "модуль 5: новых фраз пересказа и беседы в день"),
    "review_cap_terms": Spec(30, _int_range(5, 200), "модуль 5: потолок повторений терминов в день"),
    "acad_share_m1": Spec(0.25, _float_range(0.0, 1.0), "модуль 5: доля английского времени до конца модуля 1"),
    "acad_share": Spec(1 / 3, _float_range(0.0, 1.0), "модуль 5: доля английского времени дальше"),
    "acad_share_runs": Spec(0.5, _float_range(0.0, 1.0), "модуль 5: доля английского времени с прогонов"),
    "exit_live_tts": Spec(False, _bool, "критерий выхода: реплики модели озвучиваются во время сессии"),
    "cp_any_day": Spec(True, _bool, "контрольная точка блока — в любой день, не чаще раза в день "
                                    "(выкл — только в выходные); критерий выхода — и в вечернем блоке"),
    # Свободная речь: доля английского времени по модулям (карта пути, раздел 2)
    "speech_share_m2": Spec(1 / 3, _float_range(0.0, 1.0), "свободная речь в модуле 2, доля английского"),
    "speech_share_m3": Spec(0.4, _float_range(0.0, 1.0), "свободная речь в модуле 3, доля английского"),
    "speech_share_m4": Spec(0.5, _float_range(0.0, 1.0), "свободная речь в модуле 4, доля английского"),
    "speech_share_m6": Spec(2 / 3, _float_range(0.0, 1.0), "свободная речь в модуле 6, доля английского"),
    # Репетиции блока сборки
    "monologue_passes": Spec(1, _int_range(1, 5), "монолог: зачётов на ступени до следующей"),
    # Журнал ошибок
    "error_share_max": Spec(0.10, _float_range(0.0, 0.5), "доля «найди ошибку» в английском за неделю"),
    "error_tag_threshold": Spec(3, _int_range(1, 20), "ошибок по метке до появления заданий"),
    # Калибровка (слой 3)
    "calibration_days": Spec(28, _int_range(7, 90), "дней измерения фактического времени"),
}


class Settings:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self._conn = conn
        self._clock = clock

    def get(self, key: str) -> Any:
        spec = self._spec(key)
        row = self._conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return spec.default
        return json.loads(row["value_json"])

    def set(self, key: str, value: Any) -> Any:
        spec = self._spec(key)
        try:
            clean = spec.check(value)
        except SettingError as exc:
            raise SettingError(f"{spec.title}: {exc}") from exc
        except ValueError as exc:
            raise SettingError(f"{spec.title}: {exc}") from exc
        self._conn.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(clean, ensure_ascii=False), to_iso(self._clock.now())),
        )
        return clean

    def reset(self, key: str) -> None:
        self._spec(key)
        self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def all(self) -> dict[str, Any]:
        return {key: self.get(key) for key in SPEC}

    def changed(self) -> dict[str, Any]:
        return {k: v for k, v in self.all().items() if v != SPEC[k].default}

    # Производные значения, которыми пользуются остальные модули

    def norm_min(self, day: date) -> int:
        return self.get("norm_weekend_min" if day.weekday() >= 5 else "norm_weekday_min")

    def new_limit_en(self, day: date) -> int:
        return self.get("new_en_weekend" if day.weekday() >= 5 else "new_en_weekday")

    def psy_active(self) -> bool:
        """Психология включена и её банки загружены. Без банков — только английский."""
        if not self.get("psy_enabled"):
            return False
        return self._conn.execute("SELECT 1 FROM items WHERE track = 'psy' AND archived = 0 LIMIT 1"
                                  ).fetchone() is not None

    def new_limit_psy(self, day: date) -> int:
        if not self.psy_active():
            return 0
        return self.get("new_psy_weekend" if day.weekday() >= 5 else "new_psy_weekday")

    def track_shares(self) -> dict[str, float]:
        """Доли дневной нормы. Психология выключена или не загружена: английский на всю норму."""
        if self.psy_active():
            return {"en": 0.5, "psy": 0.5}
        return {"en": 1.0, "psy": 0.0}

    def is_paused(self, day: date) -> bool:
        until = self.get("pause_until")
        return until is not None and day <= date.fromisoformat(until)

    @staticmethod
    def _spec(key: str) -> Spec:
        try:
            return SPEC[key]
        except KeyError:
            raise SettingError(f"неизвестная настройка: {key}") from None
