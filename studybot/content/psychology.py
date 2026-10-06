"""Разбор банков психологии и карт тем.

Область (PATO, PSY, …) берётся из имени файла: PATO_Банк_…, PSY_Карта_тем.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..enums import ItemKind, PSY_LEVEL
from .common import Issues, short_hash

AREA_RE = re.compile(r"^([A-Z]{2,6})[_\- ]")
CODE_RE = re.compile(r"^([А-ЯЁ]{1,4})-([1-4])-(\d{2})(?:\.\s+(.+?))?\s*$")
TOPIC_CODE_RE = re.compile(r"^[А-ЯЁ]{1,3}\d{2}$")
THEME_RE = re.compile(r"^Тема:\s*(.+?)\.\s+Статус:\s*(ядро|резерв)\.(?:\s+Тип:\s*(.+?)\.)?\s*$")
# «## Уровень 1. …» или «## Блок Б — Уровень 1. …» (файлы на два блока).
LEVEL_SECTION_RE = re.compile(r"^## (?:Блок [А-ЯЁ]\s*[—–-]\s*)?Уровень ([1-4])\b")
QUESTION_RE = re.compile(r"^(\d+)\.\s+(.+)$")

TYPES = {"карточка": ItemKind.CARD, "открытый ответ": ItemKind.OPEN,
         "различение": ItemKind.DISTINCTION}

# Поля позиции: префикс строки → имя поля.
FIELDS = [
    ("О (ключ):", "answer"), ("О:", "answer"), ("В:", "prompt"), ("Ист:", "source"),
    ("Виньетка.", "vignette"), ("Вопросы.", "questions"), ("Ключ.", "key"),
    ("Угол наставника.", "mentor"),
]


def area_from_name(file_name: str) -> str | None:
    m = AREA_RE.match(file_name)
    return m.group(1) if m else None


@dataclass
class PsyItem:
    code: str
    grp: str
    level: int
    kind: ItemKind
    topics: list[str]
    is_core: bool
    prompt: str
    answer: str
    source: str
    extra: dict
    sort_key: int

    @property
    def prompt_hash(self) -> str:
        return short_hash(self.prompt, self.extra.get("questions"), self.topics, self.kind.value)

    @property
    def answer_hash(self) -> str:
        return short_hash(self.answer, self.extra.get("mentor"), self.source)


@dataclass
class ParsedPsy:
    area: str
    items: list[PsyItem] = field(default_factory=list)
    issues: Issues = field(default_factory=Issues)

    @property
    def groups(self) -> set[str]:
        return {it.grp for it in self.items}


def parse_psy_bank(text: str, area: str) -> ParsedPsy:
    parsed = ParsedPsy(area=area)
    iss = parsed.issues
    lines = text.split("\n")

    section_level: int | None = None
    current: tuple[str, int, list[str]] | None = None
    seen: set[str] = set()

    def flush() -> None:
        nonlocal current
        if current is not None:
            header, lvl, body = current
            _parse_item(parsed, header, lvl, body, seen)
            current = None

    for line in lines:
        if line.startswith("### "):
            flush()
            if section_level is not None:
                current = (line[4:].strip(), section_level, [])
            continue
        if line.startswith("#"):
            flush()
            if line.startswith("## "):
                m = LEVEL_SECTION_RE.match(line)
                section_level = int(m.group(1)) if m else None
            elif line.startswith("# "):
                section_level = None
            continue
        if current is not None:
            current[2].append(line)
    flush()

    if not parsed.items:
        iss.error("файл", "не найдено ни одной позиции под разделами «## Уровень N»")
    return parsed


def _parse_item(parsed: ParsedPsy, header: str, section_level: int,
                body: list[str], seen: set[str]) -> None:
    iss = parsed.issues
    m = CODE_RE.match(header)
    if not m:
        iss.error(header[:30], "заголовок позиции должен быть кодом вида Д-1-01")
        return
    code, grp, level = f"{m.group(1)}-{m.group(2)}-{m.group(3)}", m.group(1), int(m.group(2))
    title = m.group(4)
    if code in seen:
        iss.error(code, "повтор кода")
        return
    seen.add(code)
    if level != section_level:
        iss.error(code, f"уровень в коде {level}, а позиция в разделе «Уровень {section_level}»")

    fields: dict[str, str] = {}
    questions: list[str] = []
    theme: re.Match | None = None
    cur: str | None = None
    for raw in body:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Тема:"):
            theme = THEME_RE.match(line)
            if not theme:
                iss.error(code, "строка «Тема» не по формату «Тема: П16. Статус: ядро. Тип: карточка.»")
            cur = None
            continue
        name = None
        for prefix, fname in FIELDS:
            if line.startswith(prefix):
                name, rest = fname, line[len(prefix):].strip()
                break
        if name is not None:
            if name in fields:
                iss.error(code, f"поле «{prefix}» встречается дважды")
            fields[name] = rest
            cur = name
            continue
        if cur == "questions":
            qm = QUESTION_RE.match(line)
            if qm:
                questions.append(qm.group(2).strip())
            elif questions:
                questions[-1] += " " + line
            else:
                iss.error(code, "в «Вопросах» нужен нумерованный список")
            continue
        if cur is None:
            iss.error(code, f"строка вне полей: {line[:40]!r}")
            continue
        fields[cur] = (fields[cur] + "\n" + line).strip()

    if theme is None:
        if not any(l.strip().startswith("Тема:") for l in body):
            iss.error(code, "нет строки «Тема»")
        return
    topics = [t.strip() for t in theme.group(1).split(",")]
    bad = [t for t in topics if not TOPIC_CODE_RE.match(t)]
    if bad:
        iss.error(code, f"неверный код темы: {', '.join(bad)}")
    type_word = theme.group(3)

    if level == 4:
        if type_word:
            iss.error(code, "у виньетки поле «Тип» не ставится")
        kind = ItemKind.VIGNETTE
        required = {"vignette": "Виньетка.", "key": "Ключ.", "source": "Ист:"}
        prompt, answer = fields.get("vignette", ""), fields.get("key", "")
        if not questions:
            iss.error(code, "нет вопросов к виньетке")
        if not fields.get("mentor"):
            iss.warn(code, "нет угла наставника")
        stray = {"prompt", "answer"} & fields.keys()
    else:
        kind = TYPES.get(type_word or "")
        if kind is None:
            iss.error(code, f"неизвестный тип {type_word!r}")
            return
        if PSY_LEVEL[kind] != level:
            iss.error(code, f"тип «{type_word}» не соответствует уровню {level}")
        required = {"prompt": "В:", "answer": "О:", "source": "Ист:"}
        prompt, answer = fields.get("prompt", ""), fields.get("answer", "")
        stray = {"vignette", "questions", "key", "mentor"} & fields.keys()
    for fname, label in required.items():
        if not fields.get(fname):
            iss.error(code, f"нет поля «{label}»")
    for fname in stray:
        iss.error(code, f"поле {fname} не бывает у позиции уровня {level}")

    extra: dict = {}
    if title:
        extra["title"] = title
    if kind is ItemKind.VIGNETTE:
        extra["questions"] = questions
        if fields.get("mentor"):
            extra["mentor"] = fields["mentor"]

    parsed.items.append(PsyItem(
        code=code, grp=grp, level=level, kind=kind, topics=topics,
        is_core=theme.group(2) == "ядро", prompt=prompt, answer=answer,
        source=fields.get("source", ""), extra=extra, sort_key=len(parsed.items) + 1,
    ))


# Карта тем

TOPIC_START_RE = re.compile(r"^(?:\*\*)?([А-ЯЁ]{1,3}\d{2})\.\s+(.*)$")
TOPIC_BLOCK_RE = re.compile(r"^## Блок ([А-ЯЁ])\.\s")
TOPIC_LEVEL_RE = re.compile(r"Уровень:?\s*([1-4])\b")


@dataclass
class Topic:
    code: str
    grp: str
    title: str
    target_level: int
    sort_key: int


@dataclass
class ParsedTopics:
    area: str
    topics: list[Topic] = field(default_factory=list)
    issues: Issues = field(default_factory=Issues)


def parse_topic_map(text: str, area: str) -> ParsedTopics:
    parsed = ParsedTopics(area=area)
    iss = parsed.issues
    grp: str | None = None
    cur: list | None = None   # [code, grp, first_line, body_lines]
    bold_open = False         # заголовок темы в ** ** перенесён на следующую строку

    def flush() -> None:
        nonlocal cur
        if cur is None:
            return
        code, g, first, body = cur
        cur = None
        if "**" in first:
            tm = re.match(r"^(.*?)\*\*", first)
            title = tm.group(1) if tm else first
        else:
            tm = re.match(r"^(.*?)\.\s+В\d", first)
            title = tm.group(1) if tm else first.split(". ")[0]
        title = title.strip().rstrip(".")
        levels = TOPIC_LEVEL_RE.findall("\n".join([first] + body))
        if not levels:
            iss.error(code, "у темы нет целевого уровня")
            return
        if any(t.code == code for t in parsed.topics):
            iss.error(code, "тема встречается дважды")
            return
        parsed.topics.append(Topic(code, g, title, int(levels[-1]), len(parsed.topics) + 1))

    for line in text.split("\n"):
        if line.startswith("#"):
            flush()
            bm = TOPIC_BLOCK_RE.match(line)
            grp = bm.group(1) if bm else None
            continue
        if grp is None:
            continue
        sm = TOPIC_START_RE.match(line)
        if bold_open and cur is not None:
            cur[2] += " " + line.strip()
            bold_open = "**" not in line
            continue
        if sm:
            flush()
            cur = [sm.group(1), grp, sm.group(2), []]
            bold_open = line.startswith("**") and "**" not in sm.group(2)
        elif cur is not None:
            if line.strip() and not line.startswith("- ") and not line.startswith("  "):
                flush()   # абзац после списка темы («Угол наставника для блока…»)
            else:
                cur[3].append(line)
    flush()
    if not parsed.topics:
        iss.error("файл", "не найдено ни одной темы под разделами «## Блок X.»")
    return parsed
