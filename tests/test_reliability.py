"""Слой К8 в логике бота: копия по таймеру, сводка с копией базы, тихое воскресенье,
повтор после сорванной отправки, «Показать задание», «Копия базы», «Состояние»."""

import gzip
import sqlite3
from dataclasses import replace

from studybot.backup import BackupError
from studybot.bot import controller as ctl_mod
from studybot.bot.controller import Controller
from studybot.bot.ui import BTN_15, BTN_LONG, BTN_TODAY
from studybot.speech import StubSTT
from test_bot import at, buttons, ctl, finish_session, texts  # noqa: F401
from test_sessions import core, run  # noqa: F401

from conftest import db  # noqa: F401


def docs(res):
    return [r for r in res.replies if r.document is not None]


def marks(db, kind):
    return [r[0] for r in db.execute("SELECT mark FROM sent_marks WHERE kind = ?", (kind,))]


# ежедневная копия

def test_tick_makes_daily_backup_once(ctl, core):
    at(core, 2026, 9, 29, 3, 29)
    run(ctl.tick())
    assert core.backups.list("daily") == []
    at(core, 2026, 9, 29, 3, 31)
    run(ctl.tick())
    run(ctl.tick())
    daily = core.backups.list("daily")
    assert [b.path.name for b in daily] == ["daily-2026-09-29.sqlite3"]


def test_backup_failure_logged_and_throttled(ctl, core, db, monkeypatch):
    calls = []

    def broken(kind="manual"):
        calls.append(kind)
        raise BackupError("диск полон")

    monkeypatch.setattr(core.backups, "make", broken)
    at(core, 2026, 9, 29, 3, 31)
    run(ctl.tick())
    at(core, 2026, 9, 29, 3, 45)
    run(ctl.tick())                                       # в пределах часа не повторяет
    assert calls == ["daily"]
    ev = db.execute("SELECT level, message FROM service_events WHERE service = 'backup'").fetchall()
    assert len(ev) == 1 and "диск полон" in ev[0][1]
    at(core, 2026, 9, 29, 4, 40)
    run(ctl.tick())
    assert calls == ["daily", "daily"]
    assert "Сбои сервисов за 7 дней: 2" in run(ctl.on_command("state")).replies[0].text


# воскресенье

def test_sunday_evening_summary_with_copy(ctl, core, db):
    at(core, 2026, 10, 4, 20, 31)
    res = run(ctl.tick())
    t = texts(res)
    assert t[0].startswith("Вечер.") and any(x.startswith("Сводка за неделю") for x in t)
    [doc] = docs(res)
    assert not doc.silent and doc.mark == ("weekly_db", "2026-09-28")
    assert doc.document.name == "studybot-2026-10-04.sqlite3.gz"
    assert "Копия базы на вс 4.10" in doc.document.caption
    out = core.cfg.paths.data_dir / "check.sqlite3"
    out.write_bytes(gzip.decompress(doc.document.data))
    c = sqlite3.connect(str(out))
    assert c.execute("SELECT count(*) FROM items").fetchone()[0] == 70
    c.close()
    assert [r.mark for r in res.replies if r.mark] == [("evening", "2026-10-04"), ("weekly", "2026-09-28"),
                                                      ("weekly_db", "2026-09-28")]
    assert run(ctl.tick()).replies == []


def test_quiet_sunday_copy_alone_silent(ctl, core, db):
    at(core, 2026, 10, 4, 9, 0)
    run(ctl.on_command("pause"))
    run(ctl.on_callback("pause:1"))
    at(core, 2026, 10, 4, 20, 31)
    res = run(ctl.tick())
    assert len(res.replies) == 1 and res.replies[0].document is not None and res.replies[0].silent
    assert marks(db, "weekly") == [] and marks(db, "weekly_db") == ["2026-09-28"]


def test_skipped_sunday_copy_silent(ctl, core, db):
    at(core, 2026, 10, 4, 12, 0)
    run(ctl.on_callback("skipday"))
    at(core, 2026, 10, 4, 20, 40)
    res = run(ctl.tick())
    assert [r.silent for r in res.replies] == [True] and docs(res)


def test_failed_copy_is_resent_alone(ctl, core, db):
    at(core, 2026, 10, 4, 20, 31)
    res = run(ctl.tick())
    k = next(i for i, r in enumerate(res.replies) if r.document is not None)
    ctl.unmark([r.mark for r in res.replies[k:] if r.mark])         # Telegram не принял копию
    assert marks(db, "weekly_db") == [] and marks(db, "evening") == ["2026-10-04"]
    at(core, 2026, 10, 4, 21, 0)
    res = run(ctl.tick())
    assert len(res.replies) == 1 and docs(res)                       # сводка и уведомление не повторяются
    at(core, 2026, 10, 5, 0, 0)                                      # окно уведомления прошло
    assert run(ctl.tick()).replies == []


def test_long_session_brings_copy_once(ctl, core, db):
    at(core, 2026, 10, 3, 12, 0)                          # суббота: ввести фразы, чтобы было что делать
    run(ctl.on_text("30 мин"))
    finish_session(ctl, core)
    at(core, 2026, 10, 4, 12, 0)
    run(ctl.on_text(BTN_LONG))
    assert core.engine.current() is not None
    res = run(ctl.on_callback("end"))
    assert any(t.startswith("Сводка за неделю") for t in texts(res)) and len(docs(res)) == 1
    at(core, 2026, 10, 4, 20, 31)
    assert not docs(run(ctl.tick()))


def test_weekly_copy_can_be_turned_off(core, db):
    core.cfg = replace(core.cfg, backup=replace(core.cfg.backup, weekly_to_telegram=False))
    c = Controller(core, core.cfg, StubSTT())
    at(core, 2026, 10, 4, 20, 31)
    res = run(c.tick())
    assert any(t.startswith("Сводка за неделю") for t in texts(res)) and not docs(res)


def test_oversized_copy_becomes_text(ctl, core, db, monkeypatch):
    monkeypatch.setattr(ctl_mod, "TELEGRAM_MAX", 10)
    res = run(ctl.on_callback("more:copy"))
    assert not docs(res) and "больше предела Telegram" in res.replies[0].text
    assert db.execute("SELECT count(*) FROM service_events WHERE service = 'backup'").fetchone()[0] == 1


# кнопки и отчёты

def test_more_copy_button(ctl, core, db):
    res = run(ctl.on_text("Ещё"))
    assert "more:copy" in buttons(res.replies[0])
    res = run(ctl.on_callback("more:copy"))
    assert len(docs(res)) == 1 and not res.replies[0].silent and res.replies[0].mark is None
    assert marks(db, "weekly_db") == []                              # ручная копия не заменяет недельную


def test_today_show_task(ctl, core):
    r = run(ctl.on_text(BTN_TODAY)).replies[0]
    assert r.menu and not r.buttons
    first = run(ctl.on_text(BTN_15)).replies[0]
    r = run(ctl.on_text(BTN_TODAY)).replies[0]
    assert buttons(r) == ["show"] and "Идёт сессия" in r.text
    shown = run(ctl.on_callback("show")).replies[0]
    assert "Новая фраза" in shown.text and any(d.startswith("done:") for d in buttons(shown))
    assert shown.text in first.text
    run(ctl.on_callback("end"))
    res = run(ctl.on_callback("show"))
    assert "нет сессии" in res.replies[0].text


def test_state_shows_backups(ctl, core):
    s = run(ctl.on_command("state")).replies[0].text
    assert "Резервных копий пока нет: первая — в 03:30." in s and "по воскресеньям со сводкой" in s
    at(core, 2026, 10, 4, 20, 31)
    run(ctl.tick())
    s = run(ctl.on_command("state")).replies[0].text
    assert "Резервные копии: последняя за 04.10" in s and "ежедневных 1 из 14" in s
    assert "Копия в Telegram — неделя с пн 28.09." in s
