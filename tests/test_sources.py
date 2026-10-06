from helpers import CORPUS, ZEIG
from studybot.content.common import parse_pages
from studybot.content.sources import (
    ItemRef, PageRef, SourceLibrary, build_fragment, parse_source, split_corpus, split_pages)

import pytest


def test_parse_pages():
    assert parse_pages("5, 13, 17–19") == [5, 13, 17, 18, 19]
    with pytest.raises(ValueError):
        parse_pages("19–17")


def test_parse_source():
    s = parse_source("S10, с. 2, 21; Зейгарник, с. 189–190.")
    assert s.refs == [PageRef("S10", (2, 21)), PageRef("Зейгарник", (189, 190))]
    s = parse_source("Зейгарник, с. 230 (PATO, И-4-01); PATO, Д-3-04, И-1-02; сверка по учебнику во втором проходе.")
    assert s.refs[0] == PageRef("Зейгарник", (230,))
    assert s.refs[1] == ItemRef("PATO", ("Д-3-04", "И-1-02"))
    assert s.notes == ["сверка по учебнику во втором проходе"]
    assert parse_source("S04, с. десять").errors


def test_split():
    pages = split_pages(ZEIG)
    assert set(pages) == {230, 231} and pages[231] == "Следующая страница."
    corpus = split_corpus(CORPUS)
    assert set(corpus) == {"S04"} and set(corpus["S04"]) == {381, 382}


@pytest.fixture
def lib(tmp_path):
    (tmp_path / "PATO").mkdir()
    (tmp_path / "PATO" / "PATO_korpus_v1_1.md").write_text(CORPUS, encoding="utf-8")
    (tmp_path / "PATO" / "PATO_korpus_v1.md").write_text("## S04. old\n[с. 1]\nстарый", encoding="utf-8")
    (tmp_path / "PATO" / "PATO-K-Zeigarnik-2024.pages.md").write_text(ZEIG, encoding="utf-8")
    return SourceLibrary(tmp_path)


def test_newest_corpus_wins(lib):
    path, _ = lib.corpus("PATO")
    assert path.name == "PATO_korpus_v1_1.md"


def test_fragment_ok(lib):
    f = build_fragment("PATO", "Зейгарник, с. 230; S04, с. 381–382.", lib)
    assert f.status == "ok"
    assert "[Зейгарник, с. 230]" in f.text and "[S04, с. 382]" in f.text


def test_fragment_missing(lib):
    assert build_fragment("PATO", "Зейгарник, с. 999.", lib).status == "missing"
    assert build_fragment("PATO", "S99, с. 1.", lib).status == "missing"
    # корпуса PSY в папке нет — это не ошибка ссылки: позиция проверяется по ключу (n/a)
    nf = build_fragment("PSY", "S04, с. 381.", lib)
    assert nf.status == "n/a" and nf.text is None and "нет корпуса" in nf.problems[0]
    assert build_fragment("PATO", "CDDR, с. 1.", lib).status == "n/a"         # файла CDDR в папке нет
    assert build_fragment("PATO", "сверка по учебнику.", lib).status == "missing"
    # одна ссылка нашлась, другой книги нет: фрагмент по найденной, без ошибки
    mixed = build_fragment("PATO", "Зейгарник, с. 230; CDDR, с. 1.", lib)
    assert mixed.status == "ok" and "[Зейгарник, с. 230]" in mixed.text
    # страницы нет в имеющемся источнике — по-прежнему missing, даже рядом с отсутствующим
    assert build_fragment("PATO", "Зейгарник, с. 999; CDDR, с. 1.", lib).status == "missing"


def test_fragment_item_ref(lib):
    # перекрёстная ссылка фрагмента не даёт, фрагмент — из остальных ссылок строки
    f = build_fragment("PATO", "Зейгарник, с. 230; PATO, Д-1-01.", lib, {("PATO", "Д-1-01"): "текст позиции"})
    assert f.status == "ok" and "текст позиции" not in f.text and "[Зейгарник, с. 230]" in f.text
    assert build_fragment("PSY", "PATO, Д-1-02.", lib, {}).status == "missing"


def test_k_and_poop_refs(tmp_path):
    s = parse_source("K01, с. 56; S03, с. 2; ПООП, с. 109.")
    assert s.refs == [PageRef("K01", (56,)), PageRef("S03", (2,)), PageRef("ПООП", (109,))] and not s.errors
    (tmp_path / "NEURO").mkdir()
    (tmp_path / "NEURO" / "NEURO_korpus_v1.md").write_text(
        "# корпус\n\n## K01. Лурия\n\n[с. 56]\nТекст Лурии.\n", encoding="utf-8")
    (tmp_path / "KLIN-N-FUMO-2021-POOP-vypiska.md").write_text("# выписка\n\n[с. 109]\nПрограмма.\n", encoding="utf-8")
    lib = SourceLibrary(tmp_path)
    f = build_fragment("NEURO", "K01, с. 56.", lib)
    assert f.status == "ok" and "Текст Лурии." in f.text
    f = build_fragment("KLIN", "ПООП, с. 109.", lib)
    assert f.status == "ok" and "Программа." in f.text


def test_single_long_page_taken_whole(tmp_path):
    long_page = " ".join(f"Предложение номер {i} о памяти." for i in range(300))
    (tmp_path / "x-Zeigarnik-1.pages.md").write_text(f"# t\n\n[с. 5]\n\n{long_page}\n", encoding="utf-8")
    f = build_fragment("PATO", "Зейгарник, с. 5.", SourceLibrary(tmp_path), limit=2000)
    assert f.status == "ok" and len(f.text) > 2000 and f.problems


def test_compress_keeps_relevant(tmp_path):
    filler = " ".join(f"Посторонняя фраза номер {i} о погоде." for i in range(400))
    text = ("# t\n\n[с. 1]\n\n" + filler + " Корсаковский синдром проявляется конфабуляциями. "
            + filler + "\n\n[с. 2]\n\n" + filler)
    (tmp_path / "x-Zeigarnik-1.pages.md").write_text(text, encoding="utf-8")
    f = build_fragment("PATO", "Зейгарник, с. 1–2.", SourceLibrary(tmp_path),
                       query="корсаковский синдром конфабуляции", limit=2000)
    assert f.status == "truncated" and len(f.text) <= 2000
    assert "Корсаковский синдром проявляется конфабуляциями." in f.text
    assert "[Зейгарник, с. 2]" in f.text   # каждая страница представлена


def test_compress_fills_budget_without_overlap(tmp_path):
    # английский источник и русский ключ: пересечения нет — страницы берутся по порядку до предела
    en = " ".join(f"Sentence number {i} describes the clinical picture in detail." for i in range(200))
    (tmp_path / "x-Zeigarnik-1.pages.md").write_text(f"# t\n\n[с. 1]\n\n{en}\n\n[с. 2]\n\n{en}\n", encoding="utf-8")
    f = build_fragment("PATO", "Зейгарник, с. 1–2.", SourceLibrary(tmp_path),
                       query="клиническая картина подробно", limit=3000)
    assert f.status == "truncated" and 2500 < len(f.text) <= 3000
    assert "Sentence number 0 describes" in f.text and "[Зейгарник, с. 2]" in f.text
