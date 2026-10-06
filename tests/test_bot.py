import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from studybot.bot.controller import Controller
from studybot.bot.ui import BTN_15, BTN_30, BTN_LONG, BTN_MORE, BTN_TODAY, menu_rows
from studybot.enums import Track
from studybot.speech import FakeSTT, STTUnavailable, StubSTT
from test_sessions import auto_reply, core, run  # noqa: F401  (фикстура core)

from conftest import db  # noqa: F401

MSK = ZoneInfo("Europe/Moscow")
EN_BANK = Path(__file__).parent / "data" / "en_bank_blocks_0-1.md"


@pytest.fixture
def ctl(core):
    c = Controller(core, core.cfg, StubSTT())
    c.prepare()
    return c


def texts(res):
    return [r.text for r in res.replies]


def all_text(res):
    return "\n".join(texts(res))


def buttons(reply):
    return [b.data for row in reply.buttons for b in row]


def last(res):
    return res.replies[-1]


def step_id(reply, prefix):
    return int(next(d for d in buttons(reply) if d.startswith(prefix + ":")).split(":")[1])


def finish_session(ctl, core, sec=30):
    res = None
    for _ in range(300):
        step = core.engine.current()
        if step is None:
            return res
        core.clock.advance(seconds=sec)
        if step.kind != "task":
            res = run(ctl.on_text("I'm a teacher."))
        elif step.task().format == "intro":
            res = run(ctl.on_callback(f"done:{step.id}"))
        else:
            t = step.task()
            res = run(ctl.on_text(t.expected[0] if t.expected else "Hi, I'm Sam."))
    raise AssertionError("сессия не закончилась")


# запуск и меню

def test_prepare_forces_text_without_stt(core):
    c = Controller(core, core.cfg, StubSTT())
    assert core.settings.get("mode") == "voice"
    assert c.prepare() and core.settings.get("mode") == "text"
    c2 = Controller(core, core.cfg, FakeSTT())
    core.settings.set("mode", "voice")
    assert c2.prepare() == [] and core.settings.get("mode") == "voice"


def test_start_greeting_and_menu(ctl):
    res = run(ctl.on_start())
    r = res.replies[0]
    assert r.menu and "20:30" in r.text and "только текст" in r.text
    assert "Контент ещё не загружен" not in r.text


def test_menu_sunday_has_long():
    from datetime import date
    assert BTN_LONG in menu_rows(date(2026, 10, 4))[1]
    assert BTN_LONG not in menu_rows(date(2026, 10, 5))[1]


def test_text_without_session(ctl):
    res = run(ctl.on_text("hello"))
    assert "нет сессии" in all_text(res) and res.replies[-1].menu


# сессия

def test_session_intro_flow(ctl, core, db):
    res = run(ctl.on_text(BTN_15))
    first = res.replies[0]
    assert first.text.startswith("Сессия 15 минут. Сегодня 0 из 90, английский 0.")
    assert "Новая фраза" in first.text and "Прочитай и запомни." in first.text
    sid = step_id(first, "done")
    core.clock.advance(seconds=20)
    res = run(ctl.on_callback(f"done:{sid}"))
    nxt = res.replies[0]
    assert "По-русски:" in nxt.text or "Новая фраза" in nxt.text
    # старая кнопка того же задания больше не работает
    res = run(ctl.on_callback(f"done:{sid}"))
    assert res.toast and not res.replies


def test_answer_right_and_wrong_with_dispute(ctl, core, db):
    run(ctl.on_text(BTN_15))
    # пройти знакомство и дойти до задания с проверкой
    while True:
        step = core.engine.current()
        t = step.task()
        if t.format != "intro":
            break
        core.clock.advance(seconds=20)
        run(ctl.on_callback(f"done:{step.id}"))
    core.clock.advance(seconds=10)
    res = run(ctl.on_text(t.expected[0]))
    assert res.replies[0].text.startswith("Верно.")
    # следующее задание: неверно через модель
    step = core.engine.current()
    while step.task().format == "intro":
        run(ctl.on_callback(f"done:{step.id}"))
        step = core.engine.current()
    core.fake.push({"verdict": "error", "tag": "article", "explanation": "Нужен артикль.", "correction": "X."})
    res = run(ctl.on_text("совсем не то"))
    fb = res.replies[0]
    assert fb.text.startswith("Неверно.\nНужен артикль.\nПравильно: X.")
    rid = int(next(d for d in buttons(fb) if d.startswith("dispute:")).split(":")[1])
    assert len(res.replies) == 2                         # итог с кнопкой отдельно, следом задание
    res = run(ctl.on_callback(f"dispute:{rid}"))
    assert res.toast == "Оспорено" and "до разбора срывом не считается" in res.replies[0].text
    assert db.execute("SELECT disputed FROM reviews WHERE id = ?", (rid,)).fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM errors").fetchone()[0] == 0


def test_idk_is_wrong_by_code(ctl, core, db):
    run(ctl.on_text(BTN_15))
    step = core.engine.current()
    while step.task().format == "intro":
        run(ctl.on_callback(f"done:{step.id}"))
        step = core.engine.current()
    calls = len(core.fake.calls)
    res = run(ctl.on_callback(f"idk:{step.id}"))
    text = res.replies[0].text
    assert text.startswith("Неверно.\nПравильно:") and "Пустой ответ" not in text
    assert len(core.fake.calls) == calls                 # модель не вызывалась


def test_end_and_skip(ctl, core, db):
    run(ctl.on_text(BTN_15))
    step = core.engine.current()
    res = run(ctl.on_callback(f"skip:{step.id}"))
    assert res.replies[0].text.startswith("Отложено до следующей сессии.")
    core.clock.advance(seconds=30)
    res = run(ctl.on_callback("end"))
    r = res.replies[0]
    assert r.menu and "Сегодня" in r.text and core.engine.active() is None
    res = run(ctl.on_command("stop"))
    assert "Сессии нет" in res.replies[0].text


def test_new_session_closes_old(ctl, core):
    run(ctl.on_text(BTN_15))
    res = run(ctl.on_text(BTN_30))
    assert "Предыдущая сессия закрыта" in res.replies[0].text


def test_session_runs_to_end(ctl, core, db):
    res = run(ctl.on_text(BTN_15))
    for _ in range(100):
        step = core.engine.current()
        if step is None:
            break
        core.clock.advance(seconds=40)
        if step.kind == "task" and step.task().format == "intro":
            res = run(ctl.on_callback(f"done:{step.id}"))
        elif step.kind == "task":
            t = step.task()
            res = run(ctl.on_text(t.expected[0] if t.expected else "Hi, I'm Sam."))
        else:
            res = run(ctl.on_text("I'm fine."))
    end = res.replies[-1]
    assert end.menu and "Сессия окончена." in end.text


def test_dialog_through_controller(ctl, core, db):
    run(ctl.on_text(BTN_30))                              # день 1: ввести фразы
    finish_session(ctl, core)
    core.clock.advance(days=1)
    run(ctl.on_text(BTN_15))
    seen_dialog = False
    for _ in range(200):
        step = core.engine.current()
        if step is None:
            break
        core.clock.advance(seconds=30)
        if step.kind == "dialog":
            seen_dialog = True
            res = run(ctl.on_text("I'm a teacher."))
            if "Диалог окончен." not in all_text(res):
                assert res.replies[0].text == "Nice. And you?"
                assert "skip:" in " ".join(buttons(res.replies[0]))
        elif step.kind != "task":
            core.engine.defer(step.id)
        elif step.task().format == "intro":
            run(ctl.on_callback(f"done:{step.id}"))
        else:
            t = step.task()
            run(ctl.on_text(t.expected[0] if t.expected else "Hi"))
    assert seen_dialog


# голос

def test_voice_stub_keeps_step(ctl, core):
    run(ctl.on_text(BTN_15))
    step = core.engine.current()
    while step.task().format == "intro":
        run(ctl.on_callback(f"done:{step.id}"))
        step = core.engine.current()

    async def fetch():
        raise AssertionError("без распознавания файл не скачивается")

    res = run(ctl.on_voice(4, fetch))
    assert "не подключено" in res.replies[0].text
    assert core.engine.current().id == step.id


def test_voice_transcribed_and_counted(core, db):
    stt = FakeSTT()
    c = Controller(core, core.cfg, stt)
    run(c.on_callback("smode:15:voice"))                  # перед сессией — «голосом или текстом?»
    step = core.engine.current()
    got = []

    async def fetch():
        got.append(1)
        return b"ogg"

    stt.texts.append(step.task().expected[0])
    res = run(c.on_voice(6, fetch))                       # знакомство: бот говорит, что расслышал (0.11.3)
    assert got and res.replies[0].text.startswith("Верно. Расслышал:")
    got.clear()
    stt.calls.clear()
    step = core.engine.current()
    while step.task().format == "intro":
        run(c.on_callback(f"done:{step.id}"))
        step = core.engine.current()
    stt.texts.append(step.task().expected[0])
    before = core.days.status(core.engine.today()).voice_sec
    res = run(c.on_voice(7, fetch))
    assert res.replies[0].text.startswith("Распознано: ") and "Верно." in res.replies[0].text
    assert stt.calls == [(3, "en")]
    assert core.days.status(core.engine.today()).voice_sec == before + 7
    assert db.execute("SELECT mode FROM reviews ORDER BY id DESC LIMIT 1").fetchone()[0] == "voice"
    # сбой распознавания: задание откладывается
    step = core.engine.current()
    while step.task().format == "intro":
        run(c.on_callback(f"done:{step.id}"))
        step = core.engine.current()
    stt.texts.append(STTUnavailable("network", "сеть"))
    res = run(c.on_voice(5, fetch))
    assert res.replies[0].text.startswith("Распознавание недоступно")
    assert db.execute("SELECT status FROM session_steps WHERE id = ?", (step.id,)).fetchone()[0] == "deferred"
    assert db.execute("SELECT count(*) FROM service_events WHERE service = 'stt'").fetchone()[0] == 1


def test_typed_answer_on_voice_stage4_does_not_schedule(core, db):
    """Голосовой режим, ответ набран текстом: ступень 4 — только тренировка написания."""
    c = Controller(core, core.cfg, FakeSTT())
    core.settings.set("mode", "voice")
    item = db.execute("SELECT id FROM items WHERE code = '1.04'").fetchone()[0]
    core.scheduler.ensure_state(item)
    db.execute("UPDATE item_state SET stage = 4 WHERE item_id = ?", (item,))
    core.scheduler.enter_grid(item, core.engine.today() - timedelta(days=3), core.clock.now(), step=1)
    before = db.execute("SELECT step, due_date FROM item_state WHERE item_id = ?", (item,)).fetchone()
    run(c.on_callback("smode:5:voice"))
    step = core.engine.current()
    assert step.item_id == item and step.task().format == "ru_en"
    run(c.on_text(step.task().expected[0]))
    after = db.execute("SELECT step, due_date FROM item_state WHERE item_id = ?", (item,)).fetchone()
    assert tuple(after) == tuple(before)
    r = db.execute("SELECT mode, schedules FROM reviews ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(r) == ("text", 0)


# импорт

def test_import_document_flow(ctl, core, db, tmp_path):
    data = EN_BANK.read_bytes().replace("Повтори, пожалуйста".encode(), "Повтори ещё раз".encode())
    got = []

    async def fetch():
        got.append(1)
        return data

    res = run(ctl.on_document("bank.txt", 100, fetch))
    assert "md-файлы" in res.replies[0].text and not got
    res = run(ctl.on_document(EN_BANK.name, len(data), fetch))
    r = res.replies[0]
    assert "изменена формулировка 1" in r.text
    token = next(d for d in buttons(r) if d.startswith("imp:ok:")).split(":")[2]
    res = run(ctl.on_callback(f"imp:ok:{token}"))
    assert res.replies[0].text.startswith("Принято, версия контента 2.")
    saved = core.cfg.paths.content_dir / EN_BANK.name
    assert saved.read_bytes() == data
    assert db.execute("SELECT prompt FROM items WHERE code = '0.01'").fetchone()[0] == "Повтори ещё раз"
    res = run(ctl.on_callback(f"imp:ok:{token}"))
    assert "устарел" in res.replies[0].text


def test_import_bad_file(ctl, core, db):
    async def fetch():
        return "# Что-то другое\n".encode()

    res = run(ctl.on_document("x.md", 10, fetch))
    assert "Импорт невозможен" in res.replies[0].text and not res.replies[0].buttons


# уведомление, пауза, «не сегодня»

def at(core, y, m, d, hh, mm=0):
    core.clock.set(datetime(y, m, d, hh, mm, tzinfo=MSK))


def test_evening_notification_once(ctl, core, db):
    at(core, 2026, 9, 28, 20, 29)
    assert run(ctl.tick()).replies == []
    at(core, 2026, 9, 28, 20, 31)
    res = run(ctl.tick())
    r = res.replies[0]
    assert r.text.startswith("Вечер. Сегодня 0 из 90, английский 0.\nДо нормы 90 мин.")
    assert buttons(r) == ["start:evening", "skipday"]
    assert run(ctl.tick()).replies == []                  # второй раз не приходит
    at(core, 2026, 9, 29, 2, 0)                           # ночь — тот же учебный день
    assert run(ctl.tick()).replies == []


def test_evening_norm_done_one_line(ctl, core):
    core.days.add_time(core.engine.today(), Track.EN, 91 * 60)
    at(core, 2026, 9, 28, 20, 30)
    r = run(ctl.tick()).replies[0]
    assert r.text == "Итог дня: 91 из 90, норма набрана." and not r.buttons


def test_evening_waits_for_active_session(ctl, core):
    at(core, 2026, 9, 28, 20, 25)
    run(ctl.on_text(BTN_15))
    at(core, 2026, 9, 28, 20, 31)
    assert run(ctl.tick()).replies == []                  # идёт сессия
    at(core, 2026, 9, 28, 20, 45)                         # 20 минут без ответа — пауза
    res = run(ctl.tick())
    assert core.engine.active()["status"] == "paused" and res.replies


def test_skip_day_and_pause(ctl, core, db):
    res = run(ctl.on_callback("skipday"))
    assert "День закрыт" in res.replies[0].text
    at(core, 2026, 9, 28, 20, 31)
    assert run(ctl.tick()).replies == []
    res = run(ctl.on_command("pause"))
    assert "pause:3" in buttons(res.replies[0])
    res = run(ctl.on_callback("pause:3"))
    assert "до ср 30.09 включительно" in res.replies[0].text
    day = core.engine.today()
    assert core.queue.new_budget(Track.EN, day) == 0
    at(core, 2026, 9, 29, 20, 31)
    assert run(ctl.tick()).replies == []
    res = run(ctl.on_text(BTN_15))
    assert "Идёт пауза" in res.replies[0].text
    assert "unpause" in buttons(run(ctl.on_command("pause")).replies[0])
    run(ctl.on_callback("unpause"))
    assert not core.settings.is_paused(core.engine.today())


def test_sunday_evening_brings_weekly_once(ctl, core, db):
    at(core, 2026, 10, 4, 20, 31)                         # воскресенье
    res = run(ctl.tick())
    assert any(t.startswith("Сводка за неделю пн 28.09 — вс 4.10.") for t in texts(res))
    assert db.execute("SELECT count(*) FROM sent_marks WHERE kind = 'weekly'").fetchone()[0] == 1


# режим, настройки, отчёты

def test_mode_switch(ctl, core):
    res = run(ctl.on_callback("mode:voice"))
    assert "не подключено" in res.replies[0].text and core.settings.get("mode") == "text"
    c = Controller(core, core.cfg, FakeSTT())
    run(c.on_callback("mode:voice"))
    assert core.settings.get("mode") == "voice"


def test_settings_edit(ctl, core):
    res = run(ctl.on_command("settings"))
    text = res.replies[0].text
    assert "1. Время вечернего уведомления: 20:30" in text and "разбор темы до первой проверки" not in text
    res = run(ctl.on_text("1 25:00"))
    assert res.replies[0].text.startswith("Не принято")
    res = run(ctl.on_text("1 21:15"))
    assert core.settings.get("notify_time") == "21:15" and res.replies[0].menu
    res = run(ctl.on_text("hello"))                       # правка закрыта: это снова ответ
    assert "нет сессии" in res.replies[0].text
    run(ctl.on_command("settings"))
    run(ctl.on_text("1 сброс"))
    assert core.settings.get("notify_time") == "20:30"
    run(ctl.on_command("settings"))
    keys = __import__("studybot.bot.reports", fromlist=["editable"]).editable(core)
    n = keys.index("error_share_max") + 1
    run(ctl.on_text(f"{n} 15%"))
    assert core.settings.get("error_share_max") == pytest.approx(0.15)


def test_today_and_state(ctl, core, db):
    run(ctl.on_text(BTN_15))
    step = core.engine.current()
    core.clock.advance(seconds=30)
    run(ctl.on_callback(f"done:{step.id}"))
    res = run(ctl.on_text(BTN_TODAY))
    t = res.replies[0].text
    assert "Новых фраз: 1 из 8." in t and "Блок 0: введено 1 из" in t and "Идёт сессия" in t
    res = run(ctl.on_callback("more:state"))
    s = res.replies[0].text
    assert "Контент: версия 1, файлов 1." in s and "Распознавание: stub, не подключено." in s
    assert "Модель: работает." in s
    res = run(ctl.on_text(BTN_MORE))
    assert "more:week" in buttons(res.replies[0])


def test_weekly_with_review_list(ctl, core, db):
    run(ctl.on_text(BTN_30))
    finish_session(ctl, core)
    core.clock.advance(days=1)
    run(ctl.on_text(BTN_15))
    step = core.engine.current()
    while step.kind != "task" or step.task().format == "intro":
        run(ctl.on_callback(f"done:{step.id}"))
        step = core.engine.current()
    core.fake.push({"verdict": "acceptable", "tag": None, "explanation": "", "correction": ""})
    run(ctl.on_text("some other acceptable wording"))
    step = core.engine.current()
    core.fake.push({"verdict": "error", "tag": "to_be", "explanation": "Нет am.", "correction": "I am."})
    rid = next(d for d in buttons(run(ctl.on_text("I teacher")).replies[0]) if d.startswith("dispute:"))
    run(ctl.on_callback(rid))
    res = run(ctl.on_command("week"))
    summary, review = res.replies
    assert "Фразы: не введены" in summary.text and "Модели за неделю:" in summary.text
    assert "Оспоренные вердикты: 1." in review.text and "Кандидаты в варианты: 1." in review.text
    var = next(d for d in buttons(review) if d.startswith("var:1:"))
    disp = next(d for d in buttons(review) if d.startswith("disp:me:"))
    assert run(ctl.on_callback(var)).toast == "Вариант принят"
    assert run(ctl.on_callback(disp)).toast == "Отмечено"
    res = run(ctl.on_command("week"))
    assert len(res.replies) == 1                          # разбирать больше нечего


def test_long_session_sunday_sends_weekly(ctl, core, db):
    at(core, 2026, 10, 3, 12, 0)                          # суббота: ввести фразы
    run(ctl.on_text(BTN_30))
    finish_session(ctl, core)
    at(core, 2026, 10, 4, 12, 0)                          # воскресенье
    res = run(ctl.on_text(BTN_LONG))
    assert res.replies[0].text.startswith("Длинная сессия, 40 мин.")
    assert core.engine.current() is not None
    res = run(ctl.on_callback("end"))
    assert any(t.startswith("Сводка за неделю") for t in texts(res))
    at(core, 2026, 10, 4, 20, 31)
    res = run(ctl.tick())
    assert res.replies and not any(t.startswith("Сводка за неделю") for t in texts(res))
