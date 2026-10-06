from helpers import add_psy_item
import random
from datetime import date, timedelta

import pytest

from studybot.enums import Rating
from studybot.review_queue import ReviewQueue
from studybot.scheduler import ScheduleError, Scheduler
from studybot.settings import Settings

MON = date(2026, 9, 28)


def make_items(db, n, track="en", prefix="x"):
    v = db.execute("INSERT INTO content_versions (imported_at, track, file_name, file_sha256) "
                   "VALUES ('t', ?, 'f', 's')", (track,)).lastrowid
    ids = []
    for k in range(n):
        ids.append(db.execute(
            "INSERT INTO items (track, code, unit, kind, prompt, answer, prompt_hash, answer_hash, "
            "sort_key, content_version) VALUES (?, ?, '1', ?, 'p', 'a', 'h', 'h', ?, ?)",
            (track, f"{prefix}{k:04d}", "phrase" if track == "en" else "card", k, v)).lastrowid)
    return ids


@pytest.fixture
def st(db, clock):
    return Settings(db, clock)


@pytest.fixture
def sch(db, st):
    return Scheduler(db, st)


@pytest.fixture
def q(db, st):
    return ReviewQueue(db, st)


def state(db, item):
    return db.execute("SELECT * FROM item_state WHERE item_id = ?", (item,)).fetchone()


def test_grid_walk(db, sch, clock):
    (i,) = make_items(db, 1)
    now = clock.now()
    ch = sch.enter_grid(i, MON, now)
    assert ch.step_after == 0 and ch.due_after == "2026-09-29"
    day, dues = MON, []
    for _ in range(9):
        day = date.fromisoformat(state(db, i)["due_date"])
        ch = sch.grade(i, Rating.GOOD, day, now)
        dues.append((date.fromisoformat(ch.due_after) - day).days)
    assert dues == [3, 7, 14, 30, 60, 120, 240, 240, 240]


def test_passed_days_marks_30(db, sch, clock):
    (i,) = make_items(db, 1)
    sch.enter_grid(i, MON, clock.now(), step=sch.step_of(30))
    ch = sch.grade(i, Rating.GOOD, MON + timedelta(days=30), clock.now())
    assert ch.passed_days == 30 and ch.step_after == sch.step_of(60)


def test_hard_and_again(db, sch, clock):
    (i,) = make_items(db, 1)
    sch.enter_grid(i, MON, clock.now(), step=3)            # 14 дней
    ch = sch.grade(i, Rating.HARD, MON, clock.now())
    assert ch.step_after == 3 and ch.due_after == (MON + timedelta(days=14)).isoformat()
    assert ch.passed_days is None
    ch = sch.grade(i, Rating.AGAIN, MON, clock.now())
    assert ch.step_after == 1 and ch.lapse and ch.due_after == "2026-10-01"     # с 14 дней — на 3
    s = state(db, i)
    assert s["lapses"] == 1 and s["reps"] == 2
    ch = sch.grade(i, Rating.AGAIN, MON, clock.now())
    assert ch.step_after == 0 and ch.due_after == "2026-09-29"                  # ниже начала не падает


def test_again_full_reset_setting(db, sch, clock):
    (i,) = make_items(db, 1)
    sch.enter_grid(i, MON, clock.now(), step=5)            # 60 дней
    db.execute("INSERT INTO settings (key, value_json, updated_at) VALUES ('lapse_steps_back', '8', '2026-09-28')")
    ch = sch.grade(i, Rating.AGAIN, MON, clock.now())
    assert ch.step_after == 0 and ch.lapse                                      # прежнее правило — с начала


def test_no_schedule_answer(db, sch, clock):
    (i,) = make_items(db, 1)
    sch.enter_grid(i, MON, clock.now(), step=2)
    ch = sch.grade(i, Rating.AGAIN, MON, clock.now(), schedules=False)
    assert not ch.moved and state(db, i)["lapses"] == 0 and state(db, i)["reps"] == 1


def test_probe_confirm_step(sch, st):
    assert sch.step_of(st.get("probe_confirm_days")) == 3
    with pytest.raises(ScheduleError):
        sch.step_of(5)


def test_grade_outside_grid(db, sch, clock):
    (i,) = make_items(db, 1)
    with pytest.raises(ScheduleError):
        sch.grade(i, Rating.GOOD, MON, clock.now())
    sch.set_due(i, MON)
    with pytest.raises(ScheduleError):
        sch.grade(i, Rating.GOOD, MON, clock.now())


def test_rebalance_cap_and_order(db, sch, q, clock):
    now = clock.now()
    ids = make_items(db, 100)
    for k, i in enumerate(ids):
        # разные шаги и сроки, все просрочены
        sch.enter_grid(i, MON, now, step=k % 5)
        db.execute("UPDATE item_state SET due_date = ? WHERE item_id = ?",
                   ((MON - timedelta(days=k % 10)).isoformat(), i))
    before = q._overdue("en", MON, now)
    moved = q.rebalance("en", MON, now)
    assert moved == 60
    today = q.due_today("en", MON, now)
    assert len(today) == 40
    assert [d.item_id for d in today] == [d.item_id for d in before[:40]]
    for k in range(1, 5):
        assert q.load("en", MON + timedelta(days=k), now) <= 40
    assert q.rebalance("en", MON, now) == 0          # идемпотентна


def test_rebalance_respects_existing_load(db, sch, q, clock):
    now = clock.now()
    ids = make_items(db, 90)
    for i in ids[:50]:
        sch.enter_grid(i, MON - timedelta(days=1), now)          # срок сегодня
    for i in ids[50:]:
        sch.enter_grid(i, MON, now)                              # срок завтра, 40 штук
    q.rebalance("en", MON, now)
    assert q.load("en", MON, now) == 40
    assert q.load("en", MON + timedelta(days=1), now) == 40
    assert q.load("en", MON + timedelta(days=2), now) == 10


def test_hidden_items_not_in_queue(db, sch, q, clock):
    now = clock.now()
    a, b, c, d = make_items(db, 4)
    for i in (a, b, c, d):
        sch.enter_grid(i, MON - timedelta(days=1), now)
    db.execute("UPDATE items SET archived = 1 WHERE id = ?", (a,))
    db.execute("UPDATE items SET fragment_status = 'missing' WHERE id = ?", (b,))
    db.execute("UPDATE item_state SET deferred_until = '2099-01-01T00:00:00+00:00' WHERE item_id = ?", (c,))
    assert [x.item_id for x in q.due_today("en", MON, now)] == [d]


def test_tracks_separate(db, sch, q, clock):
    now = clock.now()
    for i in make_items(db, 45, "en", "e") + make_items(db, 45, "psy", "p"):
        sch.enter_grid(i, MON - timedelta(days=1), now)
    q.rebalance("en", MON, now)
    assert len(q.due_today("en", MON, now)) == 40
    assert len(q.due_today("psy", MON, now)) == 40
    assert q.load("psy", MON, now) == 45                 # психология не тронута


def test_new_budget(q, st, db):
    sat = MON + timedelta(days=5)
    assert q.new_budget("en", MON) == 8 and q.new_budget("en", sat) == 10
    q.spend_new("en", MON, 3)
    assert q.new_budget("en", MON) == 5
    q.spend_new("en", MON, 10)
    assert q.new_budget("en", MON) == 0
    assert q.new_budget("psy", MON) == 0                 # банков психологии нет
    add_psy_item(db)
    q.spend_new("psy", MON, 6)
    assert q.new_budget("psy", MON) == 6
    st.set("pause_until", MON.isoformat())
    assert q.new_budget("en", MON) == 0


def test_simulation_30_days_never_exceeds_cap(db, sch, q, clock):
    """30 дней: по 8 новых в день, 20 % срывов, пропуски дней; потолок держится."""
    rnd = random.Random(7)
    ids = iter(make_items(db, 400))
    day = MON
    for n in range(30):
        now = clock.now()
        if n % 7 != 3:                                   # раз в неделю пропуск
            q.rebalance("en", day, now)
            today = q.due_today("en", day, now)
            assert len(today) <= 40
            for d in today:
                r = Rating.AGAIN if rnd.random() < 0.2 else rnd.choice([Rating.GOOD, Rating.HARD])
                sch.grade(d.item_id, r, day, now)
            for _ in range(q.new_budget("en", day)):
                sch.enter_grid(next(ids), day, now)
                q.spend_new("en", day)
        clock.advance(days=1)
        day += timedelta(days=1)
    # после паузы и в любой будущий день перебалансировка держит потолок
    for k in range(60):
        d = day + timedelta(days=k)
        q.rebalance("en", d, clock.now())
        assert len(q.due_today("en", d, clock.now())) <= 40
        assert q.load("en", d, clock.now()) <= 40
    lost = db.execute("SELECT count(*) FROM item_state WHERE step IS NOT NULL AND due_date IS NULL").fetchone()[0]
    assert lost == 0
