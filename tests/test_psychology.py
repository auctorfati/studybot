from helpers import BANK, MAP, MAP_PSY
from studybot.content.psychology import area_from_name, parse_psy_bank, parse_topic_map
from studybot.enums import ItemKind


def test_bank_ok():
    p = parse_psy_bank(BANK, "PATO")
    assert not p.issues.errors, p.issues.errors
    assert [i.code for i in p.items] == ["Д-1-01", "Д-1-02", "Д-4-01"]
    card, card2, vig = p.items
    assert card.kind is ItemKind.CARD and card.is_core and card.topics == ["П16"]
    assert not card2.is_core and card2.answer.endswith("второй строке.")
    assert vig.kind is ItemKind.VIGNETTE and vig.topics == ["П16", "П17"]
    assert vig.extra["questions"] == ["Квалифицируйте нарушение.", "Чем отличается от снижения обобщения?"]
    assert vig.extra["mentor"].startswith("Граница") and vig.extra["title"] == "Разноплановость"
    assert vig.prompt.startswith("Больной") and vig.answer == "Разноплановость."
    assert p.groups == {"Д"}


def _errs(text):
    return [str(e) for e in parse_psy_bank(text, "PATO").issues.errors]


def test_bank_errors():
    assert any("не соответствует" in e for e in _errs(BANK.replace("Тип: карточка.", "Тип: различение.", 1)))
    assert any("неизвестный тип" in e for e in _errs(BANK.replace("Тип: карточка.", "Тип: тест.", 1)))
    assert any("Ист" in e for e in _errs(BANK.replace("Ист: Зейгарник, с. 230.\n\n### Д-1-02", "\n### Д-1-02")))
    assert any("Статус" in e or "Тема" in e for e in _errs(BANK.replace("Статус: ядро. Тип", "Тип", 1)))
    assert any("уровень в коде" in e for e in _errs(BANK.replace("### Д-1-02", "### Д-2-02")))
    assert any("повтор" in e for e in _errs(BANK.replace("### Д-1-02", "### Д-1-01")))
    assert any("вне полей" in e for e in _errs(BANK.replace("В: Три вида", "Три вида")))


def test_vignette_without_mentor_is_warning():
    p = parse_psy_bank(BANK.replace("Угол наставника. Граница — направить к врачу.\n", ""), "PATO")
    assert not p.issues.errors and any("наставника" in str(w) for w in p.issues.warnings)


def test_topic_map_pato():
    t = parse_topic_map(MAP, "PATO")
    assert not t.issues.errors
    assert [(x.code, x.grp, x.target_level) for x in t.topics] == [("П16", "Д", 3), ("П17", "Д", 4)]
    assert t.topics[0].title == "Классификация нарушений мышления: три группы"


def test_topic_map_psy_wrapped_title():
    t = parse_topic_map(MAP_PSY, "PSY")
    assert not t.issues.errors
    assert [(x.code, x.target_level) for x in t.topics] == [("ПС15", 4), ("ПС16", 3)]
    assert t.topics[0].title == "Расстройства ассоциативного процесса (форма мышления)"


def test_area_from_name():
    assert area_from_name("PATO_Банк_Блок_Д.md") == "PATO"
    assert area_from_name("PSY_Карта_тем.md") == "PSY"
    assert area_from_name("Банк.md") is None
