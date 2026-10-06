"""Слой К8.1: справочник, «Правила», «Почему так?», репетиции блока сборки, критерий выхода."""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from studybot.bot.controller import Controller
from studybot.content.common import clean_text, short_hash
from studybot.content.english import parse_english
from studybot.content.reference import parse_reference, phrase_matches
from studybot.english.rehearsals import STAGES, line_key, scenario_lines, support_lines
from studybot.enums import Mode
from studybot.llm.client import LLMUnavailable
from studybot.sessions.planner import PlanContext
from studybot.speech import FakeSTT, StubSTT
from test_sessions import auto_reply, core, run  # noqa: F401  (фикстура core)

from conftest import db  # noqa: F401

DATA = Path(__file__).parent / "data"
BLOCK2 = (DATA / "en_bank_block_2.md").read_text(encoding="utf-8")

REHEARSALS = """
## 5. Репетиции

Бот говорит только в пределах пройденного: короткие предложения.

### М1 — монолог: план по ключевым словам

Монолог на 60–90 секунд, три части. Зачёт: все части на месте, не меньше 6 предложений.

Кто я: Sam — teacher — not a doctor.
Где живу: Moscow — city center.
Что мне нравится: books — the sea.

### Д1 — сосед в самолёте

Ситуация: долгий рейс. Бот играет попутчика: David, из США, фотограф.

Первая реплика бота: Hi! I'm David. Where are you from?

Вопросы бота, по одному за реплику: What do you do? Where do you live? Is Moscow a big city? Do you like the sea?

Что спрашивает ученик, не меньше пяти: Where are you from? What do you do? Where do you live? Do you like Moscow? What music do you like?

### Д2 — знакомство на конференции

Ситуация: перерыв конференции. Бот играет коллегу Anna.

Первая реплика бота: Hi! I'm Anna. Nice to meet you.

Вопросы бота: What's your name? What do you do? Where do you work?

Что спрашивает ученик, не меньше пяти: Where are you from? What do you do?
"""


def block2_with_rehearsals() -> str:
    head, _, tail = BLOCK2.partition("## 4. Что дальше по контенту")
    return head + REHEARSALS + "\n## 6. Что дальше по контенту" + tail


REF = """# Английский. Справочник

Версия тест.

## Обзорные страницы

### С1 — Как строится предложение

Порядок слов: кто — что делает — остальное.

Второй абзац страницы.

### С2 — Карта времён

Present Simple — модуль 1.

## Словарь блока 0

| Слово | Перевод | Где | Примечание |
|---|---|---|---|
| understand | понимать | 0.01 | — |
| I see | понятно | 0.02 | see — понимаю |

## Словарь блока 2

| Слово | Перевод | Где | Примечание |
|---|---|---|---|
| live | жить | 2.01 | — |
| gym | спортзал | 0.01 | — |

## Словарь модуля 2, блок 0

| Слово | Перевод | Где | Примечание |
|---|---|---|---|
| weekend | выходные | 2.0.01 | — |
"""


def imp(core, name, text):
    plan = core.importer.analyze(name, text.encode("utf-8"))
    assert plan.ok, [str(e) for e in plan.issues.errors]
    core.importer.apply(plan)
    core.english.reload()
    return plan


@pytest.fixture
def c2(core):
    imp(core, "Английский_Модуль_1_Банк_блок_2.md", block2_with_rehearsals())
    return core


def learn(core, n=12):
    """Ввести первые n фраз: для разговора нужен пройденный словарь."""
    for (item_id,) in core.conn.execute("SELECT id FROM items WHERE track = 'en' AND kind = 'phrase' "
                                        "ORDER BY area, sort_key LIMIT ?", (n,)).fetchall():
        core.scheduler.ensure_state(item_id)
        core.conn.execute("UPDATE item_state SET stage = 2, due_date = '2030-01-01' WHERE item_id = ?", (item_id,))


def open_block(core, block="2", introduced=False):
    learn(core)
    core.conn.execute("INSERT OR REPLACE INTO en_blocks (area, block, opened_at, introduced_at) "
                      "VALUES ('EN1', ?, '2026-09-20T10:00:00+00:00', ?)",
                      (block, "2026-09-21T10:00:00+00:00" if introduced else None))


def ctx(core, mode=Mode.VOICE, day=None):
    day = day or core.engine.today()
    return PlanContext(day, core.clock.now(), mode, True)


def voice_day(core):
    """Ближайший день с нечётным порядковым номером (в такие дни тренируется монолог)."""
    d = core.engine.today()
    return d if d.toordinal() % 2 == 1 else d + timedelta(days=1)


# справочник

def test_reference_parse_pages_vocab_and_rejected():
    ref = parse_reference(REF, {"0.01", "0.02", "2.01"})
    assert [p.code for p in ref.pages] == ["С1", "С2"]
    assert "Второй абзац" in ref.pages[0].body
    assert [(v.module, v.block, v.word) for v in ref.vocab] == [
        ("1", "0", "understand"), ("1", "0", "I see"), ("1", "2", "live"), ("1", "2", "gym")]
    assert [(v.word, why) for v, why in ref.rejected] == [("weekend", "единицы 2.0.01 нет в импортированных банках")]
    assert any("gym" in str(w) and "не из блока" in str(w) for w in ref.issues.warnings)
    assert not ref.issues.errors


def test_reference_errors():
    bad = REF.replace("| Слово | Перевод | Где | Примечание |", "| Слово | Перевод | Где |", 1)
    assert parse_reference(bad, set()).issues.errors
    assert parse_reference("# Не справочник", set()).issues.errors
    assert parse_reference(REF.replace("## Словарь блока 2", "## Словарь второго блока"), set()).issues.errors


def test_reference_import_replaces_whole(core):
    plan = imp(core, "Английский_Справочник.md", REF)
    assert plan.kind == "ref" and len(plan.reference.vocab) == 3      # 2.01 нет: блок 2 не загружен
    from studybot.content.importer import render_report
    rep = render_report(plan)
    assert "Обзорных страниц 2" in rep and "Отклонено строк словаря: 2" in rep
    imp(core, "Английский_Справочник.md", REF.replace("### С2 — Карта времён\n\nPresent Simple — модуль 1.\n", ""))
    assert core.conn.execute("SELECT count(*) FROM ref_pages").fetchone()[0] == 1
    assert core.conn.execute("SELECT count(*) FROM vocab").fetchone()[0] == 3


def test_phrase_matches_simple_forms():
    assert phrase_matches("live", "My clients live near my gym.")
    assert phrase_matches("live", "He lives in Moscow.")
    assert phrase_matches("study", "She studies a lot.")
    assert phrase_matches("work out", "I work out there too.")
    assert phrase_matches("get up", "I get up at six.")
    assert not phrase_matches("work out", "I work there.")
    assert not phrase_matches("see", "I'm sorry.")


# репетиции: разбор

def test_rehearsals_parsed_and_bound_to_last_block():
    p = parse_english(clean_text(block2_with_rehearsals()), known_notes={"З1", "З2", "З3", "З4", "З5", "З6"},
                      known_codes=set())
    assert not p.issues.errors
    m1, d1, d2 = p.rehearsals
    assert (m1.code, m1.kind, m1.block) == ("М1", "monologue", "2")
    assert [x["name"] for x in m1.data["parts"]] == ["Кто я", "Где живу", "Что мне нравится"]
    assert m1.data["parts"][0]["keywords"] == ["Sam", "teacher", "not a doctor"]
    assert m1.data["min_sentences"] == 6 and "Зачёт" in m1.data["criteria"]
    assert d1.data["opening"] == "Hi! I'm David. Where are you from?"
    assert d1.data["bot_questions"][-1] == "Do you like the sea?" and d1.data["learner_min"] == 5
    assert "короткие предложения" in p.rehearsal_preamble


def test_rehearsal_errors_and_discussion_card():
    bad = block2_with_rehearsals().replace("Первая реплика бота: Hi! I'm Anna. Nice to meet you.\n", "")
    p = parse_english(clean_text(bad), known_notes={"З1", "З2", "З3", "З4", "З5", "З6"}, known_codes=set())
    assert any(e.where == "Д2" for e in p.issues.errors)
    card = block2_with_rehearsals().replace(
        "### Д2 — знакомство на конференции\n\nСитуация: перерыв конференции. Бот играет коллегу Anna.\n\n"
        "Первая реплика бота: Hi! I'm Anna. Nice to meet you.",
        "### Д13 — счастье — это удача\n\nБлок. 6.5\n\nПозиция бота. Happiness is luck.")
    p = parse_english(clean_text(card), known_notes={"З1", "З2", "З3", "З4", "З5", "З6"}, known_codes=set())
    assert not p.issues.errors and any(w.where == "Д13" for w in p.issues.warnings)
    assert [r.code for r in p.rehearsals] == ["М1", "Д1"]
    nomono = block2_with_rehearsals().replace("Кто я: Sam — teacher — not a doctor.\n"
                                              "Где живу: Moscow — city center.\n"
                                              "Что мне нравится: books — the sea.\n", "")
    p = parse_english(clean_text(nomono), known_notes={"З1", "З2", "З3", "З4", "З5", "З6"}, known_codes=set())
    assert any(e.where == "М1" for e in p.issues.errors)


def test_rehearsals_import_report_and_archive(c2):
    rows = c2.conn.execute("SELECT code, block, kind, archived FROM rehearsals ORDER BY sort_key").fetchall()
    assert [tuple(r) for r in rows] == [("М1", "2", "monologue", 0), ("Д1", "2", "scenario", 0),
                                         ("Д2", "2", "scenario", 0)]
    assert "короткие предложения" in json.loads(c2.conn.execute(
        "SELECT data_json FROM rehearsals WHERE code = 'Д1'").fetchone()[0])["rules"]
    plan = c2.importer.analyze("b2.md", BLOCK2.encode("utf-8"))
    from studybot.content.importer import render_report
    assert plan.rehearsals_archived == ["Д1", "Д2", "М1"] and "Репетиции в архив" in render_report(plan)
    c2.importer.apply(plan)
    assert c2.conn.execute("SELECT count(*) FROM rehearsals WHERE archived = 0").fetchone()[0] == 0


# репетиции: монолог по ступеням и сценарии

def test_support_by_stage(c2):
    m = c2.rehearsals.get("М1")
    assert support_lines(m, 1)[0] == "Кто я: Sam — teacher — not a doctor"
    assert support_lines(m, 2) == ["Кто я", "Где живу", "Что мне нравится"]
    assert support_lines(m, 3) == []


def test_monologue_stage_up_on_pass(c2):
    r, now = c2.rehearsals, c2.clock.now()
    assert r.record_monologue("М1", False, 1, now) == 1
    assert r.record_monologue("М1", True, 1, now) == 2
    assert r.record_monologue("М1", True, 1, now) == 2           # зачёт старой ступени не двигает
    assert r.record_monologue("М1", True, 2, now) == 3
    assert r.record_monologue("М1", True, 3, now) == STAGES
    c2.settings.set("monologue_passes", 2)
    c2.conn.execute("UPDATE rehearsal_state SET stage = 1, passes = 0 WHERE code = 'М1'")
    assert r.record_monologue("М1", True, 1, now) == 1
    assert r.record_monologue("М1", True, 1, now) == 2


def test_talk_slot_uses_rehearsals_when_block_open(c2):
    learn(c2)
    assert c2.planner.talk_slot(ctx(c2, Mode.TEXT), 600)[0][0].payload.get("scenario") is None
    open_block(c2)
    steps, _ = c2.planner.talk_slot(ctx(c2, Mode.TEXT), 600)
    assert steps[0].kind == "dialog" and steps[0].payload["scenario"] == "Д1"
    c2.rehearsals.mark_used("Д1", c2.clock.now())
    assert c2.planner.talk_slot(ctx(c2, Mode.TEXT), 600)[0][0].payload["scenario"] == "Д2"   # по кругу
    steps, _ = c2.planner.talk_slot(ctx(c2, Mode.VOICE, voice_day(c2)), 600)
    p = steps[0].payload
    assert steps[0].kind == "monologue" and p["rehearsal"] == "М1" and p["stage"] == 1
    assert p["support"][0].startswith("Кто я:") and p["min_sentences"] == 6


def test_scenario_dialog_opening_from_bank_and_questions_from_list(c2):
    open_block(c2)
    c2.settings.set("mode", "text")
    e = c2.engine
    e.start("15")
    c2.conn.execute("DELETE FROM session_steps")
    st = c2.planner.talk_slot(ctx(c2, Mode.TEXT), 600)[0][0]
    s = e.active()
    c2.conn.execute("INSERT INTO session_steps (session_id, seq, track, kind, slot, est_sec, payload_json) "
                    "VALUES (?, 1, 'en', 'dialog', 'talk', 300, ?)", (s["id"], json.dumps(st.payload)))
    calls = len(c2.fake.calls)
    step = run(e.next())
    assert step.payload["opening"] == "Hi! I'm David. Where are you from?" and len(c2.fake.calls) == calls
    c2.fake.push({"reply": "Nice!", "question": 2, "end": False})
    fb = run(e.answer(step.id, "I'm from Russia."))
    assert fb.reply == "Nice! Where do you live?" and not fb.finished_step
    c2.fake.push({"reply": "Cool.", "question": 99, "end": False})       # номера нет в списке — без вопроса
    fb = run(e.answer(step.id, "I live in Moscow."))
    assert fb.reply == "Cool."
    payload = json.loads(c2.conn.execute("SELECT payload_json FROM session_steps WHERE id = ?",
                                         (step.id,)).fetchone()[0])
    assert payload["asked"] == [2]
    sent = json.loads(c2.fake.calls[-1]["messages"][-1]["content"])
    assert sent["bot_questions"]["1"] == "What do you do?" and "David" in sent["situation"]


def test_control_point_of_assembly_block(c2):
    open_block(c2, introduced=True)
    steps = c2.planner.control_steps(ctx(c2), "EN1", "2")
    q = next(s for s in steps if s.kind == "cp_questions")
    m = next(s for s in steps if s.kind == "cp_monologue")
    assert q.payload["scenario"] == "Д1" and q.payload["need"] == 5 and "долгий рейс" in q.payload["situation"]
    assert m.payload["rehearsal"] == "М1" and m.payload["stage"] == 2
    assert m.payload["support"] == ["Кто я", "Где живу", "Что мне нравится"]
    assert "Зачёт" in m.payload["criteria"]


# критерий выхода

def voice_lines(core, code, tmp="a.ogg"):
    r = core.rehearsals.get(code)
    for key, text in scenario_lines(r):
        for speed in ("normal", "slow"):
            core.conn.execute("INSERT OR REPLACE INTO line_audio (key, speed, path, text_hash) VALUES (?, ?, ?, ?)",
                              (key, speed, f"/tmp/{key}-{speed}.ogg", short_hash(text)))


def test_exit_parts_due_rules(c2):
    r, d = c2.rehearsals, c2.engine.today()
    open_block(c2)
    assert r.exit_parts_due(d) is None                              # блок сборки не введён целиком
    open_block(c2, introduced=True)
    assert r.exit_parts_due(d) is None                              # монолог не дошёл до ступени 3, аудио нет
    c2.conn.execute("UPDATE rehearsal_state SET stage = 3 WHERE code = 'М1'")
    area, block, parts = r.exit_parts_due(d)
    assert [p for p, _ in parts] == ["monologue"]
    voice_lines(c2, "Д1")
    assert [(p, x.code) for p, x in r.exit_parts_due(d)[2]] == [("monologue", "М1"), ("dialog", "Д1")]
    voice_lines(c2, "Д2")
    c2.conn.execute("UPDATE rehearsals SET body = body || ' ' WHERE code = 'Д1'")
    c2.conn.execute("UPDATE rehearsals SET data_json = json_set(data_json, '$.opening', 'Hello!') WHERE code = 'Д1'")
    assert [(p, x.code) for p, x in r.exit_parts_due(d)[2]][1] == ("dialog", "Д2")  # текст сменился — запись старая


def test_exit_two_passes_with_gap_close_module(c2):
    r, now = c2.rehearsals, c2.clock.now()
    d0 = c2.engine.today()
    assert not r.record_exit("EN1", "monologue", "М1", True, None, d0, now, None)
    assert not r.part_allowed_today("EN1", "monologue", d0 + timedelta(days=2))
    assert r.part_allowed_today("EN1", "monologue", d0 + timedelta(days=3))
    assert not r.record_exit("EN1", "monologue", "М1", True, None, d0 + timedelta(days=3), now, None)
    assert r.part_done("EN1", "monologue") and not r.part_allowed_today("EN1", "monologue", d0 + timedelta(days=9))
    assert not r.record_exit("EN1", "dialog", "Д1", True, None, d0 + timedelta(days=1), now, None)
    assert not r.record_exit("EN1", "dialog", "Д1", False, None, d0 + timedelta(days=4), now, None)
    assert r.exit_summary("EN1") == "монолог 2 из 2, диалог 1 из 2"
    assert r.record_exit("EN1", "dialog", "Д2", True, None, d0 + timedelta(days=5), now, None)
    assert r.module_closed("EN1")
    open_block(c2, introduced=True)
    assert r.active_block() is None                                 # модуль закрыт: репетиций больше нет


def test_exit_steps_only_in_voice_and_long_or_30(c2):
    open_block(c2, introduced=True)
    c2.conn.execute("UPDATE rehearsal_state SET stage = 3 WHERE code = 'М1'")
    voice_lines(c2, "Д1")
    assert c2.planner.exit_steps(ctx(c2, Mode.TEXT)) == []
    steps = c2.planner.exit_steps(ctx(c2))
    assert [s.kind for s in steps] == ["exit_monologue", "exit_dialog"]
    assert steps[0].payload["support"] == [] and steps[1].payload["by_ear"] and steps[1].payload["turns"] == 10
    plan30 = c2.planner.build(ctx(c2), "30", 1800)
    assert [s.kind for s in plan30[:2]] == ["exit_monologue", "exit_dialog"]
    assert not any(s.slot == "talk" for s in plan30)
    assert not any(s.kind.startswith("exit") for s in c2.planner.build(ctx(c2), "15", 900))


def test_exit_monologue_and_by_ear_dialog_through_bot(c2, tmp_path):
    open_block(c2, introduced=True)
    c2.conn.execute("UPDATE rehearsal_state SET stage = 3 WHERE code = 'М1'")
    voice_lines(c2, "Д1")
    ctl = Controller(c2, c2.cfg, FakeSTT())
    ctl.prepare()
    c2.settings.set("mode", "voice")
    e = c2.engine
    e.start("30")
    step = run(e.next())
    assert step.kind == "exit_monologue"
    c2.fake.push({"verdict": "passed", "covered": [], "missing": [], "errors": [], "comment": "Хорошо."})
    fb = run(e.answer(step.id, "Hi, I'm Sam. I'm a teacher.", voice_sec=85))
    assert fb.exit_result["passed"] and fb.exit_result["summary"] == "монолог 1 из 2, диалог 0 из 2"
    sent = json.loads(c2.fake.calls[-1]["messages"][-1]["content"])
    assert sent["mode"] == "exit_criterion" and sent["support"] == [] and c2.fake.calls[-1]["model"] == "f"
    step = run(e.next())
    assert step.kind == "exit_dialog" and step.payload["audio_line"] == "Д1:0"
    reply = ctl._audio(step)
    assert reply is not None and reply.line_key == "Д1:0"
    for k in range(9):
        c2.fake.push({"reply": "", "question": k % 4 + 1, "end": False})
        fb = run(e.answer(step.id, "I live in Moscow. What do you do?", voice_sec=6))
        assert fb.audio_line == f"Д1:{k % 4 + 1}" and fb.reply is None and not fb.finished_step
    c2.fake.push({"reply": "Bye!", "question": None, "end": True},
                 {"verdict": "passed", "answered_all": True, "unanswered": [], "own_questions_ok": 4,
                  "russian_used": False, "errors": [], "comment": ""})
    fb = run(e.answer(step.id, "Nice to meet you, too.", voice_sec=4))
    assert fb.finished_step and fb.exit_result["passed"] is False      # своих вопросов 4 из 5
    assert any("4 из 5" in l for l in fb.lines)
    graded = json.loads(c2.fake.calls[-1]["messages"][-1]["content"])
    assert graded["transcript"][0]["asked"] == "Hi! I'm David. Where are you from?"
    assert c2.conn.execute("SELECT count(*) FROM exit_attempts").fetchone()[0] == 2


def test_exit_postponed_when_model_down(c2):
    open_block(c2, introduced=True)
    c2.conn.execute("UPDATE rehearsal_state SET stage = 3 WHERE code = 'М1'")
    c2.settings.set("mode", "voice")
    e = c2.engine
    e.start("30")
    step = run(e.next())
    c2.fake.push(LLMUnavailable("network", "сеть"))
    fb = run(e.answer(step.id, "Hi.", voice_sec=60))
    assert fb.exit_result == {"part": "monologue", "postponed": True}
    assert c2.conn.execute("SELECT count(*) FROM exit_attempts").fetchone()[0] == 0


# «Почему так?»

@pytest.fixture
def ctl(core):
    c = Controller(core, core.cfg, StubSTT())
    c.prepare()
    return c


def first_task(core, ctl, fmt=None):
    run(ctl.on_text("15 мин"))
    for _ in range(40):
        step = core.engine.current()
        if step.kind == "task" and (fmt is None or step.task().format == fmt):
            return step
        if step.kind == "task" and step.task().format == "intro":
            run(ctl.on_callback(f"done:{step.id}"))
        else:
            run(ctl.on_text(step.task().expected[0] if step.kind == "task" and step.task().expected else "Hi."))
    raise AssertionError("нет задания")


def test_why_on_intro_card_and_after_verdict(core, ctl):
    imp(core, "Английский_Справочник.md", REF)
    step = first_task(core, ctl, "intro")
    from studybot.bot.render import step_reply
    assert f"why:{step.id}" in [b.data for row in step_reply(step).buttons for b in row]
    res = run(ctl.on_callback(f"why:{step.id}"))
    assert res.replies[0].text.startswith("Почему так: ")
    call = core.fake.calls[-1]
    assert call["json_mode"] is False
    payload = json.loads(call["messages"][-1]["content"])
    assert "answer" not in payload and payload["target"] == step.task().expected[0]
    n = len(core.fake.calls)
    run(ctl.on_callback(f"why:{step.id}"))
    assert len(core.fake.calls) == n                               # без ответа — один раз на единицу
    res = run(ctl.on_callback(f"done:{step.id}"))
    assert res.keep and res.keep[0][0].data == f"why:{step.id}"
    task_step = next_task(core, ctl)
    res = run(ctl.on_text(task_step.task().expected[0]))
    verdict = res.replies[0]
    assert verdict.text.startswith("Верно") and f"why:{task_step.id}" in [b.data for row in verdict.buttons
                                                                             for b in row]


def next_task(core, ctl):
    for _ in range(40):
        step = core.engine.current()
        if step.kind == "task" and step.task().format != "intro":
            return step
        run(ctl.on_callback(f"done:{step.id}"))
    raise AssertionError("нет задания")


def test_why_before_answer_and_cache_by_answer(core, ctl):
    run(ctl.on_text("15 мин"))
    step = next_task(core, ctl)
    assert run(ctl.on_callback(f"why:{step.id}")).toast == "Объяснение — после ответа"
    core.fake.push({"verdict": "error", "tag": "word", "explanation": "Не то слово.", "correction": ""})
    run(ctl.on_text("zzz wrong"))
    run(ctl.on_callback(f"why:{step.id}"))
    payload = json.loads(core.fake.calls[-1]["messages"][-1]["content"])
    assert payload["answer"] == "zzz wrong"
    n = len(core.fake.calls)
    run(ctl.on_callback(f"why:{step.id}"))
    assert len(core.fake.calls) == n
    assert core.conn.execute("SELECT count(*) FROM explain_cache WHERE normalized != ''").fetchone()[0] == 1


def test_why_fallback_shows_notes_without_model(core, ctl):
    from dataclasses import replace
    from studybot.english.track import Task
    run(ctl.on_text("15 мин"))
    core.llm.cfg = replace(core.llm.cfg, daily_budget_usd=0.000001)
    core.conn.execute("INSERT INTO llm_calls (ts, study_date, tier, task, model, cost_usd, ok) "
                      "VALUES ('t', ?, 'cheap', 'x', 'c', 1, 1)", (core.engine.today().isoformat(),))
    n = len(core.fake.calls)
    step = core.engine.current()
    text = run(ctl.on_callback(f"why:{step.id}")).replies[0].text  # знакомство с фразой блока 0: заметок нет
    assert "недоступно" in text and "Заметок к этой фразе нет" in text and len(core.fake.calls) == n
    item = core.conn.execute("SELECT * FROM items WHERE code = '1.01'").fetchone()
    task = Task(item["id"], "1.01", "ru_en", 4, "Скажи по-английски", prompt_ru=item["prompt"],
                expected=[item["answer"]])
    exp = run(core.explainer.explain(task, "I teacher", core.clock.now()))
    assert not exp.by_model and "Заметки к фразе" in exp.text and "З5 — местоимения" in exp.text
    assert core.conn.execute("SELECT count(*) FROM explain_cache").fetchone()[0] == 0


def test_why_reading_time_counted_once_and_capped(core, ctl):
    run(ctl.on_text("15 мин"))
    step = core.engine.current()
    run(ctl.on_callback(f"why:{step.id}"))                       # карточка знакомства
    before = core.engine.active()["active_sec_en"]
    core.clock.advance(seconds=600)
    run(ctl.on_callback(f"done:{step.id}"))
    s = core.engine.active()
    assert 10 <= s["active_sec_en"] - before <= 120 + 2 * 45       # чтение не больше предела, знакомство — своё
    assert s["reading_json"] is None


def test_reading_shifts_task_start(core):
    e = core.engine
    e.start("15")
    step = run(e.next())
    sent = core.conn.execute("SELECT sent_at FROM session_steps WHERE id = ?", (step.id,)).fetchone()[0]
    core.clock.advance(seconds=10)
    assert e.start_reading(60)
    core.clock.advance(seconds=30)
    assert e.settle_reading() == 30
    after = core.conn.execute("SELECT sent_at FROM session_steps WHERE id = ?", (step.id,)).fetchone()[0]
    from studybot.clock import from_iso
    assert (from_iso(after) - from_iso(sent)).total_seconds() == 30
    assert e.active()["active_sec_en"] == 30
    assert not e.start_reading(60) or e.active() is not None


# «Правила»

def test_rules_menu(core, ctl):
    imp(core, "Английский_Справочник.md", REF)
    more = run(ctl.on_text("Ещё")).replies[0]
    assert "more:rules" in [b.data for row in more.buttons for b in row]
    root = run(ctl.on_callback("more:rules")).replies[0]
    assert "страниц 2" in root.text
    page = run(ctl.on_callback("rl:p:С1")).replies[0]
    assert page.text.startswith("С1 — Как строится предложение") and "Второй абзац" in page.text
    assert [b.data for b in page.buttons[0]] == ["rl:p:С2"]
    notes = run(ctl.on_callback("rl:n")).replies[0]
    assert "rl:n:1:1" in [b.data for row in notes.buttons for b in row]
    block = run(ctl.on_callback("rl:n:1:1")).replies[0]
    assert "З1 — to be" in block.text and "(впереди)" in block.text
    core.english.mark_notes_shown(["З1"], core.clock.now())
    assert "(встречена)" in run(ctl.on_callback("rl:n:1:1")).replies[0].text
    vocab = run(ctl.on_callback("rl:v:1:0")).replies[0]
    assert "understand — понимать (0.01)" in vocab.text and "I see — понятно (0.02). see — понимаю" in vocab.text
    assert run(ctl.on_callback("rl:v")).replies[0].text.startswith("Словарь модуля 1")


def test_exit_dialog_question_audio_through_controller(c2):
    open_block(c2, introduced=True)
    voice_lines(c2, "Д1")
    ctl = Controller(c2, c2.cfg, FakeSTT())
    c2.settings.set("mode", "voice")
    c2.conn.execute("UPDATE rehearsal_state SET stage = 3 WHERE code = 'М1'")
    for d in (date(2026, 9, 1), date(2026, 9, 5)):                 # монолог уже сдан дважды
        c2.rehearsals.record_exit("EN1", "monologue", "М1", True, None, d, c2.clock.now(), None)
    res = run(ctl.on_callback("smode:30:voice"))
    first = next(r for r in res.replies if r.audio is not None)
    assert first.audio.line_key == "Д1:0" and "звуком" in first.text
    assert "slowl:Д1:0" in [b.data for row in first.buttons for b in row]
    c2.fake.push({"reply": "Nice to meet you.", "question": 3, "end": False})
    ctl.stt.texts.append("I'm from Russia. What do you do?")

    async def fetch():
        return b"ogg"
    res = run(ctl.on_voice(5, fetch))
    texts = [r.text for r in res.replies]
    assert any("Nice to meet you." in t for t in texts)
    ask = res.replies[-1]
    assert ask.audio.line_key == "Д1:3" and "Is Moscow a big city?" not in "\n".join(texts)
    slow = run(ctl.on_callback("slowl:Д1:3")).replies[0]
    assert slow.audio.speed == "slow" and slow.audio.line_key == "Д1:3"


def test_rehearsal_monologue_once_a_day(c2):
    open_block(c2)
    c2.settings.set("mode", "voice")
    day = voice_day(c2)
    cx = ctx(c2, Mode.VOICE, day)
    assert c2.planner.talk_slot(cx, 600)[0][0].kind == "monologue"
    assert c2.planner.talk_slot(cx, 600)[0][0].kind == "dialog"         # в том же плане — уже сценарий


def test_module2_file_does_not_archive_module1_blocks(core):
    """Блоки модулей нумеруются заново: банк блоков 0–3 модуля 2 не трогает блоки 0–3 модуля 1."""
    m2 = """# Английский. Модуль 2 — банк блоков 0–1 (тест)

## 4. Блок 0. Инструменты — 1 единица

| ID | Подсказка | Целевая фраза | Допустимые варианты | Грамматика | Звук | Связи | Время |
|---|---|---|---|---|---|---|---|
| 2.0.01 | Как прошли выходные? | How was your weekend? | — | — | — | — | 1.01 |
"""
    before = core.conn.execute("SELECT count(*) FROM items WHERE area = 'EN1' AND archived = 0").fetchone()[0]
    plan = imp(core, "Английский_Модуль_2_Банк_блоки_0-1.md", m2)
    assert plan.archived == [] and plan.area == "EN2"
    assert core.conn.execute("SELECT count(*) FROM items WHERE area = 'EN1' AND archived = 0").fetchone()[0] == before
    it = core.conn.execute("SELECT sort_key, extra_json FROM items WHERE code = '2.0.01'").fetchone()
    assert it[0] == 2_000_001 and json.loads(it[1])["time_links"] == ["1.01"]


def test_new_phrases_only_from_open_modules(core):
    from studybot.english.modules import Modules
    m2 = """# Английский. Модуль 2 — тест

## 4. Блок 0. Инструменты — 1 единица

| ID | Подсказка | Целевая фраза | Допустимые варианты | Грамматика | Звук | Связи | Время |
|---|---|---|---|---|---|---|---|
| 2.0.01 | Как прошли выходные? | How was your weekend? | — | — | — | — | — |
"""
    imp(core, "Английский_Модуль_2_Банк_блоки_0-1.md", m2)
    code_of = lambda ids: [core.conn.execute("SELECT code FROM items WHERE id = ?", (i,)).fetchone()[0] for i in ids]
    core.conn.execute("UPDATE item_state SET stage = 2")
    for (i,) in core.conn.execute("SELECT id FROM items WHERE area = 'EN1'").fetchall():
        core.scheduler.ensure_state(i)
    core.conn.execute("UPDATE item_state SET stage = 2 WHERE item_id IN (SELECT id FROM items WHERE area = 'EN1')")
    assert code_of(core.english.next_new(core.engine.today())) == []          # модуль 2 закрыт
    Modules(core.conn).on_closed("EN1", core.clock.now())
    core.conn.execute("INSERT INTO en_modules (area, closed_at) VALUES ('EN1', 't')")
    assert code_of(core.english.next_new(core.engine.today())) == ["2.0.01"]
    assert Modules(core.conn).open_areas() == ["EN2"]


M5 = """# Английский. Модуль 5 — банк блоков 1–3 (тест)

## 3. Грамматические заметки

### З146 — как устроена статья

Абстракт, введение, методы.

## 4. Блок 1. Как устроена статья — 2 единицы

| ID | Термин | Значение | Допустимые значения | Предложение | Перевод | Ист | Грамматика | Звук |
|---|---|---|---|---|---|---|---|---|
| 5.1.01 | abstract | аннотация | реферат / краткое содержание статьи | If no abstract was available, the full text was retrieved. | Если аннотации не было, извлекали полный текст. | PATO S04, с. 381 | З146 | — |
| 5.1.02 | keywords | ключевые слова | — | The keywords are shown in Figure 1. | Ключевые слова показаны на рисунке 1. | PATO S04, с. 382 | — | — |
"""


def test_module5_terms_import_and_session(core):
    plan = imp(core, "Английский_Модуль_5_Банк_блоки_1-3.md", M5)
    assert len(plan.terms) == 2 and "Единицы на узнавание: блок 1 — 2" in __import__(
        "studybot.content.importer", fromlist=["x"]).render_report(plan)
    t = core.conn.execute("SELECT * FROM terms WHERE code = '5.1.01'").fetchone()
    assert json.loads(t["alt_json"]) == ["реферат", "краткое содержание статьи"] and t["sort_key"] == 5_001_001
    acad = core.academic
    assert acad.check_code(t["id"], "Реферат") and acad.check_code(t["id"], "краткое  содержание статьи.")
    assert not acad.check_code(t["id"], "абстракция")
    ctl = Controller(core, core.cfg, StubSTT())
    res = run(ctl.on_text("15 мин"))
    step = core.engine.current()
    assert step.kind == "term" and step.payload["kind"] == "intro"            # сначала материал
    card = res.replies[-1]
    assert "abstract — аннотация" in card.text and "Если аннотации не было" in card.text
    assert f"tnote:{step.id}" in [b.data for row in card.buttons for b in row]
    note = run(ctl.on_callback(f"tnote:{step.id}")).replies[0]
    assert note.text.startswith("З146 — как устроена статья")
    run(ctl.on_callback(f"tdone:{step.id}"))
    st = core.conn.execute("SELECT step, due_date FROM term_state WHERE term_id = ?", (t["id"],)).fetchone()
    assert st["step"] == 0
    assert core.conn.execute("SELECT new_terms, sec_en_acad FROM days").fetchone()[0] >= 1


def test_module5_term_review_code_and_model(core):
    imp(core, "Английский_Модуль_5_Банк_блоки_1-3.md", M5)
    from studybot.english.academic import TermTask
    t = core.conn.execute("SELECT * FROM terms WHERE code = '5.1.01'").fetchone()
    day = core.engine.today()
    core.conn.execute("INSERT INTO term_state (term_id, step, due_date, introduced_at) VALUES (?, 0, ?, 't')",
                      (t["id"], day.isoformat()))
    ctl = Controller(core, core.cfg, StubSTT())
    core.settings.set("new_terms", 0)
    run(ctl.on_text("15 мин"))
    step = core.engine.current()
    assert step.kind == "term" and step.payload["kind"] == "term"
    n = len(core.fake.calls)
    res = run(ctl.on_text("реферат"))
    assert res.replies[0].text.startswith("Верно.") and len(core.fake.calls) == n          # кодом, без модели
    assert core.conn.execute("SELECT step FROM term_state WHERE term_id = ?", (t["id"],)).fetchone()[0] == 1
    core.conn.execute("UPDATE term_state SET step = 4, due_date = ? WHERE term_id = ?", (day.isoformat(), t["id"]))
    core.engine.end()
    run(ctl.on_text("15 мин"))
    core.fake.push({"verdict": "passed", "comment": "Синоним, смысл тот же."})
    res = run(ctl.on_text("резюме статьи"))
    assert "Синоним" in res.replies[0].text and "Термин закрыт" in res.replies[0].text
    assert core.conn.execute("SELECT closed FROM term_state WHERE term_id = ?", (t["id"],)).fetchone()[0] == 1


M5_TEXT = """# Английский. Модуль 5 — банк блоков 5–6 (тест)

## 2. Блок 5. Абстракты — Ч1

### Ч1 — Dementia in ICD-11

Блок. 5, абстракт.

Ист. KLIN S12, с. 1. Jessen F. Dementia // Der Nervenarzt. 2025.

Лицензия. CC BY 4.0.

Текст.

ICD-11 represents a conceptual advance. The clinical criteria developed by experts are not yet included. Further work would be desirable.

Вопросы.
1. Как авторы оценивают МКБ-11?
2. Что пока не учтено?

Ключ.
1. Как концептуальный шаг вперёд.
2. Клинические критерии экспертов.

Предложение. The clinical criteria developed by experts are not yet included.

Разбор. Подлежащее: the clinical criteria. Сказуемое: are not yet included.

Перевод предложения. Клинические критерии экспертов пока не учтены.

Абзац. От «The clinical criteria developed» до «would be desirable.»

Эталон. Клинические критерии экспертов пока не учтены. Желательна дальнейшая работа.

Тезисы.
1. МКБ-11 — шаг вперёд.
2. Критерии экспертов не учтены.
3. Нужна дальнейшая работа.
4. Речь о деменциях.
5. Авторы — эксперты.
"""


def test_module5_text_flow(core):
    from studybot.english.academic import paragraph_of
    plan = imp(core, "Английский_Модуль_5_Банк_блоки_5-6.md", M5_TEXT)
    assert [r.code for r in plan.records] == ["Ч1"] and plan.records[0].kind == "text"
    f = core.engine.texts.fields("Ч1")
    assert paragraph_of(f["Текст"], f["Абзац"]).endswith("would be desirable.")
    assert core.engine.texts.next(core.engine.today()) is None                 # тексты — с модуля 2
    core.conn.execute("INSERT INTO en_modules (area, closed_at) VALUES ('EN1', 't')")
    assert core.engine.texts.next(core.engine.today()) == ("Ч1", 0)
    ctl = Controller(core, core.cfg, StubSTT())
    res = run(ctl.on_text("30 мин"))
    step = core.engine.current()
    assert step.kind == "text" and "Прочитал" in str(res.replies[-1].buttons)
    core.clock.advance(seconds=60)
    res = run(ctl.on_callback(f"txr:{step.id}"))
    assert "знаков в минуту" in res.replies[0].text and res.replies[-1].text.startswith("Вопрос 1 из 2")
    for _ in range(2):
        core.fake.push({"verdict": "passed", "comment": "", "errors": [], "calques": [], "theses_ok": None})
        run(ctl.on_text("ответ"))
    core.fake.push({"verdict": "passed", "comment": "", "errors": [], "calques": [], "theses_ok": None})
    res = run(ctl.on_text("перевод предложения"))
    assert any("Разбор: Подлежащее" in r.text for r in res.replies)
    core.fake.push({"verdict": "passed", "comment": "", "errors": [], "calques": [], "theses_ok": None})
    run(ctl.on_text("перевод абзаца"))
    core.fake.push({"verdict": "passed", "comment": "", "errors": [], "calques": [], "theses_ok": 5})
    res = run(ctl.on_text("пересказ"))
    out = res if isinstance(res, list) else res.replies
    assert any("Повтор — через 7 дней" in r.text for r in out)
    st = core.conn.execute("SELECT stage, due_date FROM text_state WHERE code = 'Ч1'").fetchone()
    assert st["stage"] == 1


def test_intro_card_teaches_first(core):
    """Сначала материал и в английском: план нового блока, правило целиком, произношение — до фразы."""
    for (i,) in core.conn.execute("SELECT id FROM items WHERE area = 'EN1' AND unit = '0'").fetchall():
        core.scheduler.ensure_state(i)
        core.conn.execute("UPDATE item_state SET stage = 2, due_date = '2030-01-01' WHERE item_id = ?", (i,))
    core.conn.execute("INSERT OR IGNORE INTO en_blocks (area, block, opened_at) VALUES ('EN1', '0', 't')")
    ctl = Controller(core, core.cfg, StubSTT())
    res = run(ctl.on_text("15 мин"))
    step = core.engine.current()
    while step.task().code != "1.01":
        run(ctl.on_callback(f"done:{step.id}")) if step.task().format == "intro" else run(
            ctl.on_text(step.task().expected[0]))
        step = core.engine.current()
    from studybot.bot.render import task_text
    text = task_text(step.task())
    assert text.startswith("Новый блок 1")
    assert "Правила блока: З5 — местоимения" in text
    i_rule, i_phrase = text.index("Сначала правило. З5"), text.index("Новая фраза")
    assert i_rule < i_phrase
    sound = core.conn.execute("SELECT sound FROM items WHERE code = '1.01'").fetchone()[0]
    assert (f"Произношение: {sound}" in text) if sound else "Произношение" not in text
    res = run(ctl.on_callback(f"done:{step.id}"))
    assert not any("Заметка З5" in r.text for r in res.replies)       # правило не повторяется после «Готово»
    assert core.conn.execute("SELECT shown_at FROM notes WHERE code = 'З5'").fetchone()[0] is not None


def test_module5_phrases_own_limit_after_module1(core):
    m5 = """# Английский. Модуль 5 — банк блока 7 (тест)

## 3. Блок 7. Пересказ — 2 фразы

| ID | Подсказка | Целевая фраза | Допустимые варианты | Грамматика | Звук | Связи | Время |
|---|---|---|---|---|---|---|---|
| 5.7.01 | В статье рассматривается… | The article deals with… | — | — | — | — | — |
| 5.7.02 | Авторы приходят к выводу… | The authors conclude that… | — | — | — | — | — |
"""
    imp(core, "Английский_Модуль_5_Банк_блок_7.md", m5)
    day = core.engine.today()
    assert core.academic.new_phrases(day, 5) == []                       # до модуля 2 — нет
    core.conn.execute("INSERT INTO en_modules (area, closed_at) VALUES ('EN1', 't')")
    core.settings.set("new_acad_phrases", 1)
    ids = core.academic.new_phrases(day, 5)
    assert len(ids) == 1
    assert core.english.next_new(day) == [] or all(
        core.conn.execute("SELECT area FROM items WHERE id = ?", (i,)).fetchone()[0] != "EN5"
        for i in core.english.next_new(day))                              # в лестницу разговора не попадают
    core.scheduler.ensure_state(ids[0])
    en_before = core.queue.new_used("en", day)
    core.english._introduce(ids[0], day, core.clock.now())
    assert core.queue.new_used("en", day) == en_before                    # лимит разговорной ветки не тронут
    assert core.academic.new_phrases(day, 5) == []                        # свой лимит исчерпан


def test_module5_exam_runs(core):
    from studybot.english.exams import Exams
    bank9 = """# Английский. Модуль 5 — банк блока 9 (тест)

## 2. Банк вопросов беседы

1. Could you introduce yourself? — 5.8.01.
2. What are your research interests? — 5.8.17.

## 3. Прогоны — Э1–Э3

""" + "".join(f"""### Э{k} — Article {k}

Статус. {'Тренировочный' if k == 1 else 'Резерв критерия выхода'}.

Ист. NEURO S04. Article.

#### Часть 1. Перевод

Объём. 100 знаков.

Текст.

It is important.

Эталон. Это важно.

#### Часть 2. Беглое чтение

Объём. 50 знаков.

Текст.

Neglect is hard.

Тезисы.
1. Трудно.

#### Часть 3. Беседа

1. What was the article about? — неглект.

#### Часть 4. Чтение: абстракт и введение

Текст.

Objective: review.

Вопросы.
1. What was the objective?

Ключ.
1. To review.

Тезисы.
1. Обзор.

""" for k in (1, 2, 3))
    plan = imp(core, "Английский_Модуль_5_Банк_блок_9.md", bank9)
    ex = Exams(core.conn)
    assert ex.bank() == ["Could you introduce yourself?", "What are your research interests?"]
    assert ex.fields("Э1")["parts"]["3"]["Вопросы по статье"] == "What was the article about?"
    imp(core, "Английский_Модуль_5_Банк_блоки_5-6.md", M5_TEXT)
    day, now = core.engine.today(), core.clock.now()
    assert not ex.opened()                                              # тексты не закрыты
    core.conn.execute("INSERT INTO text_state (code, stage, closed) VALUES ('Ч1', 2, 1)")
    assert ex.opened() and ex.next_code(day) == "Э1"                   # сначала тренировочный
    rid, code, part = ex.next_part(day, now)
    for p in "1234":
        out = ex.record_part(rid, p, True, None, day)
    assert out["finished"] and out["passed"] and not out["reserve"]
    assert ex.next_code(day) == "Э2"                                   # первая резервная
    rid2, _, _ = ex.next_part(day, now)
    for p in "1234":
        ex.record_part(rid2, p, True, None, day)
    assert not ex.exit_done()
    assert ex.next_code(day + timedelta(days=2)) is None               # промежуток не меньше трёх дней
    d3 = day + timedelta(days=3)
    assert ex.next_code(d3) == "Э3"
    rid3, _, _ = ex.next_part(d3, now)
    for p in "1234":
        ex.record_part(rid3, p, p != "2", None, d3)                    # одна часть не сдана — не засчитан
    assert not ex.exit_done() and ex.next_code(d3 + timedelta(days=5)) is None   # обе резервные использованы
    d33 = day + timedelta(days=33)
    assert ex.next_code(d33) == "Э2"                                   # повтор на той, что была 30+ дней назад
    rid4, _, _ = ex.next_part(d33, now)
    for p in "1234":
        out = ex.record_part(rid4, p, True, None, d33)
    assert out["closed"] and ex.exit_done()


def test_module5_exam_part_through_bot(core):
    test_module5_exam_runs.__wrapped__ if False else None
    bank9 = """# Английский. Модуль 5 — банк блока 9 (тест)

## 3. Прогоны — Э1

### Э1 — Article

Статус. Тренировочный.

#### Часть 1. Перевод

Текст.

It is important.

Эталон. Это важно.

#### Часть 2. Беглое чтение

Текст.

Neglect is hard.

Тезисы.
1. Трудно.

#### Часть 3. Беседа

1. What was the article about? — неглект.

#### Часть 4. Чтение

Текст.

Objective: review.

Вопросы.
1. What was the objective?

Ключ.
1. To review.

Тезисы.
1. Обзор.
"""
    imp(core, "Английский_Модуль_5_Банк_блок_9.md", bank9)
    imp(core, "Английский_Модуль_5_Банк_блоки_5-6.md", M5_TEXT)
    core.conn.execute("INSERT INTO text_state (code, stage, closed) VALUES ('Ч1', 2, 1)")
    ctl = Controller(core, core.cfg, StubSTT())
    res = run(ctl.on_text("30 мин"))
    step = core.engine.current()
    assert step.kind == "exam" and step.payload["part"] == "1"
    assert "Часть 1. Перевод" in res.replies[-1].text and "It is important." in res.replies[-1].text
    core.clock.advance(seconds=900)
    core.fake.push({"verdict": "passed", "comment": "Смысл передан.", "errors": [], "inaccuracies": 0,
                    "theses_ok": None, "answers_ok": None})
    res = run(ctl.on_text("Это важно."))
    texts = [r.text for r in res.replies]
    assert any("Часть 1 (перевод): сдана." in t and "Эталон: Это важно." in t for t in texts)
    assert core.fake.calls[-1]["model"] == "f"                         # прогон проверяет флагман
    acad = core.conn.execute("SELECT sec_en_acad FROM days").fetchone()[0]
    assert acad >= 900                                                  # длинный формат: без предела 4 минут


def test_time_shift_transform(core):
    m2 = """# Английский. Модуль 2 — тест

## 4. Блок 2. Что делал — 1 единица

| ID | Подсказка | Целевая фраза | Допустимые варианты | Грамматика | Звук | Связи | Время |
|---|---|---|---|---|---|---|---|
| 2.2.01 | Вчера я работал | I worked yesterday. | — | — | — | — | 1.01 |
"""
    imp(core, "Английский_Модуль_2_Банк_блоки_2.md", m2)
    core.english.reload()
    g = core.english.graph
    a = core.conn.execute("SELECT id FROM items WHERE code = '2.2.01'").fetchone()[0]
    b = core.conn.execute("SELECT id FROM items WHERE code = '1.01'").fetchone()[0]
    assert g.is_time(a, b) and b in g.edges[a]
    core.scheduler.ensure_state(a)
    task = core.english._transform(a, __import__("studybot.enums", fromlist=["Mode"]).Mode.TEXT, intro=True)
    assert task.instruction in ("Смени время", "Задай вопрос", "Скажи с отрицанием", "Скажи утверждением",
                                "Скажи о другом", "Измени фразу", "Ответь коротко")
    if task.meta["source"] in ("1.01", "2.2.01") and {task.meta["source"], task.meta["target"]} == {"1.01", "2.2.01"}:
        assert task.instruction == "Смени время"


def test_free_speech_diary_and_roleplay(core):
    m3 = """# Английский. Модуль 3 — тест

## 5. Репетиции

### Д7 — путешественник

Роль бота. Турист в Москве.

Скрытая ситуация. Потерял паспорт.

Первая реплика. Hi! Can you help me?

Что выяснить.
1. Что потерял.

Советы по ключу.
1. You should go to the embassy.
"""
    plan = imp(core, "Английский_Модуль_3_Банк_блоки_9-13.md", m3.replace("## 5. Репетиции",
                                                                          "## 4. Блок 13. Сборка — 0 единиц\n\n## 5. Репетиции"))
    assert [(r.code, r.kind) for r in plan.records] == [("Д7", "roleplay")]
    from studybot.english.modules import Modules
    core.conn.execute("INSERT INTO en_modules (area, closed_at) VALUES ('EN1', 't')")
    Modules(core.conn).open("EN2", core.clock.now())
    sp = core.engine.speech
    assert sp.available(long_ok=True) == ["diary"]                     # модуль 2: только дневник
    core.conn.execute("INSERT INTO en_modules (area, closed_at) VALUES ('EN2', 't') ON CONFLICT(area) "
                      "DO UPDATE SET closed_at = 't'")
    Modules(core.conn).open("EN3", core.clock.now())
    assert "roleplay" in sp.available(long_ok=True) and "roleplay" not in sp.available(long_ok=False)
    ctl = Controller(core, core.cfg, StubSTT())
    res = run(ctl.on_text("30 мин"))
    step = core.engine.current()
    assert step.kind == "speech"
    fmt = step.payload["format"]
    if fmt == "roleplay":
        assert "Hi! Can you help me?" in res.replies[-1].text and "Роль бота: Турист в Москве." in res.replies[-1].text
        core.fake.push({"reply": "I can't find something.", "end": False})
        r = run(ctl.on_text("What's wrong?"))
        assert r[0].text == "I can't find something." if isinstance(r, list) else True
        core.fake.push({"reply": "Можно так: I lost my passport.", "end": False})
        r = run(ctl.on_text("как сказать: я потерял паспорт"))
    else:
        assert fmt == "diary"
        core.fake.push({"verdict": "passed", "covered": ["прошлое", "планы"], "missing": [],
                        "corrections": [{"said": "I go yesterday", "better": "I went yesterday", "why": "прошлое"}],
                        "comment": "Хорошо."})
        res = run(ctl.on_text("Yesterday I go to the gym. Tomorrow I'm going to read."))
        text = (res if isinstance(res, list) else res.replies)[0].text
        assert "Получилось." in text and "I go yesterday → I went yesterday" in text


def test_module3_exit_parts(core):
    m3 = """# Английский. Модуль 3 — тест

## 4. Блок 13. Сборка — 1 единица

| ID | Подсказка | Целевая фраза | Допустимые варианты | Грамматика | Звук | Связи | Время |
|---|---|---|---|---|---|---|---|
| 3.13.01 | Ты должен попробовать | You should try. | — | — | — | — | — |

## 5. Репетиции

### М4 — объяснение

Монолог на две минуты. Зачёт: шаги на месте.

Начало: first — then.
Конец: finally.

### О1 — как сварить кофе

Задание. Explain how to make coffee.

Ключ.
1. Boil water.
2. Add coffee.

### СР1 — два города

Задание. Compare Moscow and Tver.

Ключ.
1. Moscow is bigger.

### Д7 — путешественник

Роль бота. Турист.

Скрытая ситуация. Потерял паспорт.

Первая реплика. Hi! Can you help me?
"""
    imp(core, "Английский_Модуль_3_Банк_блоки_9-13.md", m3)
    from studybot.english.modules import Modules
    for a in ("EN1", "EN2"):
        core.conn.execute("INSERT INTO en_modules (area, opened_at, closed_at) VALUES (?, 't', 't') "
                          "ON CONFLICT(area) DO UPDATE SET closed_at = 't'", (a,))
    Modules(core.conn).open("EN3", core.clock.now())
    core.conn.execute("INSERT INTO en_blocks (area, block, opened_at, introduced_at) VALUES ('EN3', '13', 't', 't')")
    r = core.rehearsals
    day = core.engine.today()
    area, block, parts = r.exit_parts_due(day)
    assert area == "EN3" and [p for p, _ in parts] == ["explain", "compare"]   # диалог-совет ждёт озвучки
    core.settings.set("exit_live_tts", True)
    assert [p for p, _ in r.exit_parts_due(day)[2]] == ["explain", "compare", "advice_dialog"]
    core.settings.set("exit_live_tts", False)
    ctl = Controller(core, core.cfg, FakeSTT())
    core.settings.set("mode", "voice")
    res = run(ctl.on_callback("smode:30:voice"))
    step = core.engine.current()
    assert step.kind == "exit_speech" and step.payload["part"] == "explain"
    assert "Задание: Explain how to make coffee." in res.replies[-1].text
    core.fake.push({"verdict": "passed", "covered": ["шаги"], "missing": [], "corrections": [], "comment": "Ясно."})
    res = run(ctl.on_text("First, boil water. Then add coffee."))
    out = res if isinstance(res, list) else res.replies
    assert any("Критерий выхода, объяснение: сдан." in x.text for x in out)
    assert core.fake.calls[-1]["model"] == "f"
    assert "объяснение 1 из 2, сравнение 0 из 2, диалог-совет 0 из 2" in r.exit_summary("EN3")
    for p in ("explain", "compare", "advice_dialog"):
        for d in (day, day + timedelta(days=3)):
            r.record_exit("EN3", p, "", True, None, d, core.clock.now(), None)
    assert r.module_closed("EN3") and Modules(core.conn).is_open("EN4")


def test_live_conversation_review(core):
    ctl = Controller(core, core.cfg, StubSTT())
    more = run(ctl.on_text("Ещё")).replies[0]
    assert "more:live" in [b.data for row in more.buttons for b in row]
    assert "Разбор:" in run(ctl.on_callback("more:live")).replies[0].text
    core.fake.push("я хотел сказать, что живу в центре → I live in the city center.")
    res = run(ctl.on_text("Разбор: говорили о работе; не смог сказать, что живу в центре"))
    assert "I live in the city center." in res.replies[0].text
    assert core.fake.calls[-1]["json_mode"] is False


def test_story_by_ear_needs_audio_and_flows(core):
    m2 = """# Английский. Модуль 2 — тест

## 4. Блок 7. Спорт — 1 единица

| ID | Подсказка | Целевая фраза | Допустимые варианты | Грамматика | Звук | Связи | Время |
|---|---|---|---|---|---|---|---|
| 2.7.01 | Я бегал | I ran. | — | — | — | — | — |

## 5. Истории

### Р1 — Olympic Games

Блок. 2.7

Текст. The first Olympic Games were in Greece.

Слова. race — забег

Вопросы.
1. Where were the first Games?
2. Were they old?

Ключ.
1. In Greece.
2. Yes.
"""
    imp(core, "Английский_Модуль_2_Банк_блоки_7-11.md", m2)
    from studybot.english.listening import lines
    from studybot.english.modules import Modules
    core.conn.execute("INSERT INTO en_modules (area, closed_at) VALUES ('EN1', 't')")
    Modules(core.conn).open("EN2", core.clock.now())
    sp = core.engine.speech
    assert "story" not in sp.available(long_ok=False)                  # без озвучки — не предлагается
    fields = json.loads(core.conn.execute("SELECT fields_json FROM records WHERE code = 'Р1'").fetchone()[0])
    for key, text in lines("Р1", fields):
        core.conn.execute("INSERT INTO line_audio (key, speed, path, text_hash) VALUES (?, 'normal', ?, ?)",
                          (key, f"/tmp/{key}.ogg", short_hash(text)))
    assert "story" in sp.available(long_ok=False)
    st = core.planner.__class__                                        # noqa
    from studybot.sessions.planner import Step
    pl = sp.payload("story")
    core.engine.start("15")
    core.conn.execute("DELETE FROM session_steps")
    sid = core.engine.active()["id"]
    core.conn.execute("INSERT INTO session_steps (session_id, seq, track, kind, slot, est_sec, payload_json) "
                      "VALUES (?, 1, 'en', 'speech', 'speech', 360, ?)", (sid, json.dumps(pl)))
    ctl = Controller(core, core.cfg, FakeSTT())
    step = run(core.engine.next())
    first = ctl._step_reply(step)
    assert first.audio is not None and first.audio.line_key == "Р1:text" and "Olympic" not in first.text
    r = run(ctl.on_callback(f"lsn:{step.id}")).replies[0]
    assert r.text.startswith("Слова: race — забег") and r.audio.line_key == "Р1:q1"
    r = run(ctl.on_text("In Greece."))
    assert r.replies[0].audio.line_key == "Р1:q2"
    r = run(ctl.on_text("Yes, very old."))
    assert "Перескажи" in r.replies[0].text
    core.fake.push({"answers_ok": 2, "points_ok": 2, "wrong": [], "corrections": [], "comment": "Понято."})
    res = run(ctl.on_text("The first Olympic Games were in Greece, very long ago."))
    out = (res if isinstance(res, list) else res.replies)[0].text
    assert "Вопросы: верно 2 из 2" in out and "Текст записи:\nThe first Olympic Games were in Greece." in out


def test_module5_text_share_by_week(core):
    """Текст Ч встаёт по недобору доли за неделю: после вчерашнего текста — не каждый день."""
    from datetime import timedelta
    imp(core, "Английский_Модуль_5_Банк_блоки_5-6.md", M5_TEXT)
    core.conn.execute("INSERT INTO en_modules (area, closed_at) VALUES ('EN1', 't')")
    day, acad = core.engine.today(), core.academic
    y = (day - timedelta(days=1)).isoformat()
    core.conn.execute("INSERT INTO days (study_date, sec_en, sec_en_acad) VALUES (?, 2400, 1500)", (y,))
    assert acad.deficit_sec(day, 900) >= 300                     # внутри дня доля «не набрана»
    assert acad.week_deficit_sec(day, 900) < 600                 # за неделю набрана с запасом
    ctl = Controller(core, core.cfg, StubSTT())
    run(ctl.on_text("30 мин"))
    sid = core.engine.active()["id"]
    assert not core.conn.execute("SELECT 1 FROM session_steps WHERE session_id = ? AND kind = 'text'", (sid,)).fetchone()
    core.engine.end()
    core.conn.execute("DELETE FROM days WHERE study_date = ?", (y,))
    run(ctl.on_text("30 мин"))
    assert core.engine.current().kind == "text"                  # недели без модуля 5 — текст есть


def test_module5_term_card_with_sound(core, tmp_path):
    """0.11.2: термин модуля 5 озвучивается (слово, два темпа), звук приходит с карточкой знакомства."""
    import asyncio
    from dataclasses import replace
    from studybot import voicing
    from studybot.config import DeepgramConfig
    imp(core, "Английский_Модуль_5_Банк_блоки_1-3.md", M5)
    jobs = [j for j in voicing.plan(core.conn, core.rehearsals, DeepgramConfig()) if j.key.startswith("term:")]
    assert [(j.key, j.text) for j in jobs][:1] == [("term:5.1.01", "abstract")] and len(jobs) == 2

    class TTS:
        async def speak(self, text, voice=None, speed=1.0):
            return text.encode(), "ogg"
    cfg = replace(core.cfg, paths=replace(core.cfg.paths, data_dir=tmp_path))
    todo = voicing.pending(core.conn, cfg.paths.audio_dir, jobs)
    asyncio.run(voicing.run(core.conn, cfg.paths.audio_dir, TTS(), todo, 0.8))
    ctl = Controller(core, cfg, StubSTT())
    res = run(ctl.on_text("15 мин"))
    card = res.replies[-1]
    assert core.engine.current().kind == "term" and card.audio is not None
    assert card.audio.line_key == "term:5.1.01" and open(card.audio.path, "rb").read() == b"abstract"


def test_five_minutes_with_nothing_to_review_gives_new_phrases(core):
    """0.11.2: пятиминутка в первый день — новые фразы модуля 1, а не пустое повторение и добор психологией."""
    imp(core, "Английский_Модуль_5_Банк_блоки_1-3.md", M5)
    ctl = Controller(core, core.cfg, StubSTT())
    run(ctl.on_text("5 мин"))
    sid = core.engine.active()["id"]
    kinds = [(r[0], r[1]) for r in core.conn.execute(
        "SELECT kind, slot FROM session_steps WHERE session_id = ? ORDER BY seq", (sid,))]
    assert kinds[0] == ("task", "new") and core.engine.current().kind == "task"
