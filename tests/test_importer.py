import json
import os
from datetime import date
from pathlib import Path

import pytest

from helpers import BANK, CORPUS, EN, MAP, ZEIG
from studybot.content.importer import Importer, detect_kind, render_report
from studybot.content.sources import SourceLibrary


@pytest.fixture
def imp(db, clock, cal, tmp_path):
    (tmp_path / "PATO").mkdir()
    (tmp_path / "PATO" / "PATO_korpus_v1_1.md").write_text(CORPUS, encoding="utf-8")
    (tmp_path / "PATO" / "PATO-K-Zeigarnik-2024.pages.md").write_text(ZEIG, encoding="utf-8")
    return Importer(db, clock, cal, SourceLibrary(tmp_path))


def run(imp, name, text, apply=True):
    plan = imp.analyze(name, text.encode("utf-8"))
    if apply:
        assert plan.ok, render_report(plan)
        imp.apply(plan)
    return plan


def test_detect():
    assert detect_kind(EN) == "en" and detect_kind(MAP) == "topics" and detect_kind(BANK) == "psy"
    assert detect_kind("# Что-то\n") is None


def test_english_import_and_reimport(imp, db):
    plan = run(imp, "en.md", EN)
    assert len(plan.new) == 5 and plan.scope == ["0", "1"]
    row = db.execute("SELECT * FROM items WHERE code = '1.01'").fetchone()
    assert row["area"] == "EN1" and row["unit"] == "1" and json.loads(row["notes_json"]) == ["З1"]
    assert db.execute("SELECT count(*) FROM notes").fetchone()[0] == 1
    again = run(imp, "en.md", EN)
    assert again.same_as_last and len(again.unchanged) == 5 and not again.new


def test_answer_change_moves_due_to_tomorrow(imp, db, clock):
    run(imp, "en.md", EN)
    item_id = db.execute("SELECT id FROM items WHERE code = '0.02'").fetchone()[0]
    db.execute("INSERT INTO item_state (item_id, stage, step, due_date) VALUES (?, 4, 3, '2026-10-20')", (item_id,))
    plan = run(imp, "en.md", EN.replace("I see. / Got it.", "I see."))
    assert plan.changed_answer == ["0.02"]
    assert db.execute("SELECT due_date FROM item_state WHERE item_id = ?", (item_id,)).fetchone()[0] == "2026-09-29"
    plan = run(imp, "en.md", EN.replace("I see. / Got it.", "I see.").replace("Понятно", "Ясно"))
    assert plan.changed_prompt == ["0.02"]


def test_delete_archives_and_restore(imp, db):
    run(imp, "en.md", EN)
    item_id = db.execute("SELECT id FROM items WHERE code = '0.02'").fetchone()[0]
    db.execute("INSERT INTO item_state (item_id, stage) VALUES (?, 2)", (item_id,))
    trimmed = EN.replace("| 0.01 | Я не понимаю | I don't understand. | I do not understand. | — | — | 0.02 |",
                         "| 0.01 | Я не понимаю | I don't understand. | I do not understand. | — | — | — |")
    trimmed = trimmed.replace("| 0.02 | Понятно | I understand. | I see. / Got it. | — | — | — |\n", "")
    plan = run(imp, "en.md", trimmed)
    assert plan.archived == ["0.02"]
    assert db.execute("SELECT archived FROM items WHERE id = ?", (item_id,)).fetchone()[0] == 1
    assert db.execute("SELECT stage FROM item_state WHERE item_id = ?", (item_id,)).fetchone()[0] == 2
    plan = run(imp, "en.md", EN)
    assert plan.restored == ["0.02"]


def test_other_block_file_not_archived(imp, db):
    run(imp, "en.md", EN)
    block2 = """# Английский. Модуль 1 — блок 2

## 1. Блок 2. Где я живу — 1 единица

| ID | Подсказка | Целевая фраза | Допустимые варианты | Грамматика | Звук | Связи |
|---|---|---|---|---|---|---|
| 2.01 | Я живу в Москве | I live in Moscow. | — | З1 | — | 1.01 |
"""
    plan = run(imp, "en2.md", block2)
    assert plan.new == ["2.01"] and not plan.archived
    assert db.execute("SELECT count(*) FROM items WHERE archived = 0").fetchone()[0] == 6


def test_psy_needs_map_and_area(imp):
    plan = run(imp, "PATO_Банк.md", BANK, apply=False)
    assert not plan.ok and "карту тем" in render_report(plan)
    plan = run(imp, "Банк.md", BANK, apply=False)
    assert not plan.ok and "имя" in render_report(plan)


def test_psy_import(imp, db):
    run(imp, "PATO_Карта_тем.md", MAP)
    assert db.execute("SELECT count(*) FROM topic_state").fetchone()[0] == 2
    plan = run(imp, "PATO_Банк_Д.md", BANK)
    assert plan.fragments == {"ok": ["Д-1-01", "Д-1-02", "Д-4-01"], "truncated": [], "missing": [], "n/a": []}
    vig = db.execute("SELECT * FROM items WHERE code = 'Д-4-01'").fetchone()
    assert vig["kind"] == "vignette" and vig["level"] == 4 and vig["unit"] == "П16"
    topics = db.execute("SELECT topic_code, is_primary FROM item_topics WHERE item_id = ? ORDER BY topic_code",
                        (vig["id"],)).fetchall()
    assert [tuple(t) for t in topics] == [("П16", 1), ("П17", 0)]
    assert "[S04, с. 381]" in vig["fragment"]
    report = render_report(plan)
    assert "целиком 3" in report


def test_psy_missing_fragment_reported(imp, db):
    run(imp, "PATO_Карта_тем.md", MAP)
    plan = run(imp, "PATO_Банк_Д.md", BANK.replace("Зейгарник, с. 230.", "Зейгарник, с. 999."))
    assert plan.fragments["missing"] == ["Д-1-01"]
    assert "нет страницы 999" in render_report(plan)
    row = db.execute("SELECT fragment, fragment_status FROM items WHERE code = 'Д-1-01'").fetchone()
    assert row["fragment"] is None and row["fragment_status"] == "missing"


def test_map_topic_in_use_cannot_disappear(imp):
    run(imp, "PATO_Карта_тем.md", MAP)
    run(imp, "PATO_Банк_Д.md", BANK)
    plan = run(imp, "PATO_Карта_тем.md", MAP.replace("П17. Операциональная сторона. В9. Т8. Уровень 4.", ""),
               apply=False)
    assert not plan.ok


def test_bad_files(imp):
    assert not imp.analyze("x.md", b"\xff\xfe").ok
    assert not imp.analyze("x.md", "# Прочее\n".encode()).ok
    plan = imp.analyze("en.md", EN.replace("| 0.02 |", "| 0.2 |").encode())
    assert not plan.ok
    with pytest.raises(ValueError):
        imp.apply(plan)


def test_apply_is_atomic(imp, db, monkeypatch):
    plan = imp.analyze("en.md", EN.encode())
    plan.rows[-1].kind = "poem"      # нарушит CHECK на последней позиции
    with pytest.raises(Exception):
        imp.apply(plan)
    assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM content_versions").fetchone()[0] == 0


# Прогон на настоящих файлах проекта: STUDYBOT_CONTENT — папка с банками,
# STUDYBOT_SOURCES — папка источников. Без них тест пропускается.

CONTENT = os.environ.get("STUDYBOT_CONTENT")
SOURCES = os.environ.get("STUDYBOT_SOURCES")


@pytest.mark.skipif(not (CONTENT and SOURCES), reason="нет STUDYBOT_CONTENT и STUDYBOT_SOURCES")
def test_real_content(db, clock, cal):
    imp = Importer(db, clock, cal, SourceLibrary(Path(SOURCES)))
    root = Path(CONTENT)
    from studybot.simulate import order_banks
    order, _ = order_banks(sorted(root.glob("*.md")))       # банки английского — по номеру блока
    for path in order:
        plan = imp.analyze(path.name, path.read_bytes())
        assert plan.ok, render_report(plan)
        imp.apply(plan)
    q = lambda sql: db.execute(sql).fetchone()[0]
    assert q("SELECT count(*) FROM items WHERE area = 'PATO'") == 494
    assert q("SELECT sum(is_core) FROM items WHERE area = 'PATO'") == 418
    assert q("SELECT count(*) FROM items WHERE area = 'PATO' AND kind = 'vignette'") == 26
    assert q("SELECT count(*) FROM items WHERE area = 'PATO' AND fragment_status = 'missing'") == 0
    assert q("SELECT count(*) FROM items WHERE area = 'EN1'") >= 70
    assert q("SELECT count(*) FROM notes") >= 6
    # сжатый фрагмент — в пределе; одна страница длиннее предела берётся целиком (0.8.1)
    assert q("SELECT max(length(fragment)) FROM items WHERE fragment_status = 'truncated'") <= 6000


@pytest.mark.skipif(not CONTENT, reason="нет STUDYBOT_CONTENT")
def test_real_banks_format_without_sources(db, clock, cal, tmp_path):
    """все банки английского и психологии разбираются без ошибок формата.
    Источники не нужны: без них позиции психологии импортируются без фрагментов."""
    from studybot.simulate import order_banks
    imp = Importer(db, clock, cal, SourceLibrary(tmp_path))
    ordered, _ = order_banks(sorted(Path(CONTENT).glob("*.md")))
    for path in ordered:
        plan = imp.analyze(path.name, path.read_bytes())
        assert plan.ok, render_report(plan)
        imp.apply(plan)
    q = lambda sql: db.execute(sql).fetchone()[0]
    areas = {r[0] for r in db.execute("SELECT DISTINCT area FROM items")}
    if "PATO" in areas:
        assert q("SELECT count(*) FROM items WHERE area = 'PATO'") >= 459
    if "PSY" in areas:
        assert q("SELECT count(*) FROM items WHERE area = 'PSY'") >= 263
        assert q("SELECT count(*) FROM items WHERE area = 'PSY' AND kind = 'vignette'") >= 18
    assert q("SELECT count(*) FROM items WHERE area = 'EN1'") >= 70


@pytest.mark.skipif(not CONTENT, reason="нет STUDYBOT_CONTENT")
def test_records_only_file_keeps_bank_items(db, clock, cal, tmp_path):
    """Файл только с записями (слушание модуля 6: блоки 1, 2, 5–13) не уводит в архив фразы банка
    тех же блоков — ни после файлов банка, ни при загрузке новой версии документом."""
    from studybot.simulate import order_banks
    imp = Importer(db, clock, cal, SourceLibrary(tmp_path))
    ordered, _ = order_banks(sorted(Path(CONTENT).glob("*.md")))
    en = [p for p in ordered if detect_kind(p.read_text(encoding="utf-8")) == "en"]
    listening = [p for p in en if "записи для слушания" in p.read_text(encoding="utf-8").splitlines()[0]]
    if not listening:
        pytest.skip("нет файла слушания модуля 6")
    for path in [p for p in en if p not in listening] + listening:      # слушание — после банка
        plan = imp.analyze(path.name, path.read_bytes())
        assert plan.ok, render_report(plan)
        assert not plan.archived, render_report(plan)
        imp.apply(plan)
    q = lambda sql: db.execute(sql).fetchone()[0]
    before = q("SELECT count(*) FROM items WHERE area = 'EN6' AND archived = 0")
    assert before > 0 and q("SELECT count(*) FROM items WHERE archived = 1") == 0
    again = imp.analyze(listening[0].name, listening[0].read_bytes() + b"\n")   # новая версия файла
    assert again.ok and not again.archived and not again.records_archived, render_report(again)
    imp.apply(again)
    assert q("SELECT count(*) FROM items WHERE area = 'EN6' AND archived = 0") == before
    assert q("SELECT count(*) FROM records WHERE archived = 0 AND code LIKE 'Л%'") == 20


def test_two_block_level_headings(imp):
    # «## Блок Б — Уровень 1. …» — файл на два блока (NEURO_Банк_Блоки_Б_В)
    run(imp, "PATO_Карта_тем.md", MAP)
    bank = BANK.replace("## Уровень 1. Термины и факты", "## Блок Д — Уровень 1. Термины и факты") \
               .replace("## Уровень 4. Применение на кейсах", "## Блок Д — Уровень 4. Применение")
    plan = run(imp, "PATO_Банк_Блок_Д.md", bank)
    assert sorted(r.code for r in plan.rows) == ["Д-1-01", "Д-1-02", "Д-4-01"]
