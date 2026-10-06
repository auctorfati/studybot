"""Оспаривание вердикта модели.

Оспоренный вердикт до разбора срывом не считается: состояние позиции
возвращается к снимку до ответа, если после него позицию не трогали; запись
в журнале ошибок по этому ответу снимается, вердикт уходит из кэша. Разбор — в недельной сводке,
правка ключа или вариантов — в файле банка и новым импортом.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..clock import to_iso
from ..english.normalize import normalize
from ..progress import restore

RESOLUTIONS = ("verdict_changed", "key_changed", "variants_changed", "upheld")


class DisputeError(RuntimeError):
    pass


def dispute(conn: sqlite3.Connection, review_id: int, now: datetime) -> bool:
    """Оспорить. Возвращает True, если состояние позиции откатано."""
    r = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    if r is None:
        raise DisputeError("нет такого ответа")
    if r["judge"] not in ("cheap", "flagship"):
        raise DisputeError("оспаривается только вердикт модели")
    if r["disputed"]:
        return False
    conn.execute("INSERT OR IGNORE INTO disputes (review_id, created_at) VALUES (?, ?)",
                 (review_id, to_iso(now)))
    conn.execute("UPDATE reviews SET disputed = 1 WHERE id = ?", (review_id,))
    conn.execute("DELETE FROM errors WHERE review_id = ?", (review_id,))
    if r["answer"]:
        # оспоренный вердикт не должен вернуться из кэша при том же ответе
        conn.execute("DELETE FROM verdict_cache WHERE normalized = ?", (normalize(r["answer"]),))
    later = conn.execute("SELECT 1 FROM reviews WHERE item_id = ? AND id > ? LIMIT 1",
                         (r["item_id"], review_id)).fetchone()
    if later is None and r["verdict"] in ("wrong", "partial") and r["state_before_json"] is not None:
        restore(conn, r["item_id"], r["state_before_json"])
        return True
    return False


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT d.id, d.review_id, i.code, i.track, r.answer, r.verdict, r.verdict_json, d.created_at "
        "FROM disputes d JOIN reviews r ON r.id = d.review_id JOIN items i ON i.id = r.item_id "
        "WHERE d.resolved_at IS NULL ORDER BY d.id").fetchall()


def resolve(conn: sqlite3.Connection, dispute_id: int, resolution: str, now: datetime) -> None:
    if resolution not in RESOLUTIONS:
        raise DisputeError(f"решение должно быть одним из: {', '.join(RESOLUTIONS)}")
    conn.execute("UPDATE disputes SET resolved_at = ?, resolution = ? WHERE id = ?",
                 (to_iso(now), resolution, dispute_id))


def pending_variants(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT v.id, i.code, i.answer AS target, v.raw_answer, v.seen_count FROM variants_pending v "
        "JOIN items i ON i.id = v.item_id WHERE v.status = 'pending' ORDER BY i.sort_key").fetchall()


def decide_variant(conn: sqlite3.Connection, variant_id: int, accept: bool) -> None:
    """Принятый вариант код засчитывает сразу; в банк он дописывается при следующей правке файла."""
    conn.execute("UPDATE variants_pending SET status = ? WHERE id = ?",
                 ("accepted" if accept else "rejected", variant_id))
