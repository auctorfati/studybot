"""Сборка английской части сессии по шаблонам программы модуля (раздел 9).

5 минут — только повторение. 15 — повторение, новое, мини-диалог.
30 — повторение, звуковая минута, новое, на слух, диалог или монолог.
Недобранное время слота переходит в следующий. Порядок повторений:
сначала молодые фразы на ступенях 2–3 (им нужен показ на следующий день),
затем сетка по срочности; всего не больше потолка в день. В текстовом режиме
фразы ступени 4 не выдаются — их речевая часть придерживается на вечер.
Сверх нормы новое не вводится: время уходит в глубокие форматы.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from ..clock import StudyCalendar, to_iso
from ..english.blocks import Blocks
from ..english.rehearsals import CONTROL_STAGE, EXIT_TURNS, STAGE_TITLE, Rehearsal, Rehearsals, support_lines
from ..english.track import FIND_ERROR, EnglishTrack, Task
from ..enums import Mode, Track
from ..review_queue import ReviewQueue
from ..settings import Settings
from .day import DayBook
from .estimates import Estimates

TEMPLATES = {
    "5": [("review", 300), ("new", 0)],      # повторять нечего или мало — остаток уходит в новые фразы (0.11.2)
    "15": [("review", 300), ("new", 300), ("talk", 300)],
    "30": [("review", 480), ("sound", 180), ("new", 480), ("hearing", 240), ("talk", 420)],
}
MAX_FIND_ERROR_PER_SLOT = 2
DIALOG_TURNS = {"short": 5, "long": 10}


@dataclass
class Step:
    kind: str                 # task, dialog, monologue, cp_questions, cp_monologue, cp_finish
    slot: str
    est_sec: int
    payload: dict
    item_id: int | None = None
    track: str = Track.EN.value
    id: int | None = None
    status: str = "planned"

    def task(self) -> Task:
        return Task(**self.payload)


@dataclass
class PlanContext:
    day: date
    now: datetime
    mode: Mode
    model_ok: bool
    planned_items: set[int] = field(default_factory=set)
    reviews_planned: int = 0
    reserved: int = 0          # места в потолке под ступень 3 до слота «на слух»
    monologue_planned: bool = False
    # психология: единицы нового, уже поставленные в план и ещё не отвеченные, — лимит считается с ними
    psy_new_pending: int = 0   # шаги этой сессии, поставленные раньше (добор в той же сессии)
    psy_new_planned: int = 0   # шаги, поставленные в этой сборке (несколько блоков за раз)


class EnglishPlanner:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, track: EnglishTrack,
                 queue: ReviewQueue, blocks: Blocks, estimates: Estimates, days: DayBook,
                 calendar: StudyCalendar, rehearsals: Rehearsals | None = None) -> None:
        self.conn = conn
        self.reh = rehearsals or Rehearsals(conn)
        self.settings = settings
        self.track = track
        self.queue = queue
        self.blocks = blocks
        self.est = estimates
        self.days = days
        self.calendar = calendar

    # кандидаты

    def _ladder_due(self, ctx: PlanContext, stages: tuple[int, ...]) -> list[int]:
        marks = ",".join("?" * len(stages))
        return [r[0] for r in self.conn.execute(
            f"SELECT i.id FROM items i JOIN item_state s ON s.item_id = i.id "
            f"WHERE i.track = 'en' AND i.archived = 0 AND s.stage IN ({marks}) AND s.due_date <= ? "
            f"AND (s.deferred_until IS NULL OR s.deferred_until <= ?) ORDER BY s.due_date, i.sort_key",
            (*stages, ctx.day.isoformat(), to_iso(ctx.now)))]

    def review_budget(self, ctx: PlanContext) -> int:
        cap = self.queue.cap(Track.EN)
        return max(0, cap - self.days.reviews_done(ctx.day, Track.EN) - ctx.reviews_planned)

    def speech_pending(self, day: date, now: datetime) -> int:
        """Сколько фраз ступени 4 ждут речевой сессии."""
        return len(self.queue.due_today(Track.EN, day, now))

    def _task_step(self, task: Task, slot: str) -> Step:
        return Step("task", slot, self.est.get(task.format), asdict(task), task.item_id)

    # слоты

    def review_slot(self, ctx: PlanContext, sec: int, stages=(2,), with_grid=True,
                    slot: str = "review") -> tuple[list[Step], int]:
        steps: list[Step] = []
        ladder = self._ladder_due(ctx, stages)
        grid = ([d.item_id for d in self.queue.due_today(Track.EN, ctx.day, ctx.now)]
                if with_grid and ctx.mode == Mode.VOICE else [])
        if slot == "hearing":
            ctx.reserved = 0
        ladder_set = set(ladder)
        for item_id in ladder + grid:
            if item_id in ctx.planned_items:
                continue
            reserve = 0 if item_id in ladder_set else ctx.reserved
            if self.review_budget(ctx) - reserve <= 0:
                if item_id in ladder_set:
                    break
                continue
            task = self.track.task_for(item_id, ctx.mode)
            if task is None:
                continue
            step = self._task_step(task, slot)
            if step.est_sec > sec:
                break
            steps.append(step)
            sec -= step.est_sec
            ctx.planned_items.add(item_id)
            ctx.reviews_planned += 1
        if slot == "review":
            fe, sec = self._find_error(ctx, sec)
            steps += fe
        return steps, sec

    def _find_error(self, ctx: PlanContext, sec: int) -> tuple[list[Step], int]:
        """«Найди ошибку» — не больше 10 % английского времени за неделю."""
        est = self.est.get(FIND_ERROR)
        week = self.calendar.week_start(ctx.day).isoformat()
        en_sec = self.conn.execute("SELECT COALESCE(sum(sec_en), 0) FROM days WHERE study_date >= ?",
                                   (week,)).fetchone()[0]
        used = self.conn.execute("SELECT COALESCE(sum(elapsed_sec), 0) FROM reviews WHERE format = ? "
                                 "AND study_date >= ?", (FIND_ERROR, week)).fetchone()[0]
        allowed = self.settings.get("error_share_max") * (en_sec + sec) - used
        n = min(MAX_FIND_ERROR_PER_SLOT, int(allowed // est), sec // est)
        if n <= 0:
            return [], sec
        steps = [self._task_step(t, "review") for t in self.track.find_error_tasks(n)]
        return steps, sec - est * len(steps)

    def new_slot(self, ctx: PlanContext, sec: int) -> tuple[list[Step], int]:
        steps: list[Step] = []
        per = self.est.get("intro") + self.est.get("transform")
        for item_id in self.track.next_new(ctx.day, limit=max(0, sec // per)):
            if item_id in ctx.planned_items:
                continue
            tasks = self.track.intro_tasks(item_id, ctx.mode)
            for t in tasks:
                steps.append(self._task_step(t, "new"))
                sec -= self.est.get(t.format)
            ctx.planned_items.add(item_id)
        return steps, max(0, sec)

    # репетиции блока сборки (слой К8.1)

    @staticmethod
    def monologue_payload(m: Rehearsal, stage: int, **extra) -> dict:
        return {"topic": m.title, "rehearsal": m.code, "stage": stage,
                "stage_title": STAGE_TITLE[stage], "support": support_lines(m, stage),
                "plan": [p["name"] for p in m.data["parts"]], "criteria": m.data.get("criteria", ""),
                "min_sentences": m.data.get("min_sentences"), **extra}

    @staticmethod
    def scenario_payload(sc: Rehearsal, vocab: list[str], turns: int, **extra) -> dict:
        return {"topic": sc.title, "scenario": sc.code, "situation": sc.data["situation"],
                "vocabulary": vocab, "turns": turns, "history": [], "learner_lines": [], "asked": [],
                **extra}

    def _rehearsal_talk(self, ctx: PlanContext, sec: int, long: bool,
                        vocab: list[str]) -> tuple[list[Step], int] | None:
        ab = self.reh.active_block()
        if ab is None:
            return None
        area, block = ab
        est_mono = self.est.get("monologue")
        if (not long and ctx.mode == Mode.VOICE and sec >= est_mono and ctx.day.toordinal() % 2 == 1
                and not ctx.monologue_planned and not self._monologue_today(ctx.day)):
            m = self.reh.monologue_for_practice(area, block)
            if m is not None:
                ctx.monologue_planned = True
                return [Step("monologue", "talk", est_mono, self.monologue_payload(m, m.stage))], sec - est_mono
        sc = self.reh.next_scenario(area, block)
        if sc is None:
            return None
        fmt = "dialog_long" if long else "dialog"
        est = self.est.get(fmt)
        if est > sec + 60:
            return [], sec
        turns = DIALOG_TURNS["long" if long else "short"]
        return [Step("dialog", "talk", est, self.scenario_payload(sc, vocab, turns))], max(0, sec - est)

    def _monologue_today(self, day: date) -> bool:
        """Монолог по плану — не чаще раза в день: остальные разговоры дня — сценарии."""
        return self.conn.execute(
            "SELECT 1 FROM session_steps st JOIN sessions s ON s.id = st.session_id WHERE s.study_date = ? "
            "AND st.kind = 'monologue' AND st.status IN ('planned', 'active', 'done') "
            "AND json_extract(st.payload_json, '$.rehearsal') IS NOT NULL LIMIT 1",
            (day.isoformat(),)).fetchone() is not None

    def exit_steps(self, ctx: PlanContext) -> list[Step]:
        """Критерий выхода модуля: части, которые сегодня можно сдавать (голос, модель доступна)."""
        if ctx.mode != Mode.VOICE or not ctx.model_ok:
            return []
        due = self.reh.exit_parts_due(ctx.day)
        if due is None:
            return []
        area, block, parts = due
        from ..english.rehearsals import DYNAMIC, EXIT_CRITERIA, EXIT_TURNS_BY, PART_TITLE
        steps = []
        for part, r in parts:
            crit = EXIT_CRITERIA.get((area, part), "")
            if part == "monologue":
                pl = self.monologue_payload(r, 3, area=area)
                if crit:
                    pl["criteria"] = crit
                steps.append(Step("exit_monologue", "exit", self.est.get("exit_monologue"), pl))
            elif part == "dialog":
                pl = self.scenario_payload(r, self.track.dialog_vocabulary(), EXIT_TURNS_BY.get(area, EXIT_TURNS),
                                           area=area, by_ear=True)
                if crit:
                    pl["criteria"] = crit
                steps.append(Step("exit_dialog", "exit", self.est.get("exit_dialog"), pl))
            elif part == "listening":
                steps.append(Step("speech", "exit", 900, {
                    "format": "listening", "record": r["code"], "title": r["title"],
                    "card": json.loads(r["fields_json"]), "turns": 0, "history": [], "answers": [],
                    "module": area[2:], "exit": True, "area": area}))
            else:
                card = r if isinstance(r, dict) else json.loads(r["fields_json"])
                mode = "talk" if part in DYNAMIC else "mono"
                steps.append(Step("exit_speech", "exit", 600 if mode != "mono" else 240, {
                    "area": area, "part": part, "title": PART_TITLE[part], "criteria": crit, "card": card,
                    "record": None if isinstance(r, dict) else r["code"], "mode": mode,
                    "turns": DYNAMIC.get(part, (None, 0))[1], "history": [], "answers": [],
                    "module": area[2:]}))
        return steps

    def talk_slot(self, ctx: PlanContext, sec: int, long: bool = False) -> tuple[list[Step], int]:
        if not ctx.model_ok:
            return [], sec
        vocab = self.track.dialog_vocabulary()
        if len(vocab) < 5:
            return [], sec                           # слишком рано для диалога
        reh = self._rehearsal_talk(ctx, sec, long, vocab)
        if reh is not None:
            return reh
        topic = self._current_topic()
        monologue = (not long and ctx.mode == Mode.VOICE and sec >= self.est.get("monologue")
                     and ctx.day.toordinal() % 2 == 1 and self._monologue_plan() is not None)
        if monologue:
            task_ru, plan = self._monologue_source()
            est = self.est.get("monologue")
            return [Step("monologue", "talk", est, {"topic": topic, "plan": plan, "task_ru": task_ru})], sec - est
        fmt = "dialog_long" if long else "dialog"
        est = self.est.get(fmt)
        if est > sec + 60:
            return [], sec
        turns = DIALOG_TURNS["long" if long else "short"]
        return [Step("dialog", "talk", est, {"topic": topic, "vocabulary": vocab, "turns": turns,
                                             "history": [], "learner_lines": []})], max(0, sec - est)

    def _current_topic(self) -> str:
        row = self.conn.execute(
            "SELECT i.unit, i.area FROM items i JOIN item_state s ON s.item_id = i.id "
            "WHERE i.track = 'en' AND s.stage >= 2 ORDER BY i.sort_key DESC LIMIT 1").fetchone()
        return f"блок {row[0]}" if row else "знакомство"

    def _monologue_source(self) -> tuple[str, list[str]] | tuple[None, None]:
        """Единица «по смыслу» последнего открытого блока: русское задание и план по предложениям."""
        row = self.conn.execute(
            "SELECT a.prompt, a.answer FROM items a WHERE a.track = 'en' AND a.kind = 'assembly' "
            "AND a.archived = 0 AND a.unit IN (SELECT i.unit FROM items i JOIN item_state s "
            "ON s.item_id = i.id WHERE i.track = 'en' AND s.stage >= 3) ORDER BY a.sort_key DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None, None
        return row[0], [s.strip() for s in row[1].replace("!", ".").split(".") if s.strip()]

    def _monologue_plan(self) -> list[str] | None:  # noqa: D401
        """План монолога — единица «по смыслу» последнего открытого блока, по предложениям."""
        return self._monologue_source()[1]

    def control_steps(self, ctx: PlanContext, area: str, block: str) -> list[Step]:
        plan = self.blocks.plan(area, block, seed=ctx.day.toordinal())
        steps = []
        for item_id in plan.ru_items:
            steps.append(self._task_step(self.track.control_task(item_id, "ru", ctx.mode), "control"))
        for item_id in plan.hearing_items:
            steps.append(self._task_step(self.track.control_task(item_id, "hear", ctx.mode), "control"))
        monos = self.reh.of_block(area, block, "monologue")
        scenario = self.reh.next_scenario(area, block)
        if scenario is not None:
            # блок сборки: свои вопросы — в ситуации сценария
            steps.append(Step("cp_questions", "control", self.est.get("cp_questions"),
                              {"topic": scenario.title, "situation": scenario.data["situation"],
                               "scenario": scenario.code,
                               "need": max(plan.own_questions, scenario.data.get("learner_min") or 0)}))
        else:
            steps.append(Step("cp_questions", "control", self.est.get("cp_questions"),
                              {"topic": plan.topic or f"блок {block}", "need": plan.own_questions}))
        if monos:
            # блок сборки: монолог по плану с опорой на названия частей, условия зачёта — из банка
            steps.append(Step("cp_monologue", "control", self.est.get("cp_monologue"),
                              self.monologue_payload(monos[0], CONTROL_STAGE, task_ru=None)))
        else:
            mono, task_ru = None, None
            if plan.monologue_item:
                task_ru, mono = self.conn.execute("SELECT prompt, answer FROM items WHERE id = ?",
                                                  (plan.monologue_item,)).fetchone()
            steps.append(Step("cp_monologue", "control", self.est.get("cp_monologue"),
                              {"topic": plan.topic, "task_ru": task_ru,
                               "plan": [s.strip() for s in (mono or "").split(".") if s.strip()]}))
        steps.append(Step("cp_finish", "control", 0, {"area": area, "block": block,
                                                      "ru": len(plan.ru_items),
                                                      "hear": len(plan.hearing_items)}))
        return steps

    # сборка

    def build(self, ctx: PlanContext, kind: str, sec: int) -> list[Step]:
        """План английской части сессии заказанной длины."""
        if kind == "long":
            return self._long(ctx, sec)
        if kind == "30" and self.settings.psy_active():
            cp = self.blocks.due_for_control(ctx.day)
            if cp and ctx.model_ok and ctx.mode == Mode.VOICE:
                return self.control_steps(ctx, *cp)
        template = TEMPLATES.get(kind)
        if template is None:                          # вечерний блок: остаток нормы кусками по 15
            template = []
            left = sec
            while left > 0:
                chunk = min(900, left)
                template += [(s, int(v * chunk / 900)) for s, v in TEMPLATES["15"]]
                left -= chunk
        norm_done = self.days.status(ctx.day).norm_done
        if any(slot == "hearing" for slot, _ in template):
            # молодые фразы ступени 3 не должны голодать из-за сетки
            ctx.reserved = len(self._ladder_due(ctx, (3,)))
        steps: list[Step] = []
        carry = 0
        for slot, slot_sec in template:
            budget = slot_sec + carry
            if slot == "new" and norm_done:
                slot = "talk" if ctx.model_ok else "hearing"
            if slot == "review":
                got, carry = self.review_slot(ctx, budget)
            elif slot == "new":
                got, carry = self.new_slot(ctx, budget)
            elif slot == "hearing":
                got, carry = self.review_slot(ctx, budget, stages=(3,), with_grid=False, slot="hearing")
            elif slot == "talk":
                got, carry = self.talk_slot(ctx, budget)
            else:                                     # звуковая минута: контента пока нет
                got, carry = [], budget
            steps += got
        if carry > 60:
            got, _ = self.review_slot(ctx, carry, stages=(2, 3), slot="topup")
            steps += got
        if kind == "30":
            exit_steps = self.exit_steps(ctx)
            if exit_steps:
                # критерий выхода — в начале сессии, на место её разговора: план длиннее
                # заказанного, и хвост сессии без зачёта не страшен, а зачёт — да
                steps = exit_steps + [s for s in steps if s.slot != "talk"]
        return steps

    def _long(self, ctx: PlanContext, sec: int) -> list[Step]:
        """Воскресная длинная сессия в переходном режиме: зачёт блока и длинный диалог.
        Если сегодня можно сдавать критерий выхода — он вместо длинного диалога."""
        steps: list[Step] = []
        cp = self.blocks.due_for_control(ctx.day)
        if cp and ctx.model_ok and ctx.mode == Mode.VOICE:
            steps += self.control_steps(ctx, *cp)
        used = sum(s.est_sec for s in steps)
        talk = self.exit_steps(ctx)
        if not talk:
            talk, _ = self.talk_slot(ctx, max(0, sec - used), long=True)
        steps += talk
        used = sum(s.est_sec for s in steps)
        if sec - used > 60:
            got, _ = self.review_slot(ctx, sec - used, stages=(2, 3))
            steps += got
        return steps

    @staticmethod
    def dump(step: Step) -> str:
        return json.dumps(step.payload, ensure_ascii=False)
