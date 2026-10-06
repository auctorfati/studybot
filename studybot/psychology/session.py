"""Шаги психологии в сессии: ответ, самооценка, виньетка в два шага, чтение разбора.

Подмешивается в SessionEngine (класс-примесь): пользуется его _elapsed,
_account, _mark и часами. Карточка в режиме самооценки: «Показать ответ»,
затем «Не знал», «С трудом», «Знал»; набранный ответ проверяет модель.
Карточка набором: ответ текстом, затем самооценка, затем модель; расхождение
самооценки с моделью пишется в вердикт для контроля доли набора.
Виньетка: ответ на вопросы, затем отдельно угол наставника — проверяет флагман.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from ..checking.psychology import RATING, log_psy_gap
from ..db import transaction
from ..enums import Judge, Mode, Rating, Track
from ..llm.client import LLMUnavailable
from .track import UNITS, PsyTask

SELF_RATING = {"0": Rating.AGAIN, "1": Rating.HARD, "2": Rating.GOOD}
VERDICT_TITLE = {"passed": "Зачтено", "partial": "Частично", "failed": "Не зачтено"}
EVENT_TEXT = {
    "closed": "Тема {topic} закрыта: проверка сдана. Карточки ядра придут повторением через две недели.",
    "digest": "Тема {topic}: верхний уровень пока не сдан. Сначала разбор темы, затем вопросы.",
    "open": "Тема {topic}: верхний уровень пока не сдан. Дальше — два вопроса по механизмам; разбор "
            "темы открывается кнопкой «Почему так».",
    "study3": "Тема {topic}: механизмы знаешь. Изучаем только верхние уровни — различения и виньетку.",
    "study1": "Тема {topic} пойдёт целиком: с карточек, по уровням.",
}


@dataclass
class PsyFeedback:
    """Итог ответа по психологии; рендер — в bot/render.py."""
    step_id: int
    lines: list[str] = field(default_factory=list)
    ask: str | None = None                # следующий вопрос того же шага (угол наставника, самооценка)
    ask_buttons: str | None = None        # 'self' — кнопки самооценки
    finished_step: bool = True
    review_id: int | None = None
    disputable: bool = False
    deferred: str | None = None
    has_digest: bool = False
    has_source: bool = False
    topic_event: str | None = None
    topic: str = ""


class PsySteps:
    """Примесь к SessionEngine."""

    def _psy_load(self, step_id: int):
        row = self.conn.execute("SELECT * FROM session_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None or row["kind"] != "psy" or row["status"] != "active":
            return None, None, None
        s = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (row["session_id"],)).fetchone()
        return row, json.loads(row["payload_json"]), s

    def _psy_fb(self, row, p: dict) -> PsyFeedback:
        topic = p["topic"]
        has_digest = self.conn.execute("SELECT 1 FROM digests WHERE topic_code = ?", (topic,)).fetchone()
        frag = self.conn.execute("SELECT fragment FROM items WHERE id = ?", (p.get("item_id"),)).fetchone()
        return PsyFeedback(row["id"], has_digest=bool(has_digest), has_source=bool(frag and frag[0]), topic=topic)

    def psy_show(self, step_id: int) -> str | None:
        """«Показать ответ» у карточки самооценки: текст ответа; время до него — в задание."""
        row, p, s = self._psy_load(step_id)
        if row is None:
            return None
        ans = self.conn.execute("SELECT answer FROM items WHERE id = ?", (p["item_id"],)).fetchone()[0]
        p["shown"] = True
        self.conn.execute("UPDATE session_steps SET payload_json = ? WHERE id = ?",
                          (json.dumps(p, ensure_ascii=False), step_id))
        return ans

    async def psy_answer(self, row, p: dict, s, text: str, voice_sec: int | None) -> PsyFeedback:
        task = PsyTask(**{k: v for k, v in p.items() if k in PsyTask.__dataclass_fields__})
        fb = self._psy_fb(row, p)
        text = (text or "").strip()
        if task.kind == "vignette" and "answer" not in p:
            p["answer"] = text
            self.conn.execute("UPDATE session_steps SET payload_json = ? WHERE id = ?",
                              (json.dumps(p, ensure_ascii=False), row["id"]))
            fb.finished_step = False
            fb.ask = ("Угол наставника: что видно в этом человеке как в клиенте наставника, с чем работает "
                      "наставничество и где граница — когда направить к психотерапевту или врачу.")
            return fb
        if task.kind == "card" and task.answer_mode in ("typed", "self") and "self" not in p:
            # набранный ответ карточки: сначала самооценка, потом модель
            p["answer"] = text
            p["answer_mode"] = "typed"
            self.conn.execute("UPDATE session_steps SET payload_json = ? WHERE id = ?",
                              (json.dumps(p, ensure_ascii=False), row["id"]))
            fb.finished_step = False
            fb.ask, fb.ask_buttons = "Как оцениваешь сам?", "self"
            return fb
        mentor = text if task.kind == "vignette" else None
        answer = p.get("answer", text) if task.kind == "vignette" else (p.get("answer") or text)
        return await self._psy_check(row, p, s, task, answer, mentor, voice_sec, fb)

    async def psy_self(self, step_id: int, key: str) -> PsyFeedback | None:
        """Самооценка карточки: в режиме самооценки — итог; после набора — сверка с моделью."""
        row, p, s = self._psy_load(step_id)
        if row is None or key not in SELF_RATING:
            return None
        task = PsyTask(**{k: v for k, v in p.items() if k in PsyTask.__dataclass_fields__})
        rating = SELF_RATING[key]
        fb = self._psy_fb(row, p)
        if p.get("answer_mode") == "typed" and p.get("answer") is not None:
            p["self"] = rating.value
            return await self._psy_check(row, p, s, task, p["answer"], None, None, fb, self_rating=rating)
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        elapsed, raw = self._elapsed(row, "card", now, None)
        with transaction(self.conn):
            out = self.psy_track.record(task, rating, judge=Judge.SELF, mode=Mode(s["mode"]), answer=None,
                                        verdict=None, day=day, now=now, session_id=s["id"], elapsed=elapsed,
                                        raw=raw, fmt="self")
            self._psy_after(task, out, day, fb)
            self._account(s["id"], day, Track.PSY.value, elapsed, 0)
            self._mark(row["id"], "done", now)
        fb.review_id = out["review_id"]
        return fb

    async def _psy_check(self, row, p: dict, s, task: PsyTask, answer: str, mentor: str | None,
                         voice_sec: int | None, fb: PsyFeedback, self_rating: Rating | None = None
                         ) -> PsyFeedback:
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        fmt = "card_typed" if task.kind == "card" else task.kind
        elapsed, raw = self._elapsed(row, fmt, now, voice_sec)
        check = await self.psy_checker.check(task.item_id, answer, mentor_answer=mentor)
        with transaction(self.conn):
            if check.rating is None:
                if self_rating is not None:
                    # модели нет: набранная карточка засчитывается самооценкой
                    out = self.psy_track.record(task, self_rating, judge=Judge.SELF, mode=Mode(s["mode"]),
                                                answer=answer, verdict=None, day=day, now=now,
                                                session_id=s["id"], elapsed=elapsed, raw=raw, fmt="typed")
                    fb.review_id = out["review_id"]
                    fb.lines.append("Проверка моделью недоступна: засчитано по самооценке.")
                    self._psy_after(task, out, day, fb)
                else:
                    fb.deferred = check.unavailable
                    self.conn.execute("UPDATE item_state SET deferred_until = 'next_session' WHERE item_id = ?",
                                      (task.item_id,))
                    self.psy_track.sched.ensure_state(task.item_id)
                self._account(s["id"], day, Track.PSY.value, elapsed, voice_sec or 0)
                self._mark(row["id"], "done" if self_rating is not None else "skipped", now)
                return fb
            v = dict(check.verdict)
            if self_rating is not None:
                v["self_rating"] = self_rating.value
                v["self_mismatch"] = int(abs(self_rating.value - check.rating.value) >= 2
                                         or (self_rating == Rating.GOOD and check.rating == Rating.AGAIN))
            out = self.psy_track.record(task, check.rating, judge=check.judge, mode=Mode(s["mode"]), answer=answer,
                                        verdict=v, day=day, now=now, session_id=s["id"], elapsed=elapsed,
                                        raw=raw, fmt="typed" if task.kind == "card" else task.kind)
            if check.call_ids:
                self.llm.link_review(check.call_ids, out["review_id"])
            log_psy_gap(self.conn, check, task.item_id, out["review_id"], answer, now, day.isoformat())
            fb.review_id, fb.disputable = out["review_id"], check.disputable
            key = self.conn.execute("SELECT answer, source_ref FROM items WHERE id = ?", (task.item_id,)).fetchone()
            fb.lines.append(f"{VERDICT_TITLE[v['verdict']]}.")
            if v.get("comment"):
                fb.lines.append(v["comment"])
            if v.get("missing"):
                fb.lines.append("Не хватило: " + "; ".join(v["missing"]) + ".")
            if v.get("outside_source"):
                fb.lines.append("Вне источника (не засчитано и не штраф): " + "; ".join(v["outside_source"]) + ".")
            if check.mentor_divergence:
                fb.lines.append(f"Угол наставника расходится с ключом (срыва нет): {check.mentor_divergence}")
            if self_rating is not None and v["self_mismatch"]:
                fb.lines.append("Самооценка разошлась с проверкой.")
            fb.lines.append(f"Ключ: {key['answer']}" + (f" ({key['source_ref']})" if key["source_ref"] else ""))
            self._psy_after(task, out, day, fb)
            self._account(s["id"], day, Track.PSY.value, elapsed, voice_sec or 0)
            self._mark(row["id"], "done", now)
        return fb

    def _psy_after(self, task: PsyTask, out: dict, day: date, fb: PsyFeedback) -> None:
        if task.purpose == "new":
            self.queue.spend_new(Track.PSY, day, UNITS[task.kind])
        if task.kind == "card" and task.answer_mode == "self" and fb.lines == []:
            pass
        if out.get("topic_event"):
            fb.topic_event = out["topic_event"]
            fb.lines.append(EVENT_TEXT[out["topic_event"]].format(topic=self._topic_label(task.topic)))

    def _topic_label(self, code: str) -> str:
        row = self.conn.execute("SELECT title FROM topics WHERE code = ?", (code,)).fetchone()
        return f"{code} «{row[0]}»" if row else code

    # чтение разбора в сессии

    def digest_part(self, topic: str, num: int):
        return self.conn.execute("SELECT * FROM digest_parts WHERE topic_code = ? AND num = ?",
                                 (topic, num)).fetchone()

    def mark_read(self, topic: str, num: int, parts: int) -> None:
        now = self.clock.now().isoformat()
        self.conn.execute(
            "INSERT INTO digest_reads (topic_code, part, status, started_at, updated_at) VALUES (?, ?, 'reading', ?, ?) "
            "ON CONFLICT(topic_code) DO UPDATE SET part = MAX(part, excluded.part), updated_at = excluded.updated_at, "
            "status = CASE WHEN status = 'read' THEN 'read' ELSE 'reading' END", (topic, num, now, now))
        if num >= parts:
            self.conn.execute("UPDATE digest_reads SET status = 'read', read_at = COALESCE(read_at, ?) "
                              "WHERE topic_code = ?", (now, topic))

    def digest_advance(self, step_id: int) -> tuple[str | None, dict | None]:
        """«Дальше»: следующая часть или конец разбора. Возвращает (событие, payload шага)."""
        row = self.conn.execute("SELECT * FROM session_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None or row["kind"] != "digest" or row["status"] != "active":
            return None, None
        p = json.loads(row["payload_json"])
        now = self.clock.now()
        with transaction(self.conn):
            if p["part"] >= p["parts"]:
                self.mark_read(p["topic"], p["parts"], p["parts"])
                if p.get("reason") in ("probe", "intro"):
                    self.psy_track.digest_done(p["topic"], now)
                self._mark(step_id, "done", now)
                return "done", p
            p["part"] += 1
            self.mark_read(p["topic"], p["part"], p["parts"])
            self.conn.execute("UPDATE session_steps SET payload_json = ?, sent_at = ? WHERE id = ?",
                              (json.dumps(p, ensure_ascii=False), now.isoformat(), step_id))
        return "part", p

    def digest_defer(self, step_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM session_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None or row["kind"] != "digest" or row["status"] != "active":
            return None
        p = json.loads(row["payload_json"])
        now = self.clock.now()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO digest_reads (topic_code, part, status, updated_at) VALUES (?, ?, 'deferred', ?) "
                "ON CONFLICT(topic_code) DO UPDATE SET status = CASE WHEN status = 'read' THEN 'read' "
                "ELSE 'deferred' END, updated_at = excluded.updated_at", (p["topic"], p["part"], now.isoformat()))
            self._mark(step_id, "skipped", now)
        return p
