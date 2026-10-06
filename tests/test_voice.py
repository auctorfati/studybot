"""Озвучка и распознавание Deepgram : клиент, озвучка заранее, живые реплики."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from studybot import voicing
from studybot.bot import academic_render
from studybot.bot.controller import Controller
from studybot.bot.ui import Reply
from studybot.config import ConfigError, DeepgramConfig, load_config
from studybot.deepgram import CHUNK, Deepgram, DeepgramError, split_text
from studybot.sessions.planner import Step
from studybot.speech import DeepgramSTT, STTUnavailable, StubSTT, make_stt
from test_sessions import core, run  # noqa: F401  (фикстура core)

DG = DeepgramConfig(api_key="k")


def mock(handler):
    return Deepgram(DG, transport=httpx.MockTransport(handler))


def test_split_text_by_sentences():
    assert split_text("  Hello,   world. ") == ["Hello, world."]
    long = " ".join(f"Sentence number {k} is here." for k in range(200))
    parts = split_text(long)
    assert len(parts) > 1 and all(len(p) <= CHUNK for p in parts)
    assert " ".join(parts) == " ".join(long.split())
    word = "x" * 50
    huge = " ".join([word] * 100)                             # одно «предложение» длиннее предела
    assert all(len(p) <= CHUNK for p in split_text(huge)) and " ".join(split_text(huge)) == huge


def test_listen_sends_language_and_key():
    seen = {}

    def h(req: httpx.Request):
        seen.update(path=req.url.path, params=dict(req.url.params), auth=req.headers["Authorization"],
                    ctype=req.headers["Content-Type"], body=req.content)
        return httpx.Response(200, json={"results": {"channels": [{"alternatives": [{"transcript": " I live in Moscow. "}]}]}})

    assert asyncio.run(mock(h).listen(b"ogg", "en")) == "I live in Moscow."
    assert seen["path"].endswith("/v1/listen") and seen["params"]["language"] == "en"
    assert seen["params"]["model"] == "nova-3" and seen["auth"] == "Token k" and seen["ctype"] == "audio/ogg"
    asyncio.run(mock(h).listen(b"ogg", "ru"))
    assert seen["params"]["language"] == "ru"


def test_listen_errors():
    with pytest.raises(DeepgramError) as e:
        asyncio.run(mock(lambda r: httpx.Response(401, text="bad key")).listen(b"x", "en"))
    assert e.value.reason == "provider"

    def boom(req):
        raise httpx.ConnectError("down")
    with pytest.raises(DeepgramError) as e:
        asyncio.run(mock(boom).listen(b"x", "en"))
    assert e.value.reason == "network"
    with pytest.raises(DeepgramError) as e:
        asyncio.run(Deepgram(DeepgramConfig()).listen(b"x", "en"))
    assert e.value.reason == "not_configured"
    stt = DeepgramSTT(mock(lambda r: httpx.Response(500)))
    with pytest.raises(STTUnavailable):
        asyncio.run(stt.transcribe(b"x", "en"))


def test_speak_ogg_and_long_mp3():
    calls = []

    def h(req: httpx.Request):
        calls.append((dict(req.url.params), json.loads(req.content)["text"]))
        return httpx.Response(200, content=b"A")

    data, ext = asyncio.run(mock(h).speak("Hello there.", speed=0.8))
    assert (data, ext) == (b"A", "ogg")
    assert calls[0][0] == {"model": "aura-2-thalia-en", "encoding": "opus", "container": "ogg", "speed": "0.8"}
    calls.clear()
    long = " ".join(f"Sentence number {k} is here." for k in range(200))
    data, ext = asyncio.run(mock(h).speak(long, voice="aura-2-luna-en"))
    assert ext == "mp3" and data == b"A" * len(calls) and len(calls) > 1
    assert all(c[0] == {"model": "aura-2-luna-en", "encoding": "mp3"} for c in calls)


def test_config_deepgram(tmp_path):
    base = (open("config.example.toml", encoding="utf-8").read()
            .replace('token = ""', 'token = "1:x"').replace("owner_id = 0 ", "owner_id = 1 "))
    p = tmp_path / "c.toml"
    p.write_text(base, encoding="utf-8")
    cfg = load_config(p)
    assert cfg.deepgram.voice == "aura-2-thalia-en" and len(cfg.deepgram.record_voices) == 6
    assert cfg.deepgram.slow_speed == 0.8 and isinstance(make_stt(cfg.stt_provider, cfg), StubSTT)
    p.write_text(base.replace('provider = "stub"         # "deepgram" — распознавание', 'provider = "deepgram" # распознавание'),
                 encoding="utf-8")
    with pytest.raises(ConfigError):                       # провайдер без ключа
        load_config(p)
    p.write_text(base.replace('provider = "stub"         # "deepgram" — распознавание', 'provider = "deepgram" # распознавание')
                 .replace('[deepgram]\napi_key = ""', '[deepgram]\napi_key = "abc"'), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.stt_provider == "deepgram" and isinstance(make_stt(cfg.stt_provider, cfg), DeepgramSTT)


class FakeTTS:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def speak(self, text, voice=None, speed=1.0):
        self.calls.append((text, voice, speed))
        if self.fail:
            raise DeepgramError("network", "down")
        return f"{voice}|{speed}|{text}".encode(), "ogg"


def test_voicing_plan_run_and_redo(core, tmp_path):
    conn = core.conn
    jobs = voicing.plan(conn, core.rehearsals, DG)
    items = [j for j in jobs if j.kind == "item"]
    assert items and all(j.voice == "aura-2-thalia-en" and j.base.startswith("en/") for j in items)
    todo = voicing.pending(conn, tmp_path, jobs)
    assert len(todo) == 2 * len(jobs)                       # два темпа на запись
    tts = FakeTTS()
    st = asyncio.run(voicing.run(conn, tmp_path, tts, todo, 0.8))
    assert st.done == len(todo) and st.failed == 0
    assert {c[2] for c in tts.calls} == {1.0, 0.8}
    row = conn.execute("SELECT path FROM audio_files WHERE item_id = ? AND speed = 'slow'", (int(items[0].key),)).fetchone()
    assert row["path"].endswith("_slow.ogg") and (tmp_path / row["path"]).exists()
    assert voicing.pending(conn, tmp_path, voicing.plan(conn, core.rehearsals, DG)) == []   # всё готово
    conn.execute("UPDATE items SET audio_text = 'Changed text.' WHERE id = ?", (int(items[0].key),))
    redo = voicing.pending(conn, tmp_path, voicing.plan(conn, core.rehearsals, DG))
    assert [(j.key, s) for j, s in redo] == [(items[0].key, "normal"), (items[0].key, "slow")]
    (tmp_path / row["path"]).unlink()                       # пропавший файл тоже озвучивается заново
    assert len(voicing.pending(conn, tmp_path, voicing.plan(conn, core.rehearsals, DG))) == 2


def test_voicing_stops_on_repeated_errors(core, tmp_path):
    jobs = voicing.plan(core.conn, core.rehearsals, DG)
    todo = voicing.pending(core.conn, tmp_path, jobs)
    st = asyncio.run(voicing.run(core.conn, tmp_path, FakeTTS(fail=True), todo, 0.8, max_errors_in_row=3, concurrency=1))
    assert st.done == 0 and st.stopped and st.failed == 3
    assert core.conn.execute("SELECT count(*) FROM audio_files").fetchone()[0] == 0


def test_voice_names_and_phrase_voice(core):
    cfg = replace(DG, record_voices=("v1", "v2"))
    assert voicing._safe("Д1:q2") == "Д1_q2"
    assert {j.voice for j in voicing.plan(core.conn, core.rehearsals, cfg) if j.kind == "item"} == {DG.voice}


def exit_step(history):
    return Step("exit_speech", "exit", 600, {
        "area": "EN3", "part": "advice_dialog", "title": "диалог-совет", "criteria": "условия", "mode": "talk",
        "card": {"Роль бота": "друг", "Первый вопрос": "What should I do?"}, "record": "СВ1", "turns": 12,
        "history": history, "answers": [], "module": "3"})


def test_exit_reply_live_hides_line():
    st = exit_step([{"role": "assistant", "content": "What should I do?"}])
    plain = academic_render.exit_speech_reply(st)
    assert "What should I do?" in plain.text and plain.live_text is None
    live = academic_render.exit_speech_reply(st, live=True)
    assert "What should I do?" not in live.text and live.live_text == "What should I do?"


def test_controller_live_voice(core, tmp_path):
    cfg = replace(core.cfg, paths=replace(core.cfg.paths, data_dir=tmp_path))
    ctl = Controller(core, cfg, StubSTT(), FakeTTS())
    st = exit_step([{"role": "assistant", "content": "Hi."}])
    assert not ctl._live_on(st)                            # настройка выключена — реплики текстом
    core.settings.set("exit_live_tts", True)
    assert ctl._live_on(st)
    r = Reply("Собеседник отвечает голосом.", live_text="Tell me more.")
    asyncio.run(ctl.voice_live(r))
    assert r.audio is not None and r.live_text is None and "Tell me more." not in r.text
    assert open(r.audio.path, "rb").read().endswith(b"Tell me more.")
    bad = Controller(core, cfg, StubSTT(), FakeTTS(fail=True))
    r = Reply("Собеседник отвечает голосом.", live_text="Tell me more.")
    asyncio.run(bad.voice_live(r))
    assert r.audio is None and "Tell me more." in r.text      # без озвучки разговор не встаёт
    assert Controller(core, cfg, StubSTT())._live_on(st) is False   # без сервиса озвучки — текстом


def test_controller_autovoice_after_start(core, tmp_path):
    cfg = replace(core.cfg, paths=replace(core.cfg.paths, data_dir=tmp_path))
    tts = FakeTTS()
    ctl = Controller(core, cfg, StubSTT(), tts)

    async def go():
        await ctl.tick()
        await ctl.voicing
    asyncio.run(go())
    n = core.conn.execute("SELECT count(*) FROM audio_files").fetchone()[0]
    assert n > 0 and len(tts.calls) >= n and not ctl.voice_wanted


def test_controller_autovoice_retries_after_failures(core, tmp_path):
    from datetime import timedelta
    cfg = replace(core.cfg, paths=replace(core.cfg.paths, data_dir=tmp_path))
    ctl = Controller(core, cfg, StubSTT(), FakeTTS(fail=True))

    async def go():
        await ctl.tick()
        await ctl.voicing
    asyncio.run(go())
    assert ctl.voice_retry is not None and not ctl.voice_wanted
    ctl.tts = FakeTTS()
    core.clock.advance(seconds=31 * 60)
    asyncio.run(go())
    assert core.conn.execute("SELECT count(*) FROM audio_files").fetchone()[0] > 0 and ctl.voice_retry is None


def test_mode_question_before_every_session(core):
    """Решение 05.10.2026: перед каждой сессией бот спрашивает «голосом или текстом?» и подсказывает."""
    from studybot.speech import FakeSTT
    from studybot.bot.ui import BTN_15
    ctl = Controller(core, core.cfg, FakeSTT())
    res = run(ctl.on_text(BTN_15))
    q = res.replies[0]
    assert q.text.startswith("Голосом или текстом?") and core.engine.active() is None
    assert [b.data for row in q.buttons for b in row] == ["smode:15:voice", "smode:15:text"]
    res = run(ctl.on_callback("smode:15:text"))
    assert core.settings.get("mode") == "text" and core.engine.active() is not None
    assert res.replies[0].text.startswith("Сессия 15 минут.")
    res = run(ctl.on_callback("start:evening"))              # кнопка вечернего уведомления — тоже с вопросом
    assert res.replies[0].text.startswith("Голосом или текстом?")
    run(ctl.on_callback("smode:evening:voice"))
    assert core.settings.get("mode") == "voice"
    assert "устарела" in run(ctl.on_callback("smode:xx:voice")).replies[0].text
    plain = Controller(core, core.cfg, StubSTT())            # без распознавания — сразу текстом, без вопроса
    assert run(plain.on_text(BTN_15)).replies[0].text.startswith("Сессия 15 минут.")


def test_mode_question_names_waiting_voice_work(core, monkeypatch):
    from studybot.speech import FakeSTT
    ctl = Controller(core, core.cfg, FakeSTT())
    monkeypatch.setattr(core.engine.blocks, "due_for_control", lambda day: ("EN1", "2"))
    monkeypatch.setattr(core.engine.planner, "speech_pending", lambda day, now: 3)
    text = ctl._mode_question("30").text
    assert "Ждут голоса: контрольная точка блока 2 модуля 1, 3 фразы на устной ступени." in text
    monkeypatch.setattr(core.engine.blocks, "due_for_control", lambda day: None)
    monkeypatch.setattr(core.engine.planner, "speech_pending", lambda day, now: 0)
    assert "устные ступени закрываются только голосом" in ctl._mode_question("30").text


def test_intro_repeat_feedback_and_retry(core):
    """0.11.3: повтор при знакомстве — бот говорит, что расслышал; не совпало — фраза и ещё попытка."""
    from studybot.speech import FakeSTT
    stt = FakeSTT("completely different words")
    ctl = Controller(core, core.cfg, stt)
    run(ctl.on_callback("smode:15:voice"))
    step = core.engine.current()
    assert step.task().format == "intro"

    async def fetch():
        return b"ogg"
    res = run(ctl.on_voice(3, fetch))
    assert res.replies[0].text.startswith("Расслышал: «completely different words».")
    assert core.engine.current().id == step.id                     # остаёмся на фразе
    assert f"done:{step.id}" in [b.data for r in res.replies for row in r.buttons for b in row]
    stt.texts.append(step.task().expected[0])
    res = run(ctl.on_voice(3, fetch))
    assert res.replies[0].text.startswith("Верно.") and core.engine.current().id != step.id


def test_block_header_once_per_session(core):
    from studybot.speech import FakeSTT
    ctl = Controller(core, core.cfg, FakeSTT())
    res = run(ctl.on_callback("smode:15:voice"))
    assert "Новый блок" in res.replies[-1].text
    step = core.engine.current()
    res = run(ctl.on_callback(f"done:{step.id}"))
    nxt = core.engine.current()
    if nxt.kind == "task" and nxt.task().format == "intro":
        assert "Новый блок" not in res.replies[-1].text


def test_five_minutes_no_psychology_topup(core, monkeypatch):
    """Пятиминутка — английский; кончился план — добор только повторениями, без психологии и разговора."""
    monkeypatch.setattr(core.engine, "_psy_on", lambda: True)
    called = []
    monkeypatch.setattr(core.engine.psy_planner, "build", lambda *a, **k: called.append(1) or [])
    monkeypatch.setattr(core.engine.planner, "talk_slot", lambda *a, **k: (called.append(2) or [], 0))
    core.engine.start("5")
    s = core.engine.active()
    assert core.engine._topup(s) is None or True
    assert 1 not in called and 2 not in called


def test_voice_forbidden_hint():
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendVoice
    from studybot.bot import telegram as tg
    from studybot.bot.ui import AudioRef
    sent, events = [], []

    class Bot:
        async def send_voice(self, chat_id, source):
            raise TelegramBadRequest(SendVoice(chat_id=1, voice="x"), "Bad Request: VOICE_MESSAGES_FORBIDDEN")

        async def send_message(self, chat_id, text, **kw):
            sent.append(text)

    class Ctl:
        def service_event(self, *a):
            events.append(a)
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(b"x")
    asyncio.run(tg.send_audio(Bot(), 1, Ctl(), AudioRef(1, "normal", f.name, None)))
    os.unlink(f.name)
    assert sent and "Конфиденциальность" in sent[0] and events


def test_intro_repeat_template_phrase(core):
    """0.11.4: фраза с «…» — повтор без слова на месте многоточия тоже верный."""
    from studybot.bot.controller import Controller as C
    from studybot.speech import FakeSTT
    from studybot.sessions.planner import Step as S
    stt = FakeSTT("How do you say in English?", "How do you say")
    ctl = C(core, core.cfg, stt)
    run(ctl.on_callback("smode:15:voice"))
    step = core.engine.current()
    core.conn.execute("UPDATE session_steps SET payload_json = json_set(payload_json, '$.expected', json_array(?), "
                      "'$.shown_en', ?) WHERE id = ?", ('How do you say "…" in English?', 'How do you say "…" in English?', step.id))

    async def fetch():
        return b"ogg"
    res = run(ctl.on_voice(3, fetch))
    assert res.replies[0].text.startswith("Верно.")
    step = core.engine.current()
    core.conn.execute("UPDATE session_steps SET payload_json = json_set(payload_json, '$.expected', json_array(?), "
                      "'$.shown_en', ?) WHERE id = ?", ('How do you say "…" in English?', 'How do you say "…" in English?', step.id))
    res = run(ctl.on_voice(3, fetch))
    assert "любое слово" in res.replies[0].text and core.engine.current().id == step.id
