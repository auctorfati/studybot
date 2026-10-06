"""«Почему так?» в английском.

Дешёвая модель получает подсказку, целевую фразу, варианты, тексты заметок
из столбца «Грамматика», строки словаря для слов фразы и ответ ученика, если он
был, и отвечает простым текстом по-русски. Кэш: пара «единица и нормализованный
ответ» запрашивается один раз, без ответа — один раз на единицу; смена ответа
единицы в банке сбрасывает кэш. При дневном пределе или отказе сервиса —
тексты заметок без модели. Вызов идёт в журнал расходов обычным путём.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from ..clock import to_iso
from ..content.reference import phrase_matches
from ..llm.client import CHEAP, LLMUnavailable
from ..llm.runner import ModelRunner
from .normalize import normalize
from .track import Task

MAX_VOCAB = 15
SEC_PER_1000 = 60              # оценка чтения по длине текста, как у разборов
CAP_SEC = 120                  # не больше двух минут на объяснение


@dataclass
class Explanation:
    target: str
    text: str
    by_model: bool
    cached: bool = False
    call_ids: list = field(default_factory=list)

    def message(self) -> str:
        return f"Почему так: {self.target}\n{self.text}"


def reading_cap(chars: int, per_1000: int = SEC_PER_1000, cap: int = CAP_SEC) -> int:
    """Предел учёта чтения: по длине текста, но не больше предела."""
    return max(10, min(cap, round(chars * per_1000 / 1000)))


class Explainer:
    def __init__(self, conn: sqlite3.Connection, runner: ModelRunner) -> None:
        self.conn = conn
        self.runner = runner

    def _item(self, code: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE track = 'en' AND code = ?", (code,)).fetchone()

    def _notes(self, rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
        codes: list[str] = []
        for r in rows:
            for c in json.loads(r["notes_json"]):
                if c not in codes:
                    codes.append(c)
        if not codes:
            return []
        marks = ",".join("?" * len(codes))
        found = {r["code"]: r for r in self.conn.execute(
            f"SELECT code, title, body FROM notes WHERE track = 'en' AND code IN ({marks})", codes)}
        return [found[c] for c in codes if c in found]

    def vocabulary(self, phrases: list[str]) -> list[dict]:
        """Строки словаря, слова которых есть во фразах (с простыми окончаниями)."""
        text = " ".join(p for p in phrases if p)
        if not text:
            return []
        out = []
        for r in self.conn.execute("SELECT word, translation, note, module, block FROM vocab "
                                   "ORDER BY CAST(module AS INTEGER), CAST(block AS INTEGER), sort_key"):
            if phrase_matches(r["word"], text):
                out.append({"word": r["word"], "translation": r["translation"],
                            "note": r["note"] or ""})
                if len(out) >= MAX_VOCAB:
                    break
        return out

    def parts(self, task: Task) -> tuple[sqlite3.Row, sqlite3.Row | None, str, bool]:
        """Целевая единица, исходная (у превращения), целевая фраза и вид проверки."""
        target = self._item(task.meta.get("target", task.code))
        source_code = task.meta.get("source")
        source = self._item(source_code) if source_code and source_code != target["code"] else None
        if task.by_meaning:
            return target, source, task.meta.get("question") or target["answer"], True
        phrase = task.expected[0] if task.expected else target["answer"]
        return target, source, phrase, False

    def _key(self, target: sqlite3.Row, source: sqlite3.Row | None, by_meaning: bool,
             answer: str | None) -> tuple[str, str]:
        norm = normalize(answer) if answer and answer.strip() else ""
        if not norm:
            return "", f"{target['answer_hash']}:unit"
        return norm, f"{target['answer_hash']}:{source['code'] if source else ''}:" \
                     f"{'meaning' if by_meaning else 'exact'}"

    def payload(self, task: Task, answer: str | None) -> dict:
        target, source, phrase, by_meaning = self.parts(task)
        notes = self._notes([target] + ([source] if source else []))
        data = {
            "task": task.instruction,
            "current_module": (target["area"] or "EN1")[2:],
            "russian_prompt": task.prompt_ru or target["prompt"],
            "shown_english": task.shown_en if task.shown_en and task.shown_en != phrase else "",
            "target": phrase,
            "variants": [] if by_meaning else json.loads(target["variants_json"]),
            "grammar_notes": [f"{n['code']} — {n['title']}: {n['body']}" for n in notes],
            "vocabulary": self.vocabulary([phrase, task.shown_en or ""]),
            "pronunciation": target["sound"] or "",
        }
        if by_meaning:
            data["example_answers"] = [t for t, _ in task.meta.get("extra_expected", [])]
        if answer and answer.strip():
            data["answer"] = answer.strip()
        return data

    def fallback(self, task: Task) -> str:
        target, source, phrase, by_meaning = self.parts(task)
        notes = self._notes([target] + ([source] if source else []))
        lines = ["Объяснение моделью сейчас недоступно."]
        if notes:
            lines.append("Заметки к фразе:")
            lines += [f"{n['code']} — {n['title']}\n{n['body']}" for n in notes]
        else:
            variants = [] if by_meaning else json.loads(target["variants_json"])
            if variants:
                lines.append("Допустимо и так: " + " / ".join(variants) + ".")
            lines.append("Заметок к этой фразе нет; правила блока — в «Ещё» → «Правила».")
        return "\n".join(lines)

    async def explain(self, task: Task, answer: str | None, now: datetime) -> Explanation:
        target, source, phrase, by_meaning = self.parts(task)
        norm, context = self._key(target, source, by_meaning, answer)
        row = self.conn.execute("SELECT text FROM explain_cache WHERE item_id = ? AND normalized = ? "
                                "AND context = ?", (target["id"], norm, context)).fetchone()
        if row:
            return Explanation(phrase, row[0], by_model=True, cached=True)
        if not self.runner.available():
            return Explanation(phrase, self.fallback(task), by_model=False)
        try:
            res = await self.runner.text("explain_en", CHEAP, self.payload(task, answer))
        except LLMUnavailable:
            return Explanation(phrase, self.fallback(task), by_model=False)
        text = res.data["text"]
        self.conn.execute("INSERT OR REPLACE INTO explain_cache (item_id, normalized, context, text, created_at) "
                          "VALUES (?, ?, ?, ?, ?)", (target["id"], norm, context, text, to_iso(now)))
        return Explanation(phrase, text, by_model=True, call_ids=res.call_ids)
