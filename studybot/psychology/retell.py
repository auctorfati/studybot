"""Кнопка «Источник»: фрагмент источника позиции в русском пересказе.

Дешёвая модель пересказывает фрагмент простым русским языком, со ссылкой на
источник и страницы; один раз на позицию и версию фрагмента. Без модели —
начало фрагмента как есть, с пометкой.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..clock import to_iso
from ..content.common import short_hash
from ..llm.client import CHEAP, LLMUnavailable
from ..llm.runner import ModelRunner

RAW_LIMIT = 1500


@dataclass
class Retell:
    ref: str
    text: str
    by_model: bool

    def message(self) -> str:
        return f"Источник: {self.ref}\n{self.text}" if self.ref else self.text


class SourceRetell:
    def __init__(self, conn: sqlite3.Connection, runner: ModelRunner) -> None:
        self.conn = conn
        self.runner = runner

    async def retell(self, item_id: int, now: datetime) -> Retell | None:
        it = self.conn.execute("SELECT prompt, answer, source_ref, fragment FROM items WHERE id = ?",
                               (item_id,)).fetchone()
        if it is None or not it["fragment"]:
            return None
        ref = it["source_ref"] or ""
        h = short_hash(it["fragment"])
        row = self.conn.execute("SELECT text FROM source_retell WHERE item_id = ? AND fragment_hash = ?",
                                (item_id, h)).fetchone()
        if row:
            return Retell(ref, row[0], True)
        if self.runner.available():
            try:
                res = await self.runner.text("retell_psy", CHEAP, {
                    "question": it["prompt"], "key": it["answer"], "source_reference": ref,
                    "source_fragment": it["fragment"]}, max_chars=3000)
                text = res.data["text"]
                self.conn.execute("INSERT OR REPLACE INTO source_retell (item_id, fragment_hash, text, created_at) "
                                  "VALUES (?, ?, ?, ?)", (item_id, h, text, to_iso(now)))
                return Retell(ref, text, True)
            except LLMUnavailable:
                pass
        raw = it["fragment"][:RAW_LIMIT].rstrip()
        if len(it["fragment"]) > RAW_LIMIT:
            raw += " …"
        return Retell(ref, "Пересказ сейчас недоступен, фрагмент как есть:\n" + raw, False)
