"""Логика бота без Telegram: события → список сообщений.

Каждое публичное действие идёт под одним замком: бот однопользовательский,
а два быстрых сообщения подряд не должны проверяться одновременно.
Состояние сессии живёт в базе (слой К6); здесь в памяти только ожидание
правки настроек и файлы импорта до кнопки «Принять».

Таймер (слой К8) делает ежедневную резервную копию, шлёт вечернее
уведомление и по воскресеньям сводку с копией базы. Отметки «отправлено»
ставятся сразу; если Telegram не принял сообщение, слой отправки снимает
отметку (unmark), и следующий тик повторяет.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from ..app import Core
from ..backup import TELEGRAM_MAX, BackupError, human_size
from ..checking.disputes import DisputeError, decide_variant, dispute, resolve
from ..clock import to_iso
from ..config import Config
from ..content.common import short_hash
from ..content.importer import render_report
from ..db import transaction
from ..english.track import INTRO
from ..sessions.engine import SessionError
from ..settings import SettingError
from ..speech import STT, STTUnavailable
from ..enums import Track
from ..english.explain import reading_cap
from ..english.rehearsals import Rehearsal
from ..english.track import Task
from ..sessions.planner import Step
from . import academic_render, psy_render, render, reports, rules
from .render import CB_DISPUTE, CB_DONE, CB_END, CB_IDK, CB_SKIP, CB_SLOW, CB_SLOW_LINE, CB_WHY, why_button
from .ui import (BTN_MODE, BTN_MORE, BTN_TODAY, MENU_KINDS, AudioRef, Button, DocumentRef, Reply,
                 Result, merge)

log = logging.getLogger(__name__)

Fetch = Callable[[], Awaitable[bytes]]

KIND_TITLE = {"5": "Сессия 5 минут", "15": "Сессия 15 минут", "30": "Сессия 30 минут",
              "evening": "Вечерний блок, {min} мин", "long": "Длинная сессия, {min} мин"}
AWAIT_TTL = timedelta(minutes=10)      # ожидание правки настроек
NOTIFY_WINDOW = timedelta(hours=3)     # опоздавшее уведомление ещё уместно столько
MAX_IMPORT_BYTES = 5 * 1024 * 1024     # банки — десятки килобайт; корпуса идут на сервер, не в бота
PENDING_IMPORTS = 5
PAUSE_DAYS = (1, 2, 3, 5, 7, 14)
BACKUP_RETRY = timedelta(hours=1)      # неудачная ежедневная копия — повтор не чаще раза в час


class Controller:
    def __init__(self, core: Core, cfg: Config, stt: STT, tts=None) -> None:
        self.core = core
        self.cfg = cfg
        self.stt = stt
        self.tts = tts                                          # Deepgram или None (озвучка во время работы)
        self.voice_wanted = tts is not None                     # озвучить недостающее: при запуске и после импорта
        self.voicing: asyncio.Task | None = None
        self.voice_retry: datetime | None = None                # после сбоев — повтор через полчаса
        self.lock = asyncio.Lock()
        self.awaiting: datetime | None = None                  # ждём строку правки настроек
        self.imports: dict[str, tuple[str, bytes]] = {}         # токен → имя и содержимое файла
        self.backup_retry: datetime | None = None               # после сбоя копии — не раньше

    # запуск

    def prepare(self) -> list[str]:
        """При старте: без распознавания голосовой режим не включается."""
        notes = []
        if not self.stt.configured and self.core.settings.get("mode") == "voice":
            self.core.settings.set("mode", "text")
            notes.append("распознавание не подключено: режим переключён на «только текст»")
        return notes

    # события

    async def on_start(self) -> Result:
        async with self.lock:
            return Result([self._greeting()])

    async def on_command(self, name: str) -> Result:
        async with self.lock:
            self.awaiting = None
            if name == "start":
                return Result([self._greeting()])
            if name == "today":
                return Result([self._today_reply()])
            if name == "week":
                return Result(reports.weekly(self.core, self.core.engine.today()))
            if name == "mode":
                return Result([self._mode_reply()])
            if name == "pause":
                return Result([self._pause_reply()])
            if name == "settings":
                return Result([self._settings_open()])
            if name == "state":
                return Result([self._state_reply()])
            if name == "import":
                return Result([self._import_hint()])
            if name == "stop":
                return Result(self._end())
            return Result([self._greeting()])

    async def on_text(self, text: str) -> Result:
        async with self.lock:
            t = text.strip()
            if t in MENU_KINDS:
                self.awaiting = None
                return Result(await self._start_or_ask(MENU_KINDS[t]))
            if t == BTN_TODAY:
                self.awaiting = None
                return Result([self._today_reply()])
            if t.lower().startswith("разбор:"):
                return Result([await self._live_review(t.split(":", 1)[1].strip())])
            if t == BTN_MODE:
                self.awaiting = None
                return Result([self._mode_reply()])
            if t == BTN_MORE:
                self.awaiting = None
                return Result([self._more_reply()])
            if self.awaiting is not None and self.core.clock.now() - self.awaiting < AWAIT_TTL:
                return Result([self._settings_edit(t)])
            self.awaiting = None
            return Result(await self._answer(t, None, None))

    async def on_voice(self, duration: int, fetch: Fetch) -> Result:
        async with self.lock:
            self.awaiting = None
            e = self.core.engine
            step = e.current()
            if step is None:
                return Result(await self._no_step())
            sec = max(1, int(duration))
            if step.kind == "task" and step.task().format == INTRO:
                # повтор за аудио при знакомстве: расшифровка не нужна, считается время
                return Result(await self._answer(None, sec, None))
            if not self.stt.configured:
                return Result([Reply("Распознавание пока не подключено. Ответь на это задание текстом.")])
            data = await fetch()
            try:
                text = (await self.stt.transcribe(data, "ru" if step.track == "psy" else "en")).strip()
            except STTUnavailable as exc:
                self._service_event("stt", "error", str(exc))
                e.defer(step.id)
                return Result(merge(Reply("Распознавание недоступно, задание вернётся в следующей сессии."),
                                    await self._advance()))
            if not text:
                return Result([Reply("Не расслышал. Повтори, пожалуйста, или ответь текстом.")])
            return Result(await self._answer(text, sec, text))

    async def on_document(self, file_name: str, size: int | None, fetch: Fetch) -> Result:
        async with self.lock:
            self.awaiting = None
            name = Path(file_name or "").name
            if not name.lower().endswith(".md"):
                return Result([Reply("Импорт принимает md-файлы: банк английского, справочник, "
                                     "банк вопросов или карту тем.")])
            if size and size > MAX_IMPORT_BYTES:
                return Result([Reply("Файл больше 5 МБ, это не банк. Корпуса источников кладутся "
                                     "на сервер в папку sources, не через бота.")])
            data = await fetch()
            plan = self.core.importer.analyze(name, data)
            report = render_report(plan)
            if not plan.ok:
                self._service_event("import", "warning", f"{name}: ошибки формата")
                return Result([Reply(report)])
            token = plan.sha256[:16]
            self.imports[token] = (name, data)
            while len(self.imports) > PENDING_IMPORTS:
                self.imports.pop(next(iter(self.imports)))
            return Result([Reply(report, [[Button("Принять", f"imp:ok:{token}"),
                                           Button("Отмена", f"imp:no:{token}")]])])

    async def on_callback(self, data: str) -> Result:
        async with self.lock:
            self.awaiting = None
            try:
                return await self._callback(data)
            except (SessionError, DisputeError, SettingError) as exc:
                return Result(toast=str(exc))

    async def tick(self) -> Result:
        """Раз в полминуты: пауза сессии без ответа, резервная копия, вечернее уведомление,
        по воскресеньям — сводка и копия базы."""
        async with self.lock:
            e = self.core.engine
            e.idle_check()
            now = self.core.clock.now()
            self._backup_if_due(now)
            self._voice_if_wanted()
            day = e.today()
            target = self.core.calendar.at_local(day, self.core.settings.get("notify_time"))
            if not target <= now < target + NOTIFY_WINDOW:
                return Result()
            s = e.active()
            if s is not None and s["status"] == "active":
                return Result()                   # идёт сессия: напомним, когда она закончится
            replies = []
            if not self._sent("evening", day.isoformat()):
                self._mark("evening", day.isoformat())
                note = reports.evening(self.core, day)
                if note is not None:
                    note.mark = ("evening", day.isoformat())
                    replies.append(note)
            if self.core.calendar.is_sunday(day):
                replies += self._sunday(day)
            return Result(replies)

    def unmark(self, marks: list[tuple[str, str]]) -> None:
        """Отправка сорвалась: снять отметки, чтобы таймер повторил."""
        for kind, mark in marks:
            self.core.conn.execute("DELETE FROM sent_marks WHERE kind = ? AND mark = ?", (kind, mark))
        if marks:
            log.warning("отправка не прошла, повтор: %s", ", ".join(f"{k} {m}" for k, m in marks))

    def _backup_if_due(self, now: datetime) -> None:
        """Ежедневная копия в 03:30. Бот был выключен — копия при первом тике после."""
        if self.backup_retry is not None and now < self.backup_retry:
            return
        b = self.core.backups
        if not b.due(now):
            return
        try:
            made = b.make("daily")
        except (BackupError, OSError, sqlite3.Error) as exc:
            self.backup_retry = now + BACKUP_RETRY
            self._service_event("backup", "error", f"ежедневная копия: {exc}")
            log.error("ежедневная копия не сделана: %s", exc)
            return
        self.backup_retry = None
        log.info("резервная копия %s, %s", made.path.name, human_size(made.size))

    def service_event(self, service: str, level: str, message: str) -> None:
        self._service_event(service, level, message)

    # озвучка во время работы: недостающее — в фоне, живые реплики — при отправке

    def _voice_if_wanted(self) -> None:
        if self.voice_retry is not None and self.core.clock.now() >= self.voice_retry:
            self.voice_retry, self.voice_wanted = None, True
        if self.tts is None or not self.voice_wanted or (self.voicing is not None and not self.voicing.done()):
            return
        self.voice_wanted = False
        self.voicing = asyncio.get_running_loop().create_task(self._autovoice())

    async def _autovoice(self) -> None:
        from .. import voicing
        try:
            jobs = voicing.plan(self.core.conn, self.core.rehearsals, self.cfg.deepgram)
            todo = voicing.pending(self.core.conn, self.cfg.paths.audio_dir, jobs)
            if not todo:
                return
            st = await voicing.run(self.core.conn, self.cfg.paths.audio_dir, self.tts, todo, self.cfg.deepgram.slow_speed)
            if st.failed:
                self.voice_retry = self.core.clock.now() + timedelta(minutes=30)
                self._service_event("tts", "warning", f"озвучка: готово {st.done}, сбоев {st.failed}"
                                    + (f"; остановлена: {st.stopped}" if st.stopped else "") + "; повтор через 30 минут")
        except Exception as exc:                                # фоновая задача не роняет бота
            self._service_event("tts", "error", f"озвучка: {exc.__class__.__name__}: {exc}")

    def _live_on(self, step) -> bool:
        """Реплики собеседника в частях критерия выхода «только звуком» — озвучкой во время сессии."""
        from ..english.rehearsals import DYNAMIC
        return (self.tts is not None and step is not None and step.kind == "exit_speech"
                and step.payload.get("mode") == "talk" and step.payload.get("part") in DYNAMIC
                and bool(self.core.settings.get("exit_live_tts")))

    async def voice_live(self, r: Reply) -> None:
        """Перед отправкой: живую реплику — в голосовое; без озвучки — текстом, чтобы разговор не встал."""
        if not r.live_text:
            return
        text, r.live_text = r.live_text, None
        try:
            if self.tts is None:
                raise RuntimeError("озвучка не подключена")
            data, ext = await self.tts.speak(text, speed=1.0)
            folder = self.cfg.paths.audio_dir / "live"
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.glob("*"):                        # живые реплики нужны один раз
                if old.stat().st_mtime < self.core.clock.now().timestamp() - 2 * 86400:
                    old.unlink(missing_ok=True)
            path = folder / f"{short_hash(text, str(self.core.clock.now()))}.{ext}"
            path.write_bytes(data)
            r.audio = AudioRef(None, "normal", str(path), None)
        except Exception as exc:
            self._service_event("tts", "warning", f"живая реплика: {exc}")
            r.text = (r.text + "\n\n" if r.text else "") + f"(озвучка недоступна, реплика текстом) {text}"

    def remember_file_id(self, item_id: int, speed: str, file_id: str) -> None:
        """После первой отправки аудио Telegram возвращает file_id — дальше шлём по нему."""
        self.core.conn.execute("UPDATE audio_files SET tg_file_id = ? WHERE item_id = ? AND speed = ?",
                               (file_id, item_id, speed))

    def remember_audio(self, audio: AudioRef, file_id: str) -> None:
        if audio.line_key:
            self.core.conn.execute("UPDATE line_audio SET tg_file_id = ? WHERE key = ? AND speed = ?",
                                   (file_id, audio.line_key, audio.speed))
        elif audio.item_id is not None:
            self.remember_file_id(audio.item_id, audio.speed, file_id)

    # сессия

    async def _start_or_ask(self, kind: str) -> list[Reply]:
        """Перед каждой сессией — «голосом или текстом?»: режим зависит от того,
        где он сейчас, а бот этого знать не может. Без распознавания — только текст, без вопроса."""
        if not self.stt.configured or kind not in KIND_TITLE:
            return await self._start(kind)
        return [self._mode_question(kind)]

    def _mode_question(self, kind: str) -> Reply:
        lines = ["Голосом или текстом?",
                 "Голосом — если можно говорить вслух: фразы на слух, ответы голосом, диалоги, речь, "
                 "контрольные точки и критерии выхода.",
                 "Текстом — если рядом люди: письменные задания и психология; речевые задания бот отложит "
                 "на вечер и напомнит о них."]
        waiting = self._voice_waiting()
        lines.append(f"Ждут голоса: {waiting}. Лучше голосом, если есть возможность." if waiting else
                     "Голосом английский идёт быстрее: устные ступени закрываются только голосом.")
        return Reply("\n".join(lines), [[Button("Голосом", f"smode:{kind}:voice"),
                                         Button("Текстом", f"smode:{kind}:text")]])

    def _voice_waiting(self) -> str:
        """Что из сегодняшнего английского идёт только голосом."""
        e = self.core.engine
        day, now = e.today(), self.core.clock.now()
        parts = []
        try:
            cp = e.blocks.due_for_control(day)
            if cp is not None:
                parts.append(f"контрольная точка блока {cp[1]} модуля {cp[0][2:]}")
            n = e.planner.speech_pending(day, now)
            if n:
                parts.append(f"{n} {_plural(n, 'фраза', 'фразы', 'фраз')} на устной ступени")
        except Exception:                                       # подсказка не должна мешать начать сессию
            return ""
        return ", ".join(parts)

    async def _start(self, kind: str) -> list[Reply]:
        e = self.core.engine
        had = e.active() is not None
        info = e.start(kind)
        title = KIND_TITLE[kind].format(min=info.ordered_sec // 60)
        head = [f"{title}. {reports.cap(info.status.counter())}."]
        if had:
            head.append("Предыдущая сессия закрыта, начатое засчитано.")
        if self.core.settings.is_paused(info.status.day):
            head.append("Идёт пауза: новое не вводится.")
        return merge(Reply("\n".join(head)), await self._advance())

    async def _advance(self) -> list[Reply]:
        e = self.core.engine
        s = e.active()
        step = await e.next()
        if step is None:
            return self._finished(s)
        return [self._step_reply(step)]

    def _step_reply(self, step) -> Reply:
        if step.kind in ("psy", "digest"):
            return psy_render.step_reply(self.core.conn, step)
        if step.kind in ("term", "text"):
            return academic_render.step_reply(self.core.conn, step)
        if step.kind == "exam":
            return academic_render.exam_reply(self.core.conn, step)
        if step.kind == "speech" and step.payload["format"] in ("story", "listening"):
            return academic_render.listen_reply(step, self._record_audio(step))
        if step.kind == "speech":
            return academic_render.speech_reply(step)
        if step.kind == "exit_speech":
            return academic_render.exit_speech_reply(step, live=self._live_on(step))
        return render.step_reply(step, self._audio(step))

    def _finished(self, s) -> list[Reply]:
        if s is None:
            return [Reply("Сейчас нет сессии. Выбери длину в меню.", menu=True)]
        conn = self.core.conn
        day = date.fromisoformat(s["study_date"])
        done = conn.execute("SELECT count(*) FROM session_steps WHERE session_id = ? AND status = 'done'",
                            (s["id"],)).fetchone()[0]
        st = self.core.days.status(day)
        if done:
            head = "Сессия окончена."
        elif conn.execute("SELECT 1 FROM items WHERE track = 'en' AND archived = 0 LIMIT 1").fetchone() is None:
            head = "Контента нет: пришли банк английского документом."
        else:
            head = "Заданий по плану сейчас нет: новое на сегодня введено, повторения сделаны."
        tail = "Норма набрана." if st.norm_done else f"До нормы {st.left_min} мин."
        replies = [Reply(f"{head} {reports.cap(st.counter())}. {tail}", menu=True)]
        if s["kind"] == "long" and self.core.calendar.is_sunday(day):
            replies += self._weekly_once(day)
        return replies

    def _end(self) -> list[Reply]:
        e = self.core.engine
        s = e.active()
        if s is None:
            return [Reply("Сессии нет.", menu=True)]
        e.end()
        return self._finished(s)

    async def _no_step(self) -> list[Reply]:
        if self.core.engine.active() is None:
            return [Reply("Сейчас нет сессии. Выбери длину в меню.", menu=True)]
        return await self._advance()

    async def _answer(self, text: str | None, voice_sec: int | None, transcript: str | None,
                      step_id: int | None = None, idk: bool = False) -> list[Reply]:
        e = self.core.engine
        step = e.current()
        if step is None:
            return await self._no_step()
        if step_id is not None and step.id != step_id:
            raise SessionError("это задание уже закрыто")
        if step.kind == "exit_speech":
            xf = await e.exit_speech_answer(step.id, "" if idk else (text or ""), voice_sec)
            if xf is None:
                raise SessionError("это задание уже закрыто")
            if xf.done:
                return merge(Reply("\n".join(xf.lines)), await self._advance())
            if xf.reply and self._live_on(step):
                return [Reply("Собеседник отвечает голосом.", [[Button("Закончить", "end")]], live_text=xf.reply)]
            return [Reply(xf.reply or "…", [[Button("Закончить", "end")]])]
        if step.kind == "speech":
            sf = await e.speech_answer(step.id, "" if idk else (text or ""), voice_sec)
            if sf is None and step.payload.get("phase") == "listen":
                return [Reply("Сначала дослушай запись и нажми «Прослушал».")]
            if sf is None:
                raise SessionError("это задание уже закрыто")
            if step.payload["format"] in ("story", "listening") and not sf.done:
                return [self._step_reply(e.current())]
            if sf.done:
                return merge(Reply("\n".join(([sf.reply] if sf.reply else []) + sf.lines)), await self._advance())
            return [Reply(sf.reply or "…", [[Button("Закончить", "end")]])]
        if step.kind == "exam":
            ef = await e.exam_answer(step.id, "" if idk else (text or ""))
            if ef is None:
                return [Reply("Сначала прочитай текст и нажми «Прочитал».",
                              academic_render.exam_reply(self.core.conn, step).buttons)]
            if ef.done:
                return merge(Reply("\n".join(ef.lines) or "Записано."), await self._advance())
            nxt = e.current()
            if step.payload["part"] == "3":
                return [Reply("\n".join(ef.lines), [[Button("Закончить", "end")]])]
            return [academic_render.exam_reply(self.core.conn, nxt)]
        if step.kind == "text":
            tf = await e.text_answer(step.id, "" if idk else (text or ""))
            if tf is None:
                return [Reply("Сначала прочитай текст и нажми «Прочитал».",
                              academic_render.step_reply(self.core.conn, step).buttons)]
            out = Reply("\n".join(tf.lines) or "Записано.")
            if tf.done:
                return merge(out, await self._advance())
            return [out, academic_render.step_reply(self.core.conn, e.current())]
        if step.kind == "term":
            if step.payload["kind"] == "intro":
                tf = e.term_intro_done(step.id)
            else:
                tf = await e.term_answer(step.id, "" if idk else (text or ""))
            if tf is None:
                raise SessionError("это задание уже закрыто")
            out = academic_render.feedback_reply(tf)
            if step.payload["kind"] == "intro":
                return await self._advance()
            return merge(out, await self._advance())
        if step.kind == "digest":
            return [Reply("Это разбор темы: «Дальше» — следующая часть, «Отложить» — вернусь к нему позже.",
                          psy_render.step_reply(self.core.conn, step).buttons)]
        if step.kind == "psy":
            if idk and step.payload["kind"] == "card" and step.payload["answer_mode"] == "self":
                pf = await e.psy_self(step.id, "0")
            else:
                pf = await e.answer_psy(step.id, text or "", voice_sec)
            if pf is None:
                raise SessionError("это задание уже закрыто")
            out = psy_render.feedback_reply(pf)
            if transcript:
                out.text = f"Распознано: {transcript}\n" + out.text
            if not pf.finished_step:
                return [out]
            return merge(out, await self._advance())
        fb = await e.answer(step.id, text, voice_sec)
        if idk:
            fb.lines = [l for l in fb.lines if l != "Пустой ответ."]
        out = render.feedback_reply(fb, transcript, step.kind)
        if fb.audio_line:
            audio = self._line_audio(step, fb.audio_line)
            step.payload["audio_line"] = fb.audio_line
            if audio is not None:
                ask = Reply("Вопрос собеседника — звуком.", render.step_buttons(step), audio=audio)
                return [out, ask] if out.text else [ask]
            # записи нет (файл пропал после проверки): вопрос текстом, чтобы диалог не встал
            code = fb.audio_line.partition(":")[0]
            r = self.core.rehearsals.get(code)
            question = dict(_scenario_lines(r)).get(fb.audio_line, "") if r else ""
            out.text = "\n".join(x for x in (out.text, question) if x)
        if not fb.finished_step:
            out.buttons = out.buttons or render.step_buttons(step)
            return [out]
        return merge(out, await self._advance())

    def _record_audio(self, step) -> AudioRef | None:
        from ..english.listening import question_key, text_key
        p = step.payload
        key = text_key(p["record"]) if p.get("phase", "listen") == "listen" else (
            question_key(p["record"], p["qi"]) if p.get("phase") == "q" else None)
        return self._line_audio(step, key) if key else None

    def _line_audio(self, step: Step | None, key: str, speed: str = "normal") -> AudioRef | None:
        """Запись реплики сценария или записи банка (история, слушание), если текст не менялся после озвучки."""
        code, _, idx = key.partition(":")
        r = self.core.rehearsals.get(code)
        if r is None:
            from ..english.listening import lines as record_lines
            rec = self.core.conn.execute("SELECT fields_json FROM records WHERE code = ?", (code,)).fetchone()
            if rec is None:
                return None
            lines = dict(record_lines(code, json.loads(rec[0])))
        else:
            lines = dict(_scenario_lines(r))
        row = self.core.rehearsals.line_audio(key, lines.get(key, ""), speed)
        if row is None:
            return None
        path = Path(row["path"])
        if not path.is_absolute():
            path = self.cfg.paths.audio_dir / path
        return AudioRef(None, speed, str(path), row["tg_file_id"], line_key=key)

    def _audio(self, step, speed: str = "normal") -> AudioRef | None:
        if step.kind == "exit_dialog" and step.payload.get("audio_line"):
            return self._line_audio(step, step.payload["audio_line"], speed)
        if step.kind != "task":
            return None
        task = step.task()
        return self._audio_for(task.audio_item, speed) if task.audio_item else None

    def _audio_for(self, item_id: int, speed: str) -> AudioRef | None:
        row = self.core.conn.execute("SELECT path, tg_file_id FROM audio_files WHERE item_id = ? AND speed = ?",
                                     (item_id, speed)).fetchone()
        if row is None:
            return None
        path = Path(row["path"])
        if not path.is_absolute():
            path = self.cfg.paths.audio_dir / path
        return AudioRef(item_id, speed, str(path), row["tg_file_id"])

    # кнопки

    async def _callback(self, data: str) -> Result:
        e = self.core.engine
        head, _, arg = data.partition(":")
        if head == CB_DONE:
            res = Result(await self._answer(None, None, None, step_id=int(arg)))
            res.keep = [[why_button(int(arg))]]
            return res
        if head == CB_WHY:
            return await self._why(int(arg))
        if head in ("pshow", "pself", "dgn", "dgl", "pwhy", "src", "dg", "dm"):
            return await self._psy_callback(head, arg)
        if head in ("tdone", "tctx", "tnote"):
            return await self._term_callback(head, int(arg))
        if head == "lsn":
            p = e.listen_heard(int(arg))
            if p is None:
                return Result(toast="Кнопка устарела")
            return Result([self._step_reply(e.current())])
        if head == "exr":
            p = e.exam_read_done(int(arg))
            if p is None:
                return Result(toast="Кнопка устарела")
            return Result([academic_render.exam_reply(self.core.conn, e.current())])
        if head == "txr":
            tf = e.text_read_done(int(arg))
            if tf is None:
                return Result(toast="Кнопка устарела")
            out = [Reply("\n".join(tf.lines))] if tf.lines else []
            return Result(out + [academic_render.step_reply(self.core.conn, e.current())])
        if head == "rl":
            return Result([rules.route(self.core.conn, arg)])
        if head == CB_SLOW_LINE:
            audio = self._line_audio(None, arg, "slow")
            if audio is None:
                return Result(toast="Медленной записи нет")
            return Result([Reply(audio=audio)])
        if head == CB_IDK:
            return Result(await self._answer("", None, None, step_id=int(arg), idk=True))
        if head == CB_SKIP:
            cur = e.current()
            if cur is None or cur.id != int(arg):
                raise SessionError("это задание уже закрыто")
            e.defer(cur.id)
            return Result(merge(Reply("Отложено до следующей сессии."), await self._advance()))
        if head == CB_SLOW:
            audio = self._audio_for(int(arg), "slow")
            if audio is None:
                return Result(toast="Медленной записи нет")
            return Result([Reply(audio=audio)])
        if head == CB_END:
            return Result(self._end())
        if head == "show":
            step = e.current()
            if step is None:
                return Result(await self._no_step())
            return Result([self._step_reply(step)])
        if head == CB_DISPUTE:
            with transaction(self.core.conn):
                rolled = dispute(self.core.conn, int(arg), self.core.clock.now())
            text = "Вердикт оспорен: до разбора срывом не считается. Разбор — в недельной сводке."
            if rolled:
                text += " Прогресс фразы возвращён к состоянию до ответа."
            res = Result([Reply(text)], toast="Оспорено")
            sid = self.core.conn.execute(
                "SELECT id FROM session_steps WHERE json_extract(payload_json, '$.review_id') = ?",
                (int(arg),)).fetchone()
            if sid is not None:
                res.keep = [[why_button(sid[0])]]
            return res
        if head == "start":
            return Result(await self._start_or_ask(arg))
        if head == "smode":
            kind, _, mode = arg.partition(":")
            if kind not in KIND_TITLE or mode not in ("voice", "text"):
                return Result([Reply("Кнопка устарела. Выбери сессию в меню.", menu=True)])
            self.core.settings.set("mode", mode)
            return Result(await self._start(kind))
        if head == "skipday":
            self.core.days.skip(e.today())
            return Result([Reply("День закрыт. До завтра.", menu=True)])
        if head == "mode":
            return Result([self._set_mode(arg)])
        if head == "pause":
            return Result([self._set_pause(int(arg))])
        if head == "unpause":
            self.core.settings.reset("pause_until")
            return Result([Reply("Пауза снята. Повторения разойдутся по дням в пределах потолка.", menu=True)])
        if head == "more":
            return self._more(arg)
        if head == "imp":
            action, _, token = arg.partition(":")
            return Result([self._import_decide(action == "ok", token)])
        if head == "var":
            accept, _, vid = arg.partition(":")
            decide_variant(self.core.conn, int(vid), accept == "1")
            return Result(toast="Вариант принят" if accept == "1" else "Вариант отклонён")
        if head == "disp":
            verdict, _, did = arg.partition(":")
            resolve(self.core.conn, int(did), "upheld" if verdict == "ok" else "verdict_changed",
                    self.core.clock.now())
            return Result(toast="Отмечено")
        return Result(toast="Кнопка устарела")

    def _more(self, name: str) -> Result:
        """Пункты меню «Ещё» — те же команды, вызов уже под замком."""
        if name == "week":
            return Result(reports.weekly(self.core, self.core.engine.today()))
        if name == "state":
            return Result([self._state_reply()])
        if name == "pause":
            return Result([self._pause_reply()])
        if name == "settings":
            return Result([self._settings_open()])
        if name == "import":
            return Result([self._import_hint()])
        if name == "copy":
            return Result(self._db_copy(self.core.engine.today()))
        if name == "rules":
            return Result([rules.root(self.core.conn)])
        if name == "digests":
            return Result([psy_render.menu(self.core.conn, "")])
        if name == "live":
            return Result([Reply("Разбор живого разговора: напиши одним сообщением, начиная со слова «Разбор:», "
                                 "о чём говорили и что не получилось сказать — по-русски, можно с тем, как сказал "
                                 "сам. Бот покажет, как сказать это в пределах пройденного. В повторение это не идёт.")])
        return Result(toast="Кнопка устарела")

    async def _live_review(self, text: str) -> Reply:
        """Разбор живого разговора (карта пути, модуль 3): как сказать это в пределах пройденного."""
        from ..english.speech import CORE
        from ..llm.client import CHEAP, LLMUnavailable
        if not text:
            return Reply("Напиши после «Разбор:», о чём говорили и что не получилось сказать.")
        mod = (self.core.engine.speech.modules.current() or "EN1")[2:]
        try:
            res = await self.core.runner.text("live_review", CHEAP, {
                "module_core": CORE.get(mod, "to be, Present Simple, вопросы с do и does, can"),
                "allowed_phrases": self.core.english.dialog_vocabulary()[:80], "learner_text": text}, max_chars=2500)
        except LLMUnavailable as exc:
            return Reply(f"Модель недоступна ({exc}): разбор сделаем позже.")
        reply = Reply(res.data["text"])
        self.core.engine.start_reading(min(240, max(20, round(len(reply.text) * 0.06))))
        return reply

    async def _term_callback(self, head: str, sid: int) -> Result:
        e, conn = self.core.engine, self.core.conn
        if head == "tdone":
            cur = e.current()
            if cur is None or cur.id != sid:
                return Result(toast="Кнопка устарела")
            return Result(await self._answer(None, None, None, step_id=sid))
        row = conn.execute("SELECT payload_json FROM session_steps WHERE id = ?", (sid,)).fetchone()
        if row is None:
            return Result(toast="Кнопка устарела")
        term_id = json.loads(row[0])["term_id"]
        reply = (academic_render.context_reply(conn, term_id) if head == "tctx"
                 else academic_render.note_reply(conn, term_id))
        st = self.core.settings
        e.start_reading(min(120, max(15, round(len(reply.text) * st.get("read_sec_per_1000") / 1000))))
        return Result([reply])

    async def _psy_callback(self, head: str, arg: str) -> Result:
        e, conn = self.core.engine, self.core.conn
        st = self.core.settings
        if head == "dm":
            return Result([psy_render.menu(conn, arg)])
        if head == "dg":
            topic, _, num = arg.rpartition(":")
            reply = psy_render.digest_part_reply(conn, topic, int(num))
            parts = conn.execute("SELECT parts FROM digests WHERE topic_code = ?", (topic,)).fetchone()
            if parts:
                e.mark_read(topic, int(num), parts[0])
                e.start_reading(min(240, max(20, round(len(reply.text) * st.get("read_sec_per_1000") / 1000))),
                                Track.PSY.value)
            return Result([reply])
        if head == "pshow":
            sid = int(arg)
            ans = e.psy_show(sid)
            if ans is None:
                return Result(toast="Кнопка устарела")
            return Result([psy_render.shown_reply(ans, sid)], keep=[])
        if head == "pself":
            sid, _, key = arg.partition(":")
            pf = await e.psy_self(int(sid), key)
            if pf is None:
                return Result(toast="Кнопка устарела")
            return Result(merge(psy_render.feedback_reply(pf), await self._advance()))
        if head in ("dgn", "dgl"):
            sid = int(arg)
            if head == "dgl":
                e.digest_time(sid)
                p = e.digest_defer(sid)
                if p is None:
                    return Result(toast="Кнопка устарела")
                return Result(merge(Reply(f"Разбор {p['topic']} отложен: предложу его в начале следующего "
                                          f"блока психологии. Он же есть в «Ещё» → «Разборы»."), await self._advance()))
            e.digest_time(sid)
            event, p = e.digest_advance(sid)
            if event is None:
                return Result(toast="Кнопка устарела")
            if event == "part":
                return Result([psy_render.digest_part_reply(conn, p["topic"], p["part"], step_id=sid)])
            return Result(merge(Reply(f"Разбор {p['topic']} прочитан."), await self._advance()))
        row = conn.execute("SELECT payload_json FROM session_steps WHERE id = ?", (int(arg),)).fetchone()
        if row is None:
            return Result(toast="Кнопка устарела")
        p = json.loads(row[0])
        if head == "pwhy":
            num = psy_render.part_for_item(conn, p["topic"], p["code"])
            return await self._psy_callback("dg", f"{p['topic']}:{num}")
        rt = await self.core.retell.retell(p["item_id"], self.core.clock.now())
        if rt is None:
            return Result(toast="Фрагмента источника нет")
        text = rt.message()
        e.start_reading(min(240, max(20, round(len(text) * st.get("read_sec_per_1000") / 1000))), Track.PSY.value)
        return Result([Reply(text)])

    async def _why(self, step_id: int) -> Result:
        """«Почему так?» по шагу: после вердикта или на карточке знакомства."""
        row = self.core.conn.execute("SELECT kind, payload_json FROM session_steps WHERE id = ?",
                                     (step_id,)).fetchone()
        if row is None or row["kind"] != "task":
            return Result(toast="Кнопка устарела")
        payload = json.loads(row["payload_json"])
        task = Task(**{k: v for k, v in payload.items() if k in Task.__dataclass_fields__})
        if task.format != INTRO and "answered" not in payload:
            return Result(toast="Объяснение — после ответа")
        exp = await self.core.explainer.explain(task, payload.get("answered"), self.core.clock.now())
        if exp.call_ids:
            self._link_calls(exp.call_ids, payload.get("review_id"))
        text = exp.message()
        st = self.core.settings
        self.core.engine.start_reading(reading_cap(len(text), st.get("read_sec_per_1000"), st.get("why_cap_sec")))
        return Result([Reply(text)])

    def _link_calls(self, call_ids: list, review_id: int | None) -> None:
        if review_id:
            self.core.llm.link_review(call_ids, review_id)

    # экраны

    def _greeting(self) -> Reply:
        mode = self.core.settings.get("mode")
        has = self.core.conn.execute("SELECT 1 FROM items WHERE track = 'en' LIMIT 1").fetchone()
        lines = ["Учебный бот. Сессия запускается кнопкой внизу: 5, 15 или 30 минут; вечерний блок "
                 "добирает остаток нормы дня. Ответ на задание — сообщением"
                 + (" или голосовым." if mode == "voice" else "."),
                 f"Уведомление — в {self.core.settings.get('notify_time')} по Москве. "
                 f"Режим: {'голос' if mode == 'voice' else 'только текст'}."]
        if not has:
            lines.append("Контент ещё не загружен: пришли банк английского md-файлом.")
        return Reply("\n".join(lines), menu=True)

    def _more_reply(self) -> Reply:
        return Reply("Ещё:", [[Button("Сводка за неделю", "more:week"), Button("Состояние", "more:state")],
                              [Button("Правила", "more:rules"), Button("Разборы", "more:digests")],
                              [Button("Разобрать разговор", "more:live")],
                              [Button("Пауза", "more:pause"), Button("Настройки", "more:settings")],
                              [Button("Импорт банка", "more:import"), Button("Копия базы", "more:copy")]])

    def _today_reply(self) -> Reply:
        """Идёт сессия — кнопка «Показать задание»: сообщение могло потеряться или уйти вверх."""
        text = reports.today_text(self.core)
        if self.core.engine.current() is not None:
            return Reply(text, [[Button("Показать задание", "show")]])
        return Reply(text, menu=True)

    def _mode_reply(self) -> Reply:
        voice = self.core.settings.get("mode") == "voice"
        text = (f"Режим: {'голос' if voice else 'только текст'}. Перед каждой сессией бот спрашивает, голосом или "
                f"текстом; здесь — режим по умолчанию до следующего ответа.")
        if not self.stt.configured:
            text += "\nГолосовой режим включится, когда будет подключено распознавание."
        return Reply(text, [[Button("Голос", "mode:voice"), Button("Только текст", "mode:text")]])

    def _set_mode(self, mode: str) -> Reply:
        if mode == "voice" and not self.stt.configured:
            return Reply("Распознавание пока не подключено, остаётся «только текст».")
        self.core.settings.set("mode", mode)
        return Reply(f"Режим: {'голос' if mode == 'voice' else 'только текст'}, со следующей сессии.",
                     menu=True)

    def _pause_reply(self) -> Reply:
        st = self.core.settings
        day = self.core.engine.today()
        if st.is_paused(day):
            until = date.fromisoformat(st.get("pause_until"))
            return Reply(f"Пауза до {reports.dm(until)} включительно.", [[Button("Снять паузу", "unpause")]])
        return Reply("Пауза: уведомлений нет, новое не вводится, повторять можно. На сколько дней, "
                     "считая сегодня?",
                     [[Button(str(n), f"pause:{n}") for n in PAUSE_DAYS]])

    def _set_pause(self, days: int) -> Reply:
        day = self.core.engine.today()
        until = day + timedelta(days=days - 1)
        self.core.settings.set("pause_until", until)
        back = until + timedelta(days=1)
        return Reply(f"Пауза до {reports.dm(until)} включительно. С {reports.dm(back)} бот снова "
                     f"напоминает, повторения разойдутся по дням в пределах потолка.", menu=True)

    def _settings_open(self) -> Reply:
        self.awaiting = self.core.clock.now()
        return Reply(reports.settings_text(self.core))

    def _settings_edit(self, line: str) -> Reply:
        if line.lower() in ("отмена", "выход", "нет"):
            self.awaiting = None
            return Reply("Настройки закрыты.", menu=True)
        try:
            text = reports.apply_setting(self.core, line)
        except SettingError as exc:
            self.awaiting = self.core.clock.now()
            return Reply(f"Не принято: {exc}. Ещё раз или «отмена».")
        self.awaiting = None
        return Reply(text + " Ещё правка — снова «Настройки».", menu=True)

    def _state_reply(self) -> Reply:
        return Reply(reports.state_text(self.core, self.cfg, self.stt.name, self.stt.configured))

    def _import_hint(self) -> Reply:
        return Reply("Пришли md-файл документом: банк английского, справочник, банк вопросов или карту тем "
                     "(карта области — раньше её банков, банки английского — по порядку блоков, справочник — "
                     "после банков английского). Бот проверит файл и покажет отчёт; применится по кнопке "
                     "«Принять».")

    def _import_decide(self, accept: bool, token: str) -> Reply:
        entry = self.imports.pop(token, None)
        if not accept:
            return Reply("Импорт отменён.")
        if entry is None:
            return Reply("Файл устарел (бот перезапускался или пришёл другой). Пришли его ещё раз.")
        name, data = entry
        plan = self.core.importer.analyze(name, data)          # заново: база могла измениться
        if not plan.ok:
            return Reply(render_report(plan))
        version = self.core.importer.apply(plan)
        self.core.english.reload()
        self.voice_wanted = self.tts is not None                 # новое и изменённое озвучится в фоне
        saved = ""
        try:
            self.cfg.paths.content_dir.mkdir(parents=True, exist_ok=True)
            (self.cfg.paths.content_dir / name).write_bytes(data)
        except OSError as exc:
            self._service_event("import", "warning", f"{name}: не сохранён в content ({exc})")
            saved = " Копию файла сохранить не удалось, см. «Состояние»."
        return Reply(f"Принято, версия контента {version}.{saved}")

    # сводка и отметки

    def _weekly_once(self, day: date) -> list[Reply]:
        """Сводка недели один раз и с ней копия базы."""
        week = self.core.calendar.week_start(day).isoformat()
        out: list[Reply] = []
        if not self._sent("weekly", week):
            self._mark("weekly", week)
            replies = reports.weekly(self.core, day)
            replies[-1].mark = ("weekly", week)
            out += replies
        out += self._weekly_copy(day, silent=False)
        return out

    def _sunday(self, day: date) -> list[Reply]:
        """Воскресенье в окне уведомления. Пауза или «не сегодня» — сводки нет,
        но копия базы уходит без звука: копия вне сервера — каждую неделю."""
        if self.core.settings.is_paused(day) or self.core.days.status(day).skipped:
            return self._weekly_copy(day, silent=True)
        return self._weekly_once(day)

    def _weekly_copy(self, day: date, silent: bool) -> list[Reply]:
        week = self.core.calendar.week_start(day).isoformat()
        if not self.cfg.backup.weekly_to_telegram or self._sent("weekly_db", week):
            return []
        self._mark("weekly_db", week)
        replies = self._db_copy(day, silent=silent)
        replies[-1].mark = ("weekly_db", week)
        return replies

    def _db_copy(self, day: date, silent: bool = False) -> list[Reply]:
        """Сжатая копия базы документом."""
        try:
            name, data = self.core.backups.snapshot_gz()
        except (BackupError, OSError, sqlite3.Error) as exc:
            self._service_event("backup", "error", f"копия для Telegram: {exc}")
            return [Reply("Копию базы сделать не удалось, подробности в «Состоянии». "
                          "Ежедневные копии лежат на сервере.", silent=silent)]
        if len(data) > TELEGRAM_MAX:
            self._service_event("backup", "warning", f"копия {human_size(len(data))} больше предела Telegram")
            return [Reply(f"Копия базы весит {human_size(len(data))} — больше предела Telegram. "
                          f"Ежедневные копии лежат на сервере в data/backups.", silent=silent)]
        caption = (f"Копия базы на {reports.dm(day)}, {human_size(len(data))}. Сохрани у себя: "
                   f"если сервер пропадёт, прогресс восстанавливается из этого файла.")
        return [Reply(document=DocumentRef(name, data, caption), silent=silent)]

    def _sent(self, kind: str, mark: str) -> bool:
        return self.core.conn.execute("SELECT 1 FROM sent_marks WHERE kind = ? AND mark = ?",
                                      (kind, mark)).fetchone() is not None

    def _mark(self, kind: str, mark: str) -> None:
        self.core.conn.execute("INSERT OR IGNORE INTO sent_marks (kind, mark, sent_at) VALUES (?, ?, ?)",
                               (kind, mark, to_iso(self.core.clock.now())))

    def _service_event(self, service: str, level: str, message: str) -> None:
        self.core.conn.execute("INSERT INTO service_events (ts, service, level, message) VALUES (?, ?, ?, ?)",
                               (to_iso(self.core.clock.now()), service, level, message[:500]))


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def _scenario_lines(r: Rehearsal) -> list[tuple[str, str]]:
    from ..english.rehearsals import scenario_lines
    return scenario_lines(r)
