"""Тексты для команд «сегодня», «состояние», недельной сводки, вечернего
уведомления и настроек. Только чтение базы.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from .. import __version__
from ..app import Core
from ..backup import human_size
from ..checking.disputes import pending, pending_variants
from ..clock import to_iso
from ..config import Config
from ..db import schema_version
from ..enums import EN_ERROR_TAGS, PSY_GAP_TAGS, TopicState, Track
from ..settings import SPEC, SettingError
from .ui import Button, Reply

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MAX_REVIEW_ITEMS = 8
VERDICT_RU = {"wrong": "ошибка", "correct": "верно", "partial": "частично", "hard": "с трудом"}


def cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def dm(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day}.{d.month:02d}"


def usd(x: float) -> str:
    return f"${x:.2f}"


# сегодня

def ladder_due(core: Core, day: date) -> int:
    return core.conn.execute(
        "SELECT count(*) FROM items i JOIN item_state s ON s.item_id = i.id WHERE i.track = 'en' "
        "AND i.archived = 0 AND s.stage IN (2, 3) AND s.due_date <= ? "
        "AND (s.deferred_until IS NULL OR s.deferred_until <= ?)",
        (day.isoformat(), to_iso(core.clock.now()))).fetchone()[0]


def current_block(core: Core) -> str | None:
    row = core.conn.execute("SELECT area, block FROM en_blocks WHERE opened_at IS NOT NULL "
                            "ORDER BY area DESC, CAST(block AS INTEGER) DESC LIMIT 1").fetchone()
    if row is None:
        return None
    total, done = core.conn.execute(
        "SELECT count(*), sum(COALESCE(s.stage, 0) >= 2) FROM items i LEFT JOIN item_state s "
        "ON s.item_id = i.id WHERE i.track = 'en' AND i.archived = 0 AND i.kind = 'phrase' "
        "AND i.area = ? AND i.unit = ?", (row["area"], row["block"])).fetchone()
    return f"Блок {row['block']}: введено {done or 0} из {total} фраз."


def today_text(core: Core) -> str:
    e, st_ = core.engine, core.settings
    now = core.clock.now()
    day = e.today()
    st = core.days.status(day)
    lines = [cap(st.counter()) + "."]
    if st.skipped:
        lines.append("День закрыт кнопкой «Не сегодня».")
    elif st.norm_done:
        lines.append("Норма набрана.")
    else:
        lines.append(f"До нормы {st.left_min} мин.")
    if st.counted:
        lines.append("День засчитан.")
    else:
        lines.append(f"Для зачёта дня: {st_.get('day_min_total')} мин, из них английского "
                     f"{st_.get('day_min_en')}.")
    if st_.is_paused(day):
        lines.append(f"Пауза до {dm(date.fromisoformat(st_.get('pause_until')))} включительно: "
                     f"новое не вводится.")
    else:
        lines.append(f"Новых фраз: {core.queue.new_used(Track.EN, day)} из {st_.new_limit_en(day)}.")
    grid = len(core.queue.due_today(Track.EN, day, now))
    ladder = ladder_due(core, day)
    lines.append(f"Повторений: сделано {core.days.reviews_done(day, Track.EN)}, "
                 f"ждут {ladder + grid}, потолок {core.queue.cap(Track.EN)}.")
    if st_.get("mode") == "text" and grid:
        lines.append(f"Из них речевых (ступень 4): {grid}, ждут голосовой сессии.")
    block = current_block(core)
    if block:
        lines.append(block)
    lines.append(f"Режим: {'голос' if st_.get('mode') == 'voice' else 'только текст'}.")
    s = e.active()
    if s is not None:
        lines.append("Сессия на паузе: ответь на задание, и она продолжится." if s["status"] == "paused"
                     else "Идёт сессия, задание ждёт ответа.")
    return "\n".join(lines)


# состояние

def state_text(core: Core, cfg: Config, stt_name: str, stt_configured: bool) -> str:
    conn = core.conn
    now = core.clock.now()
    day = core.engine.today()
    month = day.replace(day=1).isoformat()
    lines = [f"Учебный бот {__version__}, схема базы {schema_version(conn)}."]

    versions = conn.execute(
        "SELECT cv.id, cv.file_name, cv.imported_at FROM content_versions cv JOIN "
        "(SELECT file_name, max(id) AS id FROM content_versions GROUP BY file_name) last "
        "ON last.id = cv.id ORDER BY cv.id DESC LIMIT 12").fetchall()
    if versions:
        top = conn.execute("SELECT max(id) FROM content_versions").fetchone()[0]
        lines.append(f"Контент: версия {top}, файлов {len(versions)}.")
        for v in versions:
            when = core.calendar.local(datetime.fromisoformat(v["imported_at"]))
            lines.append(f"— {v['file_name']}: версия {v['id']}, {when:%d.%m %H:%M}")
    else:
        lines.append("Контент не загружен: пришли банк английского документом.")
    for track, title in ((Track.EN.value, "Английский"), (Track.PSY.value, "Психология")):
        n, arch, missing, by_key = conn.execute(
            "SELECT sum(archived = 0), sum(archived = 1), sum(archived = 0 AND fragment_status = 'missing'), "
            "sum(archived = 0 AND track = 'psy' AND fragment_status = 'n/a') "
            "FROM items WHERE track = ?", (track,)).fetchone()
        if n or arch:
            extra = f", без фрагмента {missing}" if missing else ""
            extra += f", проверка по ключу (источника нет) {by_key}" if by_key else ""
            lines.append(f"{title}: позиций {n or 0}, в архиве {arch or 0}{extra}.")
    pages = conn.execute("SELECT count(*) FROM ref_pages").fetchone()[0]
    words = conn.execute("SELECT count(*) FROM vocab").fetchone()[0]
    if pages or words:
        lines.append(f"Справочник: обзорных страниц {pages}, слов и сочетаний {words}.")

    llm = core.llm
    ok, why = llm.available()
    spent_month, calls, failed = conn.execute(
        "SELECT COALESCE(sum(cost_usd), 0), count(*), COALESCE(sum(ok = 0), 0) FROM llm_calls "
        "WHERE study_date >= ?", (month,)).fetchone()
    model = "работает" if ok else f"недоступна ({why})"
    lines.append(f"Модель: {model}. Сегодня {usd(llm.spent(day))} из предела "
                 f"{usd(cfg.llm.daily_budget_usd)}, за месяц {usd(spent_month)}, "
                 f"вызовов {calls}, неуспешных {failed}.")
    audio = conn.execute("SELECT count(*) FROM audio_files").fetchone()[0]
    lines.append(f"Распознавание: {stt_name}{'' if stt_configured else ', не подключено'}. "
                 f"Озвучка: {cfg.tts_provider}. Аудиофайлов: {audio}.")
    lines.append(backup_line(core, cfg))
    st = core.settings
    lines.append(f"Режим: {'голос' if st.get('mode') == 'voice' else 'только текст'}. "
                 f"Психология: {'включена' if st.psy_active() else ('включена, банки не загружены' if st.get('psy_enabled') else 'выключена')}.")
    if st.is_paused(day):
        lines.append(f"Пауза до {dm(date.fromisoformat(st.get('pause_until')))} включительно.")

    week_ago = to_iso(now - timedelta(days=7))
    events = conn.execute("SELECT ts, service, level, message FROM service_events WHERE ts >= ? "
                          "ORDER BY id DESC", (week_ago,)).fetchall()
    if events:
        lines.append(f"Сбои сервисов за 7 дней: {len(events)}. Последние:")
        for ev in events[:3]:
            when = core.calendar.local(datetime.fromisoformat(ev["ts"]))
            lines.append(f"— {when:%d.%m %H:%M} {ev['service']}: {ev['message'][:160]}")
    else:
        lines.append("Сбоев сервисов за 7 дней нет.")
    return "\n".join(lines)


def backup_line(core: Core, cfg: Config) -> str:
    b = core.backups
    daily = b.list("daily")
    if daily:
        text = (f"Резервные копии: последняя за {daily[0].when}, {human_size(daily[0].size)}; "
                f"ежедневных {len(daily)} из {cfg.backup.keep}.")
    else:
        text = f"Резервных копий пока нет: первая — в {cfg.backup.time}."
    row = core.conn.execute("SELECT max(mark) FROM sent_marks WHERE kind = 'weekly_db'").fetchone()
    if row[0]:
        text += f" Копия в Telegram — неделя с {dm(date.fromisoformat(row[0]))}."
    elif cfg.backup.weekly_to_telegram:
        text += " Копия в Telegram — по воскресеньям со сводкой."
    return text


# недельная сводка

def weekly(core: Core, day: date) -> list[Reply]:
    conn = core.conn
    start = core.calendar.week_start(day)
    end = start + timedelta(days=6)
    a, b = start.isoformat(), end.isoformat()
    psy = core.settings.psy_active()
    lines = [f"Сводка за неделю {dm(start)} — {dm(end)}."]

    rows = conn.execute("SELECT * FROM days WHERE study_date BETWEEN ? AND ? ORDER BY study_date",
                        (a, b)).fetchall()
    total_en = sum(r["sec_en"] for r in rows)
    total_psy = sum(r["sec_psy"] for r in rows)
    voice = sum(r["sec_voice"] for r in rows)
    counted = sum(r["counted"] for r in rows)
    per_day = []
    for r in rows:
        if r["sec_en"] + r["sec_psy"] == 0:
            continue
        d = date.fromisoformat(r["study_date"])
        part = f"{dm(d)}: {(r['sec_en'] + r['sec_psy']) // 60}"
        if psy:
            part += f" (англ. {r['sec_en'] // 60}, псих. {r['sec_psy'] // 60})"
        per_day.append(part)
    total = f"Минут: {(total_en + total_psy) // 60}"
    if psy:
        total += f", английский {total_en // 60}, психология {total_psy // 60}"
    lines.append(f"{total}; голосом {voice // 60}. Засчитано дней: {counted} из 7.")
    if per_day:
        lines.append("По дням: " + "; ".join(per_day) + ".")

    sc = core.english.stage_counts()
    lines.append(f"Фразы: не введены {sc[0]}, ступень 2 — {sc[2]}, ступень 3 — {sc[3]}, "
                 f"ступень 4 — {sc[4]}, закрыты {sc[5]}.")
    new_week = conn.execute("SELECT COALESCE(sum(new_en), 0) FROM days WHERE study_date BETWEEN ? AND ?",
                            (a, b)).fetchone()[0]
    lines.append(f"Новых за неделю: {new_week}.")
    for area, in conn.execute("SELECT DISTINCT area FROM exit_attempts ORDER BY area").fetchall():
        reh = core.rehearsals
        closed = " Модуль закрыт." if reh.module_closed(area) else ""
        lines.append(f"Критерий выхода модуля {area[2:]}: {reh.exit_summary(area)}.{closed}")
    mono = conn.execute("SELECT code, stage FROM rehearsal_state s WHERE attempts > 0 AND EXISTS "
                        "(SELECT 1 FROM rehearsals r WHERE r.code = s.code AND r.kind = 'monologue' "
                        "AND r.archived = 0) ORDER BY code").fetchall()
    if mono:
        from ..english.rehearsals import STAGE_TITLE
        lines.append("Монолог: " + ", ".join(f"{m['code']} — {STAGE_TITLE[m['stage']]}" for m in mono) + ".")

    errs = conn.execute("SELECT tag, count(*) AS n FROM errors WHERE track = 'en' AND study_date "
                        "BETWEEN ? AND ? GROUP BY tag ORDER BY n DESC, tag LIMIT 5", (a, b)).fetchall()
    if errs:
        lines.append("Главные ошибки: " + ", ".join(
            f"{EN_ERROR_TAGS.get(r['tag'], r['tag'])} {r['n']}" for r in errs) + ".")
    else:
        lines.append("Ошибок в журнале за неделю нет.")

    lo, hi = to_iso(core.calendar.day_start(start)), to_iso(core.calendar.day_end(end))
    sounds = [r[0] for r in conn.execute(
        "SELECT DISTINCT i.sound FROM items i JOIN item_state s ON s.item_id = i.id WHERE i.track = 'en' "
        "AND i.sound IS NOT NULL AND i.sound != '' AND s.introduced_at >= ? AND s.introduced_at < ? "
        "ORDER BY i.sort_key", (lo, hi))]
    if sounds:
        lines.append("Звуки недели: " + "; ".join(sounds[:6]) + ".")

    if psy:
        states = dict(conn.execute("SELECT state, count(*) FROM topic_state GROUP BY state").fetchall())
        names = {TopicState.NOT_STARTED.value: "не начаты", TopicState.PROBE.value: "срез",
                 TopicState.STUDY.value: "изучение", TopicState.CLOSED.value: "закрыты",
                 TopicState.RARE.value: "редкое повторение"}
        lines.append("Темы психологии: " + ", ".join(f"{names[k]} {states.get(k, 0)}" for k in names) + ".")
        closed = [r[0] for r in conn.execute(
            "SELECT topic_code FROM topic_state WHERE state IN ('closed', 'rare') AND updated_at >= ? "
            "AND updated_at < ? ORDER BY topic_code", (lo, hi))]
        if closed:
            lines.append("Закрыты за неделю: " + ", ".join(closed) + ".")
        read = conn.execute("SELECT count(*) FROM digest_reads WHERE status = 'read' AND read_at >= ? "
                            "AND read_at < ?", (lo, hi)).fetchone()[0]
        if read:
            lines.append(f"Разборов прочитано за неделю: {read}.")
        gaps = conn.execute("SELECT tag, count(*) AS n FROM errors WHERE track = 'psy' AND study_date "
                            "BETWEEN ? AND ? GROUP BY tag ORDER BY n DESC LIMIT 5", (a, b)).fetchall()
        if gaps:
            lines.append("Пробелы: " + ", ".join(
                f"{PSY_GAP_TAGS.get(r['tag'], r['tag'])} {r['n']}" for r in gaps) + ".")

    cost, calls = conn.execute("SELECT COALESCE(sum(cost_usd), 0), count(*) FROM llm_calls "
                               "WHERE study_date BETWEEN ? AND ?", (a, b)).fetchone()
    lines.append(f"Модели за неделю: {usd(cost)}, вызовов {calls}.")
    replies = [Reply("\n".join(lines))]
    review = review_list(core)
    if review is not None:
        replies.append(review)
    return replies


def review_list(core: Core) -> Reply | None:
    """Оспоренные вердикты и кандидаты в варианты — на разбор, с кнопками."""
    disputes = pending(core.conn)
    variants = pending_variants(core.conn)
    if not disputes and not variants:
        return None
    lines, buttons = ["На разбор."], []
    if disputes:
        lines.append(f"Оспоренные вердикты: {len(disputes)}. Правка ключа или вариантов — в файле "
                     f"банка и новым импортом; здесь — отметка итога.")
        for k, d in enumerate(disputes[:MAX_REVIEW_ITEMS], start=1):
            v = json.loads(d["verdict_json"] or "{}")
            why = v.get("explanation") or v.get("comment") or ""
            verdict = VERDICT_RU.get(d["verdict"], d["verdict"])
            lines.append(f"{k}. {d['code']}: «{d['answer'] or ''}» — {verdict}. {why}".rstrip())
            buttons.append([Button(f"{k}: модель права", f"disp:ok:{d['id']}"),
                            Button(f"{k}: прав я", f"disp:me:{d['id']}")])
        if len(disputes) > MAX_REVIEW_ITEMS:
            lines.append(f"И ещё {len(disputes) - MAX_REVIEW_ITEMS} — после разбора этих.")
    if variants:
        lines.append(f"Кандидаты в варианты: {len(variants)}. Принятый засчитывается кодом сразу, "
                     f"в банк дописывается при следующей правке файла.")
        for k, v in enumerate(variants[:MAX_REVIEW_ITEMS], start=1):
            lines.append(f"В{k}. {v['code']} «{v['target']}» — «{v['raw_answer']}», "
                         f"встречался {v['seen_count']} раз.")
            buttons.append([Button(f"В{k}: принять", f"var:1:{v['id']}"),
                            Button(f"В{k}: отклонить", f"var:0:{v['id']}")])
        if len(variants) > MAX_REVIEW_ITEMS:
            lines.append(f"И ещё {len(variants) - MAX_REVIEW_ITEMS} — после разбора этих.")
    return Reply("\n".join(lines), buttons)


# вечернее уведомление

def evening(core: Core, day: date) -> Reply | None:
    """Одно уведомление в день. None — сегодня не беспокоить."""
    if core.settings.is_paused(day):
        return None
    st = core.days.status(day)
    if st.skipped:
        return None
    held = core.conn.execute("SELECT speech_held FROM days WHERE study_date = ?",
                             (day.isoformat(),)).fetchone()[0]
    speech = core.planner.speech_pending(day, core.clock.now()) if held else 0
    speech_line = (f"Речевые задания ждут голоса: фраз ступени 4 — {speech}. "
                   f"Если можешь говорить вслух, переключи режим.") if speech else ""
    if st.norm_done:
        text = f"Итог дня: {st.total_min} из {st.norm_min}, норма набрана."
        return Reply("\n".join(l for l in (text, speech_line) if l))
    lines = [f"Вечер. {cap(st.counter())}.", f"До нормы {st.left_min} мин."]
    if speech_line:
        lines.append(speech_line)
    return Reply("\n".join(lines), [[Button("Вечерний блок", "start:evening"),
                                     Button("Не сегодня", "skipday")]])


# настройки

# Режим и пауза меняются своими кнопками; подключение психологии — вторым релизом.
HIDDEN = {"mode", "pause_until"}
PSY_ONLY = {"new_psy_weekday", "new_psy_weekend", "review_cap_psy", "probe_confirm_days",
            "selfcheck_every", "selfcheck_every_strict", "selfcheck_mismatch_share", "psy_digest_first"}


def editable(core: Core) -> list[str]:
    psy = core.settings.psy_active()
    return [k for k in SPEC if k not in HIDDEN and (psy or k not in PSY_ONLY)]


def show_value(v: Any) -> str:
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return "нет" if v is None else str(v)


def settings_text(core: Core) -> str:
    values = core.settings.all()
    lines = ["Настройки."]
    for n, key in enumerate(editable(core), start=1):
        mark = "" if values[key] == SPEC[key].default else " (изменено)"
        lines.append(f"{n}. {cap(SPEC[key].title)}: {show_value(values[key])}{mark}")
    lines.append("Правка — одной строкой «номер значение», например «1 21:00». "
                 "«номер сброс» возвращает значение по умолчанию. «отмена» — выйти.")
    return "\n".join(lines)


def parse_value(key: str, raw: str) -> Any:
    default = SPEC[key].default
    raw = raw.strip()
    if isinstance(default, bool):
        low = raw.lower()
        if low in ("да", "вкл", "yes", "1"):
            return True
        if low in ("нет", "выкл", "no", "0"):
            return False
        raise SettingError("нужно «да» или «нет»")
    if isinstance(default, list):
        return [int(x) for x in re.findall(r"\d+", raw)]
    if isinstance(default, float):
        pct = raw.endswith("%")
        try:
            value = float(raw.rstrip("%").replace(",", "."))
        except ValueError as exc:
            raise SettingError("нужно число") from exc
        return value / 100 if pct else value
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError as exc:
            raise SettingError("нужно целое число") from exc
    return raw


def apply_setting(core: Core, line: str) -> str:
    """«номер значение» или «номер сброс». Возвращает ответ для владельца."""
    m = re.match(r"^\s*(\d+)\s+(.+?)\s*$", line)
    keys = editable(core)
    if not m:
        raise SettingError("нужно «номер значение», например «1 21:00»")
    n = int(m.group(1))
    if not 1 <= n <= len(keys):
        raise SettingError(f"номер от 1 до {len(keys)}")
    key = keys[n - 1]
    if m.group(2).lower() in ("сброс", "по умолчанию"):
        core.settings.reset(key)
        return f"{cap(SPEC[key].title)}: {show_value(core.settings.get(key))} (по умолчанию)."
    value = core.settings.set(key, parse_value(key, m.group(2)))
    return f"{cap(SPEC[key].title)}: {show_value(value)}."
