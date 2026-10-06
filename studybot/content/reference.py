"""Разбор справочника английского.

Файл `Английский_Справочник.md` с заголовком «# Английский. Справочник».
Обзорные страницы: «### С1 — название», текст до следующего заголовка.
Словари: «## Словарь блока N» (модуль 1) или «## Словарь модуля N, блок M»,
в разделе таблица Слово | Перевод | Где | Примечание. «Где» — код единицы,
в которой слово встретилось впервые: двухчастный в модуле 1 (1.07),
трёхчастный в модулях 2–6 (2.3.15). Строка, чья единица не импортирована,
отклоняется с отчётом; остальное не задевается. Прочие разделы файла
(шапка, «## Обзорные страницы») пропускаются.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .common import Issues, cell

TITLE = "# Английский. Справочник"
PAGE_RE = re.compile(r"^### (С\d+) — (.+?)\s*$")
VOCAB1_RE = re.compile(r"^## Словарь блока (\d+)\s*$")
VOCABN_RE = re.compile(r"^## Словарь модуля (\d+), блок (\d+)\s*$")
WHERE_RE = re.compile(r"^(?:(\d+)\.)?(\d+)\.(\d{2,3})$")
COLUMNS = ["Слово", "Перевод", "Где", "Примечание"]


@dataclass
class RefPage:
    code: str
    title: str
    body: str
    sort_key: int


@dataclass
class VocabRow:
    module: str
    block: str
    word: str
    translation: str
    where: str
    note: str | None
    sort_key: int
    line: int


@dataclass
class ParsedReference:
    pages: list[RefPage] = field(default_factory=list)
    vocab: list[VocabRow] = field(default_factory=list)
    rejected: list[tuple[VocabRow, str]] = field(default_factory=list)   # строка и причина
    issues: Issues = field(default_factory=Issues)

    @property
    def sections(self) -> list[tuple[str, str]]:
        """Модули и блоки словарей, которые есть в файле."""
        return sorted({(v.module, v.block) for v in self.vocab + [r for r, _ in self.rejected]},
                      key=lambda mb: (int(mb[0]), int(mb[1])))


def is_reference(text: str) -> bool:
    first = next((l for l in text.split("\n") if l.startswith("# ")), "")
    return first.startswith(TITLE)


def where_block(code: str) -> tuple[str, str] | None:
    """«1.07» → ('1', '1'); «2.3.15» → ('2', '3'). Не похоже на ID — None."""
    m = WHERE_RE.match(code)
    if not m:
        return None
    return (m.group(1) or "1", m.group(2))


def _split_row(line: str) -> list[str]:
    body = line.strip().strip("|")
    return [c.strip() for c in body.split("|")]


def parse_reference(text: str, known_codes: set[str]) -> ParsedReference:
    """known_codes — коды единиц английского, уже импортированных в бота."""
    out = ParsedReference()
    iss = out.issues
    lines = text.split("\n")
    if not is_reference(text):
        iss.error("заголовок", f"первая строка должна быть «{TITLE}»")
        return out

    page: list | None = None                 # [code, title, строки]
    section: tuple[str, str] | None = None   # словарь (модуль, блок)
    header: list[str] | None = None
    seen_pages: set[str] = set()
    seen_words: dict[tuple[str, str], int] = {}
    order = 0

    def close_page() -> None:
        nonlocal page
        if page is not None:
            body = "\n".join(page[2]).strip()
            if not body:
                iss.error(page[0], "пустая обзорная страница")
            elif page[0] in seen_pages:
                iss.error(page[0], "страница встречается дважды")
            else:
                seen_pages.add(page[0])
                out.pages.append(RefPage(page[0], page[1], body, len(out.pages) + 1))
            page = None

    for no, line in enumerate(lines, start=1):
        if line.startswith("#"):
            close_page()
            header = None
            pm = PAGE_RE.match(line)
            if pm:
                page = [pm.group(1), pm.group(2), []]
                section = None
                continue
            if line.startswith("### С"):
                iss.error(f"строка {no}", "заголовок страницы должен быть «### С1 — название»")
            if line.startswith("## "):
                m1, mn = VOCAB1_RE.match(line), VOCABN_RE.match(line)
                if m1:
                    section = ("1", m1.group(1))
                elif mn:
                    section = (mn.group(1), mn.group(2))
                else:
                    section = None
                    if line.startswith("## Словарь"):
                        iss.error(f"строка {no}", "заголовок словаря должен быть «## Словарь блока N» "
                                                  "или «## Словарь модуля N, блок M»")
            continue
        if page is not None:
            page[2].append(line)
            continue
        if section is None or not line.lstrip().startswith("|"):
            header = None if not line.strip() else header
            continue
        cells = _split_row(line)
        if header is None:
            header = cells
            if header != COLUMNS:
                iss.error(f"строка {no}", f"столбцы словаря должны быть: {' | '.join(COLUMNS)}")
                section = None
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        if len(cells) != len(header):
            iss.error(f"строка {no}", f"в строке {len(cells)} ячеек, в шапке {len(header)}")
            continue
        row = {h: cell(v) for h, v in zip(header, cells)}
        order += 1
        v = VocabRow(section[0], section[1], row["Слово"], row["Перевод"], row["Где"],
                     row["Примечание"] or None, order, no)
        if not v.word or not v.translation:
            iss.error(f"строка {no}", "пустое слово или перевод")
            continue
        wb = where_block(v.where)
        if wb is None:
            out.rejected.append((v, f"«Где» {v.where or 'пусто'} не похоже на ID единицы"))
            continue
        if v.where not in known_codes:
            out.rejected.append((v, f"единицы {v.where} нет в импортированных банках"))
            continue
        if wb != section:
            iss.warn(f"строка {no}", f"{v.word}: «Где» {v.where} не из блока словаря "
                                     f"(модуль {section[0]}, блок {section[1]})")
        key = (section[0], v.word.lower())
        if key in seen_words:
            iss.warn(f"строка {no}", f"{v.word}: уже есть в словаре модуля {section[0]} "
                                     f"(строка {seen_words[key]})")
        seen_words.setdefault(key, no)
        out.vocab.append(v)
    close_page()
    if not out.pages and not out.vocab and not out.rejected:
        iss.error("файл", "не найдено ни страниц, ни словарей")
    return out


# слова фразы → строки словаря (для «Почему так?»)

TOKEN_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")


def _stems(word: str) -> set[str]:
    """Простые формы английского слова: -s, -es, -ies, -ed, -d, -ied, -ing, удвоение."""
    w = word.lower()
    out = {w}
    if w.endswith("'s"):
        out.add(w[:-2])
    for suf in ("s", "es", "ed", "d", "ing"):
        if w.endswith(suf) and len(w) - len(suf) >= 2:
            base = w[: -len(suf)]
            out.add(base)
            out.add(base + "e")
            if len(base) >= 3 and base[-1] == base[-2]:
                out.add(base[:-1])
    for suf in ("ies", "ied"):
        if w.endswith(suf) and len(w) > 4:
            out.add(w[:-3] + "y")
    return out


def phrase_matches(entry: str, phrase: str) -> bool:
    """Есть ли слово или сочетание словаря во фразе (с учётом простых окончаний)."""
    words = TOKEN_RE.findall(phrase.lower())
    forms = [_stems(w) for w in words]
    target = TOKEN_RE.findall(entry.lower())
    if not target:
        return False
    n = len(target)
    for i in range(len(words) - n + 1):
        if all(target[k] in forms[i + k] for k in range(n)):
            return True
    return False
