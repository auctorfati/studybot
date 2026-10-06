"""Граф связей и вид превращения (формат банка, раздел 2).

Связь двусторонняя, в файле записана у меньшего номера; здесь граф строится
в обе стороны по всем действующим единицам трека, включая связи между файлами.
Вид превращения код определяет по целевой фразе: знак вопроса — вопрос,
not или n't — отрицание, начало с Yes или No — краткий ответ, остальное —
утверждение; смена местоимения — другое лицо.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from .normalize import normalize

QUESTION, NEGATION, SHORT, STATEMENT = "question", "negation", "short", "statement"

PERSONS = {
    "i": 1, "my": 1, "me": 1,
    "you": 2, "your": 2,
    "he": 3, "his": 3, "him": 3,
    "she": 4, "her": 4,
    "it": 5, "this": 5, "that": 5,
    "we": 6, "our": 6, "us": 6,
    "they": 7, "their": 7, "them": 7,
}

INSTRUCTION = {
    QUESTION: "Задай вопрос",
    NEGATION: "Скажи с отрицанием",
    SHORT: "Ответь коротко",
    STATEMENT: "Скажи утверждением",
    "person": "Скажи о другом",
    "change": "Измени фразу",
    "time": "Смени время",       # столбец «Время» модулей 2–6 (карта пути, раздел 9, пункт 3)
}


def phrase_kind(target: str) -> str:
    t = target.strip()
    if t.endswith("?"):
        return QUESTION
    if re.match(r"^(yes|no)\b", t, re.I):
        return SHORT
    if re.search(r"\bnot\b|n't\b|n’t\b", t, re.I):
        return NEGATION
    return STATEMENT


def person(target: str) -> int | None:
    for w in normalize(target).split():
        if w in PERSONS:
            return PERSONS[w]
    return None


def transform_label(source: str, target: str) -> str:
    """Инструкция к заданию «из фразы источника сделай целевую»."""
    ks, kt = phrase_kind(source), phrase_kind(target)
    if kt != ks:
        return kt
    ps, pt = person(source), person(target)
    if ps is not None and pt is not None and ps != pt:
        return "person"
    return "change"


@dataclass(frozen=True)
class Node:
    item_id: int
    code: str
    prompt: str
    target: str
    kind: str      # phrase / assembly


class LinkGraph:
    def __init__(self, nodes: dict[int, Node], edges: dict[int, set[int]],
                 time_pairs: set[frozenset] | None = None) -> None:
        self.nodes = nodes
        self.edges = edges
        self.time_pairs = time_pairs or set()

    def is_time(self, a: int, b: int) -> bool:
        return frozenset((a, b)) in self.time_pairs

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "LinkGraph":
        rows = conn.execute(
            "SELECT id, code, prompt, answer, kind, links_json, extra_json FROM items "
            "WHERE track = 'en' AND archived = 0").fetchall()
        by_code = {r["code"]: r["id"] for r in rows}
        nodes = {r["id"]: Node(r["id"], r["code"], r["prompt"], r["answer"], r["kind"]) for r in rows}
        edges: dict[int, set[int]] = {i: set() for i in nodes}
        time_pairs: set[frozenset] = set()
        for r in rows:
            for code in json.loads(r["links_json"]):
                other = by_code.get(code)
                if other is not None and other != r["id"]:
                    edges[r["id"]].add(other)
                    edges[other].add(r["id"])
            for code in json.loads(r["extra_json"] or "{}").get("time_links", []):
                other = by_code.get(code)
                if other is not None and other != r["id"]:
                    edges[r["id"]].add(other)
                    edges[other].add(r["id"])
                    time_pairs.add(frozenset((r["id"], other)))
        return cls(nodes, edges, time_pairs)

    def neighbors(self, item_id: int) -> list[Node]:
        return sorted((self.nodes[i] for i in self.edges.get(item_id, ())), key=lambda n: n.code)

    def answers_to(self, item_id: int) -> list[Node]:
        """Для вопроса — связанные утверждения и краткие ответы: чем на него можно ответить."""
        return [n for n in self.neighbors(item_id) if phrase_kind(n.target) != QUESTION]
