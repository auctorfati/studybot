"""Озвучка заранее: фразы английского, реплики сценариев, истории и записи слушания.

Каждая запись — два темпа, обычный и медленный. Файл и строка в базе делаются по хэшу текста:
после правки текста запись озвучивается заново, неизменённое не трогается. Фразы — голосом
фраз, реплики сценариев — тем же голосом, истории и записи слушания —
голосами по кругу (модуль 6: разные голоса). Пути — «трек / блок / код_темп».
Бот озвучивает недостающее сам: при запуске и после импорта документом (bot/controller.py).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .content.common import short_hash
from .db import transaction

SPEEDS = ("normal", "slow")


@dataclass(frozen=True)
class Job:
    kind: str          # 'item' — фраза банка (audio_files), 'line' — реплика или запись (line_audio)
    key: str           # id позиции или ключ реплики ('Д1:0', 'Р3:text', 'Л2:q1')
    text: str
    voice: str
    base: str          # путь без темпа и расширения, от папки аудио


def term_key(code: str) -> str:
    return f"term:{code}"


def _safe(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in s)


def plan(conn: sqlite3.Connection, rehearsals, dg_cfg) -> list[Job]:
    """Всё, что должно быть озвучено при текущем контенте."""
    from .english.listening import lines as record_lines
    from .english.rehearsals import scenario_lines
    jobs: list[Job] = []
    for r in conn.execute("SELECT id, code, area, unit, audio_text FROM items WHERE track = 'en' AND archived = 0 "
                          "AND COALESCE(audio_text, '') != '' ORDER BY sort_key"):
        jobs.append(Job("item", str(r["id"]), r["audio_text"], dg_cfg.voice,
                        f"en/{_safe(r['area'])}/{_safe(str(r['unit']))}/{_safe(r['code'])}"))
    for (code,) in conn.execute("SELECT code FROM rehearsals WHERE archived = 0 ORDER BY code"):
        reh = rehearsals.get(code)
        if reh is None or reh.kind != "scenario":
            continue
        for key, text in scenario_lines(reh):
            if text.strip():
                jobs.append(Job("line", key, text, dg_cfg.voice, f"lines/{_safe(code)}/{_safe(key)}"))
    for r in conn.execute("SELECT code, term FROM terms WHERE archived = 0 ORDER BY sort_key"):
        if r["term"].strip():                 # модуль 5: как звучит термин (карточка знакомства)
            jobs.append(Job("line", term_key(r["code"]), r["term"], dg_cfg.voice, f"terms/{_safe(r['code'])}"))
    voices = dg_cfg.record_voices or (dg_cfg.voice,)
    for r in conn.execute("SELECT code, fields_json FROM records WHERE archived = 0 AND kind IN ('story', 'listening') "
                          "ORDER BY sort_key"):
        voice = voices[int(short_hash(r["code"]), 16) % len(voices)]
        for key, text in record_lines(r["code"], json.loads(r["fields_json"])):
            if text.strip():
                jobs.append(Job("line", key, text, voice, f"lines/{_safe(r['code'])}/{_safe(key)}"))
    return jobs


def _row(conn: sqlite3.Connection, job: Job, speed: str) -> sqlite3.Row | None:
    if job.kind == "item":
        return conn.execute("SELECT path, text_hash FROM audio_files WHERE item_id = ? AND speed = ?",
                            (int(job.key), speed)).fetchone()
    return conn.execute("SELECT path, text_hash FROM line_audio WHERE key = ? AND speed = ?", (job.key, speed)).fetchone()


def pending(conn: sqlite3.Connection, audio_dir: Path, jobs: list[Job]) -> list[tuple[Job, str]]:
    """Нет записи, текст изменился после озвучки или файл пропал."""
    out = []
    for job in jobs:
        h = short_hash(job.text)
        for speed in SPEEDS:
            row = _row(conn, job, speed)
            if row is None or row["text_hash"] != h:
                out.append((job, speed))
                continue
            path = Path(row["path"])
            if not (path if path.is_absolute() else audio_dir / path).exists():
                out.append((job, speed))
    return out


def summary(todo: list[tuple[Job, str]]) -> tuple[int, int]:
    """Число файлов и знаков к озвучке."""
    return len(todo), sum(len(j.text) for j, _ in todo)


def _store(conn: sqlite3.Connection, job: Job, speed: str, rel: str) -> None:
    h = short_hash(job.text)
    with transaction(conn):
        if job.kind == "item":
            conn.execute("INSERT INTO audio_files (item_id, speed, path, text_hash, tg_file_id) VALUES (?, ?, ?, ?, NULL) "
                         "ON CONFLICT(item_id, speed) DO UPDATE SET path = excluded.path, text_hash = excluded.text_hash, "
                         "tg_file_id = NULL", (int(job.key), speed, rel, h))
        else:
            conn.execute("INSERT INTO line_audio (key, speed, path, text_hash, tg_file_id) VALUES (?, ?, ?, ?, NULL) "
                         "ON CONFLICT(key, speed) DO UPDATE SET path = excluded.path, text_hash = excluded.text_hash, "
                         "tg_file_id = NULL", (job.key, speed, rel, h))


@dataclass
class Stats:
    done: int = 0
    failed: int = 0
    chars: int = 0
    stopped: str = ""


async def run(conn: sqlite3.Connection, audio_dir: Path, tts, todo: list[tuple[Job, str]], slow_speed: float,
              concurrency: int = 4, progress=None, max_errors_in_row: int = 20) -> Stats:
    """Озвучить список; запись в базу — после записи файла. Подряд много сбоев — остановка (сеть, ключ)."""
    from .deepgram import DeepgramError
    st = Stats()
    sem = asyncio.Semaphore(concurrency)
    in_row = 0
    stop = asyncio.Event()

    async def one(job: Job, speed: str) -> None:
        nonlocal in_row
        if stop.is_set():
            return
        async with sem:
            if stop.is_set():
                return
            try:
                data, ext = await tts.speak(job.text, voice=job.voice, speed=slow_speed if speed == "slow" else 1.0)
            except DeepgramError as exc:
                st.failed += 1
                in_row += 1
                if in_row >= max_errors_in_row or exc.reason == "not_configured":
                    st.stopped = str(exc)
                    stop.set()
                return
        in_row = 0
        rel = f"{job.base}_{speed}.{ext}"
        path = audio_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        _store(conn, job, speed, rel)
        st.done += 1
        st.chars += len(job.text)
        if progress and st.done % 200 == 0:
            progress(st)

    await asyncio.gather(*(one(j, s) for j, s in todo))
    return st
