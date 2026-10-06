"""Разговорные задания английского через модель: мини-диалог, исправления,
монолог, собственные вопросы. Мини-диалог и исправления ведёт дешёвая модель
в пределах пройденного; монолог и вопросы контрольной точки и критерия
выхода — флагман (программа модуля, раздел 8).

Сценарий блока сборки (слой К8.1): бот держится роли, его вопросы — только
из списка сценария, по номеру. Так вопросы критерия выхода можно озвучить
заранее и прислать только звуком."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..llm.client import CHEAP, FLAGSHIP
from ..llm.runner import ModelRunner

MAX_DIALOG_TURNS = 10


@dataclass
class Dialog:
    topic: str
    vocabulary: list[str]
    turns: int                                   # сколько обменов нужно
    by_ear: bool = False                         # вопросы бота только звуком
    history: list[dict] = field(default_factory=list)   # сообщения в формате модели
    learner_lines: list[str] = field(default_factory=list)
    finished: bool = False
    call_ids: list = field(default_factory=list)

    @property
    def exchanges(self) -> int:
        return len(self.learner_lines)


class EnglishTalk:
    def __init__(self, runner: ModelRunner) -> None:
        self.runner = runner

    def _context(self, d: Dialog) -> dict:
        return {"topic": d.topic, "allowed_phrases": d.vocabulary,
                "exchanges_done": d.exchanges, "exchanges_total": d.turns}

    async def open(self, d: Dialog) -> str:
        """Первая реплика бота."""
        res = await self.runner.run("dialog_en", CHEAP, {**self._context(d), "learner_said": None})
        d.call_ids += res.call_ids
        d.history.append({"role": "assistant", "content": res.data["reply"]})
        return res.data["reply"]

    async def reply(self, d: Dialog, learner_text: str) -> str:
        """Реплика ученика → ответ бота. После нужного числа обменов бот прощается, d.finished = True."""
        d.learner_lines.append(learner_text)
        done = d.exchanges >= min(d.turns, MAX_DIALOG_TURNS)
        payload = {**self._context(d), "learner_said": learner_text, "finish_now": done}
        res = await self.runner.run("dialog_en", CHEAP, payload, history=list(d.history))
        d.call_ids += res.call_ids
        d.history += [{"role": "user", "content": learner_text},
                      {"role": "assistant", "content": res.data["reply"]}]
        d.finished = done or res.data["end"]
        return res.data["reply"]

    async def corrections(self, d: Dialog) -> list[dict]:
        """Не больше трёх исправлений после диалога, в первую очередь по ядру модуля."""
        res = await self.runner.run("dialog_corrections", CHEAP,
                                    {"allowed_phrases": d.vocabulary, "learner_lines": d.learner_lines})
        d.call_ids += res.call_ids
        return res.data["corrections"]

    async def monologue(self, transcript: str, plan: list[str], seconds: int | None,
                        exit_criterion: bool = False, tier: str = FLAGSHIP,
                        criteria: str = "", min_sentences: int | None = None,
                        support: list[str] | None = None) -> dict:
        """Флагман — зачёт и критерий выхода; монолог в обычной сессии — дешёвая модель."""
        payload = {
            "transcript": transcript, "plan": plan, "duration_sec": seconds,
            "mode": "exit_criterion" if exit_criterion else ("control_point" if tier == FLAGSHIP
                                                              else "practice")}
        if criteria:
            payload.update(criteria=criteria, min_sentences=min_sentences, support=support or [])
        res = await self.runner.run("monologue_en", tier, payload)
        return {**res.data, "call_ids": res.call_ids}

    async def scenario_turn(self, d: Dialog, scenario: dict, asked: list[int],
                            learner_text: str | None, by_ear: bool) -> tuple[str, int | None]:
        """Реплика бота в сценарии: (отклик или ответ, номер вопроса с 1 или None).

        Номер вне списка отбрасывается: бот не задаёт вопросов, которых нет в сценарии."""
        if learner_text is not None:
            d.learner_lines.append(learner_text)
        done = d.exchanges >= min(d.turns, MAX_DIALOG_TURNS)
        payload = {"situation": scenario["situation"], "rules": scenario.get("rules", ""),
                   "allowed_phrases": d.vocabulary,
                   "bot_questions": {str(k): q for k, q in enumerate(scenario["bot_questions"], start=1)},
                   "asked": asked, "learner_should_ask": scenario.get("learner_questions", []),
                   "learner_said": learner_text, "exchanges_done": d.exchanges,
                   "exchanges_total": d.turns, "finish_now": done, "by_ear": by_ear}
        res = await self.runner.run("scenario_en", CHEAP, payload, history=list(d.history))
        d.call_ids += res.call_ids
        q = res.data["question"]
        if q is not None and not 1 <= q <= len(scenario["bot_questions"]):
            q = None
        if done:
            q = None
        line = res.data["reply"]
        asked_text = scenario["bot_questions"][q - 1] if q else ""
        if learner_text is not None:
            d.history.append({"role": "user", "content": learner_text})
        d.history.append({"role": "assistant", "content": " ".join(x for x in (line, asked_text) if x)})
        d.finished = done or res.data["end"]
        return line, q

    async def grade_exit_dialog(self, scenario: dict, transcript: list[dict], own_min: int) -> dict:
        res = await self.runner.run("exit_dialog_en", FLAGSHIP, {
            "situation": scenario["situation"], "rules": scenario.get("rules", ""),
            "transcript": transcript, "own_questions_min": own_min})
        return {**res.data, "call_ids": res.call_ids}

    async def own_questions(self, questions: list[str], topic: str) -> dict:
        res = await self.runner.run("questions_en", FLAGSHIP, {"topic": topic, "questions": questions})
        return {**res.data, "call_ids": res.call_ids}
