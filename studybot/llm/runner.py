"""Задание модели с проверкой формата: инструкция + данные → проверенный JSON."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import LLM, LLMUnavailable
from .prompts import Prompts
from .schemas import SchemaError, validate

RETRY_NOTE = ("Ответ не прошёл проверку формата: {error}. Верни только JSON-объект "
              "строго по схеме из инструкции, без пояснений вокруг.")


@dataclass
class ModelResult:
    data: dict[str, Any]
    call_ids: list[int | None] = field(default_factory=list)


class ModelRunner:
    def __init__(self, llm: LLM, prompts: Prompts) -> None:
        self.llm = llm
        self.prompts = prompts

    def available(self) -> bool:
        return self.llm.available()[0]

    async def text(self, task: str, tier: str, payload: dict | str, max_chars: int = 1500) -> ModelResult:
        """Ответ простым текстом (объяснение «Почему так?»): без JSON и без повтора."""
        reply = await self.llm.call(tier, task, self.prompts[task], payload, json_mode=False)
        text = reply.text.strip().strip("`").strip()
        if not text:
            self.llm.mark_failed(reply.call_id, "пустой ответ")
            raise LLMUnavailable("invalid", f"{task}: пустой ответ")
        if len(text) > max_chars:
            cut = text[:max_chars]
            text = cut[:cut.rfind(".") + 1] if "." in cut else cut
        return ModelResult({"text": text}, [reply.call_id])

    async def run(self, task: str, tier: str, payload: dict | str,
                  history: list[dict] | None = None) -> ModelResult:
        """Один вызов и один повтор при невалидном JSON; иначе LLMUnavailable('invalid')."""
        system = self.prompts[task]
        calls: list[int | None] = []
        reply = await self.llm.call(tier, task, system, payload, history)
        calls.append(reply.call_id)
        try:
            return ModelResult(validate(task, reply.text), calls)
        except SchemaError as exc:
            self.llm.mark_failed(reply.call_id, str(exc))
            retry_history = [*(history or []),
                             {"role": "user", "content": payload if isinstance(payload, str)
                              else __import__("json").dumps(payload, ensure_ascii=False)},
                             {"role": "assistant", "content": reply.text}]
            reply2 = await self.llm.call(tier, task, system, RETRY_NOTE.format(error=exc), retry_history)
            calls.append(reply2.call_id)
            try:
                return ModelResult(validate(task, reply2.text), calls)
            except SchemaError as exc2:
                self.llm.mark_failed(reply2.call_id, str(exc2))
                raise LLMUnavailable("invalid", f"{task}: ответ не по схеме дважды") from exc2
