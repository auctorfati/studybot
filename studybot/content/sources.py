"""Источники и фрагменты для проверки ответов моделью.

Все источники — md-файлы с маркерами страниц «[с. N]» на отдельной строке:
корпуса областей (разделы «## S04. …»), постраничный текст Зейгарник,
клинические описания МКБ-11. Папка sources_dir раскладывается по областям,
файлы находятся по шаблонам имён, поэтому новая версия корпуса кладётся
рядом и подхватывается без правки кода.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .common import clean_text, parse_pages

FRAGMENT_LIMIT = 6000

MARKER_RE = re.compile(r"^\[с\.\s*(\d+)\]\s*$")
SECTION_RE = re.compile(r"^## ([NSK]\d{2})\.\s")
CORPUS_VERSION_RE = re.compile(r"_v(\d+)(?:[._](\d+))?", re.I)

# Ссылка на страницы: «S04, с. 381–383», «K01, с. 56», «Зейгарник, с. 230», «CDDR, с. 691, 693»,
# «ПООП, с. 109». Коды K, N и S — разделы корпуса области.
PAGE_REF_RE = re.compile(r"^(Зейгарник|CDDR|ПООП|[NSK]\d{2}),\s*с\.\s*([\d\s,–—-]+)$")
# Ссылка на позицию другого банка: «PATO, Д-3-04» или «PATO, И-4-01, Д-3-04».
ITEM_REF_RE = re.compile(r"^([A-Z]{2,6}),\s*((?:[А-ЯЁ]{1,4}-\d-\d{2})(?:,\s*[А-ЯЁ]{1,4}-\d-\d{2})*)$")

# Отдельные книги и документы: псевдоним в банке → шаблон имени файла.
BOOKS = {
    "Зейгарник": "*Zeigarnik*.pages.md",
    "CDDR": "*ICD11-CDDR*.md",
    "ПООП": "*POOP*.md",          # выписка ПООП 37.05.01 (KLIN-N-FUMO-2021-POOP-vypiska.md)
}


@dataclass(frozen=True)
class PageRef:
    alias: str            # 'S04', 'Зейгарник', 'CDDR'
    pages: tuple[int, ...]

    @property
    def label(self) -> str:
        return f"{self.alias}, с. {_compact(self.pages)}"


@dataclass(frozen=True)
class ItemRef:
    area: str
    codes: tuple[str, ...]


@dataclass
class ParsedSource:
    refs: list[PageRef | ItemRef] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)    # «сверка по учебнику во втором проходе»
    errors: list[str] = field(default_factory=list)


def _compact(pages: tuple[int, ...]) -> str:
    out, i = [], 0
    while i < len(pages):
        j = i
        while j + 1 < len(pages) and pages[j + 1] == pages[j] + 1:
            j += 1
        out.append(str(pages[i]) if i == j else f"{pages[i]}–{pages[j]}")
        i = j + 1
    return ", ".join(out)


def parse_source(text: str) -> ParsedSource:
    """Разбор поля «Ист». Скобки — перекрёстные пометки, источником не считаются."""
    result = ParsedSource()
    body = text.strip().rstrip(".").strip()
    for part in body.split(";"):
        part = re.sub(r"\([^)]*\)", "", part).strip().rstrip(".").strip()
        if not part:
            continue
        pm = PAGE_REF_RE.match(part)
        if pm:
            try:
                pages = parse_pages(pm.group(2))
            except ValueError as exc:
                result.errors.append(f"{part}: {exc}")
                continue
            result.refs.append(PageRef(pm.group(1), tuple(pages)))
            continue
        im = ITEM_REF_RE.match(part)
        if im:
            codes = tuple(c.strip() for c in im.group(2).split(","))
            result.refs.append(ItemRef(im.group(1), codes))
            continue
        if re.search(r"[NSK]\d{2}|Зейгарник|CDDR|ПООП|\bс\.\s*\d", part):
            result.errors.append(f"не разобрана ссылка: {part!r}")
        else:
            result.notes.append(part)
    return result


def clean_page(text: str) -> str:
    lines = [l.rstrip() for l in text.strip("\n").split("\n")]
    out: list[str] = []
    for l in lines:
        if not l.strip():
            if out and out[-1] != "":
                out.append("")
        else:
            out.append(l.strip())
    return "\n".join(out).strip()


def split_pages(text: str) -> dict[int, str]:
    """Текст с маркерами [с. N] → {N: текст страницы}. Повтор номера склеивается."""
    pages: dict[int, str] = {}
    current: int | None = None
    buf: list[str] = []

    def put() -> None:
        if current is not None:
            chunk = clean_page("\n".join(buf))
            pages[current] = (pages[current] + "\n" + chunk).strip() if current in pages else chunk

    for line in text.split("\n"):
        m = MARKER_RE.match(line.strip())
        if m:
            put()
            current, buf = int(m.group(1)), []
        else:
            buf.append(line)
    put()
    return pages


def split_corpus(text: str) -> dict[str, dict[int, str]]:
    """Корпус → {код источника: страницы}. Текст до первого маркера раздела — описание, пропускается."""
    sections: dict[str, list[str]] = {}
    code: str | None = None
    for line in text.split("\n"):
        m = SECTION_RE.match(line)
        if m:
            code = m.group(1)
            sections[code] = []
        elif line.startswith("## "):
            code = None
        elif code is not None:
            sections[code].append(line)
    return {c: split_pages("\n".join(body)) for c, body in sections.items()}


def _corpus_version(path: Path) -> tuple[int, int]:
    m = CORPUS_VERSION_RE.search(path.stem)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


class SourceLibrary:
    """Ленивый доступ к источникам в sources_dir."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._corpora: dict[str, tuple[Path, dict[str, dict[int, str]]] | None] = {}
        self._books: dict[str, tuple[Path, dict[int, str]] | None] = {}

    def corpus(self, area: str) -> tuple[Path, dict[str, dict[int, str]]] | None:
        if area not in self._corpora:
            files = sorted(self.root.rglob(f"{area}_korpus*.md"), key=_corpus_version)
            if files:
                path = files[-1]
                self._corpora[area] = (path, split_corpus(clean_text(path.read_text(encoding="utf-8"))))
            else:
                self._corpora[area] = None
        return self._corpora[area]

    def book(self, alias: str) -> tuple[Path, dict[int, str]] | None:
        if alias not in self._books:
            files = sorted(self.root.rglob(BOOKS[alias])) if alias in BOOKS else []
            if files:
                path = files[-1]
                self._books[alias] = (path, split_pages(clean_text(path.read_text(encoding="utf-8"))))
            else:
                self._books[alias] = None
        return self._books[alias]

    def pages_for(self, area: str, alias: str) -> tuple[dict[int, str] | None, str]:
        """Страницы источника и пояснение, если не найден."""
        if alias in BOOKS:
            found = self.book(alias)
            if found is None:
                return None, f"нет файла {BOOKS[alias]} в папке источников"
            return found[1], ""
        found = self.corpus(area)
        if found is None:
            return None, f"нет корпуса {area}_korpus*.md в папке источников"
        path, sections = found
        if alias not in sections:
            return None, f"в {path.name} нет раздела {alias}"
        return sections[alias], ""

    def describe(self) -> list[str]:
        """Что найдено — для отчёта импорта и команды «состояние»."""
        out = []
        for p in sorted(self.root.rglob("*_korpus*.md")):
            out.append(p.name)
        for alias, pattern in BOOKS.items():
            for p in sorted(self.root.rglob(pattern)):
                out.append(f"{p.name} ({alias})")
        return out


@dataclass
class Fragment:
    status: str            # 'ok', 'truncated', 'missing', 'n/a' — источника нет: проверка по ключу
    text: str | None
    problems: list[str] = field(default_factory=list)


def build_fragment(area: str, source_field: str, library: SourceLibrary,
                   item_fragments: dict[tuple[str, str], str | None] | None = None,
                   limit: int = FRAGMENT_LIMIT, query: str = "") -> Fragment:
    """Собрать фрагмент по полю «Ист».

    Если полный текст страниц длиннее предела, он сжимается по ключу: остаются
    предложения, ближе всего к вопросу и ответу (query). Любая ненайденная страница —
    статус missing: позиция импортируется, но не показывается.
    Перекрёстная ссылка на позицию другого банка («PATO, Д-3-04») фрагмента не даёт: фрагмент берётся из остальных ссылок строки. item_fragments оставлен
    для совместимости вызова и не используется.
    Одна страница, которая сама длиннее предела, берётся целиком: статус ok,
    пометка в problems.
    Источника (корпуса области или отдельной книги) нет в папке источников —
    это не ошибка ссылки: позиция показывается и проверяется по ключу (статус n/a),
    если ни одна ссылка строки не нашлась; найденные ссылки дают фрагмент как обычно.
    Ненайденная страница в имеющемся источнике по-прежнему даёт missing.
    """
    parsed = parse_source(source_field)
    problems = list(parsed.errors)
    if not parsed.refs and not problems:
        problems.append("в поле «Ист» нет ссылки на источник")
    segments: list[tuple[str, str]] = []
    absent: list[str] = []
    for ref in parsed.refs:
        if isinstance(ref, ItemRef):
            continue
        pages, why = library.pages_for(area, ref.alias)
        if pages is None:
            if why.startswith("нет "):        # файла источника нет в папке — не ошибка ссылки
                absent.append(why)
            else:
                problems.append(why)
            continue
        for n in ref.pages:
            if n not in pages or not pages[n].strip():
                problems.append(f"{ref.alias}: нет страницы {n}")
            else:
                segments.append((f"{ref.alias}, с. {n}", pages[n]))
    if not segments and not problems and not absent:
        problems.append("в поле «Ист» нет ссылки на страницы источника")
    if problems:
        return Fragment("missing", None, problems)
    if not segments:
        return Fragment("n/a", None, sorted(set(absent)))

    full = "\n\n".join(f"[{label}]\n{text}" for label, text in segments)
    if len(full) <= limit:
        return Fragment("ok", full)
    if len(segments) == 1:
        return Fragment("ok", full, [f"одна страница длиннее предела ({len(full)} знаков), взята целиком"])
    return Fragment("truncated", compress(segments, query, limit),
                    [f"сжат по ключу: {len(full)} знаков → предел {limit}"])


# Сжатие по ключу

WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z]{4,}")
SENT_RE = re.compile(r"(?<=[.!?…»])\s+(?=[«(А-ЯЁA-Z0-9])")
STOP = {"котор", "также", "этого", "может", "между", "более", "очень", "всего", "когда",
        "тольк", "перед", "через", "после", "этот", "этих", "было", "были", "того", "того",
        "сама", "само", "свои", "своих", "таки", "такж", "имеет", "этой", "этом", "этим"}


def _stems(text: str) -> set[str]:
    out = set()
    for w in WORD_RE.findall(text.lower().replace("ё", "е")):
        st = w[:5]
        if st not in STOP:
            out.add(st)
    return out


def compress(segments: list[tuple[str, str]], query: str, limit: int) -> str:
    """Оставить предложения, ближе всего к вопросу и ключу, в исходном порядке.

    Каждая цитируемая страница получает хотя бы своё лучшее предложение; вокруг
    выбранного предложения берутся соседние, пропуски помечаются «…».
    """
    import math

    sents: list[list[str]] = []
    for _, text in segments:
        flat = re.sub(r"\s*\n\s*", " ", text).strip()
        sents.append([x for x in SENT_RE.split(flat) if x.strip()])
    q = _stems(query)
    all_stems = [[_stems(x) for x in seg] for seg in sents]
    n_sent = sum(len(seg) for seg in sents) or 1
    df: dict[str, int] = {}
    for seg in all_stems:
        for st in seg:
            for w in st & q:
                df[w] = df.get(w, 0) + 1
    idf = {w: math.log(1 + n_sent / c) for w, c in df.items()}
    scored = []
    for si, seg in enumerate(all_stems):
        for i, st in enumerate(seg):
            scored.append((sum(idf.get(w, 0) for w in st & q), si, i))

    budget = limit - sum(len(label) + 6 for label, _ in segments)
    chosen: set[tuple[int, int]] = set()
    used = 0

    def take(si: int, i: int) -> bool:
        nonlocal used
        if (si, i) in chosen or not 0 <= i < len(sents[si]):
            return True
        cost = len(sents[si][i]) + 3
        if used + cost > budget:
            return False
        chosen.add((si, i))
        used += cost
        return True

    # Сначала лучшее предложение каждой страницы (при равенстве — более раннее),
    # затем предложения, близкие к вопросу и ключу, с соседями.
    ranked = sorted(scored, key=lambda t: (-t[0], t[1], t[2]))
    best_per_seg = {}
    for score, si, i in ranked:
        best_per_seg.setdefault(si, i)
    for si, i in best_per_seg.items():
        take(si, i)
    for score, si, i in ranked:
        if score <= 0:
            break
        if not take(si, i):
            continue
        take(si, i - 1)
        take(si, i + 1)
    # Остаток предела — страницы по порядку. Нужен прежде всего для
    # английских источников: русский ключ с ними почти не пересекается, и без
    # заполнения от страницы оставалось одно случайное предложение.
    for si, seg in enumerate(sents):
        for i in range(len(seg)):
            take(si, i)

    out = []
    for si, (label, _) in enumerate(segments):
        idx = sorted(i for s2, i in chosen if s2 == si)
        if not idx:
            continue
        parts, prev = [], None
        for i in idx:
            if prev is not None and i != prev + 1:
                parts.append("…")
            parts.append(sents[si][i])
            prev = i
        if idx[0] > 0:
            parts.insert(0, "…")
        if idx[-1] < len(sents[si]) - 1:
            parts.append("…")
        out.append(f"[{label}]\n" + " ".join(parts))
    return "\n\n".join(out)[:limit]
