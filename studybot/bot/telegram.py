"""Telegram через aiogram 3: события → Controller → сообщения.

Здесь нет учебной логики: только фильтр владельца, загрузка файлов,
отправка текста, кнопок, аудио и документов, таймер уведомлений. Бот
отвечает одному пользователю; всё остальное молча отбрасывается.

Если Telegram не принял сообщение, отметки «отправлено» с этого места
снимаются (Controller.unmark), и таймер повторяет уведомление, сводку или
копию базы. После сбоя таймер отступает: минута, две, четыре… до десяти.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from io import BytesIO
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, ErrorEvent, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

from ..app import build_core
from ..clock import SystemClock
from ..config import Config
from ..speech import make_stt, make_tts
from .controller import Controller
from .ui import MENU_LABELS, AudioRef, Reply, Result, menu_rows

log = logging.getLogger(__name__)

COMMANDS = [
    ("start", "Меню"),
    ("today", "Сегодня"),
    ("week", "Сводка за неделю"),
    ("mode", "Голос или только текст"),
    ("pause", "Пауза на несколько дней"),
    ("settings", "Настройки"),
    ("state", "Состояние бота"),
    ("import", "Как загрузить банк"),
    ("stop", "Закончить сессию"),
]
TICK_SEC = 30
TICK_MAX_SEC = 600          # после сбоев отправки таймер отступает до десяти минут
TEXT_LIMIT = 4000
# после этих кнопок кнопки под сообщением убираются, чтобы не нажать дважды
ONE_SHOT = {"done", "idk", "skip", "dispute", "imp", "start", "skipday", "mode", "pause", "unpause",
            "pshow", "pself", "dgn", "dgl", "tdone", "txr", "exr", "lsn"}
VOICE_EXT = {".ogg", ".oga", ".opus"}
VOICE_FORBIDDEN_HINT = ("Telegram не пропускает голосовые сообщения от бота: они запрещены в настройках. "
                        "Открой «Настройки» → «Конфиденциальность» → «Голосовые сообщения» → «Исключения» → "
                        "«Всегда разрешать» и добавь этого бота. Потом нажми «Сегодня» → «Показать задание».")


def inline(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=b.text, callback_data=b.data) for b in row] for row in rows])


def main_menu(ctl: Controller) -> ReplyKeyboardMarkup:
    rows = menu_rows(ctl.core.engine.today())
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
                               resize_keyboard=True, is_persistent=True)


def chunks(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


async def send_audio(bot: Bot, chat_id: int, ctl: Controller, a: AudioRef) -> None:
    if not a.file_id and not Path(a.path).exists():
        ctl.service_event("tts", "warning", f"нет аудиофайла {a.path}")
        return
    source = a.file_id or FSInputFile(a.path)
    if Path(a.path).suffix.lower() in VOICE_EXT:
        try:
            msg = await bot.send_voice(chat_id, source)
        except TelegramBadRequest as exc:
            if "VOICE_MESSAGES_FORBIDDEN" not in str(exc):
                raise
            ctl.service_event("telegram", "warning", "голосовые запрещены в настройках конфиденциальности")
            await bot.send_message(chat_id, VOICE_FORBIDDEN_HINT)
            return
        file_id = msg.voice.file_id if msg.voice else None
    else:
        msg = await bot.send_audio(chat_id, source)
        file_id = msg.audio.file_id if msg.audio else None
    if file_id and file_id != a.file_id:
        ctl.remember_audio(a, file_id)


async def send(bot: Bot, chat_id: int, ctl: Controller, res: Result) -> None:
    for k, r in enumerate(res.replies):
        try:
            await send_reply(bot, chat_id, ctl, r)
        except Exception:
            ctl.unmark([x.mark for x in res.replies[k:] if x.mark is not None])
            raise


async def send_reply(bot: Bot, chat_id: int, ctl: Controller, r: Reply) -> None:
    await ctl.voice_live(r)
    if r.audio is not None:
        await send_audio(bot, chat_id, ctl, r.audio)
    if r.document is not None:
        await bot.send_document(chat_id, BufferedInputFile(r.document.data, r.document.name),
                                caption=r.document.caption or None, disable_notification=r.silent)
    if not r.text:
        return
    markup = inline(r.buttons) if r.buttons else (main_menu(ctl) if r.menu else None)
    parts = chunks(r.text)
    for k, part in enumerate(parts):
        await bot.send_message(chat_id, part, reply_markup=markup if k == len(parts) - 1 else None,
                               disable_notification=r.silent)


def build_dispatcher(ctl: Controller, owner_id: int) -> Dispatcher:
    router = Router(name="studybot")
    router.message.filter(F.chat.type == ChatType.PRIVATE, F.from_user.id == owner_id)
    router.callback_query.filter(F.from_user.id == owner_id)

    @router.message(CommandStart())
    async def on_start(m: Message, bot: Bot) -> None:
        await send(bot, m.chat.id, ctl, await ctl.on_start())

    @router.message(Command(*[c for c, _ in COMMANDS if c != "start"]))
    async def on_command(m: Message, command: CommandObject, bot: Bot) -> None:
        await send(bot, m.chat.id, ctl, await ctl.on_command(command.command))

    @router.message(F.voice)
    async def on_voice(m: Message, bot: Bot) -> None:
        await bot.send_chat_action(m.chat.id, ChatAction.TYPING)

        async def fetch() -> bytes:
            buf = await bot.download(m.voice, destination=BytesIO())
            return buf.getvalue()

        await send(bot, m.chat.id, ctl, await ctl.on_voice(m.voice.duration or 1, fetch))

    @router.message(F.document)
    async def on_document(m: Message, bot: Bot) -> None:
        async def fetch() -> bytes:
            buf = await bot.download(m.document, destination=BytesIO())
            return buf.getvalue()

        res = await ctl.on_document(m.document.file_name or "", m.document.file_size, fetch)
        await send(bot, m.chat.id, ctl, res)

    @router.message(F.text)
    async def on_text(m: Message, bot: Bot) -> None:
        if m.text not in MENU_LABELS:
            await bot.send_chat_action(m.chat.id, ChatAction.TYPING)
        await send(bot, m.chat.id, ctl, await ctl.on_text(m.text))

    @router.message()
    async def on_other(m: Message, bot: Bot) -> None:
        await bot.send_message(m.chat.id, "Принимаю текст, голосовые и md-файлы для импорта.")

    @router.callback_query()
    async def on_callback(q: CallbackQuery, bot: Bot) -> None:
        data = q.data or ""
        res = await ctl.on_callback(data)
        await q.answer(res.toast)
        if data.partition(":")[0] in ONE_SHOT and q.message is not None:
            try:
                await q.message.edit_reply_markup(reply_markup=inline(res.keep) if res.keep else None)
            except Exception:                     # сообщение старое или уже без кнопок
                pass
        chat_id = q.message.chat.id if q.message is not None else owner_id
        await send(bot, chat_id, ctl, res)

    dp = Dispatcher()
    dp.include_router(router)

    @dp.errors()
    async def on_error(event: ErrorEvent, bot: Bot) -> bool:
        exc = event.exception
        log.exception("ошибка обработки обновления", exc_info=exc)
        try:
            ctl.service_event("telegram", "error", f"{exc.__class__.__name__}: {exc}")
            await bot.send_message(owner_id, f"Внутренняя ошибка ({exc.__class__.__name__}). "
                                             f"Подробности в журнале сервиса.")
        except Exception:
            log.exception("не удалось сообщить об ошибке")
        return True

    return dp


async def tick_loop(bot: Bot, ctl: Controller, chat_id: int, every: int = TICK_SEC) -> None:
    wait = every
    while True:
        try:
            res = await ctl.tick()
            if res.replies:
                await send(bot, chat_id, ctl, res)
            wait = every
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("таймер")
            wait = min(wait * 2, TICK_MAX_SEC)
            try:
                ctl.service_event("telegram", "error", f"таймер: {exc.__class__.__name__}: {exc}")
            except Exception:
                pass
        await asyncio.sleep(wait)


async def run_bot(cfg: Config, conn: sqlite3.Connection) -> None:
    core = build_core(cfg, conn, SystemClock())
    ctl = Controller(core, cfg, make_stt(cfg.stt_provider, cfg), make_tts(cfg))
    for note in ctl.prepare():
        log.info(note)
    bot = Bot(cfg.token)
    dp = build_dispatcher(ctl, cfg.owner_id)
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    except Exception:
        log.exception("не удалось задать список команд")
    ticker = asyncio.create_task(tick_loop(bot, ctl, cfg.owner_id))
    log.info("бот запущен, владелец %s", cfg.owner_id)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        ticker.cancel()
        await bot.session.close()
