"""Постраничный текст из PDF издателя (один раз на книгу).

Нужен pdftotext из poppler-utils: он даёт чистый текст с правильными
переносами. Номер страницы — страница файла; у Зейгарник она совпадает
с печатной. Строка с номером страницы (колонтитул) убирается.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


class ExtractError(RuntimeError):
    pass


def extract_pages(pdf: Path, out: Path, title: str) -> int:
    if shutil.which("pdftotext") is None:
        raise ExtractError("нет pdftotext: установите poppler-utils")
    res = subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), "-"],
                         capture_output=True, check=False)
    if res.returncode != 0:
        raise ExtractError(res.stderr.decode("utf-8", "replace").strip())
    pages = res.stdout.decode("utf-8").split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    parts = [f"# {title}", "",
             f"Постраничный текст из {pdf.name}. Маркер [с. N] стоит перед текстом страницы N файла.", ""]
    for n, text in enumerate(pages, start=1):
        lines = text.split("\n")
        # номер страницы среди первых строк — колонтитул
        for i, line in enumerate(lines[:4]):
            if line.strip() == str(n):
                del lines[i]
                break
        body = re.sub(r"\n{3,}", "\n\n", "\n".join(l.rstrip() for l in lines)).strip()
        parts += [f"[с. {n}]", "", body, ""]
    out.write_text("\n".join(parts), encoding="utf-8")
    return len(pages)
