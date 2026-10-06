"""Репетиции блока сборки и критерий выхода модуля (программа модуля, раздел 1).

Монолог по плану идёт в три ступени: 1 — по ключевым словам, 2 — по названиям
частей, 3 — без опоры. Зачёт тренировочного монолога на текущей ступени переводит
на следующую (по умолчанию — один зачёт). Сценарии диалога выбираются по кругу.

Репетиции работают, когда открыт их блок (введена первая фраза блока сборки).
Контрольная точка блока сборки берёт монолог с опорой на названия частей
и свои вопросы в ситуации сценария.

Критерий выхода: монолог без опоры и десять обменов по одному из сценариев,
вопросы бота только звуком. Каждая часть сдаётся дважды, между зачётами не
меньше трёх дней. Попытка — в 30-минутной или длинной сессии, только голосом,
не чаще раза в учебный день. Монолог открывается, когда тренировка дошла до
ступени 3; диалог — когда у сценария озвучены первая реплика и все вопросы бота.
Модуль закрывается, когда обе части сданы.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..clock import to_iso
from ..content.common import short_hash

STAGES = 3
EXIT_PASSES = 2
EXIT_GAP_DAYS = 3
EXIT_TURNS = 10
CONTROL_STAGE = 2              # контрольная точка блока: опора — названия частей
STAGE_TITLE = {1: "по ключевым словам", 2: "по названиям частей", 3: "без опоры"}

# Части критерия выхода по модулям (программа модуля 1, раздел 1; карта пути, разделы 3–7)
EXIT_SPEC = {
    "EN1": ("monologue", "dialog"),
    "EN2": ("monologue", "story", "dialog"),
    "EN3": ("explain", "compare", "advice_dialog"),
    "EN4": ("explain", "discussion"),
    "EN6": ("talk", "listening"),
}
PART_TITLE = {"monologue": "монолог", "dialog": "диалог", "story": "рассказ", "explain": "объяснение",
              "compare": "сравнение", "advice_dialog": "диалог-совет", "discussion": "обсуждение",
              "talk": "разговор", "listening": "слушание"}
EXIT_TURNS_BY = {"EN1": 10, "EN2": 15}
DYNAMIC = {"advice_dialog": ("roleplay", 12), "discussion": ("discussion", 10), "talk": ("topic", 10)}
STORY_TOPICS = ("пересказ фильма или книги", "поездка", "событие", "жизнь известного человека")
EXIT_CRITERIA = {
    ("EN2", "monologue"): "Монолог о вчерашнем дне и планах на неделю, две минуты без опоры: не меньше 16 "
                          "предложений, прошлое и будущее на месте, не больше трёх ошибок в ядре модуля (формы "
                          "прошедшего, did, was и were, going to и will).",
    ("EN2", "story"): "Рассказ на две минуты на заданную тему; прошедшее время на месте; не больше трёх ошибок "
                      "в ядре модуля 2.",
    ("EN2", "dialog"): "Диалог на пятнадцать обменов, вопросы бота только звуком: ответ по смыслу на каждый вопрос; "
                       "не меньше семи своих вопросов построены верно, из них не меньше трёх — в прошедшем времени.",
    ("EN3", "explain"): "Объяснение на две минуты: порядок действий и советы; по объяснению можно это сделать; "
                        "не больше пяти ошибок в ядре модулей 2–3.",
    ("EN3", "compare"): "Сравнение на две минуты: не меньше пяти верных сравнений; не больше пяти ошибок в ядре "
                        "модулей 2–3.",
    ("EN3", "advice_dialog"): "Диалог-совет на двенадцать обменов, реплики бота только звуком: ситуация выяснена "
                              "вопросами, не меньше трёх советов с should или have to; не больше пяти ошибок в ядре "
                              "модулей 2–3.",
    ("EN4", "explain"): "Объяснение на три минуты на заданную тему; четыре части — тезис, две причины, пример, вывод; "
                        "не меньше 20 предложений, связки на месте, не больше пяти ошибок в ядре модулей 2–4.",
    ("EN4", "discussion"): "Обсуждение десять минут, реплики бота только звуком: бот задаёт не меньше пяти вопросов "
                           "«почему» и «а если» и дважды возражает; ученик держит позицию или меняет её "
                           "с объяснением и использует не меньше трёх инструментов разговора.",
    ("EN6", "talk"): "Разговор десять минут без подготовки на случайную тему, с несогласием и уточнениями: "
                     "без перехода на русский и без длинных пауз, ошибок, мешающих смыслу, не больше пяти.",
    ("EN6", "listening"): "Незнакомая запись на 4–5 минут: верно не меньше восьми ответов из десяти и пересказ "
                          "за две минуты.",
}
LISTENING_RESERVE = ("Л6", "Л12", "Л17", "Л19")


@dataclass
class Rehearsal:
    code: str
    area: str
    block: str
    kind: str
    title: str
    data: dict
    stage: int = 1
    uses: int = 0

    @property
    def module(self) -> str:
        return self.area[2:] if self.area.startswith("EN") else self.area


def line_key(code: str, idx: int) -> str:
    """Ключ реплики сценария для аудио: 0 — первая реплика, 1… — вопросы бота."""
    return f"{code}:{idx}"


def scenario_lines(r: Rehearsal) -> list[tuple[str, str]]:
    """Реплики бота, которые озвучиваются заранее: (ключ, текст)."""
    out = [(line_key(r.code, 0), r.data["opening"])]
    out += [(line_key(r.code, k), q) for k, q in enumerate(r.data["bot_questions"], start=1)]
    return out


def support_lines(r: Rehearsal, stage: int) -> list[str]:
    """Опора монолога на ступени: части с ключевыми словами, только названия или ничего."""
    parts = r.data.get("parts", [])
    if stage <= 1:
        return [f"{p['name']}: {' — '.join(p['keywords'])}" for p in parts]
    if stage == 2:
        return [p["name"] for p in parts]
    return []


class Rehearsals:
    def __init__(self, conn: sqlite3.Connection, passes_to_advance=1) -> None:
        """passes_to_advance — число или функция без аргументов (настройка monologue_passes)."""
        self.conn = conn
        self._passes = passes_to_advance

    @property
    def passes_to_advance(self) -> int:
        return self._passes() if callable(self._passes) else int(self._passes)

    def _load(self, row: sqlite3.Row) -> Rehearsal:
        st = self.conn.execute("SELECT stage, uses FROM rehearsal_state WHERE code = ?",
                               (row["code"],)).fetchone()
        return Rehearsal(row["code"], row["area"], row["block"], row["kind"], row["title"],
                         json.loads(row["data_json"]), st["stage"] if st else 1, st["uses"] if st else 0)

    def get(self, code: str) -> Rehearsal | None:
        row = self.conn.execute("SELECT * FROM rehearsals WHERE code = ?", (code,)).fetchone()
        return self._load(row) if row else None

    def of_block(self, area: str, block: str, kind: str | None = None) -> list[Rehearsal]:
        rows = self.conn.execute(
            "SELECT * FROM rehearsals WHERE area = ? AND block = ? AND archived = 0 "
            + ("AND kind = ? " if kind else "") + "ORDER BY sort_key",
            (area, block, kind) if kind else (area, block)).fetchall()
        return [self._load(r) for r in rows]

    def has_block(self, area: str, block: str) -> bool:
        return bool(self.of_block(area, block))

    def active_block(self) -> tuple[str, str] | None:
        """Открытый блок с репетициями (самый поздний), модуль которого не закрыт."""
        row = self.conn.execute(
            "SELECT b.area, b.block FROM en_blocks b WHERE b.opened_at IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM rehearsals r WHERE r.area = b.area AND r.block = b.block "
            "AND r.archived = 0) "
            "AND NOT EXISTS (SELECT 1 FROM en_modules m WHERE m.area = b.area AND m.closed_at IS NOT NULL) "
            "ORDER BY b.area DESC, CAST(b.block AS INTEGER) DESC LIMIT 1").fetchone()
        return (row[0], row[1]) if row else None

    # монолог

    def monologue_for_practice(self, area: str, block: str) -> Rehearsal | None:
        """План с наименьшей ступенью; при равенстве — реже использованный, затем по порядку."""
        items = self.of_block(area, block, "monologue")
        if not items:
            return None
        return min(items, key=lambda r: (r.stage, r.uses))

    def monologue_for_exit(self, area: str, block: str) -> Rehearsal | None:
        items = [r for r in self.of_block(area, block, "monologue") if r.stage >= STAGES]
        return items[0] if items else None

    def record_monologue(self, code: str, passed: bool, stage: int, now: datetime) -> int:
        """Итог тренировочного монолога. Возвращает ступень после."""
        self.conn.execute("INSERT OR IGNORE INTO rehearsal_state (code) VALUES (?)", (code,))
        st = self.conn.execute("SELECT stage, passes FROM rehearsal_state WHERE code = ?",
                               (code,)).fetchone()
        cur, passes = st["stage"], st["passes"]
        if passed and stage == cur:
            passes += 1
            if passes >= self.passes_to_advance and cur < STAGES:
                cur, passes = cur + 1, 0
        elif not passed and stage == cur:
            passes = 0
        self.conn.execute("UPDATE rehearsal_state SET stage = ?, passes = ?, attempts = attempts + 1, "
                          "uses = uses + 1, last_at = ? WHERE code = ?", (cur, passes, to_iso(now), code))
        return cur

    # сценарии

    def next_scenario(self, area: str, block: str, need_audio: bool = False) -> Rehearsal | None:
        items = self.of_block(area, block, "scenario")
        if need_audio:
            items = [r for r in items if self.lines_voiced(r)]
        if not items:
            return None
        return min(items, key=lambda r: r.uses)          # при равенстве — первый по порядку банка

    def mark_used(self, code: str, now: datetime) -> None:
        self.conn.execute("INSERT OR IGNORE INTO rehearsal_state (code) VALUES (?)", (code,))
        self.conn.execute("UPDATE rehearsal_state SET uses = uses + 1, last_at = ? WHERE code = ?",
                          (to_iso(now), code))

    # аудио реплик

    def line_audio(self, key: str, text: str, speed: str = "normal") -> sqlite3.Row | None:
        row = self.conn.execute("SELECT * FROM line_audio WHERE key = ? AND speed = ?", (key, speed)).fetchone()
        if row is None or row["text_hash"] != short_hash(text):
            return None                       # нет записи или текст изменился после озвучки
        return row

    def lines_voiced(self, r: Rehearsal) -> bool:
        return all(self.line_audio(k, t) is not None for k, t in scenario_lines(r))

    # критерий выхода

    def exit_passes(self, area: str, part: str) -> list[date]:
        return [date.fromisoformat(r[0]) for r in self.conn.execute(
            "SELECT study_date FROM exit_attempts WHERE area = ? AND part = ? AND passed = 1 "
            "ORDER BY study_date", (area, part))]

    def part_done(self, area: str, part: str) -> bool:
        """Две сдачи с промежутком не меньше трёх дней."""
        days = self.exit_passes(area, part)
        if len(days) < EXIT_PASSES:
            return False
        first = days[0]
        return any((d - first).days >= EXIT_GAP_DAYS for d in days[1:])

    def part_allowed_today(self, area: str, part: str, day: date) -> bool:
        if self.part_done(area, part):
            return False
        days = self.exit_passes(area, part)
        return not days or (day - days[-1]).days >= EXIT_GAP_DAYS

    def tried_today(self, area: str, day: date) -> bool:
        return self.conn.execute("SELECT 1 FROM exit_attempts WHERE area = ? AND study_date = ? LIMIT 1",
                                 (area, day.isoformat())).fetchone() is not None

    def module_closed(self, area: str) -> bool:
        return self.conn.execute("SELECT 1 FROM en_modules WHERE area = ? AND closed_at IS NOT NULL",
                                 (area,)).fetchone() is not None

    def exit_parts_due(self, day: date) -> tuple[str, str, list[tuple[str, Rehearsal]]] | None:
        """Какие части критерия выхода предложить сегодня: (модуль, блок, [(часть, репетиция)])."""
        ab = self.active_block()
        if ab is None:
            return None
        area, block = ab
        intro = self.conn.execute("SELECT introduced_at FROM en_blocks WHERE area = ? AND block = ?",
                                  (area, block)).fetchone()
        if intro is None or intro[0] is None or self.tried_today(area, day):
            return None
        parts: list = []
        for part in EXIT_SPEC.get(area, ("monologue", "dialog")):
            if not self.part_allowed_today(area, part, day):
                continue
            src = self.exit_source(area, block, part, day)
            if src is not None:
                parts.append((part, src))
        return (area, block, parts) if parts else None

    def exit_source(self, area: str, block: str, part: str, day: date):
        """Источник части: репетиция, запись банка или тема. None — часть сейчас не провести."""
        if part == "monologue":
            return self.monologue_for_exit(area, block)
        if part == "dialog":
            return self.next_scenario(area, block, need_audio=True)
        if part == "story":
            return {"topic": STORY_TOPICS[day.toordinal() % len(STORY_TOPICS)]}
        kind = {"explain": "statement" if area == "EN4" else "explain", "compare": "compare",
                "listening": "listening"}.get(part) or DYNAMIC.get(part, (None,))[0]
        if part in DYNAMIC and not self._dynamic_ok():
            return None                       # реплики бота только звуком: нужна озвучка во время сессии
        row = self.conn.execute(
            "SELECT r.*, COALESCE(s.uses, 0) AS uses FROM records r LEFT JOIN record_state s ON s.code = r.code "
            "WHERE r.archived = 0 AND r.area = ? AND r.kind = ? ORDER BY uses, r.sort_key", (area, kind)).fetchall()
        if part == "listening":
            row = [r for r in row if r["code"] in LISTENING_RESERVE
                   and self.line_audio(f"{r['code']}:text", json.loads(r["fields_json"]).get("Текст", "")) is not None]
        return row[0] if row else None

    def _dynamic_ok(self) -> bool:
        """Части с репликами модели (диалог-совет, обсуждение, разговор) — только звуком: пока озвучка во время
        сессии не подключена, они не предлагаются (настройка exit_live_tts)."""
        row = self.conn.execute("SELECT value_json FROM settings WHERE key = 'exit_live_tts'").fetchone()
        return bool(row and json.loads(row[0]))

    def record_exit(self, area: str, part: str, code: str, passed: bool, verdict: dict | None,
                    day: date, now: datetime, session_id: int | None) -> bool:
        """Записать попытку части. Возвращает True, если модуль только что закрыт."""
        self.conn.execute(
            "INSERT INTO exit_attempts (area, part, ts, study_date, session_id, rehearsal, passed, verdict_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (area, part, to_iso(now), day.isoformat(), session_id, code, int(passed),
             json.dumps(verdict, ensure_ascii=False) if verdict else None))
        if self.module_closed(area):
            return False
        if all(self.part_done(area, p) for p in EXIT_SPEC.get(area, ("monologue", "dialog"))):
            self.conn.execute("INSERT INTO en_modules (area, closed_at) VALUES (?, ?) "
                              "ON CONFLICT(area) DO UPDATE SET closed_at = excluded.closed_at",
                              (area, to_iso(now)))
            from .modules import Modules
            Modules(self.conn).on_closed(area, now)          # следующий модуль открывается
            return True
        return False

    def exit_summary(self, area: str) -> str:
        """«монолог 1 из 2, диалог 0 из 2» — для сообщения и сводки."""
        def part(p: str) -> str:
            days = self.exit_passes(area, p)
            n = 2 if self.part_done(area, p) else min(len(days), 1)
            return f"{n} из {EXIT_PASSES}"
        return ", ".join(f"{PART_TITLE[p]} {part(p)}" for p in EXIT_SPEC.get(area, ("monologue", "dialog")))

    def next_exit_day(self, area: str, part: str, day: date) -> date | None:
        days = self.exit_passes(area, part)
        if not days or self.part_done(area, part):
            return None
        return days[-1] + timedelta(days=EXIT_GAP_DAYS)
