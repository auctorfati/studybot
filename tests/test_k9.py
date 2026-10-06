"""Слой К9: разборы тем психологии в импорте."""

import json

import pytest

from helpers import BANK, CORPUS, MAP, ZEIG
from studybot.content.digests import parse_digests
from studybot.content.importer import Importer, detect_kind, render_report
from studybot.content.psychology import area_from_name
from studybot.content.sources import SourceLibrary

DIG = """# Разборы тем. Патопсихология, блок Д — тест

Версия 1.0.

## П16 — Классификация нарушений мышления

### 1. Коротко
Три группы нарушений мышления (Зейгарник, с. 230).
Позиции: Д-1-01.

### 2. Чем отличается от соседнего
Отличие от снижения обобщения (S04, с. 381).

### 3. Пример из практики
Пример.
Позиции: Д-4-01.

### 4. Для наставника
Граница.

### 5. Где читать
Зейгарник, с. 230–231.
Позиции: Д-1-02

## П17 — Операциональная сторона

### 1. Коротко
Текст.
"""


@pytest.fixture
def imp(db, clock, cal, tmp_path):
    (tmp_path / "PATO").mkdir()
    (tmp_path / "PATO" / "PATO_korpus_v1_1.md").write_text(CORPUS, encoding="utf-8")
    (tmp_path / "PATO" / "PATO-K-Zeigarnik-2024.pages.md").write_text(ZEIG, encoding="utf-8")
    i = Importer(db, clock, cal, SourceLibrary(tmp_path))
    for name, text in (("PATO_Карта_тем.md", MAP), ("PATO_Банк_Блок_Д.md", BANK)):
        plan = i.analyze(name, text.encode())
        assert plan.ok, render_report(plan)
        i.apply(plan)
    return i


def topics_items(db):
    return ({r[0]: r[1] for r in db.execute("SELECT code, area FROM topics")},
            {r[0]: r[1] for r in db.execute("SELECT code, unit FROM items WHERE track = 'psy'")})


def test_detect_and_area_with_spaces():
    assert detect_kind(DIG) == "digest"
    assert area_from_name("NEURO Разборы Блок Г.md") == "NEURO"


def test_parse_digest_parts_and_positions(db, imp):
    topics, items = topics_items(db)
    p = parse_digests(DIG, "PATO", topics, items, imp.library)
    d16, d17 = p.digests
    assert [x.title for x in d16.parts] == ["Коротко", "Чем отличается от соседнего", "Пример из практики",
                                             "Для наставника", "Где читать"]
    assert d16.parts[0].positions == ["Д-1-01"] and d16.parts[0].body == "Три группы нарушений мышления (Зейгарник, с. 230)."
    assert d16.parts[4].positions == ["Д-1-02"]
    assert not [w for w in p.issues.warnings if w.where == "П16"]
    warns17 = [w.text for w in p.issues.warnings if w.where == "П17"]
    assert any("Где читать" in w for w in warns17) and any("Для наставника" in w for w in warns17)


def test_digest_warnings_and_rejections(db, imp):
    topics, items = topics_items(db)
    bad = (DIG.replace("(S04, с. 381)", "(S04, с. 999)").replace("Позиции: Д-4-01.", "Позиции: Д-1-01, Д-9-99.")
           .replace("## П17 — Операциональная сторона", "## П99 — Нет такой темы"))
    p = parse_digests(bad, "PATO", topics, items, imp.library)
    texts = [w.text for w in p.issues.warnings]
    assert any("S04, с. 999 — страница не найдена" in t for t in texts)
    assert any("Д-1-01 — в частях 1 и 3" in t for t in texts)
    assert any("Д-9-99 нет" in t for t in texts)
    assert p.rejected == [("П99", "темы нет в загруженных картах тем")]
    long = DIG.replace("Пример.", "а" * 3600)
    assert any("длиннее 3500" in w.text for w in parse_digests(long, "PATO", topics, items).issues.warnings)
    gap = DIG.replace("### 2. Чем отличается", "### 3. Чем отличается")
    assert parse_digests(gap, "PATO", topics, items).rejected[0][0] == "П16"
    assert parse_digests(DIG, "PSY", topics, items).rejected[0][1].startswith("тема из области PATO")


def test_import_replaces_topics_of_file(db, imp):
    plan = imp.analyze("PATO_Разборы_Блок_Д.md", DIG.encode())
    assert plan.ok and plan.kind == "digest"
    rep = render_report(plan)
    assert "Тем с разборами 2 (П16, П17), частей 6" in rep
    imp.apply(plan)
    assert db.execute("SELECT parts FROM digests WHERE topic_code = 'П16'").fetchone()[0] == 5
    db.execute("INSERT INTO digest_reads (topic_code, part, status) VALUES ('П16', 5, 'reading')")
    short = DIG.split("### 3. Пример из практики")[0] + "## П17 — Операциональная сторона\n\n### 1. Коротко\nТекст.\n"
    plan = imp.analyze("PATO_Разборы_Блок_Д.md", short.encode())
    assert plan.digests_replaced == ["П16", "П17"] and "уже были: П16, П17" in render_report(plan)
    imp.apply(plan)
    assert db.execute("SELECT count(*) FROM digest_parts WHERE topic_code = 'П16'").fetchone()[0] == 2
    assert db.execute("SELECT part FROM digest_reads WHERE topic_code = 'П16'").fetchone()[0] == 2
    assert json.loads(db.execute("SELECT positions_json FROM digest_parts WHERE topic_code = 'П16' AND num = 1")
                      .fetchone()[0]) == ["Д-1-01"]


def test_bank_report_lists_topics_without_digest(db, imp):
    plan = imp.analyze("PATO_Банк_Блок_Д.md", BANK.replace("Вопрос с корпусом", "Вопрос с корпусом 2").encode())
    assert plan.topics_without_digest == ["П16"] and "Тем без разбора: 1 (П16)" in render_report(plan)


# трек психологии через бота

from studybot.bot.controller import Controller
from studybot.enums import Track
from studybot.speech import StubSTT
from test_sessions import core, run  # noqa: F401,E402


@pytest.fixture
def pc(core, tmp_path):
    """Ядро с банком английского и патопсихологией: карта, банк, разбор, источники."""
    src = tmp_path / "src"
    (src / "PATO").mkdir(parents=True)
    (src / "PATO" / "PATO_korpus_v1_1.md").write_text(CORPUS, encoding="utf-8")
    (src / "PATO" / "PATO-K-Zeigarnik-2024.pages.md").write_text(ZEIG, encoding="utf-8")
    core.importer.library = SourceLibrary(src)
    for name, text in (("PATO_Карта_тем.md", MAP), ("PATO_Банк_Блок_Д.md", BANK), ("PATO_Разборы_Блок_Д.md", DIG)):
        plan = core.importer.analyze(name, text.encode())
        assert plan.ok, render_report(plan)
        core.importer.apply(plan)
    core.days.add_time(core.engine.today(), Track.EN, 600)      # английский минимум набран: блок — психология
    core.settings.set("psy_digest_first", False)                # сценарии среза; разбор первым — отдельный тест
    return core


@pytest.fixture
def pc_nosrc(core, tmp_path):
    """То же ядро, но папка источников пуста: психология проверяется по ключу."""
    (tmp_path / "nosrc").mkdir()
    core.importer.library = SourceLibrary(tmp_path / "nosrc")
    for name, text in (("PATO_Карта_тем.md", MAP), ("PATO_Банк_Блок_Д.md", BANK), ("PATO_Разборы_Блок_Д.md", DIG)):
        plan = core.importer.analyze(name, text.encode())
        assert plan.ok, render_report(plan)
        core.importer.apply(plan)
    core.days.add_time(core.engine.today(), Track.EN, 600)
    core.settings.set("psy_digest_first", False)
    return core


def texts(res):
    return [r.text for r in (res.replies if hasattr(res, "replies") else res)]


def buttons(reply):
    return [b.data for row in reply.buttons for b in row]


def test_psy_slice_fail_digest_then_open_questions(pc):
    ctl = Controller(pc, pc.cfg, StubSTT())
    res = run(ctl.on_text("15 мин"))
    step = pc.engine.current()
    assert step.kind == "psy" and step.payload["purpose"] == "probe" and step.payload["kind"] == "vignette"
    assert "Виньетка" in res.replies[-1].text and "Вопросы:" in res.replies[-1].text
    res = run(ctl.on_text("Не знаю, что это."))
    assert "Угол наставника" in res[-1].text if isinstance(res, list) else "Угол наставника" in res.replies[-1].text
    pc.fake.push({"verdict": "failed", "present": [], "missing": ["разноплановость"], "outside_source": [],
                  "comment": "Не названо главное.", "tag": "syndrome", "confusion": None,
                  "mentor": {"matches": True, "divergence": ""}})
    res = run(ctl.on_text("Направить к врачу."))
    out = res if isinstance(res, list) else res.replies
    assert any("Не зачтено." in r.text and "Ключ: Разноплановость." in r.text for r in out)
    verdict = next(r for r in out if "Не зачтено." in r.text)
    assert {"pwhy:%d" % step.id, "src:%d" % step.id} <= set(buttons(verdict))
    assert any("Сначала разбор темы" in r.text for r in out)
    nxt = out[-1]
    assert nxt.text.startswith("Разбор П16 «Классификация нарушений мышления» — часть 1 из 5: Коротко")
    dstep = pc.engine.current()
    for k in range(4):
        pc.clock.advance(seconds=30)
        r = run(ctl.on_callback(f"dgn:{dstep.id}")).replies[-1]
    assert "часть 5 из 5" in r.text and buttons(r)[0] == f"dgn:{dstep.id}"
    res = run(ctl.on_callback(f"dgn:{dstep.id}"))
    assert "Разбор П16 прочитан." in res.replies[0].text
    assert pc.conn.execute("SELECT status FROM digest_reads WHERE topic_code = 'П16'").fetchone()[0] == "read"
    st = json.loads(pc.conn.execute("SELECT probe_json FROM topic_state WHERE topic_code = 'П16'").fetchone()[0])
    assert st["phase"] in ("open", "done")
    assert pc.engine.active()["active_sec_psy"] > 0


def test_psy_why_opens_digest_part_and_source_retell(pc):
    ctl = Controller(pc, pc.cfg, StubSTT())
    run(ctl.on_text("15 мин"))
    step = pc.engine.current()
    pc.fake.push({"verdict": "passed", "present": ["разноплановость"], "missing": [], "outside_source": [],
                  "comment": "Верно.", "tag": None, "confusion": None, "mentor": {"matches": True, "divergence": ""}})
    run(ctl.on_text("Разноплановость."))
    res = run(ctl.on_text("Граница — к врачу."))
    out = res if isinstance(res, list) else res.replies
    assert any("Тема П16" in r.text and "закрыта" in r.text for r in out)
    closed = next(r for r in out if "закрыта" in r.text)
    assert "dg:П16:1" in buttons(closed)
    why = run(ctl.on_callback(f"pwhy:{step.id}")).replies[0]
    assert "часть 3 из 5: Пример из практики" in why.text                 # Д-4-01 — в части 3
    assert "dg:П16:2" in buttons(why) and "dg:П16:4" in buttons(why)
    n = len(pc.fake.calls)
    src = run(ctl.on_callback(f"src:{step.id}")).replies[0]
    assert src.text.startswith("Источник: Зейгарник, с. 230; S04, с. 381") and len(pc.fake.calls) == n + 1
    assert pc.fake.calls[-1]["json_mode"] is False
    run(ctl.on_callback(f"src:{step.id}"))
    assert len(pc.fake.calls) == n + 1                                     # пересказ — из кэша
    cards = pc.conn.execute("SELECT count(*) FROM item_state s JOIN items i ON i.id = s.item_id "
                            "WHERE i.kind = 'card' AND i.is_core = 1 AND s.step = 3").fetchone()[0]
    assert cards == 1                                                       # Д-1-01 — ядро, с шага 14 дней


def test_psy_card_self_assessment_and_typed(pc):
    from studybot.psychology.track import PsyTask
    pc.conn.execute("INSERT OR REPLACE INTO topic_state (topic_code, state, level, probe_json) VALUES "
                    "('П16', 'study', 0, '{\"phase\": \"done\", \"from_level\": 1}')")
    ctl = Controller(pc, pc.cfg, StubSTT())
    res = run(ctl.on_text("15 мин"))
    step = pc.engine.current()
    assert step.payload["kind"] == "card" and step.payload["purpose"] == "new"
    assert f"pshow:{step.id}" in buttons(res.replies[-1])
    shown = run(ctl.on_callback(f"pshow:{step.id}")).replies[0]
    assert shown.text.startswith("Ответ: Операциональная сторона") and f"pself:{step.id}:2" in buttons(shown)
    run(ctl.on_callback(f"pself:{step.id}:2"))
    r = pc.conn.execute("SELECT format, judge, rating FROM reviews ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(r) == ("self", "self", 3)
    assert pc.conn.execute("SELECT new_psy_units FROM days").fetchone()[0] == 1
    # набранный ответ: самооценка, затем модель; расхождение отмечается
    task = PsyTask(**{k: v for k, v in step.payload.items() if k in PsyTask.__dataclass_fields__})
    assert task.answer_mode == "self"


def test_digest_menu(pc):
    ctl = Controller(pc, pc.cfg, StubSTT())
    root = run(ctl.on_callback("more:digests")).replies[0]
    assert "Патопсихология — 0 из 2" in buttons(root)[0] or "dm:a:PATO" in buttons(root)
    blocks = run(ctl.on_callback("dm:a:PATO")).replies[0]
    assert "dm:b:PATO:Д" in buttons(blocks)
    topics = run(ctl.on_callback("dm:b:PATO:Д")).replies[0]
    assert "dg:П16:1" in buttons(topics)
    part = run(ctl.on_callback("dg:П16:5")).replies[0]
    assert "часть 5 из 5: Где читать" in part.text and "dg:П16:4" in buttons(part)
    assert pc.conn.execute("SELECT status FROM digest_reads WHERE topic_code = 'П16'").fetchone()[0] == "read"
    assert "прочитан" in run(ctl.on_callback("dm:b:PATO:Д")).replies[0].buttons[0][0].text


def test_digest_first_then_probe(pc):
    """Сначала материал: новая тема начинается с разбора, срез — после него."""
    pc.settings.set("psy_digest_first", True)
    ctl = Controller(pc, pc.cfg, StubSTT())
    res = run(ctl.on_text("15 мин"))
    first = res.replies[-1]
    assert "Новая тема. Сначала разбор, потом вопросы." in first.text
    assert "часть 1 из 5: Коротко" in first.text
    dstep = pc.engine.current()
    for _ in range(5):
        pc.clock.advance(seconds=40)
        res = run(ctl.on_callback(f"dgn:{dstep.id}"))
    out = res.replies
    assert "Разбор П16 прочитан." in out[0].text
    step = pc.engine.current()
    assert step.kind == "psy" and step.payload["purpose"] == "probe" and step.payload["kind"] == "vignette"
    pc.fake.push({"verdict": "failed", "present": [], "missing": ["x"], "outside_source": [], "comment": "",
                  "tag": "syndrome", "confusion": None, "mentor": {"matches": True, "divergence": ""}})
    run(ctl.on_text("ответ"))
    res = run(ctl.on_text("угол"))
    texts_ = [r.text for r in (res if isinstance(res, list) else res.replies)]
    # открытых вопросов в тестовом банке нет: тема целиком с карточек; разбор второй раз не идёт
    assert any("пойдёт целиком" in t for t in texts_), texts_
    assert pc.engine.current().payload["kind"] == "card"


def test_deferred_digest_offered_next_session(pc):
    pc.settings.set("psy_digest_first", True)
    ctl = Controller(pc, pc.cfg, StubSTT())
    run(ctl.on_text("15 мин"))
    dstep = pc.engine.current()
    res = run(ctl.on_callback(f"dgl:{dstep.id}"))
    assert "отложен" in res.replies[0].text
    assert pc.conn.execute("SELECT status FROM digest_reads WHERE topic_code = 'П16'").fetchone()[0] == "deferred"
    run(ctl.on_text("Закончить")) if pc.engine.active() else None
    pc.engine.end()
    pc.clock.advance(seconds=3600)
    res = run(ctl.on_text("15 мин"))
    assert any("Разбор П16" in r.text for r in res.replies)


def test_topic_with_target_below_study_levels_closes(pc):
    """Тема с целевым уровнем 2, у которой срез открытыми вопросами сдан, не зависает в изучении."""
    import json as _json
    tr = pc.engine.psy_track
    code = pc.conn.execute("SELECT code FROM topics WHERE archived = 0 LIMIT 1").fetchone()[0]
    pc.conn.execute("UPDATE topics SET target_level = 2 WHERE code = ?", (code,))
    tr.start_topic(code, pc.clock.now())
    tr._study(code, pc.clock.now(), 3, _json.loads(tr.state(code)["probe_json"]))   # как после сданных открытых
    assert tr.state(code)["state"] == "study"
    assert tr.maybe_close(code, pc.engine.today(), pc.clock.now())
    assert tr.state(code)["state"] == "closed"


def test_psy_without_sources_shown_without_source_button(pc_nosrc):
    """Без корпусов позиции показываются; под вердиктом нет кнопки «Источник»."""
    ctl = Controller(pc_nosrc, pc_nosrc.cfg, StubSTT())
    run(ctl.on_text("15 мин"))
    step = pc_nosrc.engine.current()
    assert step.kind == "psy" and step.payload["kind"] == "vignette"
    run(ctl.on_text("Не знаю, что это."))
    pc_nosrc.fake.push({"verdict": "failed", "present": [], "missing": ["разноплановость"], "outside_source": [],
                        "comment": "Не названо главное.", "tag": "syndrome", "confusion": None,
                        "mentor": {"matches": True, "divergence": ""}})
    res = run(ctl.on_text("Направить к врачу."))
    out = res if isinstance(res, list) else res.replies
    verdict = next(r for r in out if "Не зачтено." in r.text)
    data = set(buttons(verdict))
    assert "pwhy:%d" % step.id in data and "src:%d" % step.id not in data
    payload = json.loads(pc_nosrc.fake.calls[-1]["messages"][-1]["content"])
    assert payload["source_fragment"] == ""
