"""Сменный слой вызова модели.

Одна функция: вызвать модель уровня «дешёвая» или «флагман» с заданием.
Поставщик задаётся в конфиге; Moonshot говорит в формате OpenAI Chat
Completions, поэтому смена поставщика — это адрес, ключ и названия моделей.
Каждый вызов пишется в llm_calls с токенами и стоимостью. Дневной предел
расхода проверяется до вызова: при его достижении модель не вызывается,
задания откладываются, сессия идёт форматами с проверкой кодом.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import httpx

from ..clock import Clock, StudyCalendar, to_iso
from ..config import LLMConfig

CHEAP, FLAGSHIP = "cheap", "flagship"


class LLMUnavailable(RuntimeError):
    """Модель недоступна: нет ключа, предел расхода, сеть, ошибка поставщика."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason     # 'not_configured', 'budget', 'network', 'provider', 'invalid'


@dataclass
class LLMReply:
    text: str
    tokens_in: int
    tokens_out: int
    model: str
    call_id: int | None = None


class Transport(Protocol):
    async def chat(self, model: str, messages: list[dict], json_mode: bool,
                   temperature: float | None, max_tokens: int, timeout: float,
                   extra: dict | None = None) -> LLMReply: ...


class OpenAICompatible:
    """Chat Completions по HTTP. Ответ модели с рассуждением: берётся content, не reasoning_content."""

    def __init__(self, base_url: str, api_key: str,
                 http: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.http = http or httpx.AsyncClient()

    async def chat(self, model, messages, json_mode, temperature, max_tokens, timeout,
                   extra=None) -> LLMReply:
        body: dict = {**(extra or {}), "model": model, "messages": messages, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if temperature is not None:
            body["temperature"] = temperature
        try:
            r = await self.http.post(f"{self.base_url}/chat/completions", json=body, timeout=timeout,
                                     headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as exc:
            raise LLMUnavailable("network", f"сеть: {exc.__class__.__name__}") from exc
        if r.status_code != 200:
            detail = r.text[:200].replace(self.api_key, "***") if self.api_key else r.text[:200]
            raise LLMUnavailable("provider", f"HTTP {r.status_code}: {detail}")
        try:
            data = r.json()
            text = data["choices"][0]["message"].get("content") or ""
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable("provider", "ответ поставщика не по формату") from exc
        return LLMReply(text, int(usage.get("prompt_tokens", 0)),
                        int(usage.get("completion_tokens", 0)), data.get("model", model))


class LLM:
    """Вызов модели с учётом расходов и журналом."""

    def __init__(self, conn: sqlite3.Connection, cfg: LLMConfig, clock: Clock,
                 calendar: StudyCalendar, transport: Transport | None = None) -> None:
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        self.calendar = calendar
        self.transport = transport or (OpenAICompatible(cfg.base_url, cfg.api_key)
                                       if cfg.configured else None)

    def spent(self, day: date) -> float:
        return self.conn.execute("SELECT COALESCE(sum(cost_usd), 0) FROM llm_calls WHERE study_date = ?",
                                 (day.isoformat(),)).fetchone()[0]

    def cost(self, tier: str, tokens_in: int, tokens_out: int) -> float:
        p = self.cfg.prices
        pin, pout = ((p.cheap_input, p.cheap_output) if tier == CHEAP
                     else (p.flagship_input, p.flagship_output))
        return (tokens_in * pin + tokens_out * pout) / 1_000_000

    def available(self) -> tuple[bool, str]:
        if self.transport is None:
            return False, "модель не настроена"
        day = self.calendar.study_date(self.clock.now())
        if self.cfg.daily_budget_usd and self.spent(day) >= self.cfg.daily_budget_usd:
            return False, "дневной предел расхода исчерпан"
        return True, ""

    async def call(self, tier: str, task: str, system: str, user: str | dict,
                   history: list[dict] | None = None, json_mode: bool = True) -> LLMReply:
        ok, why = self.available()
        if not ok:
            raise LLMUnavailable("budget" if "предел" in why else "not_configured", why)
        cheap = tier == CHEAP
        model = self.cfg.cheap_model if cheap else self.cfg.flagship_model
        extra = self.cfg.cheap_extra if cheap else self.cfg.flagship_extra
        max_tokens = self.cfg.max_tokens if cheap else (self.cfg.flagship_max_tokens or self.cfg.max_tokens)
        timeout = self.cfg.timeout_sec if cheap else (self.cfg.flagship_timeout_sec or self.cfg.timeout_sec)
        content = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False, indent=1)
        messages = [{"role": "system", "content": system}, *(history or []),
                    {"role": "user", "content": content}]
        now = self.clock.now()
        day = self.calendar.study_date(now)
        try:
            reply = await self.transport.chat(model, messages, json_mode and self.cfg.json_mode,
                                              self.cfg.temperature, max_tokens, timeout,
                                              extra=extra or None)
        except LLMUnavailable as exc:
            self._log(now, day, tier, task, model, 0, 0, False, str(exc))
            self._event("error", f"{task}: {exc}")
            raise
        reply.call_id = self._log(now, day, tier, task, reply.model, reply.tokens_in,
                                  reply.tokens_out, True, None)
        return reply

    def mark_failed(self, call_id: int | None, error: str) -> None:
        """Ответ получен, но не прошёл проверку формата: вызов оплачен, но неуспешен."""
        if call_id:
            self.conn.execute("UPDATE llm_calls SET ok = 0, error = ? WHERE id = ?", (error[:300], call_id))

    def link_review(self, call_ids: list[int | None], review_id: int) -> None:
        for cid in call_ids:
            if cid:
                self.conn.execute("UPDATE llm_calls SET review_id = ? WHERE id = ?", (review_id, cid))

    def _log(self, now, day, tier, task, model, tin, tout, ok, error) -> int:
        return self.conn.execute(
            "INSERT INTO llm_calls (ts, study_date, tier, task, model, tokens_in, tokens_out, cost_usd, ok, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (to_iso(now), day.isoformat(), tier, task, model, tin, tout,
             self.cost(tier, tin, tout), int(ok), error)).lastrowid

    def _event(self, level: str, message: str) -> None:
        self.conn.execute("INSERT INTO service_events (ts, service, level, message) VALUES (?, 'llm', ?, ?)",
                          (to_iso(self.clock.now()), level, message[:500]))


class FakeTransport:
    """Подставная модель для тестов: отдаёт заготовленные ответы по очереди."""

    def __init__(self, replies: list[str | Exception] | None = None, auto=None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict] = []
        self.auto = auto          # auto(messages) -> str, когда очередь пуста

    def push(self, *replies: str | dict | Exception) -> None:
        for r in replies:
            self.replies.append(json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r)

    async def chat(self, model, messages, json_mode, temperature, max_tokens, timeout,
                   extra=None) -> LLMReply:
        self.calls.append({"model": model, "messages": messages, "json_mode": json_mode,
                           "max_tokens": max_tokens, "timeout": timeout, "extra": extra})
        if not self.replies and self.auto is not None:
            return LLMReply(self.auto(messages), 1000, 100, model)
        if not self.replies:
            raise LLMUnavailable("provider", "подставной модели нечего ответить")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return LLMReply(r, 1000, 100, model)
