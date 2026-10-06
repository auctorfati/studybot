"""Снимок и откат состояния позиции (для оспаривания)."""

from __future__ import annotations

import json
import sqlite3

STATE_FIELDS = ("stage", "streak", "step", "due_date", "reps", "lapses", "spoken_in_dialog",
                "closed", "introduced_at", "last_review_at", "deferred_until")


def snapshot(conn: sqlite3.Connection, item_id: int) -> str | None:
    row = conn.execute("SELECT * FROM item_state WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    return json.dumps({k: row[k] for k in STATE_FIELDS}, ensure_ascii=False)


def restore(conn: sqlite3.Connection, item_id: int, snap: str | None) -> None:
    if snap is None:
        conn.execute("DELETE FROM item_state WHERE item_id = ?", (item_id,))
        return
    data = json.loads(snap)
    sets = ", ".join(f"{k} = ?" for k in STATE_FIELDS)
    conn.execute(f"UPDATE item_state SET {sets} WHERE item_id = ?",
                 (*[data.get(k) for k in STATE_FIELDS], item_id))
