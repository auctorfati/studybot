"""Слой aiogram на подставной сессии: фильтр владельца, меню, кнопки, аудио, без сети."""

import asyncio
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import (AnswerCallbackQuery, EditMessageReplyMarkup, SendAudio, SendChatAction,
                             SendDocument, SendMessage, SetMyCommands)
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from aiogram.types import Audio as TgAudio

from studybot.bot.controller import Controller
from studybot.bot.telegram import build_dispatcher, chunks, send
from studybot.bot.ui import AudioRef, DocumentRef, Reply, Result
from studybot.speech import StubSTT
from test_sessions import core, run  # noqa: F401

from conftest import db  # noqa: F401

OWNER, STRANGER = 1001, 2002
TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.fail = set()             # типы методов, на которых «сеть падает»

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if type(method) in self.fail:
            raise TelegramNetworkError(method=method, message="сеть")
        if isinstance(method, (SendMessage, SendAudio, SendDocument)):
            extra = {}
            if isinstance(method, SendAudio):
                extra["audio"] = TgAudio(file_id="tg-audio-1", file_unique_id="u1", duration=2)
            return Message(message_id=len(self.requests), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type="private"),
                           text=getattr(method, "text", None), **extra)
        return True

    async def close(self):
        pass

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""


@pytest.fixture
def env(core):
    ctl = Controller(core, core.cfg, StubSTT())
    ctl.prepare()
    session = FakeSession()
    bot = Bot(TOKEN, session=session)
    dp = build_dispatcher(ctl, OWNER)
    return ctl, bot, dp, session


def msg(user_id, text, mid=1):
    return Message(message_id=mid, date=datetime.now(timezone.utc),
                   chat=Chat(id=user_id, type="private"),
                   from_user=User(id=user_id, is_bot=False, first_name="A"), text=text)


def feed(dp, bot, **kw):
    run(dp.feed_update(bot, Update(update_id=1, **kw)))


def test_owner_only(env):
    ctl, bot, dp, session = env
    feed(dp, bot, message=msg(STRANGER, "/start"))
    feed(dp, bot, message=msg(STRANGER, "15 мин"))
    assert session.requests == []
    feed(dp, bot, message=msg(OWNER, "/start"))
    sent = [r for r in session.requests if isinstance(r, SendMessage)]
    assert len(sent) == 1 and sent[0].chat_id == OWNER
    kb = sent[0].reply_markup
    assert [b.text for b in kb.keyboard[0]] == ["5 мин", "15 мин", "30 мин"] and kb.is_persistent


def test_session_buttons_and_callback(env):
    ctl, bot, dp, session = env
    feed(dp, bot, message=msg(OWNER, "15 мин"))
    first = [r for r in session.requests if isinstance(r, SendMessage)][-1]
    data = [b.callback_data for row in first.reply_markup.inline_keyboard for b in row]
    assert any(d.startswith("done:") for d in data) and "end" in data
    assert not any(isinstance(r, SendChatAction) for r in session.requests)    # кнопка меню — без «печатает»
    session.requests.clear()
    q = CallbackQuery(id="q1", from_user=User(id=OWNER, is_bot=False, first_name="A"),
                      chat_instance="c", data=next(d for d in data if d.startswith("done:")),
                      message=msg(OWNER, first.text, mid=7))
    feed(dp, bot, callback_query=q)
    kinds = [type(r) for r in session.requests]
    assert kinds[0] is AnswerCallbackQuery and EditMessageReplyMarkup in kinds and SendMessage in kinds
    # чужое нажатие игнорируется
    session.requests.clear()
    q2 = CallbackQuery(id="q2", from_user=User(id=STRANGER, is_bot=False, first_name="B"),
                       chat_instance="c", data="end", message=msg(STRANGER, "x"))
    feed(dp, bot, callback_query=q2)
    assert session.requests == []


def test_text_answer_shows_typing(env):
    ctl, bot, dp, session = env
    feed(dp, bot, message=msg(OWNER, "что-то"))
    assert isinstance(session.requests[0], SendChatAction)


def test_audio_sent_once_then_by_file_id(env, tmp_path, db):
    ctl, bot, dp, session = env
    item = db.execute("SELECT id FROM items LIMIT 1").fetchone()[0]
    f = tmp_path / "a.mp3"
    f.write_bytes(b"ID3")
    db.execute("INSERT INTO audio_files (item_id, speed, path, text_hash) VALUES (?, 'normal', ?, 'h')",
               (item, str(f)))
    ref = ctl._audio_for(item, "normal")
    run(send(bot, OWNER, ctl, Result([Reply("текст", audio=ref)])))
    assert isinstance(session.requests[0], SendAudio) and isinstance(session.requests[1], SendMessage)
    assert db.execute("SELECT tg_file_id FROM audio_files WHERE item_id = ?", (item,)).fetchone()[0] == "tg-audio-1"
    session.requests.clear()
    ref = ctl._audio_for(item, "normal")
    run(send(bot, OWNER, ctl, Result([Reply(audio=ref)])))
    assert session.requests[0].audio == "tg-audio-1"


def test_missing_audio_file_is_event_not_crash(env, db):
    ctl, bot, dp, session = env
    item = db.execute("SELECT id FROM items LIMIT 1").fetchone()[0]
    run(send(bot, OWNER, ctl, Result([Reply("т", audio=AudioRef(item, "normal", "/нет/файла.mp3", None))])))
    assert [type(r) for r in session.requests] == [SendMessage]
    assert db.execute("SELECT count(*) FROM service_events WHERE service = 'tts'").fetchone()[0] == 1


def test_chunks():
    text = "\n".join("строка %d " % i * 20 for i in range(100))
    parts = chunks(text, 1000)
    assert all(len(p) <= 1000 for p in parts) and "\n".join(parts) == text


def test_document_sent_silently(env):
    ctl, bot, dp, session = env
    doc = DocumentRef("studybot-2026-10-04.sqlite3.gz", b"\x1f\x8b data", "Копия базы")
    run(send(bot, OWNER, ctl, Result([Reply(document=doc, silent=True), Reply("тихо", silent=True)])))
    d, m = session.requests
    assert isinstance(d, SendDocument) and d.disable_notification and d.caption == "Копия базы"
    assert d.document.filename == "studybot-2026-10-04.sqlite3.gz"
    assert isinstance(m, SendMessage) and m.disable_notification


def test_failed_send_unmarks_rest(env, db):
    ctl, bot, dp, session = env
    for kind, mark in (("evening", "d"), ("weekly", "w"), ("weekly_db", "w")):
        db.execute("INSERT INTO sent_marks VALUES (?, ?, 't')", (kind, mark))
    res = Result([Reply("вечер", mark=("evening", "d")), Reply("сводка", mark=("weekly", "w")),
                  Reply(document=DocumentRef("c.gz", b"x"), mark=("weekly_db", "w"))])
    session.fail = {SendDocument}
    with pytest.raises(TelegramNetworkError):
        run(send(bot, OWNER, ctl, res))
    left = {r[0] for r in db.execute("SELECT kind FROM sent_marks")}
    assert left == {"evening", "weekly"}                  # снята только недоставленная копия
    session.fail = {SendMessage}
    db.execute("INSERT INTO sent_marks VALUES ('weekly_db', 'w', 't')")
    with pytest.raises(TelegramNetworkError):
        run(send(bot, OWNER, ctl, res))
    assert db.execute("SELECT count(*) FROM sent_marks").fetchone()[0] == 0
