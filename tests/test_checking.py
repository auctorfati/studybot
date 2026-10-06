import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from studybot.checking.disputes import (DisputeError, decide_variant, dispute, pending,
                                        pending_variants, resolve)
from studybot.checking.english import EnglishChecker, log_en_error
from studybot.checking.psychology import PsyChecker, log_psy_gap
from studybot.checking.talk import Dialog, EnglishTalk
from studybot.config import LLMConfig, LLMPrices
from studybot.content.importer import Importer
from studybot.content.sources import SourceLibrary
from studybot.english.track import EnglishTrack
from studybot.enums import Judge, Mode, Rating
from studybot.llm.client import LLM, FakeTransport, LLMUnavailable, OpenAICompatible
from studybot.llm.prompts import Prompts
from studybot.llm.runner import ModelRunner
from studybot.llm.schemas import SchemaError, extract_json, validate
from studybot.review_queue import ReviewQueue
from studybot.scheduler import Scheduler
from studybot.settings import Settings

from helpers import BANK, CORPUS, MAP, ZEIG

ROOT = Path(__file__).resolve().parents[1]
EN_BANK = Path(__file__).parent / "data" / "en_bank_blocks_0-1.md"
MON = date(2026, 9, 28)


def cfg(**kw):
    base = dict(provider="moonshot", base_url="https://api.test/v1", api_key="sk-secret",
                cheap_model="cheap-m", flagship_model="flag-m", daily_budget_usd=0.5,
                prices=LLMPrices(1.0, 4.0, 3.0, 15.0))
    base.update(kw)
    return LLMConfig(**base)


@pytest.fixture
def fake():
    return FakeTransport()


@pytest.fixture
def llm(db, clock, cal, fake):
    return LLM(db, cfg(), clock, cal, fake)


@pytest.fixture
def runner(llm):
    return ModelRunner(llm, Prompts(ROOT / "prompts"))


@pytest.fixture
def en(db, clock, cal, tmp_path):
    imp = Importer(db, clock, cal, SourceLibrary(tmp_path))
    imp.apply(imp.analyze(EN_BANK.name, EN_BANK.read_bytes()))
    st = Settings(db, clock)
    return EnglishTrack(db, st, Scheduler(db, st), ReviewQueue(db, st))


def iid(db, code):
    return db.execute("SELECT id FROM items WHERE code = ?", (code,)).fetchone()[0]


def run(coro):
    return asyncio.run(coro)


# схемы

def test_extract_json_fenced():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(SchemaError):
        extract_json("нет json")


def test_validators():
    ok = validate("check_en", '{"verdict": "acceptable", "tag": null, "explanation": "", "correction": ""}')
    assert ok["verdict"] == "acceptable" and ok["tag"] is None
    with pytest.raises(SchemaError):
        validate("check_en", '{"verdict": "error", "tag": "grammar", "explanation": "x"}')
    with pytest.raises(SchemaError):
        validate("check_psy", '{"verdict": "failed", "present": [], "missing": [], "comment": "", "tag": null}')
    with pytest.raises(SchemaError):
        validate("dialog_corrections", json.dumps({"corrections": [
            {"said": "a", "better": "b", "tag": "word", "why": "c"}] * 4}))
    v = validate("check_vignette", json.dumps({
        "verdict": "passed", "present": ["a"], "missing": [], "outside_source": [], "comment": "с. 1",
        "tag": None, "confusion": None, "mentor": {"matches": False, "divergence": "иначе"}}))
    assert v["mentor"]["divergence"] == "иначе"


def test_prompts_all_present(tmp_path):
    p = Prompts(ROOT / "prompts")
    assert "JSON" in p["check_en"] and "outside_source" in p["check_psy"]
    from studybot.llm.prompts import PromptError
    with pytest.raises(PromptError):
        Prompts(tmp_path)


# клиент

def test_openai_transport_and_cost(db, clock, cal):
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"model": "cheap-m", "choices": [
            {"message": {"content": '{"x": 1}', "reasoning_content": "думаю"}}],
            "usage": {"prompt_tokens": 2000, "completion_tokens": 500}})

    t = OpenAICompatible("https://api.test/v1", "sk-secret",
                         httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    llm = LLM(db, cfg(), clock, cal, t)
    reply = run(llm.call("cheap", "check_en", "sys", {"a": "б"}))
    assert reply.text == '{"x": 1}'
    assert seen["auth"] == "Bearer sk-secret"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert "temperature" not in seen["body"]
    row = db.execute("SELECT * FROM llm_calls").fetchone()
    assert row["tokens_in"] == 2000 and row["cost_usd"] == pytest.approx(0.004)


def test_tier_extra_fields_max_tokens_and_timeout(db, clock, cal):
    """Дешёвой модели — рассуждение выключено, флагману — своя глубина, предел ответа и ожидание."""
    seen = []

    def handler(request: httpx.Request):
        seen.append((json.loads(request.content), request.extensions.get("timeout", {}).get("read")))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    t = OpenAICompatible("https://api.test/v1", "sk-secret",
                         httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    llm = LLM(db, cfg(cheap_extra={"thinking": {"type": "disabled"}},
                      flagship_extra={"reasoning_effort": "low"},
                      flagship_max_tokens=8000, flagship_timeout_sec=120.0), clock, cal, t)
    run(llm.call("cheap", "check_en", "sys", "u"))
    run(llm.call("flagship", "check_vignette", "sys", "u"))
    (cheap, t_cheap), (flag, t_flag) = seen
    assert cheap["thinking"] == {"type": "disabled"} and "reasoning_effort" not in cheap
    assert cheap["max_tokens"] == 1500 and cheap["model"] == "cheap-m" and t_cheap == 60.0
    assert flag["reasoning_effort"] == "low" and "thinking" not in flag
    assert flag["max_tokens"] == 8000 and flag["model"] == "flag-m" and t_flag == 120.0
    # без настроек — запрос как раньше
    seen.clear()
    run(LLM(db, cfg(), clock, cal, t).call("flagship", "check_vignette", "sys", "u"))
    assert set(seen[0][0]) == {"model", "messages", "max_tokens", "response_format"} and seen[0][1] == 60.0


def test_provider_error_hides_key(db, clock, cal):
    t = OpenAICompatible("https://api.test/v1", "sk-secret", httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key sk-secret"))))
    llm = LLM(db, cfg(), clock, cal, t)
    with pytest.raises(LLMUnavailable) as e:
        run(llm.call("cheap", "check_en", "sys", "u"))
    assert "sk-secret" not in str(e.value)
    assert db.execute("SELECT count(*) FROM service_events").fetchone()[0] == 1


def test_budget_and_not_configured(db, clock, cal, fake):
    llm = LLM(db, cfg(daily_budget_usd=0.001), clock, cal, fake)
    fake.push("{}")
    run(llm.call("flagship", "t", "s", "u"))          # 1000*3 + 100*15 = 0.0045 > 0.001
    assert llm.available() == (False, "дневной предел расхода исчерпан")
    with pytest.raises(LLMUnavailable):
        run(llm.call("cheap", "t", "s", "u"))
    assert not LLM(db, cfg(api_key=""), clock, cal).available()[0]


def test_runner_retry_then_fail(runner, fake, db):
    fake.push("не json", {"verdict": "acceptable", "tag": None, "explanation": "", "correction": ""})
    res = run(runner.run("check_en", "cheap", {"a": 1}))
    assert res.data["verdict"] == "acceptable" and len(res.call_ids) == 2
    fake.push("мусор", "снова мусор")
    with pytest.raises(LLMUnavailable) as e:
        run(runner.run("check_en", "cheap", {"a": 1}))
    assert e.value.reason == "invalid"
    assert db.execute("SELECT count(*) FROM llm_calls WHERE ok = 0").fetchone()[0] == 3


# английский

def test_en_code_first_no_model(en, runner, fake, db, clock):
    t = en.task_for(iid(db, "1.07"), Mode.TEXT) or None
    en.record(en.intro_tasks(iid(db, "1.07"), Mode.TEXT)[0], None, judge=Judge.CODE, mode=Mode.TEXT,
              day=MON, now=clock.now())
    t = en.task_for(iid(db, "1.07"), Mode.TEXT)
    c = run(EnglishChecker(db, runner).check(t, t.expected[0].upper(), clock.now()))
    assert c.correct and c.judge == Judge.CODE and not fake.calls
    assert run(EnglishChecker(db, runner).check(t, "  ", clock.now())).correct is False


def test_en_model_verdict_cache_and_variant(en, runner, fake, db, clock):
    en.record(en.intro_tasks(iid(db, "0.16"), Mode.TEXT)[0], None, judge=Judge.CODE, mode=Mode.TEXT,
              day=MON, now=clock.now())
    task = en.task_for(iid(db, "0.16"), Mode.TEXT)
    checker = EnglishChecker(db, runner)
    fake.push({"verdict": "acceptable", "tag": None, "explanation": "Разговорный вариант.", "correction": ""})
    c = run(checker.check(task, "Thank you very much", clock.now()))
    assert c.correct and c.judge == Judge.CHEAP and c.disputable and not c.cached
    payload = json.loads(fake.calls[0]["messages"][-1]["content"])
    assert payload["target"] == "Thank you." and payload["check"] == "exact"
    c2 = run(checker.check(task, "thank you very much!", clock.now()))
    assert c2.cached and len(fake.calls) == 1                         # кэш по нормализованному ответу
    (v,) = pending_variants(db)
    assert v["raw_answer"] == "Thank you very much" and v["seen_count"] == 1
    decide_variant(db, v["id"], True)
    db.execute("DELETE FROM verdict_cache")
    c3 = run(checker.check(task, "Thank you very much.", clock.now()))
    assert c3.correct and c3.judge == Judge.CODE and len(fake.calls) == 1


def test_en_error_logged_and_disputed(en, runner, fake, db, clock):
    en.record(en.intro_tasks(iid(db, "1.07"), Mode.TEXT)[0], None, judge=Judge.CODE, mode=Mode.TEXT,
              day=MON, now=clock.now())
    db.execute("UPDATE item_state SET stage = 2, streak = 1 WHERE item_id = ?", (iid(db, "1.07"),))
    task = en.task_for(iid(db, "1.07"), Mode.TEXT)
    fake.push({"verdict": "error", "tag": "article", "explanation": "Перед профессией нужен a.",
               "correction": task.expected[0]})
    answer = "I am math teacher"
    c = run(EnglishChecker(db, runner).check(task, answer, clock.now()))
    assert c.correct is False and c.tag == "article"
    out = en.record(task, c.correct, judge=c.judge, mode=Mode.TEXT, day=MON, now=clock.now(),
                    answer=answer, verdict_json=c.verdict)
    log_en_error(db, c, task.meta.get("target", task.code), out.review_id, answer, clock.now(), "2026-09-28")
    assert db.execute("SELECT tag FROM errors").fetchone()[0] == "article"
    assert db.execute("SELECT streak FROM item_state WHERE item_id = ?", (iid(db, "1.07"),)).fetchone()[0] == 0
    assert dispute(db, out.review_id, clock.now())
    s = db.execute("SELECT streak, due_date FROM item_state WHERE item_id = ?", (iid(db, "1.07"),)).fetchone()
    assert s["streak"] == 1                                           # откат к снимку
    assert db.execute("SELECT count(*) FROM errors").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM verdict_cache").fetchone()[0] == 0   # не вернётся из кэша
    assert not dispute(db, out.review_id, clock.now())                # повторно — ничего
    (d,) = pending(db)
    resolve(db, d["id"], "upheld", clock.now())
    assert pending(db) == []
    with pytest.raises(DisputeError):
        resolve(db, d["id"], "как-то", clock.now())


def test_dispute_code_verdict_rejected(en, db, clock):
    en.record(en.intro_tasks(iid(db, "0.16"), Mode.TEXT)[0], None, judge=Judge.CODE, mode=Mode.TEXT,
              day=MON, now=clock.now())
    rid = db.execute("SELECT max(id) FROM reviews").fetchone()[0]
    with pytest.raises(DisputeError):
        dispute(db, rid, clock.now())


def test_en_model_down_defers(en, db, clock, cal):
    runner = ModelRunner(LLM(db, cfg(api_key=""), clock, cal), Prompts(ROOT / "prompts"))
    en.record(en.intro_tasks(iid(db, "0.16"), Mode.TEXT)[0], None, judge=Judge.CODE, mode=Mode.TEXT,
              day=MON, now=clock.now())
    task = en.task_for(iid(db, "0.16"), Mode.TEXT)
    c = run(EnglishChecker(db, runner).check(task, "Thanks so much", clock.now()))
    assert c.correct is None and c.unavailable
    c = run(EnglishChecker(db, runner).check(task, "thanks", clock.now()))
    assert c.correct                                                  # код работает без модели


def test_en_meaning_payload(en, runner, fake, db, clock):
    for code in ("1.01", "1.02"):
        en.record(en.intro_tasks(iid(db, code), Mode.TEXT)[0], None, judge=Judge.CODE, mode=Mode.TEXT,
                  day=MON, now=clock.now())
    db.execute("UPDATE item_state SET stage = 3 WHERE item_id = ?", (iid(db, "1.02"),))
    task = en.task_for(iid(db, "1.02"), Mode.TEXT)
    fake.push({"verdict": "acceptable", "tag": None, "explanation": "Отвечает на вопрос.", "correction": ""})
    c = run(EnglishChecker(db, runner).check(task, "I am Sam", clock.now()))
    payload = json.loads(fake.calls[0]["messages"][-1]["content"])
    assert c.correct and payload["check"] == "meaning" and payload["question"] == "What's your name?"
    assert pending_variants(db) == []                                 # по смыслу — не кандидат в варианты


# психология

@pytest.fixture
def psy(db, clock, cal, tmp_path):
    (tmp_path / "PATO").mkdir()
    (tmp_path / "PATO" / "PATO_korpus_v1_1.md").write_text(CORPUS, encoding="utf-8")
    (tmp_path / "PATO" / "PATO-K-Zeigarnik-2024.pages.md").write_text(ZEIG, encoding="utf-8")
    imp = Importer(db, clock, cal, SourceLibrary(tmp_path))
    imp.apply(imp.analyze("PATO_Карта_тем.md", MAP.encode()))
    imp.apply(imp.analyze("PATO_Банк_Д.md", BANK.encode()))


PSY_OK = {"verdict": "partial", "present": ["операциональная сторона"], "missing": ["динамика"],
          "outside_source": [], "comment": "Зейгарник, с. 230.", "tag": "term", "confusion": None}


def test_psy_card_payload_and_gap(psy, runner, fake, db, clock):
    fake.push(PSY_OK)
    item = iid(db, "Д-1-01")
    c = run(PsyChecker(db, runner).check(item, "операциональная сторона"))
    assert c.rating == Rating.HARD and c.judge == Judge.CHEAP
    payload = json.loads(fake.calls[0]["messages"][-1]["content"])
    assert "[Зейгарник, с. 230]" in payload["source_fragment"] and payload["key"].startswith("Операциональная")
    assert fake.calls[0]["model"] == "cheap-m"
    rid = db.execute("INSERT INTO reviews (item_id, ts, study_date, format, mode, verdict, judge) "
                     "VALUES (?, 't', 'd', 'card', 'text', 'partial', 'cheap')", (item,)).lastrowid
    log_psy_gap(db, c, item, rid, "операциональная сторона", clock.now(), "2026-09-28")
    assert db.execute("SELECT tag FROM errors WHERE track = 'psy'").fetchone()[0] == "term"


def test_psy_vignette_flagship_mentor(psy, runner, fake, db):
    fake.push({**PSY_OK, "verdict": "passed", "tag": None,
               "mentor": {"matches": False, "divergence": "Не названо направление к врачу."}})
    c = run(PsyChecker(db, runner).check(iid(db, "Д-4-01"), "разноплановость", "работаю сам"))
    assert c.rating == Rating.GOOD and c.judge == Judge.FLAGSHIP
    assert c.mentor_divergence == "Не названо направление к врачу."
    payload = json.loads(fake.calls[0]["messages"][-1]["content"])
    assert fake.calls[0]["model"] == "flag-m" and payload["mentor_answer"] == "работаю сам"
    assert len(payload["vignette_questions"]) == 2


def test_psy_missing_fragment_never_checked(psy, runner, fake, db):
    db.execute("UPDATE items SET fragment_status = 'missing', fragment = NULL WHERE code = 'Д-1-02'")
    c = run(PsyChecker(db, runner).check(iid(db, "Д-1-02"), "ответ"))
    assert c.rating is None and not fake.calls


def test_psy_without_sources_checked_by_key(db, clock, cal, tmp_path, runner, fake):
    """Источников в папке нет: позиции импортируются со статусом n/a и проверяются по ключу."""
    (tmp_path / "empty").mkdir()
    imp = Importer(db, clock, cal, SourceLibrary(tmp_path / "empty"))
    imp.apply(imp.analyze("PATO_Карта_тем.md", MAP.encode()))
    plan = imp.analyze("PATO_Банк_Д.md", BANK.encode())
    assert plan.ok and not plan.fragments["missing"] and len(plan.fragments["n/a"]) == 3
    imp.apply(plan)
    row = db.execute("SELECT fragment, fragment_status FROM items WHERE code = 'Д-1-01'").fetchone()
    assert row["fragment"] is None and row["fragment_status"] == "n/a"
    fake.push(PSY_OK)
    c = run(PsyChecker(db, runner).check(iid(db, "Д-1-01"), "операциональная сторона"))
    assert c.rating == Rating.HARD
    payload = json.loads(fake.calls[0]["messages"][-1]["content"])
    assert payload["source_fragment"] == "" and payload["key"].startswith("Операциональная")


# разговор

def test_dialog_flow(runner, fake):
    talk = EnglishTalk(runner)
    d = Dialog("Кто я", ["I'm a teacher."], turns=2)
    fake.push({"reply": "Hi! What's your name?", "end": False})
    assert run(talk.open(d)) == "Hi! What's your name?"
    fake.push({"reply": "Nice! What do you do?", "end": False})
    run(talk.reply(d, "My name is Sam"))
    assert not d.finished
    fake.push({"reply": "Great. Bye!", "end": True})
    assert run(talk.reply(d, "I am teacher")) == "Great. Bye!" and d.finished
    msgs = fake.calls[-1]["messages"]
    assert [m["role"] for m in msgs][:2] == ["system", "assistant"]
    fake.push({"corrections": [{"said": "I am teacher", "better": "I'm a teacher.", "tag": "article",
                                "why": "Перед профессией a."}]})
    assert run(talk.corrections(d))[0]["tag"] == "article"
    assert len(d.call_ids) == 4


def test_monologue_and_questions_flagship(runner, fake):
    talk = EnglishTalk(runner)
    fake.push({"verdict": "passed", "covered": ["кто я"], "missing": [], "errors": [], "comment": "Хорошо."})
    r = run(talk.monologue("Hi I'm Sam", ["кто я"], 95, exit_criterion=True))
    assert r["verdict"] == "passed" and fake.calls[-1]["model"] == "flag-m"
    fake.push({"items": [{"question": "What's your name?", "ok": True, "fix": ""},
                         {"question": "Where you live?", "ok": False, "fix": "Where do you live?"}]})
    assert run(talk.own_questions(["What's your name?", "Where you live?"], "Кто я"))["accepted"] == 1
