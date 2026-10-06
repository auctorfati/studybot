"""Сборка блока психологии: сначала повторения по сроку, затем разбор
и новое текущей темы по уровням, на хвост — различения и виньетки, подошедшие
по сроку. Виньетка не ставится в блок короче 15 минут. Воскресная длинная
сессия при включённой психологии — кейс: виньетки по сроку и новые, затем
различения и обычный блок.
"""

from __future__ import annotations

import json
import sqlite3

from ..enums import Track
from ..review_queue import ReviewQueue
from ..sessions.estimates import Estimates
from ..sessions.planner import PlanContext, Step
from .track import UNITS, PsyTask, PsyTrack

VIGNETTE_MIN_SEC = 900
DIGEST_PART_SEC = 120


def est_key(t: PsyTask) -> str:
    if t.kind == "card":
        return "card_typed" if t.answer_mode == "typed" else "card"
    return t.kind


class PsyPlanner:
    def __init__(self, conn: sqlite3.Connection, track: PsyTrack, queue: ReviewQueue,
                 estimates: Estimates) -> None:
        self.conn = conn
        self.track = track
        self.queue = queue
        self.est = estimates

    def _est(self, t: PsyTask) -> int:
        e = self.est.get(est_key(t))
        return e + (self.est.get("vignette_mentor") if t.kind == "vignette" else 0)

    def _step(self, t: PsyTask, slot: str) -> Step:
        return Step("psy", slot, self._est(t), t.dump(), item_id=t.item_id, track=Track.PSY.value)

    def digest_step(self, topic: str, reason: str) -> Step | None:
        row = self.conn.execute("SELECT title, parts FROM digests WHERE topic_code = ?", (topic,)).fetchone()
        if row is None:
            return None
        read = self.conn.execute("SELECT part, status FROM digest_reads WHERE topic_code = ?", (topic,)).fetchone()
        start = read["part"] if read and read["status"] != "read" else 0
        left = max(1, row["parts"] - start)
        return Step("digest", "digest", min(left, 6) * self.est.get("digest_part"),
                    {"topic": topic, "title": row["title"], "parts": row["parts"], "part": start,
                     "reason": reason}, track=Track.PSY.value)

    def build(self, ctx: PlanContext, sec: int, long: bool = False,
              skip_digests: set[str] = frozenset(), session_sec: int | None = None) -> list[Step]:
        """session_sec — длина всей сессии: виньетка не ставится в сессию короче 15 минут."""
        steps: list[Step] = []
        allow_vig = (session_sec if session_sec is not None else sec) >= VIGNETTE_MIN_SEC
        used = 0
        due = self.track.due(ctx.day, 200, exclude=ctx.planned_items, now=ctx.now)

        def take(t: PsyTask, slot: str) -> bool:
            nonlocal used
            st = self._step(t, slot)
            if used + st.est_sec > sec and steps:
                return False
            steps.append(st)
            used += st.est_sec
            ctx.planned_items.add(t.item_id)
            return True

        if long:
            # кейс: виньетки по сроку, затем срез или новая виньетка текущих тем
            for t in [t for t in due if t.kind == "vignette"]:
                if not take(t, "case"):
                    break
        # 1. повторения по сроку: карточки и открытые вопросы
        for t in due:
            if t.item_id in ctx.planned_items or t.kind in ("distinction", "vignette"):
                continue
            if used >= sec * (0.4 if long else 0.6) and steps:
                break                             # место новому и хвосту
            take(t, "review")
        # 2. разбор и новое текущей темы
        budget = max(0, self.queue.new_budget(Track.PSY, ctx.day) - ctx.psy_new_pending - ctx.psy_new_planned)
        tasks, digests = self.track.next_new(budget, ctx.now, exclude=ctx.planned_items,
                                             allow_vignette=allow_vig, skip_digests=skip_digests)
        for topic in digests:
            if topic in skip_digests:
                continue
            pr = json.loads(self.track.state(topic)["probe_json"])
            ds = self.digest_step(topic, "intro" if pr.get("after_digest") == "top" else "probe")
            if ds is not None and used < sec:
                steps.append(ds)
                used += ds.est_sec
            break                                 # один разбор за раз: вопросы темы — после него
        else:
            for t in tasks:
                if not take(t, "new"):
                    break
                ctx.psy_new_planned += UNITS[t.kind]
        # 3. хвост: различения и виньетки по сроку
        for t in due:
            if t.item_id in ctx.planned_items or t.kind not in ("distinction", "vignette"):
                continue
            if t.kind == "vignette" and not allow_vig:
                continue
            if not take(t, "tail"):
                break
        # 4. остаток — другие повторения по сроку
        for t in due:
            if t.item_id in ctx.planned_items or (t.kind == "vignette" and not allow_vig):
                continue
            if not take(t, "review"):
                break
        return steps

    def units(self, steps: list[Step]) -> int:
        return sum(UNITS[s.payload["kind"]] for s in steps if s.kind == "psy" and s.payload["purpose"] == "new")
