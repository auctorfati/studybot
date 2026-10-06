from helpers import EN
from studybot.content.english import parse_english


def test_parse_ok():
    p = parse_english(EN)
    assert not p.issues.errors, p.issues.errors
    assert p.module == "1" and p.area == "EN1"
    assert [i.code for i in p.items] == ["0.01", "0.02", "1.01", "1.02", "1.03"]
    assert p.blocks == {"0": "Рабочие фразы", "1": "Кто я"}
    i = {x.code: x for x in p.items}
    assert i["0.02"].variants == ["I see.", "Got it."]
    assert i["1.01"].notes == ["З1"] and i["1.01"].links == ["1.02"]
    assert i["1.02"].extra == {"ready_chunk": True, "wildcard": True}
    assert i["1.02"].audio_text == "What does it mean?" and i["1.02"].sound is None
    assert i["1.03"].assembly and i["1.03"].extra["meaning"] == "ядро блока"
    assert i["0.01"].variants == ["I do not understand."]
    assert [n.code for n in p.notes] == ["З1"] and "to be" in p.notes[0].body
    assert i["1.01"].sort_key == 1_001_001          # модуль · блок · номер


def test_hashes_split_prompt_and_answer():
    a = parse_english(EN).items[0]
    b = parse_english(EN.replace("Я не понимаю", "Не понимаю")).items[0]
    c = parse_english(EN.replace("I do not understand.", "I don't get it.")).items[0]
    assert a.answer_hash == b.answer_hash and a.prompt_hash != b.prompt_hash
    assert a.answer_hash != c.answer_hash


def _errors(text, **kw):
    return [str(e) for e in parse_english(text, **kw).issues.errors]


def test_errors():
    assert any("ID" in e for e in _errors(EN.replace("| 0.02 |", "| 0.2 |")))
    assert any("повтор" in e for e in _errors(EN.replace("| 0.02 |", "| 0.01 |")))
    assert any("блока 0" in e for e in _errors(EN.replace("| 0.02 |", "| 1.09 |")))
    assert any("З7" in e for e in _errors(EN.replace("| З1 |", "| З7 |")))
    assert any("1.xx" in e for e in _errors(EN.replace("| 1.02 |\n", "| 1.xx |\n", 1)))
    assert any("несуществующую единицу" in e for e in _errors(EN.replace("| 0.02 |\n| 0.02", "| 0.05 |\n| 0.02")))
    assert any("столбцы" in e for e in _errors(EN.replace("| Звук |", "| Звуки |", 1)))
    assert any("пустая целевая" in e for e in _errors(EN.replace("I understand. |", "— |", 1)))


def test_known_codes_from_other_files():
    text = EN.replace("| 1.02 |\n", "| 1.02, 2.01 |\n", 1)
    assert _errors(text)
    assert not _errors(text, known_codes={"2.01"})


def test_count_warning():
    p = parse_english(EN.replace("— 2 единицы", "— 3 единицы"))
    assert not p.issues.errors and p.issues.warnings
