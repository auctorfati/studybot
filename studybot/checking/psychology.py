"""Проверка ответа по психологии.

Модели передаются вопрос, ключ, фрагмент источника и ответ. Оценка только
по ключу и фрагменту; элемент вне источника не засчитывается и не штрафуется.
Если источника нет в папке источников (статус n/a), фрагмент пустой и оценка
идёт только по ключу.
Открытые вопросы, различения и набранные карточки — дешёвая модель, виньетки —
флагман. Угол наставника сверяется с ключом без штрафа: расхождение
показывается, срыва не ставит.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from ..clock import to_iso
from ..enums import Judge, Rating
from ..llm.client import CHEAP, FLAGSHIP, LLMUnavailable
from ..llm.runner import ModelRunner

RATING = {"passed": Rating.GOOD, "partial": Rating.HARD, "failed": Rating.AGAIN}


@dataclass
class PsyCheck:
    rating: Rating | None            # None — отложить без оценки
    judge: Judge
    verdict: dict | None = None
    unavailable: str | None = None
    disputable: bool = True
    call_ids: list = field(default_factory=list)

    @property
    def mentor_divergence(self) -> str | None:
        m = (self.verdict or {}).get("mentor")
        return None if not m or m["matches"] else (m["divergence"] or "есть расхождение с ключом")


class PsyChecker:
    def __init__(self, conn: sqlite3.Connection, runner: ModelRunner) -> None:
        self.conn = conn
        self.runner = runner

    async def check(self, item_id: int, answer: str, mentor_answer: str | None = None) -> PsyCheck:
        it = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        vignette = it["kind"] == "vignette"
        judge = Judge.FLAGSHIP if vignette else Judge.CHEAP
        if not (answer or "").strip():
            return PsyCheck(Rating.AGAIN, Judge.CODE, {"verdict": "failed", "comment": "Пустой ответ."},
                            disputable=False)
        if it["fragment_status"] == "missing":
            # такие позиции не показываются; сюда попасть не должны
            return PsyCheck(None, judge, unavailable="нет фрагмента источника")
        if not self.runner.available():
            return PsyCheck(None, judge, unavailable=self.runner.llm.available()[1])
        extra = json.loads(it["extra_json"])
        payload = {
            "item_type": it["kind"],
            "question": it["prompt"],
            "key": it["answer"],
            "source_reference": it["source_ref"],
            "source_fragment": it["fragment"] or "",
            "answer": answer.strip(),
        }
        task, tier = "check_psy", CHEAP
        if vignette:
            task, tier = "check_vignette", FLAGSHIP
            payload["vignette_questions"] = extra.get("questions", [])
            payload["mentor_key"] = extra.get("mentor", "")
            payload["mentor_answer"] = (mentor_answer or "").strip()
        try:
            result = await self.runner.run(task, tier, payload)
        except LLMUnavailable as exc:
            return PsyCheck(None, judge, unavailable=str(exc))
        v = result.data
        return PsyCheck(RATING[v["verdict"]], judge, v, call_ids=result.call_ids)


def log_psy_gap(conn: sqlite3.Connection, check: PsyCheck, item_id: int, review_id: int,
                answer: str, now: datetime, day: str) -> None:
    v = check.verdict or {}
    if check.rating in (None, Rating.GOOD) or not v.get("tag"):
        return
    conn.execute(
        "INSERT INTO errors (track, item_id, review_id, ts, study_date, tag, detail, answer, explanation) "
        "VALUES ('psy', ?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, review_id, to_iso(now), day, v["tag"], v.get("confusion"), answer, v.get("comment", "")))
