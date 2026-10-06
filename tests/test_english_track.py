from datetime import date, timedelta
from pathlib import Path

import pytest

from studybot.content.importer import Importer
from studybot.content.sources import SourceLibrary
from studybot.english.blocks import Blocks, ControlResult
from studybot.english.graph import LinkGraph, phrase_kind, transform_label
from studybot.english.normalize import contains_phrase, matches, normalize
from studybot.english.track import (ASK, DICTATION, HEAR_ANSWER, HEAR_REPEAT, INTRO, READ_ANSWER,
                                    REPEAT, RU_EN, TRANSFORM, EnglishTrack)
from studybot.enums import Judge, Mode
from studybot.review_queue import ReviewQueue
from studybot.scheduler import Scheduler
from studybot.settings import Settings

BANK = Path(__file__).parent / "data" / "en_bank_blocks_0-1.md"
MON = date(2026, 9, 28)
SAT = date(2026, 10, 3)


# нормализация

@pytest.mark.parametrize("a,b", [
    ("I'm thirty-four years old.", "i am 34 years old"),
    ("What’s your name?", "what is your name"),
    ("We aren't doctors", "We are not doctors."),
    ("I live in the centre", "I live in the center."),
    ("OKAY", "OK."),
    ("Let's start!", "let us start"),
    ("I can't", "I cannot"),
])
def test_normalize_equal(a, b):
    assert normalize(a) == normalize(b)


def test_matches_and_wildcard():
    assert matches("hi my name is sam", "Hi! My name is Sam.", [])
    assert matches("Got it", "I understand.", ["I see.", "Got it."])
    assert not matches("I'm work", "I work.", [])
    assert matches('What does "psychologist" mean?', 'What does "…" mean?', [])
    assert not matches("What does mean?", 'What does "…" mean?', [])
    assert not matches("", "OK.", [])
    assert contains_phrase("well I'm not a doctor you know", "I'm not a doctor.")
    assert not contains_phrase("I'm not a doctorate", "I'm not a doctor.")


def test_phrase_kinds():
    assert phrase_kind("Are you a student?") == "question"
    assert phrase_kind("Yes, I am.") == "short"
    assert phrase_kind("He isn't a doctor.") == "negation"
    assert phrase_kind("I'm a teacher too.") == "statement"
    assert transform_label("I'm not a doctor.", "He isn't a doctor.") == "person"
    assert transform_label("I'm a math teacher.", "Are you a teacher?") == "question"


# трек на настоящем банке

@pytest.fixture
def env(db, clock, cal, tmp_path):
    imp = Importer(db, clock, cal, SourceLibrary(tmp_path))
    plan = imp.analyze(BANK.name, BANK.read_bytes())
    assert plan.ok
    imp.apply(plan)
    st = Settings(db, clock)
    sch = Scheduler(db, st)
    q = ReviewQueue(db, st)
    return EnglishTrack(db, st, sch, q), sch, q, st


def iid(db, code):
    return db.execute("SELECT id FROM items WHERE track = 'en' AND code = ?", (code,)).fetchone()[0]


def srow(db, code):
    return db.execute("SELECT * FROM item_state WHERE item_id = ?", (iid(db, code),)).fetchone()


def introduce(track, db, codes, clock, day=MON):
    for c in codes:
        t = track.intro_tasks(iid(db, c), Mode.VOICE)[0]
        track.record(t, None, judge=Judge.CODE, mode=Mode.VOICE, day=day, now=clock.now())


def test_graph_both_ways(env, db):
    track = env[0]
    g = track.graph
    assert {n.code for n in g.neighbors(iid(db, "1.02"))} == {"1.01", "1.35", "1.37"}
    assert [n.code for n in g.answers_to(iid(db, "1.02"))] == ["1.01"]
    assert [n.code for n in g.answers_to(iid(db, "1.06"))] == ["1.04"]


def test_next_new_order_and_limit(env, db):
    track = env[0]
    new = track.next_new(MON)
    codes = [db.execute("SELECT code FROM items WHERE id = ?", (i,)).fetchone()[0] for i in new]
    assert codes == [f"0.0{k}" for k in range(1, 9)]


def test_intro(env, db, clock):
    track, _, q, _ = env
    tasks = track.intro_tasks(iid(db, "1.07"), Mode.VOICE)
    assert [t.format for t in tasks] == [INTRO, TRANSFORM] or [t.format for t in tasks] == [INTRO, ASK]
    assert tasks[0].notes == ["З1", "З3"]
    assert tasks[1].expected == ["I'm a math teacher."]          # сосед ещё не введён
    out = track.record(tasks[0], None, judge=Judge.CODE, mode=Mode.VOICE, day=MON, now=clock.now())
    assert out.stage_after == 2
    s = srow(db, "1.07")
    assert s["stage"] == 2 and s["due_date"] == "2026-09-29" and s["step"] is None
    assert q.new_used("en", MON) == 1
    assert track.intro_tasks(iid(db, "1.08"), Mode.VOICE)[0].notes == []   # З1 и З3 уже показаны
    # трансформация при знакомстве лестницу не двигает
    track.record(tasks[1], True, judge=Judge.CODE, mode=Mode.VOICE, day=MON, now=clock.now())
    assert srow(db, "1.07")["streak"] == 0
    assert db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 2


def test_assembly_never_introduced(env, db):
    track = env[0]
    db.execute("UPDATE item_state SET stage = 2")   # ничего нет
    for _ in range(10):
        new = track.next_new(SAT, limit=100)
        assert iid(db, "1.45") not in new
        break


def test_ladder_to_grid(env, db, clock):
    track, sch, _, _ = env
    introduce(track, db, ["1.13", "1.25"], clock)
    day = MON + timedelta(days=1)
    t = track.task_for(iid(db, "1.13"), Mode.VOICE)
    assert t.format == TRANSFORM and t.expected == ["He isn't a doctor."]    # сосед введён → превращаем текущую
    track.record(t, False, judge=Judge.CODE, mode=Mode.VOICE, day=day, now=clock.now())
    assert srow(db, "1.13")["streak"] == 0 and srow(db, "1.13")["stage"] == 2
    for k in range(2):
        t = track.task_for(iid(db, "1.13"), Mode.VOICE)
        track.record(t, True, judge=Judge.CODE, mode=Mode.VOICE, day=day + timedelta(days=k + 1), now=clock.now())
    assert srow(db, "1.13")["stage"] == 3
    # ступень 3 без аудио: утверждение — запасной формат с русского
    t = track.task_for(iid(db, "1.13"), Mode.VOICE)
    assert t.format == RU_EN and t.meta.get("no_audio")
    for k in range(2):
        track.record(t, True, judge=Judge.CODE, mode=Mode.VOICE, day=day + timedelta(days=3 + k), now=clock.now())
    s = srow(db, "1.13")
    assert s["stage"] == 4 and s["step"] == 0
    assert s["due_date"] == (day + timedelta(days=5)).isoformat()


def test_repeat_without_links(env, db, clock):
    track = env[0]
    introduce(track, db, ["0.16"], clock)
    t = track.task_for(iid(db, "0.16"), Mode.TEXT)
    assert t.format == REPEAT and t.expected == ["Thank you."] and t.code_match("thanks")


def test_hearing_formats(env, db, clock):
    track = env[0]
    introduce(track, db, ["1.01", "1.02"], clock)
    db.execute("UPDATE item_state SET stage = 3 WHERE item_id IN (?, ?)", (iid(db, "1.01"), iid(db, "1.02")))
    t = track.task_for(iid(db, "1.02"), Mode.VOICE)
    assert t.format == READ_ANSWER and t.by_meaning and t.shown_en == "What's your name?"
    assert t.code_match("Hi, I'm Sam") and not t.code_match("I'm a doctor")
    for c in ("1.01", "1.02"):
        db.execute("INSERT INTO audio_files (item_id, speed, path, text_hash) VALUES (?, 'normal', 'x', 'h')",
                   (iid(db, c),))
    assert track.task_for(iid(db, "1.02"), Mode.VOICE).format == HEAR_ANSWER
    assert track.task_for(iid(db, "1.01"), Mode.VOICE).format == HEAR_REPEAT
    assert track.task_for(iid(db, "1.01"), Mode.TEXT).format == DICTATION


def test_stage4_text_does_not_move_interval(env, db, clock):
    track, sch, _, _ = env
    introduce(track, db, ["0.16"], clock)
    i = iid(db, "0.16")
    db.execute("UPDATE item_state SET stage = 4 WHERE item_id = ?", (i,))
    sch.enter_grid(i, MON, clock.now(), step=2)
    before = srow(db, "0.16")["due_date"]
    t = track.task_for(i, Mode.TEXT)
    assert not t.schedules and t.meta["writing_only"]
    track.record(t, True, judge=Judge.CODE, mode=Mode.TEXT, day=MON, now=clock.now())
    assert srow(db, "0.16")["due_date"] == before
    t = track.task_for(i, Mode.VOICE)
    out = track.record(t, True, judge=Judge.CODE, mode=Mode.VOICE, day=MON, now=clock.now())
    assert out.change.step_after == 3
    r = db.execute("SELECT schedules, step_before, step_after FROM reviews ORDER BY id DESC").fetchone()
    assert tuple(r) == (1, 2, 3)


def test_close_after_30_and_dialog(env, db, clock):
    track, sch, _, _ = env
    introduce(track, db, ["1.13"], clock)
    i = iid(db, "1.13")
    db.execute("UPDATE item_state SET stage = 4 WHERE item_id = ?", (i,))
    sch.enter_grid(i, MON, clock.now(), step=sch.step_of(30))
    t = track.task_for(i, Mode.VOICE)
    out = track.record(t, True, judge=Judge.CODE, mode=Mode.VOICE, day=MON + timedelta(days=30), now=clock.now())
    assert out.change.passed_days == 30 and not out.closed          # в диалоге ещё не звучала
    assert track.mark_spoken(["Hello! I'm not a doctor, I'm a writer."]) == ["1.13"]
    assert srow(db, "1.13")["closed"] == 1
    assert track.stage_counts()[5] == 1


def test_spoken_before_30_closes_later(env, db, clock):
    track, sch, _, _ = env
    introduce(track, db, ["0.16"], clock)
    track.mark_spoken(["thank you"])
    i = iid(db, "0.16")
    db.execute("UPDATE item_state SET stage = 4 WHERE item_id = ?", (i,))
    sch.enter_grid(i, MON, clock.now(), step=sch.step_of(14))
    out = track.record(track.task_for(i, Mode.VOICE), True, judge=Judge.CODE, mode=Mode.VOICE,
                       day=MON, now=clock.now())
    assert not out.closed
    out = track.record(track.task_for(i, Mode.VOICE), True, judge=Judge.CODE, mode=Mode.VOICE,
                       day=MON, now=clock.now())
    assert out.closed


def test_find_error_threshold(env, db, clock):
    track = env[0]
    i = iid(db, "1.07")
    for k in range(2):
        db.execute("INSERT INTO errors (track, item_id, ts, study_date, tag, answer) "
                   "VALUES ('en', ?, 't', '2026-09-28', 'article', 'I am math teacher')", (i,))
    assert track.find_error_tasks(5) == []
    db.execute("INSERT INTO errors (track, item_id, ts, study_date, tag, answer) "
               "VALUES ('en', ?, 't', '2026-09-28', 'article', 'I math teacher')", (i,))
    tasks = track.find_error_tasks(5)
    assert len(tasks) == 3 and tasks[0].expected == ["I'm a math teacher."]
    for t in tasks:
        track.mark_error_used(t.meta["error_id"])
        track.mark_error_used(t.meta["error_id"])
    assert track.find_error_tasks(5) == []


def test_block_control_point(env, db, clock, cal):
    track = env[0]
    blocks = Blocks(db, cal)
    codes = [r[0] for r in db.execute("SELECT code FROM items WHERE unit = '0' AND kind = 'phrase'")]
    introduce(track, db, codes[:-1], clock)
    assert blocks.due_for_control(SAT) is None
    introduce(track, db, codes[-1:], clock)
    assert blocks.due_for_control(MON) == ("EN1", "0")                    # по умолчанию — в любой день
    db.execute("INSERT INTO settings (key, value_json, updated_at) VALUES ('cp_any_day', 'false', '2026-09-28')")
    assert blocks.due_for_control(MON) is None                            # настройка: только выходные
    assert blocks.due_for_control(SAT) == ("EN1", "0")
    db.execute("DELETE FROM settings WHERE key = 'cp_any_day'")
    plan = blocks.plan("EN1", "0", seed=1)
    assert len(plan.ru_items) == 10 and len(set(plan.ru_items)) == 10
    assert iid(db, "0.04") not in plan.ru_items                          # многоточие не для зачёта
    assert all(phrase_kind(db.execute("SELECT answer FROM items WHERE id = ?", (i,)).fetchone()[0]) == "question"
               for i in plan.hearing_items)
    fail = ControlResult(7, 10, 8, 8, 5, True)
    assert not blocks.record(plan, fail, clock.now())
    ok = ControlResult(8, 10, 7, 8, 5, True)
    assert blocks.record(plan, ok, clock.now())
    assert blocks.due_for_control(SAT) is None
    row = db.execute("SELECT cp_attempts FROM en_blocks WHERE block = '0'").fetchone()
    assert row[0] == 2


def test_control_point_once_a_day(env, db, clock, cal):
    track = env[0]
    blocks = Blocks(db, cal)
    codes = [r[0] for r in db.execute("SELECT code FROM items WHERE unit = '0' AND kind = 'phrase'")]
    introduce(track, db, codes, clock)
    assert blocks.due_for_control(MON) == ("EN1", "0")
    db.execute("INSERT INTO sessions (id, study_date, kind, ordered_sec, started_at, status) "
               "VALUES (901, ?, 'evening', 1800, '2026-09-28T17:00:00+00:00', 'done')", (MON.isoformat(),))
    db.execute("INSERT INTO session_steps (session_id, seq, track, kind, slot, est_sec, payload_json, status) "
               "VALUES (901, 1, 'en', 'task', 'control', 30, '{}', 'done')")
    assert blocks.tried_today(MON) and blocks.due_for_control(MON) is None    # попытка сегодня уже была
    assert blocks.due_for_control(MON + timedelta(days=1)) == ("EN1", "0")
