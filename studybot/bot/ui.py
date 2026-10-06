"""Что бот отправляет, без привязки к библиотеке Telegram.

Логика (controller.py) возвращает список Reply; слой aiogram (telegram.py)
только переводит их в сообщения. Так вся логика бота проверяется тестами
без сети и без Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

# Постоянное меню внизу экрана. Надписи — это же и команды: текст кнопки
# приходит боту обычным сообщением.
BTN_5, BTN_15, BTN_30 = "5 мин", "15 мин", "30 мин"
BTN_EVENING = "Вечерний блок"
BTN_LONG = "Длинная сессия"
BTN_TODAY = "Сегодня"
BTN_MODE = "Режим"
BTN_MORE = "Ещё"

MENU_KINDS = {BTN_5: "5", BTN_15: "15", BTN_30: "30", BTN_EVENING: "evening", BTN_LONG: "long"}
MENU_LABELS = set(MENU_KINDS) | {BTN_TODAY, BTN_MODE, BTN_MORE}


def menu_rows(day: date) -> list[list[str]]:
    """Воскресенье — день длинной сессии: её кнопка появляется рядом с вечерним блоком."""
    second = [BTN_EVENING, BTN_LONG] if day.weekday() == 6 else [BTN_EVENING]
    return [[BTN_5, BTN_15, BTN_30], second, [BTN_TODAY, BTN_MODE, BTN_MORE]]


@dataclass(frozen=True)
class Button:
    text: str
    data: str            # callback_data, не длиннее 64 байт


@dataclass
class AudioRef:
    item_id: int | None  # аудио фразы банка
    speed: str           # 'normal' или 'slow'
    path: str            # абсолютный путь к файлу
    file_id: str | None  # идентификатор Telegram после первой отправки
    line_key: str | None = None   # или реплика сценария: 'Д1:3' (вопрос бота только звуком)


@dataclass
class DocumentRef:
    name: str            # имя присланного файла
    data: bytes
    caption: str = ""


@dataclass
class Reply:
    text: str = ""
    buttons: list[list[Button]] = field(default_factory=list)
    menu: bool = False               # приложить постоянное меню (с кнопками под текстом несовместимо)
    audio: AudioRef | None = None    # отправить перед текстом
    live_text: str | None = None     # реплика «только звуком»: озвучивается при отправке, текстом не показывается
    document: DocumentRef | None = None   # отправить файлом (копия базы)
    silent: bool = False             # без звука уведомления
    # Отметка «отправлено» (sent_marks), которую ставит эта группа сообщений. Стоит на
    # последнем сообщении группы: если отправка сорвалась на нём или раньше, отметка
    # снимается и таймер повторит группу (сводка и копия не теряются).
    mark: tuple[str, str] | None = None

    @property
    def plain(self) -> bool:
        """Можно ли склеить с соседним сообщением."""
        return (not self.buttons and not self.menu and self.audio is None and self.document is None
                and self.mark is None and not self.silent)


def merge(first: Reply | None, rest: list[Reply]) -> list[Reply]:
    """Короткий итог ответа приклеивается к следующему заданию, чтобы не плодить сообщения."""
    if first is None or not first.text:
        return rest
    if not first.plain or not rest or rest[0].audio is not None:
        return [first, *rest]
    head = rest[0]
    if head.document is not None or not head.text:
        return [first, *rest]
    return [replace(head, text=first.text + "\n\n" + head.text), *rest[1:]]


@dataclass
class Result:
    """Итог обработки события: сообщения и всплывающая подсказка для нажатой кнопки."""
    replies: list[Reply] = field(default_factory=list)
    toast: str | None = None
    # После одноразовой кнопки клавиатура сообщения убирается; если здесь кнопки —
    # остаются они (например, «Почему так?» после «Готово» или «Оспорить»).
    keep: list[list[Button]] | None = None
