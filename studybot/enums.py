"""Закрытые списки ядра: метки, типы, оценки. Новые значения — только правкой кода."""

from __future__ import annotations

from enum import Enum, IntEnum


class Track(str, Enum):
    EN = "en"
    PSY = "psy"


class ItemKind(str, Enum):
    # английский
    PHRASE = "phrase"          # обычная единица банка
    ASSEMBLY = "assembly"      # «по смыслу»: сборка блока, в сетку не идёт
    # психология, уровни слоя 2
    CARD = "card"              # 1
    OPEN = "open"              # 2
    DISTINCTION = "distinction"  # 3
    VIGNETTE = "vignette"      # 4


PSY_LEVEL = {
    ItemKind.CARD: 1,
    ItemKind.OPEN: 2,
    ItemKind.DISTINCTION: 3,
    ItemKind.VIGNETTE: 4,
}

# Цена в единицах лимита нового по психологии.
PSY_NEW_COST = {
    ItemKind.CARD: 1,
    ItemKind.DISTINCTION: 2,
    ItemKind.OPEN: 3,
    ItemKind.VIGNETTE: 6,
}


class TopicState(str, Enum):
    NOT_STARTED = "not_started"
    PROBE = "probe"        # входной срез сверху вниз
    STUDY = "study"        # изучение по уровням
    CLOSED = "closed"      # пройден уровень 4
    RARE = "rare"          # редкое повторение


class Judge(str, Enum):
    """Кто вынес вердикт по ответу."""
    CODE = "code"
    SELF = "self"          # самооценка на карточке
    CHEAP = "cheap"
    FLAGSHIP = "flagship"


class Rating(IntEnum):
    """Оценка в шкале FSRS: история совместима с переходом."""
    AGAIN = 1   # срыв, «не знал», «не зачтено»
    HARD = 2    # «с трудом», «частично»
    GOOD = 3    # верно
    EASY = 4    # в сетке не используется, оставлено для FSRS


class Mode(str, Enum):
    VOICE = "voice"   # «могу говорить вслух»
    TEXT = "text"     # «только текст»


# Журнал ошибок английского: девять меток.
EN_ERROR_TAGS: dict[str, str] = {
    "article": "артикль",
    "s_ending": "окончание -s",
    "do_question": "вопрос с do",
    "preposition": "предлог",
    "word_order": "порядок слов",
    "where_there": "where и there",
    "calque": "калька",
    "to_be": "to be",
    "word": "слово",
}

# Журнал пробелов психологии: пять меток.
PSY_GAP_TAGS: dict[str, str] = {
    "term": "термин",
    "mechanism": "механизм",
    "confusion": "смешение близких конструктов",
    "syndrome": "квалификация синдрома",
    "mentor_border": "граница наставника",
}
