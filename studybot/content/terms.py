"""Контекст единицы на узнавание модуля 5: абзац статьи вокруг предложения (кнопка «Контекст»).

«Ист» — «PATO S10, с. 3»: код области, по нему корпус, дальше код источника и страница
(банк модуля 5, раздел 1). Абзац ищется по началу предложения; не найден — начало страницы.
"""

from __future__ import annotations

import re

from .sources import PageRef, SourceLibrary, parse_source

REF_RE = re.compile(r"^([A-Z]{2,6})\s+(.+)$")
CONTEXT_LIMIT = 1500


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def build_context(source: str | None, sentence: str, library: SourceLibrary) -> tuple[str | None, str | None]:
    """(абзац, проблема). Проблема — текст для отчёта, если страницу не нашли."""
    if not source:
        return None, "нет ссылки «Ист»"
    m = REF_RE.match(source.strip())
    if not m:
        return None, f"«Ист» {source!r}: нужен код области и ссылка, например «PATO S10, с. 3»"
    area, rest = m.group(1), m.group(2)
    parsed = parse_source(rest)
    if parsed.errors or not parsed.refs:
        return None, f"«Ист» {source!r} не разобрана"
    ref = parsed.refs[0]
    if not isinstance(ref, PageRef):
        return None, f"«Ист» {source!r} не ссылается на страницу"
    pages, why = library.pages_for(area, ref.alias)
    if pages is None:
        return None, why
    page = pages.get(ref.pages[0])
    if not page:
        return None, f"{area} {ref.alias}: нет страницы {ref.pages[0]}"
    head = _norm(sentence.split("…")[0])[:60]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", page) if p.strip()]
    for k, p in enumerate(paragraphs):
        if head and head[:40] in _norm(p):
            text = p
            if len(text) < 300 and k + 1 < len(paragraphs):
                text += "\n\n" + paragraphs[k + 1]
            return text[:CONTEXT_LIMIT], None
    return page[:CONTEXT_LIMIT], "предложение на странице не найдено — показывается начало страницы"
