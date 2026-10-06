"""Форматы на слух: пересказ истории Р (модули 2–6) и длинное слушание Л (модуль 6).

Порядок (файл слушания модуля 6, раздел 1; формат историй — банк модуля 2, раздел 1):
запись звуком без текста на экране, слушать можно сколько угодно раз; после первого
прослушивания — «Слова»; затем вопросы звуком по одному, ответ голосом; затем пересказ;
текст записи открывается только после пересказа. Модель сверяет ответы с ключом
и пересказ с пунктами. Записи и вопросы озвучиваются заранее; без записи формат не предлагается.
Резервные Л6, Л12, Л17, Л19 — только для критерия выхода модуля 6.
"""

from __future__ import annotations

import json
import sqlite3

from .academic import numbered
from .rehearsals import LISTENING_RESERVE, Rehearsals


def text_key(code: str) -> str:
    return f"{code}:text"


def question_key(code: str, k: int) -> str:
    return f"{code}:q{k}"


def parts(fields: dict) -> tuple[list[str], list[str], list[str]]:
    """(вопросы, ключ, пункты пересказа). У историй пункты — ключ фактов."""
    qs, keys = numbered(fields.get("Вопросы", "")), numbered(fields.get("Ключ", ""))
    points = numbered(fields.get("Пересказ", "")) or keys
    return qs, keys, points


def lines(code: str, fields: dict) -> list[tuple[str, str]]:
    """Что озвучить заранее: текст записи и вопросы по одному."""
    qs, _, _ = parts(fields)
    return [(text_key(code), fields.get("Текст", ""))] + [(question_key(code, k), q) for k, q in enumerate(qs, 1)]


def voiced(reh: Rehearsals, code: str, fields: dict) -> bool:
    return all(reh.line_audio(k, t) is not None for k, t in lines(code, fields))


def pick(conn: sqlite3.Connection, reh: Rehearsals, kind: str, areas: list[str]) -> sqlite3.Row | None:
    """Озвученная запись по кругу; резерв критерия в тренировке не открывается."""
    if not areas:
        return None
    marks = ",".join("?" * len(areas))
    for r in conn.execute(
            f"SELECT r.*, COALESCE(s.uses, 0) AS uses FROM records r LEFT JOIN record_state s ON s.code = r.code "
            f"WHERE r.archived = 0 AND r.kind = ? AND r.area IN ({marks}) ORDER BY uses, r.sort_key", (kind, *areas)):
        if r["code"] in LISTENING_RESERVE:
            continue
        if voiced(reh, r["code"], json.loads(r["fields_json"])):
            return r
    return None
