"""SQLite: подключение, миграции, транзакции.

Бот однопользовательский, запросы короткие, поэтому используется синхронный
sqlite3 из стандартной библиотеки: вызовы из обработчиков aiogram занимают
доли миллисекунды и цикл событий не блокируют. Каждое действие записывается
сразу — перезапуск сервиса прогресс не теряет. Журнал WAL
с synchronous = FULL: зафиксированный ответ переживает и падение процесса,
и сбой питания сервера; цена — миллисекунда на запись, для одного
пользователя незаметна.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def connect(path: str | Path) -> sqlite3.Connection:
    """Открыть базу. ':memory:' — для тестов."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Явная транзакция: всё или ничего. Вложенные вызовы идут точками сохранения."""
    if conn.in_transaction:
        name = f"sp_{id(conn)}_{_savepoint_counter()}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


_counter = 0


def _savepoint_counter() -> int:
    global _counter
    _counter += 1
    return _counter


def _migrations() -> list[tuple[int, str, str]]:
    pkg = resources.files(__package__) / "migrations"
    found = []
    for entry in pkg.iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort()
    numbers = [n for n, _, _ in found]
    if numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError(f"нумерация миграций с разрывом: {numbers}")
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def latest_version() -> int:
    """Версия схемы, которую знает этот код: номер последней миграции."""
    found = _migrations()
    return found[-1][0] if found else 0


def pending_migrations(conn: sqlite3.Connection) -> list[str]:
    current = schema_version(conn)
    return [name for number, name, _ in _migrations() if number > current]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Применить недостающие миграции. Возвращает имена применённых."""
    current = schema_version(conn)
    applied = []
    for number, name, sql in _migrations():
        if number <= current:
            continue
        # executescript сам завершает открытую транзакцию, поэтому BEGIN/COMMIT внутри скрипта
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise RuntimeError(f"миграция {name} не применена: {exc}") from exc
        applied.append(name)
    return applied


def open_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
