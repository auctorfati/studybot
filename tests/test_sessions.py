from helpers import add_psy_item
import asyncio
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from studybot.app import build_core
from studybot.clock import FakeClock
from studybot.config import load_config
from studybot.enums import Mode, Track
from studybot.llm.client import FakeTransport
from studybot.sessions.engine import NEXT_SESSION, SessionError

ROOT = Path(__file__).resolve().parents[1]
EN_BANK = Path(__file__).parent / "data" / "en_bank_blocks_0-1.md"
MSK = ZoneInfo("Europe/Moscow")


def run(c):
    return asyncio.run(c)


def auto_reply(messages):
    """Подставная модель по умолчанию: отвечает по виду инструкции."""
    system = messages[0]["content"]
    if "conversation partner" in system:
        return json.dumps({"reply": "Nice. And you?", "end": False})
    if "после короткого диалога" in system:
        return json.dumps({"corrections": []})
    if "устный монолог" in system:
        return json.dumps({"verdict": "passed", "covered": [], "missing": [], "errors": [], "comment": ""})
    if "сам задаёт" in system:
        return json.dumps({"items": [{"question": f"Q{k}?", "ok": True, "fix": ""} for k in range(5)]})
    return json.dumps({"verdict": "acceptable", "tag": None, "explanation": "", "correction": ""})


@pytest.fixture
def core(tmp_path, db):
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    text = text.replace('api_key = ""', 'api_key = "k"') \
               .replace(f'prompts_dir = "prompts"', f'prompts_dir = "{ROOT / "prompts"}"')
    text = re.sub(r'^cheap_model = "[^"]*"', 'cheap_model = "c"', text, flags=re.M)
    text = re.sub(r'^flagship_model = "[^"]*"', 'flagship_model = "f"', text, flags=re.M)
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    cfg = load_config(p)
    clock = FakeClock(datetime(2026, 9, 28, 19, 0, tzinfo=MSK))          # понедельник, вечер
    fake = FakeTransport(auto=auto_reply)
    c = build_core(cfg, db, clock, fake)
    c.fake = fake
    c.cfg = cfg
    c.importer.apply(c.importer.analyze(EN_BANK.name, EN_BANK.read_bytes()))
    c.english.reload()
    return c


def answer_all(core, correct=True, max_steps=200, step_sec=20):
    """Пройти сессию: на каждое задание ответить целевой фразой (или заведомо неверно)."""
    e, n = core.engine, 0
    while n < max_steps:
        step = run(e.next())
        if step is None:
            return n
        core.clock.advance(seconds=step_sec)
        if step.kind == "task":
            t = step.task()
            text = (t.expected[0] if t.expected else "Hi, I'm Sam.") if correct else "zzz"
            if not correct:
                core.fake.push({"verdict": "error", "tag": "word", "explanation": "Не то слово.", "correction": ""})
            run(e.answer(step.id, text))
        elif step.kind == "dialog":
            while not run(e.answer(step.id, "I'm a teacher.")).finished_step:
                pass
        else:
            e.defer(step.id)
        n += 1
    raise AssertionError("сессия не закончилась")


# день

def test_day_counted_rules(core):
    d = core.engine.today()
    core.days.add_time(d, Track.PSY, 600)
    assert not core.days.status(d).counted                  # нет английского минимума
    core.days.add_time(d, Track.EN, 300)
    st = core.days.status(d)
    assert st.counted and st.total_min == 15
    assert st.counter() == "сегодня 15 из 90, английский 5, психология 10"


def test_choose_track(core):
    d = core.engine.today()
    assert core.days.choose_track(d) == Track.EN            # банков психологии нет
    add_psy_item(core.conn)
    assert core.days.choose_track(d, short=True) == Track.EN  # английский минимум не набран
    core.days.add_time(d, Track.EN, 1200)
    assert core.days.choose_track(d) == Track.PSY
    core.days.add_time(d, Track.PSY, 1500)
    assert core.days.choose_track(d) == Track.EN


def test_estimates_calibrate(core, db):
    assert core.estimates.get("transform") == 25
    item = db.execute("SELECT id FROM items LIMIT 1").fetchone()[0]
    for sec in (40, 50, 60, 70, 80):
        db.execute("INSERT INTO reviews (item_id, ts, study_date, format, mode, verdict, judge, raw_sec) "
                   "VALUES (?, 't', '2026-09-28', 'transform', 'text', 'correct', 'code', ?)", (item, sec))
    core.estimates.reset()
    assert core.estimates.get("transform") == 60


# сессии

def test_first_session_new_only(core, db):
    info = core.engine.start("15")
    assert info.ordered_sec == 900
    steps = db.execute("SELECT slot, kind FROM session_steps WHERE session_id = ?", (info.id,)).fetchall()
    assert {s["slot"] for s in steps} == {"new"}              # повторять ещё нечего, диалог рано
    first = run(core.engine.next())
    assert first.task().format == "intro"
    core.clock.advance(seconds=30)
    fb = run(core.engine.answer(first.id, None))
    assert fb.correct is None and fb.notes == []             # у 0.01 заметок нет
    st = core.engine.status()
    assert st.en_sec == 30


def test_time_capped_and_session_end(core, db):
    core.engine.start("5")
    core.engine.start("15")                                   # старая сессия закрывается
    assert db.execute("SELECT count(*) FROM sessions WHERE status = 'done'").fetchone()[0] == 1
    step = run(core.engine.next())
    core.clock.advance(minutes=30)                            # долго думал
    run(core.engine.answer(step.id, None))
    assert core.engine.status().en_sec == 90                  # 2 × 45 секунд оценки знакомства
    with pytest.raises(SessionError):
        run(core.engine.answer(step.id, None))


def test_full_session_and_next_day_reviews(core, db):
    core.engine.start("30")
    answer_all(core, step_sec=60)
    d0 = core.engine.today()
    assert core.queue.new_used("en", d0) == 8                 # лимит будней
    assert core.days.status(d0).counted
    core.clock.advance(days=1)
    info = core.engine.start("15")
    slots = [r[0] for r in db.execute("SELECT slot FROM session_steps WHERE session_id = ?", (info.id,))]
    assert "review" in slots
    answer_all(core)
    d1 = core.engine.today()
    assert core.days.reviews_done(d1, "en") > 0


def test_idle_pause(core, db):
    core.engine.start("15")
    run(core.engine.next())
    core.clock.advance(minutes=11)
    assert core.engine.idle_check()
    assert core.engine.active()["status"] == "paused"


def test_defer_until_next_session(core, db):
    core.engine.start("15")
    step = run(core.engine.next())
    core.engine.defer(step.id)                                # знакомство: состояния ещё нет, просто пропуск
    assert db.execute("SELECT status FROM session_steps WHERE id = ?", (step.id,)).fetchone()[0] == "deferred"
    answer_all(core)
    core.clock.advance(days=1)
    core.engine.start("5")
    step = run(core.engine.next())
    core.engine.defer(step.id)
    assert db.execute("SELECT deferred_until FROM item_state WHERE item_id = ?",
                      (step.item_id,)).fetchone()[0] == NEXT_SESSION
    core.engine.start("5")
    assert db.execute("SELECT count(*) FROM item_state WHERE deferred_until = ?",
                      (NEXT_SESSION,)).fetchone()[0] == 0


def test_model_down_defers_and_code_continues(core, db):
    core.engine.start("30")
    answer_all(core)
    core.clock.advance(days=1)
    core.engine.start("15")
    step = run(core.engine.next())
    while step.kind != "task" or step.task().format == "intro":
        run(core.engine.answer(step.id, None)) if step.kind == "task" else core.engine.defer(step.id)
        step = run(core.engine.next())
    core.fake.replies.clear()
    core.fake.auto = None                                     # модель молчит
    fb = run(core.engine.answer(step.id, "совсем другое"))
    assert fb.deferred and fb.correct is None
    assert db.execute("SELECT deferred_until FROM item_state WHERE item_id = ?",
                      (step.item_id,)).fetchone()[0] == NEXT_SESSION
    step = run(core.engine.next())
    t = step.task()
    fb = run(core.engine.answer(step.id, t.expected[0] if t.expected else "Hi, I'm Sam."))
    assert fb.correct in (True, None)


def test_text_mode_holds_speech(core, db):
    core.engine.start("30")
    answer_all(core)
    for i in db.execute("SELECT item_id FROM item_state WHERE stage = 2").fetchall()[:5]:
        db.execute("UPDATE item_state SET stage = 4 WHERE item_id = ?", (i[0],))
        core.scheduler.enter_grid(i[0], core.engine.today(), core.clock.now())
    core.clock.advance(days=1)
    core.settings.set("mode", "text")
    info = core.engine.start("15")
    formats = [json.loads(r[0]).get("format") for r in db.execute(
        "SELECT payload_json FROM session_steps WHERE session_id = ?", (info.id,))]
    assert "ru_en" not in formats
    assert db.execute("SELECT speech_held FROM days WHERE study_date = ?",
                      (core.engine.today().isoformat(),)).fetchone()[0] == 1


def test_evening_block_takes_rest_of_norm(core):
    d = core.engine.today()
    core.days.add_time(d, Track.EN, 20 * 60)
    info = core.engine.start("evening")
    assert info.ordered_sec == 70 * 60                      # норма в будни 90 минут
    core.days.add_time(d, Track.EN, 55 * 60)
    assert core.engine.start("evening").ordered_sec == 15 * 60


def test_error_logged_through_engine(core, db):
    core.engine.start("30")
    answer_all(core)
    core.clock.advance(days=1)
    core.engine.start("15")
    n = 0
    while True:
        step = run(core.engine.next())
        if step is None:
            break
        if step.kind == "task" and step.task().format not in ("intro",):
            core.fake.push({"verdict": "error", "tag": "article", "explanation": "Нужен a.", "correction": ""})
            fb = run(core.engine.answer(step.id, "I am math teacher"))
            assert fb.correct is False and fb.disputable and fb.review_id
            n += 1
            break
        run(core.engine.answer(step.id, None)) if step.kind == "task" else core.engine.defer(step.id)
    assert n == 1 and db.execute("SELECT count(*) FROM errors").fetchone()[0] == 1


def test_control_point_sunday(core, db):
    # весь блок 0 введён и доведён до ступени 3
    ids = [r[0] for r in db.execute("SELECT id FROM items WHERE unit = '0' AND kind = 'phrase'")]
    now = core.clock.now()
    for i in ids:
        t = core.english.intro_tasks(i, Mode.VOICE)[0]
        core.english.record(t, None, judge=__import__("studybot.enums", fromlist=["Judge"]).Judge.CODE,
                            mode=Mode.VOICE, day=core.engine.today(), now=now)
    db.execute("UPDATE item_state SET stage = 3")
    core.clock.set(datetime(2026, 10, 4, 12, 0, tzinfo=MSK))  # воскресенье
    info = core.engine.start("long")
    kinds = [r[0] for r in db.execute("SELECT kind FROM session_steps WHERE session_id = ?", (info.id,))]
    assert kinds.count("task") >= 10 and "cp_questions" in kinds and "cp_monologue" in kinds
    result = None
    while True:
        step = run(core.engine.next())
        if step is None:
            break
        core.clock.advance(seconds=15)
        if step.kind == "task":
            t = step.task()
            if t.by_meaning:
                core.fake.push({"verdict": "acceptable", "tag": None, "explanation": "", "correction": ""})
            run(core.engine.answer(step.id, t.expected[0] if t.expected else "I don't know."))
        elif step.kind == "cp_questions":
            core.fake.push({"items": [{"question": f"Q{k}?", "ok": True, "fix": ""} for k in range(5)]})
            run(core.engine.answer(step.id, "Q0? Q1? Q2? Q3? Q4?"))
        elif step.kind == "cp_monologue":
            core.fake.push({"verdict": "passed", "covered": [], "missing": [], "errors": [], "comment": ""})
            fb = run(core.engine.answer(step.id, "Let me think. I'm ready.", voice_sec=40))
            result = fb.control_result
        elif step.kind == "dialog":
            while True:
                core.fake.push({"reply": "Good. Bye!", "end": True})
                fb = run(core.engine.answer(step.id, "OK."))
                if fb.finished_step:
                    core.fake.push({"corrections": []})
                    break
        else:
            core.engine.defer(step.id)
    assert result and result["passed"] and result["ru"] == [10, 10]
    assert db.execute("SELECT cp_passed_at IS NOT NULL FROM en_blocks WHERE block = '0'").fetchone()[0] == 1
    # лестница от зачёта не сдвинулась
    assert db.execute("SELECT count(*) FROM item_state WHERE stage != 3").fetchone()[0] == 0


def test_two_weeks_simulation(core, db):
    """14 дней: вечерние сессии, 15 % ошибок; потолки и лимиты держатся."""
    import random
    rnd = random.Random(3)
    for day in range(14):
        core.engine.start("30")
        e = core.engine
        while True:
            step = run(e.next())
            if step is None:
                break
            core.clock.advance(seconds=25)
            if step.kind == "task":
                t = step.task()
                if t.format == "intro" or rnd.random() > 0.15:
                    text = t.expected[0] if t.expected else "Hi, I'm Sam."
                    if t.by_meaning:
                        core.fake.push({"verdict": "acceptable", "tag": None, "explanation": "", "correction": ""})
                else:
                    text = "wrong answer"
                    core.fake.push({"verdict": "error", "tag": "word", "explanation": "Не то.", "correction": ""})
                run(e.answer(step.id, text))
            elif step.kind == "dialog":
                core.fake.push({"reply": "Nice. Bye!", "end": True}, {"corrections": []})
                run(e.answer(step.id, "I'm fine."))
            else:
                e.defer(step.id)
            core.fake.replies.clear()
        d = e.today()
        assert core.days.reviews_done(d, "en") <= 40
        assert core.queue.new_used("en", d) <= (10 if d.weekday() >= 5 else 8)
        core.clock.advance(days=1)
        core.clock.set(core.clock.now().replace(hour=16))
    stages = core.english.stage_counts()
    assert stages[4] > 0 and stages[0] < 70
    assert db.execute("SELECT count(*) FROM days WHERE counted = 1").fetchone()[0] >= 12
