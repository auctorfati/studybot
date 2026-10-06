"""Прогоны кандидатского экзамена — записи Э модуля 5 (банк блока 9, раздел 1).

Четыре части: 1 — письменный перевод (60 минут, словарь можно), 2 — беглое чтение
на время и передача содержания по-русски, 3 — беседа о научной работе голосом,
4 — чтение абстракта и введения, вопросы и пересказ по-английски. Проверяет флагман.
Прогон идёт целиком или по частям, по одной части за раз; засчитан, если сданы все четыре.
Тренировочные Э1, Э2 открываются, когда закрыты все тексты Ч и все фразы блоков 7–8
прошли третью ступень; тогда модулю 5 отдаётся половина английского времени.
Критерий выхода модуля 5 — два засчитанных прогона на резервных Э3 и Э4 с промежутком
не меньше трёх дней; несданная попытка — следующая на другой резервной; когда обе
использованы — повтор на той, что была не меньше 30 дней назад.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

from ..clock import to_iso

PART_TITLE = {"1": "перевод", "2": "беглое чтение", "3": "беседа", "4": "чтение и пересказ"}
EXIT_GAP_DAYS = 3
RESERVE_REUSE_DAYS = 30


class Exams:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def runs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM records WHERE kind = 'exam' AND archived = 0 ORDER BY sort_key"
                                 ).fetchall()

    def fields(self, code: str) -> dict:
        r = self.conn.execute("SELECT fields_json FROM records WHERE code = ?", (code,)).fetchone()
        return json.loads(r[0]) if r else {}

    def bank(self) -> list[str]:
        return self.fields("Э0").get("questions", [])

    @staticmethod
    def reserve(fields: dict) -> bool:
        return "Резерв" in fields.get("Статус", "")

    def opened(self) -> bool:
        """Тексты Ч закрыты все, фразы блоков 7–8 модуля 5 — на третьей ступени и выше."""
        texts = self.conn.execute("SELECT count(*) FROM records r LEFT JOIN text_state s ON s.code = r.code "
                                  "WHERE r.kind = 'text' AND r.archived = 0 AND COALESCE(s.closed, 0) = 0"
                                  ).fetchone()[0]
        phrases = self.conn.execute(
            "SELECT count(*) FROM items i LEFT JOIN item_state s ON s.item_id = i.id WHERE i.track = 'en' "
            "AND i.area = 'EN5' AND i.archived = 0 AND i.kind = 'phrase' AND COALESCE(s.stage, 0) < 3").fetchone()[0]
        has = self.conn.execute("SELECT 1 FROM records WHERE kind = 'text' AND archived = 0 LIMIT 1").fetchone()
        return bool(has) and texts == 0 and phrases == 0

    def open_run(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM exam_runs WHERE finished_date IS NULL ORDER BY id LIMIT 1").fetchone()

    def _passes(self, reserve_only: bool = True) -> list[date]:
        rows = self.conn.execute("SELECT code, finished_date FROM exam_runs WHERE passed = 1 ORDER BY finished_date"
                                 ).fetchall()
        return [date.fromisoformat(r[1]) for r in rows if not reserve_only or self.reserve(self.fields(r[0]))]

    def exit_done(self) -> bool:
        days = self._passes()
        return len(days) >= 2 and any((d - days[0]).days >= EXIT_GAP_DAYS for d in days[1:])

    def next_code(self, day: date) -> str | None:
        """Какой прогон начать: сначала тренировочные, затем резервные по правилам критерия."""
        if not self.opened() or self.exit_done():
            return None
        done = {r[0] for r in self.conn.execute("SELECT code FROM exam_runs WHERE finished_date IS NOT NULL")}
        runs = self.runs()
        for r in runs:
            if not self.reserve(json.loads(r["fields_json"])) and r["code"] not in done:
                return r["code"]
        passes = self._passes()
        if passes and (day - passes[-1]).days < EXIT_GAP_DAYS:
            return None
        reserve = [r["code"] for r in runs if self.reserve(json.loads(r["fields_json"]))]
        last = {r[0]: date.fromisoformat(r[1]) for r in self.conn.execute(
            "SELECT code, max(finished_date) FROM exam_runs WHERE finished_date IS NOT NULL GROUP BY code")}
        for code in reserve:
            if code not in last:
                return code
        old = [c for c in reserve if (day - last[c]).days >= RESERVE_REUSE_DAYS]
        return min(old, key=lambda c: last[c]) if old else None

    def next_part(self, day: date, now: datetime) -> tuple[int, str, str] | None:
        """(id прогона, код, часть) — следующая несданная часть открытого прогона или новый прогон."""
        run = self.open_run()
        if run is None:
            code = self.next_code(day)
            if code is None:
                return None
            rid = self.conn.execute("INSERT INTO exam_runs (code, started_at) VALUES (?, ?)",
                                    (code, to_iso(now))).lastrowid
            run = self.conn.execute("SELECT * FROM exam_runs WHERE id = ?", (rid,)).fetchone()
        parts = json.loads(run["parts_json"])
        for p in ("1", "2", "3", "4"):
            if p not in parts:
                return run["id"], run["code"], p
        return None

    def record_part(self, run_id: int, part: str, passed: bool, verdict: dict | None, day: date) -> dict:
        run = self.conn.execute("SELECT * FROM exam_runs WHERE id = ?", (run_id,)).fetchone()
        parts = json.loads(run["parts_json"])
        parts[part] = {"passed": passed, "verdict": verdict}
        finished = len(parts) == 4
        all_passed = finished and all(v["passed"] for v in parts.values())
        self.conn.execute("UPDATE exam_runs SET parts_json = ?, finished_date = ?, passed = ? WHERE id = ?",
                          (json.dumps(parts, ensure_ascii=False), day.isoformat() if finished else None,
                           int(all_passed) if finished else None, run_id))
        out = {"finished": finished, "passed": all_passed, "reserve": self.reserve(self.fields(run["code"])),
               "closed": False}
        if finished and all_passed and self.exit_done():
            self.conn.execute("INSERT INTO en_modules (area, closed_at) VALUES ('EN5', ?) ON CONFLICT(area) "
                              "DO UPDATE SET closed_at = COALESCE(closed_at, excluded.closed_at)",
                              (day.isoformat(),))
            out["closed"] = True
        return out
