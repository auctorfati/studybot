"""Движок сессии: запуск, выдача шагов, приём ответа, учёт времени.

Обработчики Telegram (слой К7) только показывают шаги и передают ответы.
Всё состояние — в базе: план сессии в session_steps, прогресс в item_state,
так что перезапуск бота сессию не теряет.

Активное время задания — от выдачи до ответа, но не больше двойной оценки
формата и не больше task_cap_sec. Голосовое учитывается по фактической длине.
Десять минут без ответа — пауза; следующий ответ продолжает сессию.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from ..checking.english import EnglishChecker, log_en_error
from ..checking.talk import Dialog, EnglishTalk
from ..clock import Clock, StudyCalendar, from_iso, to_iso
from ..db import transaction
from ..english.blocks import Blocks, ControlResult
from ..english.rehearsals import STAGE_TITLE, Rehearsals, line_key
from ..english.track import FIND_ERROR, INTRO, RU_EN, EnglishTrack, Task
from ..enums import Judge, Mode, Track
from ..llm.client import CHEAP, LLMUnavailable
from ..review_queue import ReviewQueue
from ..settings import Settings
from .day import DayBook, DayStatus
from .estimates import Estimates
from .planner import EnglishPlanner, PlanContext, Step
from ..psychology.session import PsyFeedback, PsySteps
from ..english.academic import Texts
from ..english.academic_session import ExamSteps, TermFeedback, TermSteps, TextFeedback, TextSteps
from ..english.exams import Exams
from ..english.speech import FORMATS, Speech
from ..english.speech_session import ExitSpeechSteps, ListenSteps, SpeechFeedback, SpeechSteps

ORDERED = {"5": 300, "15": 900, "30": 1800}
CP_EVENING_MIN_SEC = 1500           # контрольная точка в вечернем блоке — если он не короче 25 минут
NEXT_SESSION = "next_session"      # отложено до следующей сессии (строка больше любой даты ISO)
TOPUP_MIN_SEC = 120                # план кончился, а до заказанной длины больше двух минут — добор
MAX_TALKS = 3                      # разговоров за сессию: время сверх материала уходит в речь


class SessionError(RuntimeError):
    pass


@dataclass
class Feedback:
    correct: bool | None = None
    lines: list[str] = field(default_factory=list)      # объяснение, исправление, заметки
    review_id: int | None = None
    disputable: bool = False
    reply: str | None = None                              # реплика бота в диалоге
    finished_step: bool = True
    deferred: str | None = None                           # почему задание отложено
    notes: list[dict] = field(default_factory=list)       # заметки после знакомства
    control_result: dict | None = None
    step_id: int | None = None
    explainable: bool = False                             # под вердиктом — «Почему так?»
    audio_line: str | None = None                         # вопрос бота только звуком (критерий выхода)
    exit_result: dict | None = None                       # итог части критерия выхода


@dataclass
class SessionInfo:
    id: int
    kind: str
    ordered_sec: int
    status: DayStatus


class SessionEngine(PsySteps, TermSteps, TextSteps, ExamSteps, SpeechSteps, ExitSpeechSteps, ListenSteps):
    def __init__(self, conn: sqlite3.Connection, settings: Settings, clock: Clock,
                 calendar: StudyCalendar, track: EnglishTrack, queue: ReviewQueue,
                 checker: EnglishChecker, talk: EnglishTalk, blocks: Blocks,
                 estimates: Estimates, days: DayBook, planner: EnglishPlanner,
                 model_ok=lambda: True, rehearsals: Rehearsals | None = None,
                 psy_track=None, psy_planner=None, psy_checker=None, llm=None,
                 academic=None, runner=None) -> None:
        self.conn, self.settings, self.clock, self.calendar = conn, settings, clock, calendar
        self.track, self.queue, self.checker, self.talk = track, queue, checker, talk
        self.blocks, self.est, self.days, self.planner = blocks, estimates, days, planner
        self.model_ok = model_ok
        self.reh = rehearsals or planner.reh
        self.psy_track, self.psy_planner, self.psy_checker, self.llm = psy_track, psy_planner, psy_checker, llm
        self.academic, self.runner = academic, runner
        self.texts = Texts(conn)
        self.exams = Exams(conn)
        self.speech = Speech(conn, settings)

    # сессия

    def today(self) -> date:
        return self.calendar.study_date(self.clock.now())

    def active(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sessions WHERE status IN ('active', 'paused') "
                                 "ORDER BY id DESC LIMIT 1").fetchone()

    def start(self, kind: str) -> SessionInfo:
        now, day = self.clock.now(), self.today()
        mode = Mode(self.settings.get("mode"))
        with transaction(self.conn):
            old = self.active()
            if old is not None:
                self._close(old["id"], now)
            first_today = self.conn.execute("SELECT 1 FROM sessions WHERE study_date = ? LIMIT 1",
                                            (day.isoformat(),)).fetchone() is None
            if first_today:
                self.queue.rebalance(Track.EN, day, now)
                if self.settings.psy_active():
                    self.queue.rebalance(Track.PSY, day, now)
            self.conn.execute("UPDATE item_state SET deferred_until = NULL WHERE deferred_until = ?",
                              (NEXT_SESSION,))
            ordered = self._ordered(kind, day)
            sid = self.conn.execute(
                "INSERT INTO sessions (study_date, kind, ordered_sec, started_at, last_activity_at, mode) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (day.isoformat(), kind, ordered, to_iso(now), to_iso(now), mode.value)).lastrowid
            ctx = PlanContext(day, now, mode, self.model_ok())
            steps = self.compose(ctx, kind, ordered)
            for seq, st in enumerate(steps, start=1):
                self.conn.execute(
                    "INSERT INTO session_steps (session_id, seq, track, kind, slot, item_id, est_sec, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (sid, seq, st.track, st.kind, st.slot, st.item_id, st.est_sec,
                     json.dumps(st.payload, ensure_ascii=False)))
            if mode == Mode.TEXT:
                self.days.set_speech_held(day, self.planner.speech_pending(day, now) > 0)
        return SessionInfo(sid, kind, ordered, self.days.status(day))

    # сборка сессии из блоков

    def _psy_on(self) -> bool:
        return self.psy_planner is not None and self.settings.psy_active()

    def _en_block(self, ctx: PlanContext, kind: str, sec: int, with_exit: bool = False) -> list[Step]:
        steps = self.planner.build(ctx, kind, sec)
        if with_exit:
            ex = self.planner.exit_steps(ctx)
            if ex:
                steps = ex + [st for st in steps if st.slot != "talk"]
        return steps

    def _psy_block(self, ctx: PlanContext, sec: int, long: bool = False, session_sec: int | None = None
                   ) -> list[Step]:
        return self.psy_planner.build(ctx, sec, long=long, skip_digests=self._deferred_digests(ctx.day),
                                      session_sec=session_sec)

    def _deferred_digests(self, day: date) -> set[str]:
        """Разборы, отложенные в сессиях сегодня, в этой сессии снова не встают в начало
        (предлагаются в начале следующего блока психологии — следующей сессии)."""
        return {json.loads(r[0])["topic"] for r in self.conn.execute(
            "SELECT st.payload_json FROM session_steps st JOIN sessions s ON s.id = st.session_id "
            "WHERE st.kind = 'digest' AND st.status = 'skipped' AND s.status = 'active'")}

    def _academic_steps(self, ctx: PlanContext, block_sec: int, exclude: set[int]) -> list[Step]:
        """Модуль 5 внутри английского блока: доля времени, сначала повторения, затем новое."""
        if self.academic is None:
            return []
        want = self.academic.deficit_sec(ctx.day, block_sec)
        if want < 60:
            return []
        steps, used = [], 0
        tasks = self.academic.due(ctx.day, 40, exclude) + self.academic.new(ctx.day, 10, exclude)
        for t in tasks:
            est = self.est.get("term_" + t.kind)
            if used + est > want and steps:
                break
            steps.append(Step("term", "academic", est, t.dump()))
            exclude.add(t.term_id)
            used += est
        # фразы пересказа и беседы блоков 7–8 — по лестнице, знакомство здесь, повторения — в общей очереди
        per = self.est.get("intro") + self.est.get("transform")
        for item_id in self.academic.new_phrases(ctx.day, max(0, (want - used) // per), ctx.planned_items):
            for t in self.track.intro_tasks(item_id, ctx.mode):
                st = self.planner._task_step(t, "academic")
                steps.append(st)
                used += st.est_sec
            ctx.planned_items.add(item_id)
        return steps

    def _text_step(self, ctx: PlanContext, sec: int) -> list[Step]:
        """Текст Ч — в 30-минутной и вечерней сессии, один в день, когда доля модуля 5 за неделю
        недобрана хотя бы на половину текста."""
        if self.academic is None or self.academic.week_deficit_sec(ctx.day, sec) < max(300, self.est.get("text") // 2):
            return []
        if self.conn.execute("SELECT 1 FROM session_steps st JOIN sessions s ON s.id = st.session_id "
                             "WHERE s.study_date = ? AND st.kind IN ('text', 'exam') LIMIT 1",
                             (ctx.day.isoformat(),)).fetchone():
            return []
        ex = self.exams.next_part(ctx.day, ctx.now)
        if ex is not None:
            run_id, code, part = ex
            phase = "read" if part in ("2", "4") else ("talk" if part == "3" else "task")
            payload = {"run_id": run_id, "code": code, "part": part, "phase": phase}
            if part == "3":
                q = (self.exams.bank() or ["Could you introduce yourself?"])[0]
                payload["history"] = [{"role": "assistant", "content": q}]
            from ..english.academic_session import EXAM_EST
            return [Step("exam", "academic", EXAM_EST[part], payload)]
        nxt = self.texts.next(ctx.day)
        if nxt is None:
            return []
        return [Step("text", "academic", self.est.get("text"), {"code": nxt[0], "stage": nxt[1], "phase": "read"})]

    def _speech_steps(self, ctx: PlanContext, block_sec: int, long_ok: bool) -> list[Step]:
        """Свободная речь модулей 2–6: доля английского блока по модулю (карта пути, раздел 2)."""
        if not ctx.model_ok or block_sec < 600:
            return []
        want = int(self.speech.share() * block_sec)
        steps, used = [], 0
        while used < want:
            fmt = self.speech.next_format(ctx.day, long_ok)
            if fmt is None or any(st.payload["format"] == fmt for st in steps):
                break
            est = FORMATS[fmt][2]
            steps.append(Step("speech", "speech", est, self.speech.payload(fmt)))
            used += est
        return steps

    def _en_with_academic(self, ctx: PlanContext, kind: str, sec: int, with_exit: bool = False) -> list[Step]:
        acad = self._academic_steps(ctx, sec, ctx.__dict__.setdefault("terms_planned", set()))
        speech = self._speech_steps(ctx, sec, long_ok=ctx.__dict__.get("long_ok", False))
        main = self._en_block(ctx, kind, sec, with_exit) if kind != "plain" else []
        if kind == "5":                       # пятиминутка — сначала основной путь, термины модуля 5 — после
            return main + acad + speech
        return acad + speech + main

    def compose(self, ctx: PlanContext, kind: str, sec: int) -> list[Step]:
        """Предмет блока выбирает недобор; 30 минут — два блока по 15 с переключением;
        вечерний блок — куски по 15; длинная — кейс по психологии. В английских блоках —
        доля модуля 5 (банк модуля 5, раздел 1)."""
        if not self._psy_on():
            if kind == "long":
                return self.planner.build(ctx, kind, sec)
            text = self._text_step(ctx, sec) if kind in ("30", "evening") else []
            acad = [] if text else self._academic_steps(ctx, sec, set())
            speech = self._speech_steps(ctx, sec, long_ok=kind in ("30", "evening"))
            main = self.planner.build(ctx, kind, sec)
            steps = (main + acad + speech) if kind == "5" else (text + acad + speech + main)
            # критерий выхода — в начале сессии (на свежую голову), остальное — за ним
            return [st for st in steps if st.slot == "exit"] + [st for st in steps if st.slot != "exit"]
        day = ctx.day
        ctx.long_ok = kind in ("30", "evening", "long")
        any_day = self.blocks.any_day()
        if kind == "long":
            # критерий выхода английского — в начале длинной сессии, затем кейс по психологии
            ex = self.planner.exit_steps(ctx) if any_day else []
            steps = self._psy_block(ctx, max(sec - sum(st.est_sec for st in ex), 900), long=True)
            return ex + steps if (ex or steps) else self.planner.build(ctx, kind, sec)
        cp_steps: list[Step] = []
        if kind == "30" or (kind == "evening" and any_day and sec >= CP_EVENING_MIN_SEC):
            cp = self.blocks.due_for_control(day)
            if cp and ctx.model_ok and ctx.mode == Mode.VOICE:
                cp_steps = self.planner.control_steps(ctx, *cp)
                if kind == "30":
                    return cp_steps
                sec = max(0, sec - sum(st.est_sec for st in cp_steps))
                if sec < 300:
                    return cp_steps
        if kind == "5":
            first = self.days.choose_track(day, short=True)
            blocks = [(first, sec)]
        elif kind in ("15",):
            blocks = [(self.days.choose_track(day), sec)]
        else:
            st = self.days.status(day)
            shares = self.settings.track_shares()
            norm = st.norm_min * 60
            deficit = {Track.EN: shares["en"] * norm - st.en_sec, Track.PSY: shares["psy"] * norm - st.psy_sec}
            first = self.days.choose_track(day)
            blocks, left = [], sec
            cur = first
            while left > 0:
                chunk = min(900, left)
                blocks.append((cur, chunk))
                deficit[cur] -= chunk
                left -= chunk
                cur = Track.PSY if deficit[Track.PSY] > deficit[Track.EN] else Track.EN
                if kind == "30" and len(blocks) == 1:
                    cur = Track.EN if first == Track.PSY else Track.PSY       # переключение
        steps: list[Step] = []
        debt = 0                                            # текст Ч длиннее блока: лишнее снимаем со следующих блоков
        for track, chunk in blocks:
            if debt >= chunk:
                debt -= chunk
                continue
            chunk, debt = chunk - debt, 0
            text = (self._text_step(ctx, chunk) if track == Track.EN and kind in ("30", "evening")
                    and sum(c for _, c in blocks) >= self.est.get("text")
                    and not any(st.kind == "text" for st in steps) else [])
            if text:
                debt = max(0, sum(st.est_sec for st in text) - chunk)
            with_exit = kind == "30" or (kind == "evening" and any_day and not cp_steps)
            got = (self._psy_block(ctx, chunk, session_sec=sec) if track == Track.PSY else
                   text or self._en_with_academic(ctx, "5" if chunk < 600 else "15", chunk, with_exit=with_exit))
            if not got:                                     # предмету нечего дать — другой предмет
                got = (self._en_block(ctx, "5" if chunk < 600 else "15", chunk) if track == Track.PSY
                       else self._psy_block(ctx, chunk, session_sec=sec))
            steps += got
        steps = cp_steps + steps
        return [st for st in steps if st.slot == "exit"] + [st for st in steps if st.slot != "exit"]

    def _ordered(self, kind: str, day: date) -> int:
        if kind in ORDERED:
            return ORDERED[kind]
        if kind == "long":
            return self.settings.get("sunday_long_min") * 60
        if kind == "evening":
            return max(15, self.days.status(day).left_min) * 60
        raise SessionError(f"неизвестная длина сессии: {kind}")

    def end(self) -> DayStatus:
        s = self.active()
        if s is not None:
            with transaction(self.conn):
                self._close(s["id"], self.clock.now())
        return self.days.status(self.today())

    def _close(self, sid: int, now: datetime) -> None:
        self._settle_reading(sid, now)
        self.conn.execute("UPDATE session_steps SET status = 'skipped' WHERE session_id = ? "
                          "AND status IN ('planned', 'active')", (sid,))
        self.conn.execute("UPDATE sessions SET status = 'done', ended_at = ? WHERE id = ?", (to_iso(now), sid))

    # чтение: объяснение «Почему так?», позже — части разборов

    def start_reading(self, cap_sec: int, track: str = Track.EN.value) -> bool:
        """Открыто чтение: время до следующего действия идёт в сессию, не больше cap_sec.
        Без активной сессии не учитывается. Возвращает, учитывается ли."""
        s = self.active()
        if s is None:
            return False
        now = self.clock.now()
        with transaction(self.conn):
            self._settle_reading(s["id"], now)
            self.conn.execute("UPDATE sessions SET reading_json = ?, last_activity_at = ?, status = 'active' "
                              "WHERE id = ?", (json.dumps({"at": to_iso(now), "cap": int(cap_sec),
                                                          "track": track}), to_iso(now), s["id"]))
        return True

    def _settle_reading(self, sid: int, now: datetime) -> int:
        """Закрыть открытое чтение: учесть время и сдвинуть отправку текущего шага, чтобы
        это же время не легло ещё и в задание."""
        row = self.conn.execute("SELECT study_date, reading_json FROM sessions WHERE id = ?", (sid,)).fetchone()
        if row is None or not row["reading_json"]:
            return 0
        r = json.loads(row["reading_json"])
        at = from_iso(r["at"])
        sec = max(0, min(int((now - at).total_seconds()), int(r["cap"])))
        self.conn.execute("UPDATE sessions SET reading_json = NULL WHERE id = ?", (sid,))
        if sec:
            self._account(sid, date.fromisoformat(row["study_date"]), r.get("track", Track.EN.value), sec, 0)
            cur = self.conn.execute("SELECT id, sent_at FROM session_steps WHERE session_id = ? AND "
                                    "status = 'active' ORDER BY seq LIMIT 1", (sid,)).fetchone()
            if cur is not None and cur["sent_at"] and from_iso(cur["sent_at"]) <= at:
                shifted = min(from_iso(cur["sent_at"]) + timedelta(seconds=sec), now)
                self.conn.execute("UPDATE session_steps SET sent_at = ? WHERE id = ?", (to_iso(shifted), cur["id"]))
        return sec

    def settle_reading(self) -> int:
        s = self.active()
        if s is None:
            return 0
        with transaction(self.conn):
            return self._settle_reading(s["id"], self.clock.now())

    def _psy_new_pending(self, session_id: int) -> int:
        """Единицы нового по психологии в шагах этой сессии, которые поставлены, но ещё не отвечены."""
        from ..psychology.track import UNITS
        total = 0
        for (pj,) in self.conn.execute(
                "SELECT payload_json FROM session_steps WHERE session_id = ? AND track = 'psy' AND kind = 'psy' "
                "AND status IN ('planned', 'active')", (session_id,)):
            pl = json.loads(pj)
            if pl.get("purpose") == "new":
                total += UNITS.get(pl.get("kind"), 0)
        return total

    def _session_sec(self, s: sqlite3.Row) -> int:
        return s["active_sec_en"] + s["active_sec_psy"]

    # шаги

    def current(self) -> Step | None:
        """Выданный и ещё не отвеченный шаг активной сессии; ничего не выдаёт заново."""
        s = self.active()
        if s is None:
            return None
        row = self.conn.execute("SELECT * FROM session_steps WHERE session_id = ? AND status = 'active' "
                                "ORDER BY seq LIMIT 1", (s["id"],)).fetchone()
        return self._load(row) if row else None

    def _load(self, row: sqlite3.Row) -> Step:
        return Step(row["kind"], row["slot"], row["est_sec"], json.loads(row["payload_json"]),
                    row["item_id"], row["track"], row["id"], row["status"])

    async def next(self) -> Step | None:
        """Следующий шаг. None — сессия окончена (время набрано или задания кончились)."""
        s = self.active()
        if s is None:
            return None
        cur = self.conn.execute("SELECT * FROM session_steps WHERE session_id = ? AND status = 'active' "
                                "ORDER BY seq LIMIT 1", (s["id"],)).fetchone()
        if cur is not None:
            return self._load(cur)
        if s["reading_json"]:
            self.settle_reading()
            s = self.active()
        if self._session_sec(s) >= s["ordered_sec"]:
            with transaction(self.conn):
                self._close(s["id"], self.clock.now())
            return None
        row = self.conn.execute("SELECT * FROM session_steps WHERE session_id = ? AND status = 'planned' "
                                "ORDER BY seq LIMIT 1", (s["id"],)).fetchone()
        if row is None and s["ordered_sec"] - self._session_sec(s) >= TOPUP_MIN_SEC:
            row = self._topup(s)
        if row is None:
            with transaction(self.conn):
                self._close(s["id"], self.clock.now())
            return None
        step = self._load(row)
        now = self.clock.now()
        if step.kind == "speech":
            self.speech_open(step)
        elif step.kind == "digest":
            if step.payload["part"] < 1:
                step.payload["part"] = 1
            self.mark_read(step.payload["topic"], step.payload["part"], step.payload["parts"])
        elif step.kind in ("dialog", "exit_dialog") and step.payload.get("scenario"):
            sc = self.reh.get(step.payload["scenario"])
            if sc is None:
                with transaction(self.conn):
                    self._mark(step.id, "skipped", now)
                return await self.next()
            opening = sc.data["opening"]
            step.payload.update(history=[{"role": "assistant", "content": opening}], opening=opening)
            if step.kind == "exit_dialog":
                step.payload["audio_line"] = line_key(sc.code, 0)
            self.reh.mark_used(sc.code, now)
        elif step.kind == "dialog":
            try:
                d = Dialog(step.payload["topic"], step.payload["vocabulary"], step.payload["turns"])
                opening = await self.talk.open(d)
            except LLMUnavailable as exc:
                self._mark(step.id, "deferred", now)
                self._event(str(exc))
                return await self.next()
            step.payload.update(history=d.history, opening=opening)
        with transaction(self.conn):
            self.conn.execute("UPDATE session_steps SET status = 'active', sent_at = ?, payload_json = ? "
                              "WHERE id = ?", (to_iso(now), json.dumps(step.payload, ensure_ascii=False), step.id))
            self.conn.execute("UPDATE sessions SET status = 'active', last_activity_at = ? WHERE id = ?",
                              (to_iso(now), s["id"]))
        step.status = "active"
        return step

    def _topup(self, s: sqlite3.Row) -> sqlite3.Row | None:
        """Добор, когда план кончился раньше времени: сначала повторения по сроку,
        затем разговор (не больше трёх за сессию). Новое сверх плана не вводится."""
        now = self.clock.now()
        day = date.fromisoformat(s["study_date"])
        used = {r[0] for r in self.conn.execute(
            "SELECT item_id FROM session_steps WHERE session_id = ? AND item_id IS NOT NULL", (s["id"],))}
        talks = self.conn.execute("SELECT count(*) FROM session_steps WHERE session_id = ? AND slot = 'talk'",
                                  (s["id"],)).fetchone()[0]
        ctx = PlanContext(day, now, Mode(s["mode"]), self.model_ok(), planned_items=used,
                          psy_new_pending=self._psy_new_pending(s["id"]))
        left = s["ordered_sec"] - self._session_sec(s)
        steps = []
        last = self.conn.execute("SELECT track FROM session_steps WHERE session_id = ? AND status = 'done' "
                                 "ORDER BY seq DESC LIMIT 1", (s["id"],)).fetchone()
        five = s["kind"] == "5"                  # пятиминутка — только английский, без разговора (0.11.3)
        if not five and self._psy_on() and (self.days.choose_track(day) == Track.PSY or (last and last[0] == Track.PSY.value)):
            # тема психологии продолжается в той же сессии: разбор после несданного среза,
            # вопросы после разбора — в пределах заказанного времени и лимита нового
            steps = self.psy_planner.build(ctx, max(left, 300), skip_digests=self._deferred_digests(day),
                                           session_sec=s["ordered_sec"])
        if not steps:
            steps, _ = self.planner.review_slot(ctx, left, stages=(2, 3), slot="topup")
        if not steps and talks < MAX_TALKS and not five:
            steps, _ = self.planner.talk_slot(ctx, max(left, self.est.get("dialog")))
        if not steps:
            return None
        seq = self.conn.execute("SELECT COALESCE(max(seq), 0) FROM session_steps WHERE session_id = ?",
                                (s["id"],)).fetchone()[0]
        with transaction(self.conn):
            for k, st in enumerate(steps, start=seq + 1):
                self.conn.execute(
                    "INSERT INTO session_steps (session_id, seq, track, kind, slot, item_id, est_sec, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (s["id"], k, st.track, st.kind, st.slot, st.item_id, st.est_sec,
                     json.dumps(st.payload, ensure_ascii=False)))
        return self.conn.execute("SELECT * FROM session_steps WHERE session_id = ? AND seq = ?",
                                 (s["id"], seq + 1)).fetchone()

    def _mark(self, step_id: int, status: str, now: datetime) -> None:
        self.conn.execute("UPDATE session_steps SET status = ?, done_at = ? WHERE id = ?",
                          (status, to_iso(now), step_id))

    def _event(self, message: str) -> None:
        self.conn.execute("INSERT INTO service_events (ts, service, level, message) VALUES (?, 'llm', 'warning', ?)",
                          (to_iso(self.clock.now()), message[:500]))

    def _elapsed(self, step_row: sqlite3.Row, fmt: str, now: datetime, voice_sec: int | None) -> tuple[int, int]:
        raw = int((now - from_iso(step_row["sent_at"])).total_seconds()) if step_row["sent_at"] else 0
        cap = min(2 * self.est.get(fmt), self.settings.get("task_cap_sec"))
        counted = min(raw, cap)
        if voice_sec:
            counted = min(max(counted, voice_sec), self.settings.get("task_cap_sec"))
        return max(0, counted), max(0, raw)

    def _account(self, sid: int, day: date, track: str, sec: int, voice_sec: int) -> None:
        col = "active_sec_en" if track == Track.EN.value else "active_sec_psy"
        self.conn.execute(f"UPDATE sessions SET {col} = {col} + ?, voice_sec = voice_sec + ?, "
                          f"last_activity_at = ?, status = 'active' WHERE id = ?",
                          (sec, voice_sec, to_iso(self.clock.now()), sid))
        self.days.add_time(day, track, sec, voice_sec)

    def idle_check(self) -> bool:
        """Вызывается по таймеру: 10 минут без ответа — сессия на паузе."""
        s = self.active()
        if s is None or s["status"] == "paused":
            return False
        idle = (self.clock.now() - from_iso(s["last_activity_at"])).total_seconds()
        if idle >= self.settings.get("idle_pause_sec"):
            self.conn.execute("UPDATE sessions SET status = 'paused' WHERE id = ?", (s["id"],))
            return True
        return False

    async def answer_psy(self, step_id: int, text: str, voice_sec: int | None = None) -> PsyFeedback | None:
        self.settle_reading()
        row = self.conn.execute("SELECT * FROM session_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None or row["kind"] != "psy" or row["status"] != "active":
            return None
        s = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (row["session_id"],)).fetchone()
        return await self.psy_answer(row, json.loads(row["payload_json"]), s, text, voice_sec)

    def digest_time(self, step_id: int) -> int:
        """Время чтения части разбора: по факту, не больше оценки по длине и четырёх минут."""
        row = self.conn.execute("SELECT * FROM session_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None or not row["sent_at"]:
            return 0
        p = json.loads(row["payload_json"])
        part = self.digest_part(p["topic"], p["part"])
        cap = min(240, max(20, round(len(part["body"]) * self.settings.get("read_sec_per_1000") / 1000))) \
            if part else 60
        now = self.clock.now()
        sec = max(0, min(int((now - from_iso(row["sent_at"])).total_seconds()), cap))
        s = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (row["session_id"],)).fetchone()
        with transaction(self.conn):
            self._account(s["id"], date.fromisoformat(s["study_date"]), Track.PSY.value, sec, 0)
        return sec

    async def answer(self, step_id: int, text: str | None, voice_sec: int | None = None) -> Feedback:
        self.settle_reading()
        row = self.conn.execute("SELECT * FROM session_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None or row["status"] != "active":
            raise SessionError("это задание уже закрыто")
        step = self._load(row)
        s = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (row["session_id"],)).fetchone()
        mode = Mode(s["mode"])
        if step.kind == "task":
            return await self._answer_task(row, step, s, mode, text or "", voice_sec)
        if step.kind in ("dialog", "exit_dialog"):
            return await self._answer_dialog(row, step, s, text or "", voice_sec)
        if step.kind in ("monologue", "cp_monologue", "exit_monologue"):
            return await self._answer_monologue(row, step, s, text or "", voice_sec)
        if step.kind == "cp_questions":
            return await self._answer_questions(row, step, s, text or "", voice_sec)
        raise SessionError(f"шаг {step.kind} не принимает ответ")

    async def _answer_task(self, row, step: Step, s, mode: Mode, text: str, voice_sec) -> Feedback:
        task = step.task()
        typed = voice_sec is None
        if typed and task.voice:
            # голосовое задание, а ответ набран: проверка как у набранного текста,
            # на ступени 4 — тренировка написания, интервал не двигается
            task.voice = False
            if task.format == RU_EN and task.stage == 4 and task.schedules and not task.meta.get("control"):
                task.schedules = False
                task.meta["writing_only"] = True
        mode = Mode.TEXT if typed else Mode.VOICE          # в reviews — канал ответа
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        elapsed, raw = self._elapsed(row, task.format, now, voice_sec)
        fb = Feedback()
        if task.format == INTRO:
            check = None
            correct, judge = None, Judge.CODE
        else:
            check = await self.checker.check(task, text, now)
            correct, judge = check.correct, check.judge
        with transaction(self.conn):
            if task.format != INTRO and correct is None:
                out = self.track.record(task, None, judge=judge, mode=mode, day=day, now=now, answer=text,
                                        elapsed_sec=elapsed, raw_sec=raw, session_id=s["id"])
                self.conn.execute("UPDATE item_state SET deferred_until = ? WHERE item_id = ?",
                                  (NEXT_SESSION, task.item_id))
                fb.deferred = check.unavailable or "проверка недоступна"
                fb.lines.append("Проверка сейчас недоступна, задание вернётся в следующей сессии.")
            else:
                out = self.track.record(task, correct, judge=judge, mode=mode, day=day, now=now,
                                        answer=text or None, elapsed_sec=elapsed, raw_sec=raw,
                                        session_id=s["id"], verdict_json=check.verdict if check else None)
                fb.correct, fb.review_id = correct, out.review_id
                fb.step_id, fb.explainable = step.id, task.format != INTRO
                step.payload.update(answered=text or "", review_id=out.review_id)
                self.conn.execute("UPDATE session_steps SET payload_json = ? WHERE id = ?",
                                  (json.dumps(step.payload, ensure_ascii=False), step.id))
                if check is not None:
                    fb.disputable = check.disputable
                    if check.explanation:
                        fb.lines.append(check.explanation)
                    if correct is False:
                        right = check.correction or (task.expected[0] if task.expected else "")
                        if right:
                            fb.lines.append(f"Правильно: {right}")
                        log_en_error(self.conn, check, task.meta.get("target", task.code), out.review_id,
                                     text, now, day.isoformat())
                if task.format == FIND_ERROR:
                    self.track.mark_error_used(task.meta["error_id"])
                elif task.format != INTRO and not self.track.is_practice(task):
                    self.days.add_review(day, Track.EN)
                if task.format == INTRO and task.meta.get("rules"):
                    pass                      # правило уже было на карточке знакомства — не повторять
                elif task.format == INTRO and task.notes:
                    fb.notes = [dict(r) for r in self.conn.execute(
                        f"SELECT code, title, body FROM notes WHERE track = 'en' AND code IN "
                        f"({','.join('?' * len(task.notes))}) ORDER BY code", task.notes)]
                if out.closed:
                    fb.lines.append("Фраза закрыта: уходит в редкое повторение.")
                if task.meta.get("control"):
                    step.payload["result"] = correct
                    self.conn.execute("UPDATE session_steps SET payload_json = ? WHERE id = ?",
                                      (json.dumps(step.payload, ensure_ascii=False), step.id))
            if check is not None and check.call_ids and out.review_id:
                self.checker.runner.llm.link_review(check.call_ids, out.review_id)
            self._account(s["id"], day, step.track, elapsed, voice_sec or 0)
            if step.slot == "academic":
                self.conn.execute("UPDATE days SET sec_en_acad = sec_en_acad + ? WHERE study_date = ?",
                                  (elapsed, day.isoformat()))
            self._mark(step.id, "done", now)
        return fb

    async def _answer_dialog(self, row, step: Step, s, text: str, voice_sec) -> Feedback:
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        p = step.payload
        d = Dialog(p["topic"], p["vocabulary"], p["turns"], history=p.get("history", []),
                   learner_lines=p.get("learner_lines", []))
        elapsed, raw = self._elapsed(row, "dialog", now, voice_sec)
        elapsed = min(elapsed, 90)                  # одна реплика — не больше полутора минут
        fb = Feedback(finished_step=False)
        exit_part = step.kind == "exit_dialog"
        sc = self.reh.get(p["scenario"]) if p.get("scenario") else None
        try:
            if sc is not None:
                by_ear = bool(p.get("by_ear"))
                line, q = await self.talk.scenario_turn(d, {**sc.data}, p.get("asked", []), text, by_ear)
                if q is not None:
                    p.setdefault("asked", []).append(q)
                    question = sc.data["bot_questions"][q - 1]
                    p.setdefault("transcript", []).append({"who": "learner", "text": text})
                    p["transcript"].append({"who": "bot", "reply": line, "asked": question})
                    if by_ear:
                        fb.audio_line = line_key(sc.code, q)
                        fb.reply = line or None
                    else:
                        fb.reply = " ".join(x for x in (line, question) if x)
                else:
                    p.setdefault("transcript", []).append({"who": "learner", "text": text})
                    p["transcript"].append({"who": "bot", "reply": line, "asked": ""})
                    fb.reply = line or None
            else:
                fb.reply = await self.talk.reply(d, text)
        except LLMUnavailable as exc:
            d.finished = True
            fb.deferred = str(exc)
        result = None
        if d.finished and exit_part and fb.deferred is None:
            transcript = [{"who": "bot", "reply": "", "asked": p.get("opening", "")}] + p.get("transcript", [])
            try:
                result = await self.talk.grade_exit_dialog(sc.data, transcript, sc.data.get("learner_min") or 5)
            except LLMUnavailable as exc:
                fb.deferred = str(exc)
        with transaction(self.conn):
            if d.finished:
                fb.finished_step = True
                marked = self.track.mark_spoken(d.learner_lines)
                if marked:
                    fb.lines.append(f"Прозвучали в диалоге: {', '.join(marked)}.")
                if exit_part:
                    fb.lines += self._finish_exit(step, s, day, now, "dialog", sc.code if sc else "", result, fb)
                elif fb.deferred is None:
                    try:
                        for c in await self.talk.corrections(d):
                            fb.lines.append(f"{c['said']} → {c['better']} ({c['why']})")
                    except LLMUnavailable as exc:
                        self._event(str(exc))
                self._mark(step.id, "done", now)
            else:
                p.update(history=d.history, learner_lines=d.learner_lines)
                self.conn.execute("UPDATE session_steps SET payload_json = ?, sent_at = ? WHERE id = ?",
                                  (json.dumps(p, ensure_ascii=False), to_iso(now), step.id))
            self._account(s["id"], day, step.track, elapsed, voice_sec or 0)
        return fb

    def _finish_exit(self, step: Step, s, day: date, now: datetime, part: str, code: str,
                     result: dict | None, fb: Feedback) -> list[str]:
        """Записать попытку части критерия выхода. Без проверки (модель недоступна) — не попытка."""
        area = step.payload.get("area", "")
        if result is None:
            fb.exit_result = {"part": part, "postponed": True}
            return []
        if part == "dialog":
            need = (self.reh.get(code).data.get("learner_min") if self.reh.get(code) else None) or 5
            passed = (result["verdict"] == "passed" and result["answered_all"]
                      and result["own_questions_ok"] >= need and not result["russian_used"])
        else:
            passed = result["verdict"] == "passed"
        verdict = {k: v for k, v in result.items() if k != "call_ids"}
        closed = self.reh.record_exit(area, part, code, passed, verdict, day, now, s["id"])
        fb.correct = passed
        fb.exit_result = {"part": part, "passed": passed, "closed": closed, "area": area,
                          "summary": self.reh.exit_summary(area),
                          "next": self.reh.next_exit_day(area, part, day)}
        lines = []
        if part == "dialog":
            if result["unanswered"]:
                lines.append("Без ответа: " + "; ".join(result["unanswered"]))
            lines.append(f"Своих вопросов построено верно: {result['own_questions_ok']} из {need}.")
            if result["russian_used"]:
                lines.append("При заминке звучал русский: нужны рабочие фразы.")
        else:
            if result["missing"]:
                lines.append("Не прозвучало: " + "; ".join(result["missing"]))
        lines += [f"{e['said']} → {e['better']} ({e['why']})" for e in result["errors"]]
        if result["comment"]:
            lines.append(result["comment"])
        return lines

    async def _answer_monologue(self, row, step: Step, s, text: str, voice_sec) -> Feedback:
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        fmt = "exit_monologue" if step.kind == "exit_monologue" else "monologue"
        elapsed, raw = self._elapsed(row, fmt, now, voice_sec)
        control = step.kind == "cp_monologue"
        exit_part = step.kind == "exit_monologue"
        p = step.payload
        fb = Feedback()
        try:
            res = await self.talk.monologue(
                text, p.get("plan", []), voice_sec, exit_criterion=exit_part,
                tier="flagship" if (control or exit_part) else CHEAP,
                criteria=p.get("criteria", ""), min_sentences=p.get("min_sentences"),
                support=p.get("support"))
        except LLMUnavailable as exc:
            res, fb.deferred = None, str(exc)
        with transaction(self.conn):
            if res is not None:
                fb.correct = res["verdict"] == "passed"
                if exit_part:
                    fb.lines += self._finish_exit(step, s, day, now, "monologue", p.get("rehearsal", ""), res, fb)
                else:
                    if res["missing"]:
                        fb.lines.append("Не прозвучало: " + "; ".join(res["missing"]))
                    fb.lines += [f"{e['said']} → {e['better']} ({e['why']})" for e in res["errors"]]
                    if res["comment"]:
                        fb.lines.append(res["comment"])
                if p.get("rehearsal") and step.kind == "monologue":
                    before = p.get("stage", 1)
                    after = self.reh.record_monologue(p["rehearsal"], fb.correct, before, now)
                    if after > before:
                        fb.lines.append(f"Монолог {p['rehearsal']}: дальше — {STAGE_TITLE[after]}.")
                self.track.mark_spoken([text])
            elif exit_part:
                self._finish_exit(step, s, day, now, "monologue", p.get("rehearsal", ""), None, fb)
            p["result"] = None if res is None else fb.correct
            self.conn.execute("UPDATE session_steps SET payload_json = ? WHERE id = ?",
                              (json.dumps(p, ensure_ascii=False), step.id))
            self._account(s["id"], day, step.track, elapsed, voice_sec or 0)
            self._mark(step.id, "done", now)
            fb.control_result = self._maybe_finish_control(s["id"], now)
        return fb

    async def _answer_questions(self, row, step: Step, s, text: str, voice_sec) -> Feedback:
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        elapsed, raw = self._elapsed(row, "cp_questions", now, voice_sec)
        qs = [q.strip() for q in text.replace("?", "?\n").split("\n") if q.strip()]
        fb = Feedback()
        try:
            res = await self.talk.own_questions(qs, step.payload["topic"])
        except LLMUnavailable as exc:
            res, fb.deferred = None, str(exc)
        with transaction(self.conn):
            if res is not None:
                fb.correct = res["accepted"] >= step.payload["need"]
                fb.lines += [f"{q['question']} → {q['fix']}" for q in res["items"] if not q["ok"]]
                fb.lines.append(f"Засчитано вопросов: {res['accepted']} из {step.payload['need']}.")
            step.payload["accepted"] = None if res is None else res["accepted"]
            self.conn.execute("UPDATE session_steps SET payload_json = ? WHERE id = ?",
                              (json.dumps(step.payload, ensure_ascii=False), step.id))
            self._account(s["id"], day, step.track, elapsed, voice_sec or 0)
            self._mark(step.id, "done", now)
        return fb

    def _maybe_finish_control(self, sid: int, now: datetime) -> dict | None:
        """После монолога зачёта — итог контрольной точки по шагам сессии."""
        fin = self.conn.execute("SELECT * FROM session_steps WHERE session_id = ? AND kind = 'cp_finish' "
                                "AND status = 'planned'", (sid,)).fetchone()
        if fin is None:
            return None
        info = json.loads(fin["payload_json"])
        rows = self.conn.execute("SELECT kind, payload_json FROM session_steps WHERE session_id = ? "
                                 "AND slot = 'control' AND kind != 'cp_finish'", (sid,)).fetchall()
        ru = hear = 0
        questions, mono = 0, False
        for r in rows:
            p = json.loads(r["payload_json"])
            if r["kind"] == "task":
                ok = p.get("result") is True
                if p["meta"].get("control") == "ru":
                    ru += ok
                else:
                    hear += ok
            elif r["kind"] == "cp_questions":
                if p.get("accepted") is None:
                    self._mark(fin["id"], "skipped", now)
                    return {"block": info["block"], "postponed": True}
                questions = p["accepted"]
            elif r["kind"] == "cp_monologue":
                if p.get("result") is None:
                    self._mark(fin["id"], "skipped", now)
                    return {"block": info["block"], "postponed": True}
                mono = bool(p["result"])
        result = ControlResult(ru, info["ru"], hear, info["hear"], questions, mono)
        plan = self.blocks.plan(info["area"], info["block"])
        passed = self.blocks.record(plan, result, now)
        self._mark(fin["id"], "done", now)
        return {"block": info["block"], "passed": passed, "ru": [ru, info["ru"]],
                "hearing": [hear, info["hear"]], "questions": questions, "monologue": mono}

    def defer(self, step_id: int) -> None:
        """«Сейчас не могу слушать»: задание — в следующую сессию, эта добирается другими форматами."""
        self.settle_reading()
        row = self.conn.execute("SELECT * FROM session_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None or row["status"] not in ("active", "planned"):
            raise SessionError("это задание уже закрыто")
        with transaction(self.conn):
            self._mark(step_id, "deferred", self.clock.now())
            if row["item_id"]:
                self.conn.execute("UPDATE item_state SET deferred_until = ? WHERE item_id = ?",
                                  (NEXT_SESSION, row["item_id"]))

    def status(self) -> DayStatus:
        return self.days.status(self.today())
