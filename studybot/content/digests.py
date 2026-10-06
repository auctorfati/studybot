"""Разбор файлов разборов тем психологии.

Файл «<ОБЛАСТЬ>_Разборы_Блок_<буква>.md», заголовок «# Разборы тем. …». Тема —
«## ПС30 — название», части — «### 1. Коротко», текст до следующего заголовка.
Последняя строка части «Позиции: код, код» — позиции банка, которые часть
объясняет (необязательна). Код позиции — не больше чем в одной части темы.

Ошибка (тема отклоняется): кода темы нет среди тем загруженных карт; нет частей;
номера частей не по порядку. Предупреждения: первая часть не «Коротко», последняя
не «Где читать», нет обязательной части; часть длиннее 3 500 знаков; позиция не
найдена или из другой темы; позиция в двух частях; ссылка в скобках не разобрана
или страница не найдена. Модели разбор не передаётся, фрагменты не готовятся.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .common import Issues
from .sources import PageRef, SourceLibrary, parse_source

TITLE = "# Разборы тем"
TOPIC_RE = re.compile(r"^## (\S+) — (.+?)\s*$")
PART_RE = re.compile(r"^### (\d+)\. (.+?)\s*$")
POS_RE = re.compile(r"^Позиции:\s*(.+?)\.?\s*$")
REF_IN_TEXT_RE = re.compile(r"\(([^()]*?(?:[NSK]\d{2}|Зейгарник|CDDR|ПООП)[^()]*?с\.\s*\d[^()]*)\)")
PART_LIMIT = 3500
FIRST, LAST = "Коротко", "Где читать"
REQUIRED = ("Чем отличается от соседнего", "Пример из практики", "Для наставника")


@dataclass
class DigestPart:
    num: int
    title: str
    body: str
    positions: list[str]


@dataclass
class Digest:
    code: str
    title: str
    parts: list[DigestPart] = field(default_factory=list)
    line: int = 0


@dataclass
class ParsedDigests:
    area: str
    digests: list[Digest] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)     # (код темы, причина)
    issues: Issues = field(default_factory=Issues)


def is_digests(text: str) -> bool:
    first = next((l for l in text.split("\n") if l.startswith("# ")), "")
    return first.startswith(TITLE)


def _split(text: str) -> list[Digest]:
    out: list[Digest] = []
    part: list | None = None
    for no, line in enumerate(text.split("\n"), start=1):
        tm, pm = TOPIC_RE.match(line), PART_RE.match(line)
        if line.startswith("## ") and not line.startswith("### "):
            if tm:
                out.append(Digest(tm.group(1), tm.group(2), line=no))
                part = None
            else:
                out.append(Digest("", line[3:].strip(), line=no))
                part = None
            continue
        if pm and out:
            part = [int(pm.group(1)), pm.group(2), []]
            out[-1].parts.append(part)          # временно список, ниже — DigestPart
            continue
        if part is not None:
            part[2].append(line)
    for d in out:
        parts = []
        for num, title, lines in d.parts:
            while lines and not lines[-1].strip():
                lines.pop()
            positions: list[str] = []
            if lines:
                m = POS_RE.match(lines[-1].strip())
                if m:
                    positions = [c.strip() for c in m.group(1).split(",") if c.strip()]
                    lines.pop()
            parts.append(DigestPart(num, title, "\n".join(lines).strip(), positions))
        d.parts = parts
    return out


def parse_digests(text: str, area: str, topics: dict[str, str], items: dict[str, str],
                  library: SourceLibrary | None = None) -> ParsedDigests:
    """topics — код темы → область по загруженным картам; items — код позиции → её тема."""
    out = ParsedDigests(area)
    iss = out.issues
    if not is_digests(text):
        iss.error("заголовок", f"первая строка должна начинаться с «{TITLE}»")
        return out
    pages_cache: dict[str, tuple[dict[int, str] | None, str]] = {}
    for d in _split(text):
        where = d.code or f"строка {d.line}"
        if not d.code:
            out.rejected.append((where, "заголовок темы должен быть «## КОД — название»"))
            continue
        if d.code not in topics:
            out.rejected.append((d.code, "темы нет в загруженных картах тем"))
            continue
        if topics[d.code] != area:
            out.rejected.append((d.code, f"тема из области {topics[d.code]}, файл — {area}"))
            continue
        if not d.parts:
            out.rejected.append((d.code, "нет частей «### 1. …»"))
            continue
        nums = [p.num for p in d.parts]
        if nums != list(range(1, len(nums) + 1)):
            out.rejected.append((d.code, "номера частей не по порядку с 1"))
            continue
        titles = [p.title for p in d.parts]
        if titles[0] != FIRST:
            iss.warn(d.code, f"первая часть должна быть «{FIRST}»")
        if titles[-1] != LAST:
            iss.warn(d.code, f"последняя часть должна быть «{LAST}»")
        for req in REQUIRED:
            if req not in titles:
                iss.warn(d.code, f"нет части «{req}»")
        seen: dict[str, int] = {}
        for p in d.parts:
            if not p.body:
                iss.warn(d.code, f"часть {p.num} пустая")
            if len(p.body) > PART_LIMIT:
                iss.warn(d.code, f"часть {p.num} длиннее {PART_LIMIT} знаков ({len(p.body)})")
            for c in p.positions:
                if c in seen and seen[c] != p.num:
                    iss.warn(d.code, f"позиция {c} — в частях {seen[c]} и {p.num}")
                seen.setdefault(c, p.num)
                if c not in items:
                    iss.warn(d.code, f"позиции {c} нет в загруженных банках")
                elif items[c] != d.code:
                    iss.warn(d.code, f"позиция {c} из темы {items[c]}")
            if library is not None:
                for m in REF_IN_TEXT_RE.finditer(p.body):
                    src = parse_source(m.group(1))
                    for e in src.errors:
                        iss.warn(d.code, f"часть {p.num}: {e}")
                    for ref in src.refs:
                        if not isinstance(ref, PageRef):
                            continue
                        if ref.alias not in pages_cache:
                            pages_cache[ref.alias] = library.pages_for(area, ref.alias)
                        pages, why = pages_cache[ref.alias]
                        if pages is None:
                            if why.startswith("в "):    # корпус есть, раздела нет — ошибка ссылки
                                iss.warn(d.code, f"часть {p.num}: {ref.alias} — {why}")
                            continue                    # источника нет на сервере — как у банков
                        missing = [n for n in ref.pages if n not in pages]
                        if missing:
                            iss.warn(d.code, f"часть {p.num}: {ref.alias}, с. "
                                             f"{', '.join(map(str, missing))} — страница не найдена")
        out.digests.append(d)
    if not out.digests and not out.rejected:
        iss.error("файл", "не найдено ни одной темы «## КОД — название»")
    return out
