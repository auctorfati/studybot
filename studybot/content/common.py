"""Общее для разборщиков: сообщения, хэши, чистка текста."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

EMPTY_CELLS = {"", "—", "–", "-"}
DASHES = "–—-"


@dataclass
class Issue:
    """Ошибка формата или предупреждение с местом в файле."""
    where: str          # код позиции или «строка 57»
    text: str

    def __str__(self) -> str:
        return f"{self.where}: {self.text}"


@dataclass
class Issues:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    def error(self, where: str, text: str) -> None:
        self.errors.append(Issue(where, text))

    def warn(self, where: str, text: str) -> None:
        self.warnings.append(Issue(where, text))

    def extend(self, other: "Issues") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)


def clean_text(text: str) -> str:
    """NFC, неразрывные пробелы, переводы строк Windows."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\u00a0", " ").replace("\ufeff", "")


def cell(value: str) -> str:
    value = value.strip()
    return "" if value in EMPTY_CELLS else value


def short_hash(*parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_pages(spec: str) -> list[int]:
    """«5, 13, 17–19» → [5, 13, 17, 18, 19]. Ошибка — ValueError."""
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError("пустой номер страницы")
        m = re.fullmatch(rf"(\d+)\s*[{DASHES}]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                raise ValueError(f"диапазон {part} наоборот")
            pages.extend(range(a, b + 1))
        elif part.isdigit():
            pages.append(int(part))
        else:
            raise ValueError(f"не номер страницы: {part!r}")
    return pages
