import sqlite3

import pytest

from studybot.db import (connect, latest_version, migrate, open_db, pending_migrations,
                         schema_version, transaction)

TABLES = {"item_topics", "en_blocks", "session_steps", "sent_marks",
    "content_versions", "topics", "items", "notes", "audio_files", "item_state",
    "topic_state", "sessions", "reviews", "days", "errors", "disputes",
    "variants_pending", "verdict_cache", "llm_calls", "service_events", "settings",
}


def test_schema_created(db):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names
    assert schema_version(db) == latest_version() == len(dbmod_list())
    assert pending_migrations(db) == []


def test_migrate_idempotent(db):
    assert migrate(db) == []


def test_file_db_wal(tmp_path):
    conn = open_db(tmp_path / "x.sqlite3")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
    conn = connect(tmp_path / "x.sqlite3")
    assert migrate(conn) == [] and schema_version(conn) == latest_version()
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2          # FULL


def _version(db):
    cur = db.execute(
        "INSERT INTO content_versions (imported_at, track, file_name, file_sha256) "
        "VALUES ('2026-09-28T09:00:00+00:00', 'en', 'bank.md', 'x')")
    return cur.lastrowid


def _item(db, v, code="1.01", **kw):
    fields = dict(track="en", code=code, unit="1", kind="phrase", prompt="p", answer="a",
                  prompt_hash="h1", answer_hash="h2", sort_key=1, content_version=v)
    fields.update(kw)
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    return db.execute(f"INSERT INTO items ({cols}) VALUES ({marks})", tuple(fields.values())).lastrowid


def test_item_code_unique_per_track(db):
    v = _version(db)
    _item(db, v)
    with pytest.raises(sqlite3.IntegrityError):
        _item(db, v)
    _item(db, v, track="psy", kind="card", unit="П16")  # тот же код в другом треке — можно


def test_checks_and_foreign_keys(db):
    v = _version(db)
    with pytest.raises(sqlite3.IntegrityError):
        _item(db, v, code="x", kind="poem")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO item_state (item_id) VALUES (999)")
    item = _item(db, v)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO reviews (item_id, ts, study_date, format, mode, verdict, rating, judge) "
                   "VALUES (?, 't', 'd', 'card', 'voice', 'correct', 5, 'code')", (item,))


def test_transaction_rollback(db):
    v = _version(db)
    with pytest.raises(RuntimeError):
        with transaction(db):
            _item(db, v, code="1.02")
            raise RuntimeError("сбой посередине")
    assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 0


def test_nested_transaction(db):
    v = _version(db)
    with transaction(db):
        _item(db, v, code="1.03")
        with pytest.raises(ValueError):
            with transaction(db):
                _item(db, v, code="1.04")
                raise ValueError
    codes = [r[0] for r in db.execute("SELECT code FROM items")]
    assert codes == ["1.03"]


def test_sent_marks_keep_rows_through_0007(tmp_path):
    """Миграция 0007 пересоздаёт sent_marks: отметки К7 не теряются, новый вид принимается."""
    conn = connect(tmp_path / "old.sqlite3")
    from studybot import db as dbmod
    for number, name, sql in dbmod._migrations():
        if number > 6:
            break
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    conn.execute("INSERT INTO sent_marks VALUES ('evening', '2026-09-28', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sent_marks VALUES ('weekly_db', '2026-09-28', 't')")
    later = [name for number, name, _ in dbmod._migrations() if number >= 7]
    assert pending_migrations(conn) == later
    assert migrate(conn) == later
    assert conn.execute("SELECT count(*) FROM sent_marks").fetchone()[0] == 1
    conn.execute("INSERT INTO sent_marks VALUES ('weekly_db', '2026-09-28', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sent_marks VALUES ('other', '2026-09-28', 't')")


def dbmod_list():
    from studybot import db as dbmod
    return list(dbmod._migrations())
