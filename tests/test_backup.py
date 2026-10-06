"""Слой К8: резервные копии, восстановление, замок, команды сервера."""

import gzip
import json
import os
import sqlite3
import stat
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from studybot.__main__ import main
from studybot.backup import (BackupError, Backups, backup_before_migrations, human_size, inspect_copy,
                             restore_database)
from studybot.clock import FakeClock, StudyCalendar
from studybot.config import BackupConfig
from studybot.db import _migrations, connect, latest_version, migrate, open_db, schema_version
from studybot.lock import InstanceLock, LockBusy

MSK = ZoneInfo("Europe/Moscow")
ROOT = Path(__file__).resolve().parents[1]
CAL = StudyCalendar.from_strings("Europe/Moscow", "03:00")


def at(y, m, d, hh, mm=0):
    return FakeClock(datetime(y, m, d, hh, mm, tzinfo=MSK))


def filled(path):
    """Рабочая база с одной версией контента и одной позицией."""
    conn = open_db(path)
    conn.execute("INSERT INTO content_versions (imported_at, track, file_name, file_sha256) "
                 "VALUES ('2026-09-28T09:00:00+00:00', 'en', 'b.md', 'x')")
    conn.execute("INSERT INTO items (track, code, unit, kind, prompt, answer, prompt_hash, answer_hash, "
                 "sort_key, content_version) VALUES ('en', '0.01', '0', 'phrase', 'p', 'a', 'h', 'h', 1, 1)")
    return conn


def old_schema(path, upto=6):
    conn = connect(path)
    for number, _, sql in _migrations():
        if number > upto:
            break
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    return conn


@pytest.fixture
def work(tmp_path):
    conn = filled(tmp_path / "data" / "studybot.sqlite3")
    yield tmp_path, conn
    conn.close()


def backups(conn, tmp_path, clock, keep=14):
    return Backups(conn, tmp_path / "data" / "backups", CAL, clock, BackupConfig("03:30", keep, True))


# копия

def test_daily_copy_is_valid_private_standalone(work):
    tmp, conn = work
    b = backups(conn, tmp, at(2026, 9, 28, 3, 31))
    made = b.make("daily")
    assert made.path.name == "daily-2026-09-28.sqlite3" and made.kind == "daily"
    assert stat.S_IMODE(made.path.stat().st_mode) == 0o600
    copy = sqlite3.connect(str(made.path))
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert copy.execute("SELECT code FROM items").fetchone()[0] == "0.01"
    copy.close()
    assert not list(made.path.parent.glob("*.part"))
    assert made.when == "28.09"


def test_due_after_time_once_per_local_date(work):
    tmp, conn = work
    clock = at(2026, 9, 28, 3, 29)
    b = backups(conn, tmp, clock)
    assert not b.due(clock.now())
    clock.advance(minutes=2)
    assert b.due(clock.now())
    b.make("daily")
    assert not b.due(clock.now())
    clock.advance(hours=23)                   # 02:31 следующего дня — ещё рано
    assert not b.due(clock.now())
    clock.advance(hours=1)
    assert b.due(clock.now())


def test_rotation_keeps_last_daily_and_five_others(work):
    tmp, conn = work
    clock = at(2026, 9, 1, 3, 30)
    b = backups(conn, tmp, clock)
    for _ in range(16):
        b.make("daily")
        b.make("manual")
        clock.advance(days=1)
    daily = b.list("daily")
    assert len(daily) == 14 and daily[0].day.isoformat() == "2026-09-16"
    assert daily[-1].day.isoformat() == "2026-09-03"
    assert len(b.list("manual")) == 5
    assert b.last("daily").day.isoformat() == "2026-09-16"


def test_stale_part_removed_fresh_kept(work):
    tmp, conn = work
    b = backups(conn, tmp, at(2026, 9, 28, 12))
    b.dir.mkdir(parents=True)
    old, fresh = b.dir / "x.part", b.dir / "y.part"
    old.write_text("x")
    fresh.write_text("y")
    past = datetime.now().timestamp() - 7200
    os.utime(old, (past, past))
    b.rotate()
    assert not old.exists() and fresh.exists()


def test_snapshot_gz_opens_as_database(work):
    tmp, conn = work
    name, data = backups(conn, tmp, at(2026, 10, 4, 20, 31)).snapshot_gz()
    assert name == "studybot-2026-10-04.sqlite3.gz"
    out = tmp / "x.sqlite3"
    out.write_bytes(gzip.decompress(data))
    assert inspect_copy(out).items == 1
    assert not list((tmp / "data" / "backups").glob("*.part"))


def test_human_size():
    assert human_size(300) == "1 КБ" and human_size(230_000) == "225 КБ"
    assert human_size(3 * 1024 * 1024 + 200_000) == "3,2 МБ"


# восстановление

def test_restore_dry_run_and_apply(work):
    tmp, conn = work
    clock = at(2026, 9, 28, 12)
    copy = backups(conn, tmp, clock).make("manual")
    conn.execute("INSERT INTO settings VALUES ('notify_time', '\"21:00\"', 't')")   # изменение после копии
    conn.close()
    db = tmp / "data" / "studybot.sqlite3"
    res = restore_database(copy.path, db, tmp / "data" / "backups", CAL, clock, BackupConfig(), apply=False)
    assert res.info.items == 1 and res.safety is None
    res = restore_database(copy.path, db, tmp / "data" / "backups", CAL, clock, BackupConfig(), apply=True)
    assert res.safety is not None and res.safety.kind == "pre-restore"
    c = connect(db)
    assert c.execute("SELECT count(*) FROM settings").fetchone()[0] == 0
    c.close()
    s = sqlite3.connect(str(res.safety.path))                     # прежняя база сохранена целиком
    assert s.execute("SELECT count(*) FROM settings").fetchone()[0] == 1
    s.close()
    assert copy.path.exists()                                     # исходник не тронут


def test_restore_old_copy_is_migrated(tmp_path):
    old = old_schema(tmp_path / "old.sqlite3")
    old.close()
    gz = tmp_path / "old.sqlite3.gz"
    gz.write_bytes(gzip.compress((tmp_path / "old.sqlite3").read_bytes()))
    db = tmp_path / "data" / "studybot.sqlite3"
    res = restore_database(gz, db, tmp_path / "b", CAL, at(2026, 9, 28, 12), BackupConfig(), apply=True)
    assert res.info.schema == 6 and res.migrations == [n for k, n, _ in __import__("studybot.db", fromlist=["x"])._migrations() if k >= 7] and res.safety is None
    c = connect(db)
    assert schema_version(c) == latest_version()
    c.close()


def test_restore_refuses_bad_files(tmp_path):
    db = tmp_path / "db.sqlite3"
    args = (db, tmp_path / "b", CAL, at(2026, 9, 28, 12), BackupConfig())
    junk = tmp_path / "junk.sqlite3"
    junk.write_text("not a database")
    with pytest.raises(BackupError, match="не является базой"):
        restore_database(junk, *args, apply=True)
    other = tmp_path / "other.sqlite3"
    c = sqlite3.connect(str(other))
    c.execute("CREATE TABLE t (x)")
    c.close()
    with pytest.raises(BackupError, match="не база учебного бота"):
        restore_database(other, *args, apply=True)
    newer = tmp_path / "newer.sqlite3"
    c = open_db(newer)
    c.execute("PRAGMA user_version = 99")
    c.close()
    with pytest.raises(BackupError, match="более новой версии"):
        restore_database(newer, *args, apply=True)
    with pytest.raises(BackupError, match="нет файла"):
        restore_database(tmp_path / "missing.sqlite3", *args, apply=True)
    assert not db.exists()
    assert not list((tmp_path / "b").glob("*.part"))


def test_backup_before_migrations(tmp_path):
    clock = at(2026, 9, 28, 12)
    old = old_schema(tmp_path / "a.sqlite3")
    made = backup_before_migrations(old, tmp_path / "b", CAL, clock, BackupConfig())
    assert made is not None and made.kind == "pre-migrate"
    assert inspect_copy(made.path).schema == 6
    migrate(old)
    assert backup_before_migrations(old, tmp_path / "b", CAL, clock, BackupConfig()) is None
    fresh = connect(tmp_path / "new.sqlite3")
    assert backup_before_migrations(fresh, tmp_path / "b", CAL, clock, BackupConfig()) is None


# замок

def test_lock_single_instance(tmp_path):
    a, b = InstanceLock(tmp_path / "x.lock"), InstanceLock(tmp_path / "x.lock")
    assert a.acquire()
    assert not b.acquire() and b.holder() == str(os.getpid())
    with pytest.raises(LockBusy):
        with b:
            pass
    a.release()
    with b:
        assert not a.acquire()
    assert a.acquire()
    a.release()


# команды

@pytest.fixture
def server(tmp_path):
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    text = text.replace('token = ""', 'token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"') \
               .replace("owner_id = 0 ", "owner_id = 1001 ") \
               .replace('prompts_dir = "prompts"', f'prompts_dir = "{ROOT / "prompts"}"')
    cfg = tmp_path / "config.toml"
    cfg.write_text(text, encoding="utf-8")
    cfg.chmod(0o600)
    return tmp_path, str(cfg)


def test_cli_backup_list_restore(server, capsys):
    tmp, cfg = server
    assert main(["--config", cfg, "backup"]) == 1                 # базы ещё нет
    filled(tmp / "data" / "studybot.sqlite3").close()
    assert main(["--config", cfg, "backup"]) == 0
    made = next((tmp / "data" / "backups").glob("manual-*.sqlite3"))
    assert main(["--config", cfg, "backups"]) == 0
    assert made.name in capsys.readouterr().out
    assert main(["--config", cfg, "restore", str(made)]) == 0
    out = capsys.readouterr().out
    assert "проверка целостности: ok" in out and "--apply" in out
    lock = InstanceLock(tmp / "data" / "studybot.lock")
    assert lock.acquire()
    assert main(["--config", cfg, "restore", str(made), "--apply"]) == 3
    assert main(["--config", cfg, "run"]) == 3                    # второй экземпляр не стартует
    lock.release()
    assert main(["--config", cfg, "restore", str(made), "--apply"]) == 0
    assert "База восстановлена" in capsys.readouterr().out
    assert main(["--config", cfg, "restore"]) == 2


def test_cli_init_db_copies_before_migrations(server, capsys):
    tmp, cfg = server
    old_schema(tmp / "data" / "studybot.sqlite3").close()
    assert main(["--config", cfg, "init-db"]) == 0
    out = capsys.readouterr().out
    assert "копия перед миграциями" in out and "0007_reliability.sql" in out
    assert len(list((tmp / "data" / "backups").glob("pre-migrate-*.sqlite3"))) == 1
    assert main(["--config", cfg, "init-db"]) == 0
    assert len(list((tmp / "data" / "backups").glob("pre-migrate-*.sqlite3"))) == 1


def test_cli_ping_model(server, capsys, monkeypatch):
    import httpx
    tmp, cfg = server
    assert main(["--config", cfg, "ping-model"]) == 2              # модель не настроена
    import re
    text = Path(cfg).read_text(encoding="utf-8").replace('api_key = ""', 'api_key = "sk-x"')
    text = re.sub(r'^cheap_model = "[^"]*"', 'cheap_model = "c"', text, flags=re.M)
    text = re.sub(r'^flagship_model = "[^"]*"', 'flagship_model = "f"', text, flags=re.M)
    Path(cfg).write_text(text, encoding="utf-8")
    seen = []

    def handler(request):
        seen.append(request)
        if b'"model":"f"' in request.content.replace(b" ", b""):
            return httpx.Response(400, text="response_format not supported")
        return httpx.Response(200, json={"model": "c-1", "choices": [{"message": {"content": '{"ok": true}'}}],
                                         "usage": {"prompt_tokens": 20, "completion_tokens": 5}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    assert main(["--config", cfg, "ping-model"]) == 1
    out = capsys.readouterr().out
    assert "cheap (c thinking={'type': 'disabled'}): ответ за" in out and "JSON да" in out
    assert "flagship (f reasoning_effort=low): не отвечает" in out and "json_mode = false" in out
    body = json.loads(seen[0].content)
    assert body["thinking"] == {"type": "disabled"} and body["max_tokens"] == 1500
    body = json.loads(seen[1].content)
    assert body["reasoning_effort"] == "low" and body["max_tokens"] == 8000
    c = sqlite3.connect(str(tmp / "data" / "studybot.sqlite3"))
    assert c.execute("SELECT count(*), sum(ok) FROM llm_calls").fetchone() == (2, 1)
    c.close()


def test_cli_import_folder_in_order(server, capsys):
    """Папка целиком: карта, банк и разбор кладутся в любом порядке имён, импорт идёт карта → банк → разбор."""
    from helpers import BANK, MAP
    from test_k9 import DIG
    tmp, cfg = server
    folder = tmp / "content_in"
    folder.mkdir()
    (folder / "PATO_Банк_Блок_Д.md").write_text(BANK, encoding="utf-8")     # по имени — раньше карты
    (folder / "PATO_Карта_тем.md").write_text(MAP, encoding="utf-8")
    (folder / "PATO_Разборы_Блок_Д.md").write_text(DIG, encoding="utf-8")
    (folder / "README.md").write_text("# Просто текст\n", encoding="utf-8")
    assert main(["--config", cfg, "init-db"]) == 0
    capsys.readouterr()
    assert main(["--config", cfg, "import", str(folder), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "README.md: не банк" in out and "Файлов 3, принято 3." in out
    conn = connect(tmp / "data" / "studybot.sqlite3")
    assert conn.execute("SELECT count(*) FROM items WHERE track = 'psy'").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM digests").fetchone()[0] >= 1
    conn.close()
