"""Приёмка: прогон 30 условных дней через бота и команда acceptance."""

import shutil
from pathlib import Path

import pytest

from helpers import BANK, MAP
from studybot.acceptance import check_content, passed, render, run_acceptance
from studybot.config import load_config
from studybot.simulate import Scenario, order_banks, simulate

ROOT = Path(__file__).resolve().parents[1]
EN_BANK = Path(__file__).parent / "data" / "en_bank_blocks_0-1.md"
SUNDAYS = (6, 13, 20, 27)


@pytest.fixture
def cfg(tmp_path):
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    text = text.replace('token = ""', 'token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"') \
               .replace("owner_id = 0 ", "owner_id = 1001 ") \
               .replace('prompts_dir = "prompts"', f'prompts_dir = "{ROOT / "prompts"}"')
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    p.chmod(0o600)
    return load_config(p)


def test_thirty_days_text_mode(cfg, tmp_path):
    """30 дней без нарушений потолков и лимитов; текстовый режим без распознавания и озвучки;
    при недоступной и ненастроенной модели бот работает форматами кода."""
    rep = simulate(cfg, [EN_BANK], tmp_path / "sim")
    assert rep.ok, rep.text()
    assert len(rep.rows) == 30
    assert [rep.copies[n] for n in SUNDAYS] == [1, 1, 1, 1]
    assert [rep.silent_copies[n] for n in SUNDAYS] == [0, 0, 0, 1]      # 27 — воскресенье в паузе
    assert [rep.weekly[n] for n in SUNDAYS] == [1, 1, 1, 0]
    assert rep.send_failures == 1 and rep.restarts_ok == 2 and rep.backups == 14
    assert all(r.new <= (10 if r.day.weekday() >= 5 else 8) for r in rep.rows)
    assert all(r.reviews <= 40 for r in rep.rows)
    down = [r for r in rep.rows if "API недоступен" in r.events]
    assert len(down) == 2 and all(r.counted for r in down)             # занятия шли без модели
    assert rep.rows[25].new == 0 and "пауза" in rep.rows[25].events
    text = rep.text()
    assert "Нарушений нет" in text and "текстовый режим" in text


def test_thirty_days_voice_mode_grid_under_cap(cfg, tmp_path):
    rep = simulate(cfg, [EN_BANK], tmp_path / "sim", Scenario(voice=True, seed=11))
    assert rep.ok, rep.text()
    assert max(r.reviews for r in rep.rows) == 40                        # потолок достигается и держится
    assert rep.stages[4] > 0


def test_simulation_needs_english(cfg, tmp_path):
    (tmp_path / "PATO_Карта_тем.md").write_text(MAP, encoding="utf-8")
    with pytest.raises(ValueError, match="банк английского"):
        simulate(cfg, [tmp_path / "PATO_Карта_тем.md"], tmp_path / "sim", Scenario(days=2))


def test_order_banks(tmp_path):
    block2 = """# Английский. Модуль 1 — банк блока 2

## 3. Блок 2. Где я живу — 1 единица

| ID | Подсказка | Целевая фраза | Допустимые варианты | Грамматика | Звук | Связи |
|---|---|---|---|---|---|---|
| 2.01 | Я живу в Москве | I live in Moscow. | — | З1 | — | 1.01 |
"""
    files = {"PSY_Банк_x.md": BANK.replace("Блок Д", "Блок Г"), "PATO_Банк_Блок_Д.md": BANK,
             "PATO_Карта_тем.md": MAP, "Банк_блоки_0-1.md": EN_BANK.read_text(encoding="utf-8"),
             "Банк_блок_2.md": block2, "notes.md": "# Заметки\n"}
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    ordered, skipped = order_banks(sorted(tmp_path.glob("*.md")))
    assert [p.name for p in ordered] == ["PATO_Карта_тем.md", "PATO_Банк_Блок_Д.md", "PSY_Банк_x.md",
                                         "Банк_блоки_0-1.md", "Банк_блок_2.md"]
    assert skipped == ["notes.md"]


def test_acceptance_command(cfg, tmp_path):
    content = cfg.paths.content_dir
    content.mkdir(parents=True)
    shutil.copy(EN_BANK, content / EN_BANK.name)
    (content / "PATO_Карта_тем.md").write_text(MAP, encoding="utf-8")
    (content / "PATO_Банк_Блок_Д.md").write_text(BANK, encoding="utf-8")
    checks = run_acceptance(cfg, days=8, with_tests=False)
    by = {c.title: c for c in checks}
    assert passed(checks), render(checks)
    assert by["Импорт банков без ошибок формата"].status == "ok"
    assert by["Фрагменты источников"].status == "ok"                    # источников в sources/ нет: проверка по ключу
    assert any("проверка по ключу" in l for l in by["Фрагменты источников"].lines)
    assert by["Резервные копии"].status == "ok" and by["Прогон 8 условных дней"].status == "ok"
    assert by["Конфиг"].status == "warn"                                # модель не настроена
    assert not any(c.status == "later" for c in checks)                 # К9 закрыт: отложенных проверок нет
    assert any("разборы: тем 0" in l for l in by["Импорт банков без ошибок формата"].lines)
    text = render(checks)
    assert text.startswith("Приёмка перед запуском.") and "готово к запуску текстового режима" in text
    assert not cfg.paths.db_file.exists()                               # рабочая база не создавалась


def test_acceptance_fails_on_bad_bank(cfg, tmp_path):
    folder = tmp_path / "banks"
    folder.mkdir()
    (folder / "PATO_Банк_Блок_Д.md").write_text(BANK.replace("Тема: П16. Статус: ядро. Тип: карточка.",
                                                             "Тема: П16. Статус: где-то. Тип: карточка."),
                                                encoding="utf-8")
    content, _, banks = check_content(cfg, folder)
    assert content.status == "fail"
    empty, _, none = check_content(cfg, tmp_path / "nothing")
    assert empty.status == "fail" and none == []


def test_blocks_imported_in_block_order(cfg, tmp_path):
    """Блок 2 ссылается на заметки и фразы блоков 0–1: порядок файлов на входе не важен."""
    block2 = Path(__file__).parent / "data" / "en_bank_block_2.md"
    sim_dir = tmp_path / "sim"
    rep = simulate(cfg, [block2, EN_BANK], sim_dir, Scenario(days=3))
    assert rep.ok, rep.text()
    import sqlite3
    c = sqlite3.connect(str(sim_dir / "data" / "studybot.sqlite3"))
    assert c.execute("SELECT count(*) FROM items WHERE area = 'EN1' AND unit = '2'").fetchone()[0] == 45
    assert c.execute("SELECT count(*) FROM notes").fetchone()[0] == 12
    c.close()
