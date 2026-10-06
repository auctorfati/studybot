"""Меню «Правила»: обзорные страницы справочника, грамматические заметки
по блокам и словарь по блокам. Длинный текст делит на сообщения слой Telegram.

Заметка относится к блоку, где на неё впервые ссылается единица;
встреченные (показанные после знакомства с фразой) отмечены. Кнопки:
rl — корень; rl:p, rl:p:С1 — страницы; rl:n, rl:n:1, rl:n:1:6 — заметки;
rl:v, rl:v:1, rl:v:1:6 — словарь.
"""

from __future__ import annotations

import json
import sqlite3

from .ui import Button, Reply

ROW = 4
TITLE_MAX = 40


def _rows(buttons: list[Button], width: int = ROW) -> list[list[Button]]:
    return [buttons[i:i + width] for i in range(0, len(buttons), width)]


def _short(text: str) -> str:
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1].rstrip() + "…"


def root(conn: sqlite3.Connection) -> Reply:
    pages = conn.execute("SELECT count(*) FROM ref_pages").fetchone()[0]
    notes = conn.execute("SELECT count(*) FROM notes WHERE track = 'en'").fetchone()[0]
    words = conn.execute("SELECT count(*) FROM vocab").fetchone()[0]
    text = (f"Правила английского. Обзорных страниц {pages}, заметок {notes}, "
            f"слов и сочетаний в словаре {words}.")
    if not pages and not words:
        text += " Справочник ещё не загружен: пришли «Английский_Справочник.md» документом после банков."
    return Reply(text, [[Button("Обзорные страницы", "rl:p")],
                        [Button("Заметки по блокам", "rl:n"), Button("Словарь по блокам", "rl:v")]])


def pages_list(conn: sqlite3.Connection) -> Reply:
    rows = conn.execute("SELECT code, title FROM ref_pages ORDER BY sort_key").fetchall()
    if not rows:
        return Reply("Обзорных страниц нет: справочник ещё не загружен.", [[Button("Назад", "rl")]])
    buttons = [[Button(_short(f"{r['code']} — {r['title']}"), f"rl:p:{r['code']}")] for r in rows]
    return Reply("Обзорные страницы:", buttons + [[Button("Назад", "rl")]])


def page(conn: sqlite3.Connection, code: str) -> Reply:
    rows = conn.execute("SELECT code, title, body FROM ref_pages ORDER BY sort_key").fetchall()
    codes = [r["code"] for r in rows]
    if code not in codes:
        return pages_list(conn)
    k = codes.index(code)
    r = rows[k]
    nav = []
    if k > 0:
        nav.append(Button(f"← {codes[k - 1]}", f"rl:p:{codes[k - 1]}"))
    if k + 1 < len(codes):
        nav.append(Button(f"{codes[k + 1]} →", f"rl:p:{codes[k + 1]}"))
    buttons = ([nav] if nav else []) + [[Button("К страницам", "rl:p")]]
    return Reply(f"{r['code']} — {r['title']}\n\n{r['body']}", buttons)


# заметки

def note_blocks(conn: sqlite3.Connection) -> dict[tuple[str, str], list[str]]:
    """(модуль, блок) → коды заметок по порядку первой ссылки; без ссылок — блок «—»."""
    out: dict[tuple[str, str], list[str]] = {}
    placed: set[str] = set()
    for r in conn.execute("SELECT area, unit, notes_json FROM items WHERE track = 'en' AND archived = 0 "
                          "ORDER BY area, sort_key"):
        for c in json.loads(r["notes_json"]):
            if c not in placed:
                placed.add(c)
                out.setdefault(((r["area"] or "EN1")[2:], r["unit"]), []).append(c)
    rest = [r[0] for r in conn.execute("SELECT code FROM notes WHERE track = 'en' ORDER BY "
                                       "CAST(substr(code, 2) AS INTEGER)") if r[0] not in placed]
    if rest:
        out[("—", "—")] = rest
    return out


def _modules(keys) -> list[str]:
    mods = {m for m, _ in keys if m != "—"}
    return sorted(mods, key=int)


def _block_buttons(prefix: str, module: str, blocks: list[str], back: str) -> list[list[Button]]:
    buttons = [Button(f"Блок {b}", f"{prefix}:{module}:{b}") for b in sorted(blocks, key=int)]
    return _rows(buttons) + [[Button("Назад", back)]]


def notes_root(conn: sqlite3.Connection, module: str | None = None) -> Reply:
    nb = note_blocks(conn)
    if not nb:
        return Reply("Заметок нет: банки английского ещё не загружены.", [[Button("Назад", "rl")]])
    mods = _modules(nb)
    if module is None and len(mods) > 1:
        return Reply("Заметки: выбери модуль.",
                     _rows([Button(f"Модуль {m}", f"rl:n:{m}") for m in mods]) + [[Button("Назад", "rl")]])
    module = module or mods[0]
    blocks = [b for m, b in nb if m == module]
    back = "rl:n" if len(mods) > 1 else "rl"
    text = f"Заметки модуля {module}: выбери блок. Заметка стоит в блоке, где на неё впервые ссылается фраза."
    buttons = _block_buttons("rl:n", module, blocks, back)
    if ("—", "—") in nb:
        buttons.insert(-1, [Button("Без ссылок из фраз", "rl:n:—:—")])
    return Reply(text, buttons)


def notes_block(conn: sqlite3.Connection, module: str, block: str) -> Reply:
    codes = note_blocks(conn).get((module, block), [])
    if not codes:
        return notes_root(conn, module if module != "—" else None)
    marks = ",".join("?" * len(codes))
    found = {r["code"]: r for r in conn.execute(
        f"SELECT code, title, body, shown_at FROM notes WHERE track = 'en' AND code IN ({marks})", codes)}
    head = (f"Заметки модуля {module}, блок {block}." if module != "—"
            else "Заметки без ссылок из фраз.")
    parts = [head]
    for c in codes:
        n = found.get(c)
        if n is None:
            continue
        seen = "встречена" if n["shown_at"] else "впереди"
        parts.append(f"{n['code']} — {n['title']} ({seen})\n{n['body']}")
    back = f"rl:n:{module}" if module != "—" else "rl:n"
    return Reply("\n\n".join(parts), [[Button("К блокам", back)]])


# словарь

def vocab_root(conn: sqlite3.Connection, module: str | None = None) -> Reply:
    keys = [(r[0], r[1]) for r in conn.execute("SELECT DISTINCT module, block FROM vocab")]
    if not keys:
        return Reply("Словаря нет: справочник ещё не загружен.", [[Button("Назад", "rl")]])
    mods = _modules(keys)
    if module is None and len(mods) > 1:
        return Reply("Словарь: выбери модуль.",
                     _rows([Button(f"Модуль {m}", f"rl:v:{m}") for m in mods]) + [[Button("Назад", "rl")]])
    module = module or mods[0]
    blocks = [b for m, b in keys if m == module]
    back = "rl:v" if len(mods) > 1 else "rl"
    return Reply(f"Словарь модуля {module}: выбери блок. В блоке — слова, которых не было раньше.",
                 _block_buttons("rl:v", module, blocks, back))


def vocab_block(conn: sqlite3.Connection, module: str, block: str) -> Reply:
    rows = conn.execute("SELECT word, translation, where_code, note FROM vocab WHERE module = ? AND block = ? "
                        "ORDER BY sort_key", (module, block)).fetchall()
    if not rows:
        return vocab_root(conn, module)
    lines = [f"Словарь модуля {module}, блок {block} — {len(rows)}."]
    for r in rows:
        line = f"{r['word']} — {r['translation']} ({r['where_code']})"
        if r["note"]:
            line += f". {r['note']}"
        lines.append(line)
    return Reply("\n".join(lines), [[Button("К блокам", f"rl:v:{module}")]])


def route(conn: sqlite3.Connection, arg: str) -> Reply:
    """arg — часть callback после «rl»: '', 'p', 'p:С1', 'n', 'n:1', 'n:1:6', 'v', …"""
    parts = [p for p in arg.split(":") if p] if arg else []
    if not parts:
        return root(conn)
    head, rest = parts[0], parts[1:]
    if head == "p":
        return page(conn, rest[0]) if rest else pages_list(conn)
    if head == "n":
        if len(rest) >= 2:
            return notes_block(conn, rest[0], rest[1])
        return notes_root(conn, rest[0] if rest else None)
    if head == "v":
        if len(rest) >= 2:
            return vocab_block(conn, rest[0], rest[1])
        return vocab_root(conn, rest[0] if rest else None)
    return root(conn)
