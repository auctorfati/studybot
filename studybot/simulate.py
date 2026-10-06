"""Прогон условных дней на ускоренных часах.

Бот целиком — Controller со всем ядром, база в файле во временной папке,
часы подставные. Ученик живёт по расписанию: будни кусками и вечерним
блоком по уведомлению, суббота двумя сессиями, воскресенье длинной сессией.
В сценарий вшиты неудобные случаи: ошибки, «не знаю», иные формулировки,
оспаривания и разбор в сводке, пропущенный день, «не сегодня», пауза на три
дня с тихим воскресеньем, вечерний блок после полуночи, два перезапуска
посреди сессии, дни без модели (сеть недоступна; модель не настроена),
сорванная отправка копии базы.

Модель подставная: валидный JSON по виду задания, без сети и расходов.
Распознавание и озвучка — заглушки, то есть проверяется текстовый режим
целиком; с voice=True — голосовой режим с подставным распознаванием.

После каждого учебного дня — проверки потолка, лимита нового, учёта времени
и зачёта дня, уведомлений; в конце — целостность базы, ежедневные копии,
сводки и копии в Telegram. Настоящие база, Telegram и модель не затрагиваются.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import random
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .app import build_core
from .bot.controller import Controller
from .bot.ui import BTN_5, BTN_15, BTN_30, BTN_EVENING, BTN_LONG, Reply, Result
from .clock import FakeClock
from .config import Config, Paths
from .content.importer import detect_kind, render_report
from .db import connect, latest_version, migrate
from .english.track import INTRO
from .enums import EN_ERROR_TAGS, Track
from .llm.client import LLMReply, LLMUnavailable
from .llm.prompts import NAMES, Prompts
from .speech import FakeSTT, StubSTT

ACADEMIC_ANSWER = "В статье описано исследование: авторы сравнили две группы и нашли различия в показателях."
SPACED_OK = "Ответ по ключу темы, все элементы на месте."
SPACED_PART = "Ответ по ключу темы, одного элемента не хватает."
SPACED_FAIL = "Не помню толком, путаю с соседней темой."
IDLE_STEP = timedelta(minutes=15)
MAX_STEPS = 400
OVERSHOOT_SEC = 600          # сессия может перебрать заказанное на последнее задание или диалог
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
KIND_BUTTON = {"5": BTN_5, "15": BTN_15, "30": BTN_30, "evening": BTN_EVENING, "long": BTN_LONG}
DIALOG_LINES = ("I'm a teacher.", "I live in Moscow.", "I like books.", "Sorry, I don't understand.",
                "Yes, I do.", "No, I don't.")
QUESTIONS = "What do you do? Where do you live? Do you like books? Do you have a dog? What is your name?"
MONOLOGUE = "Hi, I'm Sam. I'm a teacher. I live in Moscow. I like books. That's all."


@dataclass
class Scenario:
    """Сценарий прогона. Дни считаются от нуля; первый — понедельник."""
    days: int = 30
    start: date = date(2026, 9, 28)
    seed: int = 7
    error_rate: float = 0.15
    idk_rate: float = 0.03
    alt_rate: float = 0.04           # своя формулировка: модель признаёт допустимой
    dispute_rate: float = 0.15       # доля неверных вердиктов модели, которые ученик оспаривает
    missed: tuple[int, ...] = (4, 19)        # ни одной сессии, уведомление остаётся без ответа
    skip: tuple[int, ...] = (11,)            # по уведомлению — «Не сегодня»
    pause_start: int | None = 25             # пауза на pause_days, считая этот день
    pause_days: int = 3
    late: tuple[int, ...] = (2, 16)          # вечерний блок в 01:30 — засчитывается в этот день
    restart: tuple[int, ...] = (7, 15)       # перезапуск бота посреди дневной сессии
    model_down: tuple[int, ...] = (9, 10)    # API не отвечает весь день
    no_model: tuple[int, ...] = (17,)        # модель не настроена весь день
    send_fail: tuple[int, ...] = (13,)       # Telegram не принял копию базы с первого раза
    why_rate: float = 0.08           # доля вердиктов, под которыми нажато «Почему так?»
    # ученик: flat — доля ошибок постоянна; spaced — вспоминает тем лучше, чем дальше позиция по сетке
    # (первое повторение ~80 %, второе ~85 %, дальше ~90 %, как у интервального повторения с целевым 90 %)
    learner: str = "flat"
    voice: bool = False

    def paused(self, n: int) -> bool:
        return self.pause_start is not None and self.pause_start <= n < self.pause_start + self.pause_days


class SimModel:
    """Подставная модель: валидный JSON по виду задания. Токены оцениваются по длине текста."""

    def __init__(self, prompts: Prompts, rnd: random.Random) -> None:
        self.task_of = {prompts[n]: n for n in NAMES}
        self.rnd = rnd
        self.down = False
        self.calls = 0

    async def chat(self, model, messages, json_mode, temperature, max_tokens, timeout,
                   extra=None) -> LLMReply:
        if self.down:
            raise LLMUnavailable("network", "сеть: ConnectError (прогон)")
        self.calls += 1
        task = self.task_of.get(messages[0]["content"], "?")
        user = messages[-1]["content"]
        if task == "check_en":
            if "zzz" in user:
                tag = self.rnd.choice(sorted(EN_ERROR_TAGS))
                data = {"verdict": "error", "tag": tag, "explanation": "Не та конструкция.",
                        "correction": ""}
            else:
                data = {"verdict": "acceptable", "tag": None, "explanation": "", "correction": ""}
        elif task == "dialog_en":
            data = {"reply": "Nice. And you?", "end": False}
        elif task == "dialog_corrections":
            data = {"corrections": []}
        elif task == "monologue_en":
            data = {"verdict": "passed", "covered": [], "missing": [], "errors": [], "comment": ""}
        elif task == "questions_en":
            data = {"items": [{"question": f"Q{k}?", "ok": True, "fix": ""} for k in range(5)]}
        elif task == "explain_en":
            text = ("Здесь работает порядок слов: кто — что делает — остальное. "
                    "Так говорят, потому что смысл в английском держит порядок, а не окончания.")
            self.why_calls = getattr(self, "why_calls", 0) + 1
            return LLMReply(text, len(user) // 3, len(text) // 3, model)
        elif task == "scenario_en":
            asked = json.loads(user).get("asked", []) if user.startswith("{") else []
            q = next((k for k in range(1, 9) if k not in asked), None)
            data = {"reply": "Nice.", "question": q, "end": False}
        elif task == "exit_dialog_en":
            data = {"verdict": "passed", "answered_all": True, "unanswered": [], "own_questions_ok": 12,
                    "russian_used": False, "errors": [], "comment": ""}
        elif task == "check_text":
            r = self.rnd.random()
            data = {"verdict": "passed" if r < 0.65 else ("partial" if r < 0.85 else "failed"), "comment": "",
                    "errors": [], "calques": [], "theses_ok": self.rnd.choice([3, 4, 4, 5]) if '"retell"' in user else None}
        elif task == "exam_check":
            data = {"verdict": "passed" if self.rnd.random() < 0.7 else "failed", "comment": "", "errors": [],
                    "inaccuracies": 0, "theses_ok": self.rnd.choice([3, 4, 5, 5]), "answers_ok": self.rnd.choice([3, 4, 5, 5])}
        elif task == "exam_talk":
            if '"grade"' in user:
                data = {"verdict": "passed" if self.rnd.random() < 0.7 else "failed", "answered_all": True,
                        "errors": [], "comment": ""}
            else:
                data = {"question": "Could you say more about the method?", "end": False}
        elif task == "speech_partner":
            data = {"reply": "I see. Why do you think so?", "end": False}
        elif task == "listen_review":
            data = {"answers_ok": self.rnd.choice([3, 4, 5]), "points_ok": self.rnd.choice([3, 4, 5]), "wrong": [],
                    "corrections": [], "comment": ""}
        elif task == "retell_psy":
            text = "Во фрагменте сказано главное: признак описан на примере больного (с. 230)."
            return LLMReply(text, len(user) // 3, len(text) // 3, model)
        else:
            r = self.rnd.random()
            verdict = "passed" if r < 0.6 else ("partial" if r < 0.75 else "failed")
            if SPACED_OK in user or SPACED_PART in user or SPACED_FAIL in user:
                verdict = "passed" if SPACED_OK in user else ("partial" if SPACED_PART in user else "failed")
            data = {"verdict": verdict, "present": [], "missing": [] if verdict == "passed" else ["элемент ключа"],
                    "outside_source": [], "comment": "", "tag": None if verdict == "passed" else "mechanism",
                    "confusion": None, "mentor": {"matches": True, "divergence": ""}}
        text = json.dumps(data, ensure_ascii=False)
        tokens_in = sum(len(str(m.get("content", ""))) for m in messages) // 3
        return LLMReply(text, tokens_in, max(20, len(text) // 3), model)


@dataclass
class DayRow:
    day: date
    minutes: int = 0
    en_min: int = 0
    counted: bool = False
    new: int = 0
    reviews: int = 0
    speech_waiting: int = 0
    events: list[str] = field(default_factory=list)


@dataclass
class SimReport:
    scenario: Scenario
    rows: list[DayRow] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    evening: Counter = field(default_factory=Counter)
    weekly: Counter = field(default_factory=Counter)
    copies: Counter = field(default_factory=Counter)
    silent_copies: Counter = field(default_factory=Counter)
    send_failures: int = 0
    disputes: int = 0
    whys: int = 0
    psy_steps: int = 0
    digest_parts: int = 0
    term_steps: int = 0
    reviewed: int = 0
    restarts_ok: int = 0
    stages: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    model_calls: int = 0
    backups: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations

    def text(self) -> str:
        sc = self.scenario
        mode = "голосовой режим, подставное распознавание" if sc.voice else \
            "текстовый режим, без распознавания и озвучки"
        out = [f"Прогон {sc.days} условных дней с {_dm(sc.start)}: английский, {mode}.",
               "", "день        мин  зачтён  новых  повт  ступень 4 ждёт  события"]
        for r in self.rows:
            out.append(f"{_dm(r.day):<10} {r.minutes:>4}  {'да' if r.counted else 'нет':<6} {r.new:>5} "
                       f"{r.reviews:>5}  {r.speech_waiting:>14}  {', '.join(r.events)}".rstrip())
        active = [r for r in self.rows if r.minutes]
        avg = sum(r.minutes for r in active) // len(active) if active else 0
        st = self.stages
        out += ["", f"Засчитано дней: {sum(r.counted for r in self.rows)} из {len(self.rows)}; "
                    f"в среднем {avg} мин в день с занятиями.",
                f"Фразы: не введены {st.get(0, 0)}, ступень 2 — {st.get(2, 0)}, ступень 3 — {st.get(3, 0)}, "
                f"ступень 4 — {st.get(4, 0)}, закрыты {st.get(5, 0)}.",
                f"Уведомлений: {sum(self.evening.values())}; сводок: {sum(self.weekly.values())}; копий базы "
                f"в Telegram: {sum(self.copies.values())}, из них без звука {sum(self.silent_copies.values())}; "
                f"сорванных отправок: {self.send_failures}.",
                f"Резервных копий на диске: {self.backups}. Перезапусков с продолжением сессии: "
                f"{self.restarts_ok}. Оспорено вердиктов: {self.disputes}, разобрано в сводке: {self.reviewed}. "
                f"«Почему так?» и «Источник» нажато: {self.whys}. Заданий психологии: {self.psy_steps}, "
                f"частей разборов: {self.digest_parts}, шагов модуля 5: {self.term_steps}.",
                f"Вызовов модели: {self.model_calls}; оценка расхода по ценам конфига: "
                + (f"${self.cost_usd:.2f}" if self.cost_usd else "цены не заданы") + "."]
        out += self.notes
        if self.violations:
            out += ["", f"Нарушения: {len(self.violations)}."] + [f"— {v}" for v in self.violations]
        else:
            out += ["", "Нарушений нет: потолки, лимиты, учёт дня, уведомления и копии в норме."]
        return "\n".join(out)


def _dm(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day}.{d.month:02d}"


def _run(coro):
    return asyncio.run(coro)


def _buttons(r: Reply) -> list[str]:
    return [b.data for row in r.buttons for b in row]


BLOCK_RE = re.compile(r"^## .*?Блок (\d+)\.", re.MULTILINE)
MODULE_RE = re.compile(r"^# Английский\. Модуль (\d+)", re.MULTILINE)


def order_banks(paths: list[Path]) -> tuple[list[Path], list[str]]:
    """Порядок импорта: карты тем, банки психологии (PATO раньше PSY — перекрёстные ссылки),
    разборы тем (после банков: строки «Позиции»), банки английского по модулю и номеру первого блока (блок 2 ссылается на заметки и фразы
    блоков 0–1; по имени файла «блок_2» встал бы раньше «блоки_0-1»), справочник последним —
    его «Где» ссылается на единицы банков. Неузнанные файлы — в пропущенные."""
    rank = {"topics": 0, "psy": 1, "digest": 2, "en": 3, "ref": 4}
    found, skipped = [], []
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            text = ""
        kind = detect_kind(text) if text else None
        if kind is None:
            skipped.append(p.name)
            continue
        blocks = [int(b) for b in BLOCK_RE.findall(text)] if kind == "en" else []
        if kind == "en" and re.search(r"^## \d+\. Раздел \d+\.", text, re.M):
            blocks.append(4)                     # модуль 5, блок 4 — разделы словаря
        mod = MODULE_RE.search(text) if kind == "en" else None
        found.append((rank[kind], int(mod.group(1)) if mod else 0, min(blocks, default=0), p.name, p))
    return [p for *_, p in sorted(found)], skipped


class Simulation:
    def __init__(self, cfg: Config, banks: list[Path], workdir: Path, scenario: Scenario | None = None) -> None:
        self.sc = scenario or Scenario()
        self.rnd = random.Random(self.sc.seed)
        work = Path(workdir)
        self.cfg = replace(cfg, token="0:sim", owner_id=1,
                           paths=Paths(work, work / "data", work / "content", cfg.paths.sources_dir,
                                       cfg.paths.prompts_dir))
        self.cfg.paths.ensure()
        self.cal = self.cfg.calendar()
        self.clock = FakeClock(datetime.combine(self.sc.start, time(3, 0), self.cal.tz))
        self.model = SimModel(Prompts(self.cfg.paths.prompts_dir), self.rnd)
        self.stt = FakeSTT() if self.sc.voice else StubSTT()
        self.report = SimReport(self.sc)
        self.conn = connect(self.cfg.paths.db_file)
        migrate(self.conn)
        self._boot()
        self._import(banks)
        self._failed_today = False

    # запуск

    def _boot(self) -> None:
        self.core = build_core(self.cfg, self.conn, self.clock, self.model)
        self.ctl = Controller(self.core, self.cfg, self.stt)
        self.ctl.prepare()
        if self.sc.voice:
            self.core.settings.set("mode", "voice")

    def _import(self, banks: list[Path]) -> None:
        ordered, _ = order_banks(banks)
        en = 0
        for p in ordered:
            plan = self.core.importer.analyze(p.name, p.read_bytes())
            if not plan.ok:
                self.report.violations.append(f"импорт {p.name}: {render_report(plan, 3)}")
                continue
            self.core.importer.apply(plan)
            en += plan.kind == "en"
        self.core.english.reload()
        if not en:
            raise ValueError("для прогона нужен банк английского")

    def _restart(self, n: int) -> None:
        self.conn.close()
        self.conn = connect(self.cfg.paths.db_file)
        migrate(self.conn)
        self._boot()
        self._model_state(n)

    def _model_state(self, n: int) -> None:
        self.model.down = n in self.sc.model_down
        self.core.llm.transport = None if n in self.sc.no_model else self.model

    # прогон

    def run(self) -> SimReport:
        quiet = logging.getLogger("studybot")          # сорванная отправка в сценарии — не шум в выводе
        level = quiet.level
        quiet.setLevel(logging.ERROR)
        try:
            for n in range(self.sc.days):
                self._day(n)
            self._final()
        finally:
            quiet.setLevel(level)
            self.conn.close()
        return self.report

    def _goto(self, moment: datetime) -> None:
        if moment > self.clock.now():
            self.clock.set(moment)

    def _plan(self, n: int, day: date) -> list[tuple[datetime, str]]:
        sc = self.sc
        at = lambda t, d=day: datetime.combine(d, t, self.cal.tz)  # noqa: E731
        if n in sc.missed:
            return []
        if sc.paused(n):
            acts = []
            if n == sc.pause_start:
                acts.append((at(time(9, 0)), "pause"))
            if n == sc.pause_start + 1:
                acts.append((at(time(13, 10)), "15"))          # повторять в паузу можно
            return acts
        wd = day.weekday()
        if wd < 5:
            acts = [(at(time(13, 10)), "15")]
            if n % 2 == 0:
                acts.append((at(time(7, 40)), "5"))
        elif wd == 5:
            acts = [(at(time(11, 0)), "30"), (at(time(16, 30)), "15")]
        else:
            acts = [(at(time(12, 0)), "long")]
        if n in sc.late:
            acts.append((at(time(1, 30), day + timedelta(days=1)), "late"))
        return sorted(acts)

    def _day(self, n: int) -> None:
        day = self.sc.start + timedelta(days=n)
        self._failed_today = False
        self._model_state(n)
        # вызовы модели этого дня — после переключения модели: поздний блок прошлого дня мог перейти за 03:00
        self._calls_before = self.conn.execute("SELECT COALESCE(max(id), 0) FROM llm_calls").fetchone()[0]
        acts = self._plan(n, day)
        end = self.cal.day_end(day)
        self._goto(self.cal.day_start(day))
        while self.clock.now() < end:
            while acts and acts[0][0] <= self.clock.now():
                self._act(n, acts.pop(0)[1])
            self._tick(n)
            nxt = self.clock.now() + IDLE_STEP
            if acts and acts[0][0] < nxt:
                nxt = acts[0][0]
            self._goto(min(nxt, end))
        self._check_day(n, day)

    def _act(self, n: int, act: str) -> None:
        if act == "pause":
            res = _run(self.ctl.on_command("pause"))
            self._deliver(n, res)
            self._deliver(n, _run(self.ctl.on_callback(f"pause:{self.sc.pause_days}")))
            return
        kind = "evening" if act == "late" else act
        restart = n in self.sc.restart and act in ("15", "30")
        self._session(n, self.ctl.on_text(KIND_BUTTON[kind]), restart)

    def _tick(self, n: int) -> None:
        self._react(n, self._deliver(n, _run(self.ctl.tick())))

    # сессия и ответы

    def _session(self, n: int, starter, restart: bool = False) -> None:
        self._n = n
        res = _run(starter)
        ask = next((b.data for r in res.replies for row in r.buttons for b in row if b.data.startswith("smode:")), None)
        if ask is not None:                       # «голосом или текстом?» — по режиму сценария
            self._deliver(n, res)
            res = _run(self.ctl.on_callback(f"smode:{ask.split(':')[1]}:{'voice' if self.sc.voice else 'text'}"))
        self._react(n, self._deliver(n, res))
        steps = 0
        while True:
            step = self.core.engine.current()
            if step is None:
                return
            steps += 1
            if steps > MAX_STEPS:
                self.report.violations.append(f"{_dm(self._day_of(n))}: сессия не закончилась за {MAX_STEPS} шагов")
                self.core.engine.end()
                return
            if restart and steps == 5:
                restart = False
                self._restart(n)
                after = self.core.engine.current()
                if after is None or after.id != step.id:
                    self.report.violations.append(f"{_dm(self._day_of(n))}: после перезапуска сессия "
                                                  f"не продолжилась с того же задания")
                    return
                self.report.restarts_ok += 1
            sec = self._think(step)
            self.clock.advance(seconds=sec)
            self._react(n, self._deliver(n, self._answer(step, sec)))
            if steps % 5 == 0:
                self._tick(n)

    def _fail_p(self, p: dict) -> float:
        """Ученик «spaced»: вероятность срыва по шагу сетки позиции (новое — как первый показ)."""
        row = self.conn.execute("SELECT step FROM item_state WHERE item_id = ?", (p.get("item_id"),)).fetchone()
        step = row[0] if row and row[0] is not None else None
        if p.get("purpose") == "new" or step is None:
            return 0.25
        return {0: 0.20, 1: 0.15}.get(step, 0.10)

    def _think(self, step) -> int:
        """Сколько ученик тратит на шаг: оценка формата с разбросом; диалог — по репликам."""
        base = step.est_sec
        if step.kind in ("dialog", "exit_dialog"):
            base = step.est_sec / max(1, int(step.payload.get("turns", 4)))
        if step.kind in ("speech", "exit_speech") and step.payload.get("format") in ("story", "listening"):
            base = {"listen": 180, "q": 40, "retell": 120}.get(step.payload.get("phase"), 60)
            return max(8, int(base * self.rnd.uniform(0.6, 1.2)))
        if step.kind in ("text", "exam"):
            # модуль 5: шаг из нескольких фаз — время по фазе, а не оценка всего шага на каждый ответ
            p = step.payload
            if p.get("phase") == "read":
                base = 300 if p.get("part") == "4" else 240
            elif step.kind == "exam" and p.get("part") == "1":
                base = 3000
            elif step.kind == "exam" and p.get("part") == "3":
                base = 60
            else:
                base = {"q": 90, "sentence": 150, "paragraph": 600, "retell": 180}.get(p.get("phase"), 120)
            return max(8, int(base * self.rnd.uniform(0.6, 1.2)))
        return max(8, min(240, int(base * self.rnd.uniform(0.6, 1.2))))

    def _answer(self, step, sec: int) -> Result:
        sc, ctl = self.sc, self.ctl
        voice = False
        if step.kind == "term":
            self.report.term_steps += 1
            if step.payload["kind"] == "intro":
                return _run(ctl.on_callback(f"tdone:{step.id}"))
            if self.rnd.random() < 0.08:
                self._deliver(self._n, _run(ctl.on_callback(f"tctx:{step.id}")))
            answer = step.payload["meaning"] if self.rnd.random() < 0.75 else "что-то другое"
            return _run(ctl.on_text(answer))
        if step.kind in ("speech", "exit_speech") and step.payload.get("phase") == "listen":
            return _run(ctl.on_callback(f"lsn:{step.id}"))      # история и слушание: сначала «Прослушал»
        if step.kind in ("text", "exam"):
            # модуль 5: текст Ч и прогон экзамена — «Прочитал» кнопкой, дальше ответы сообщениями
            p = step.payload
            if p.get("phase") == "read":
                return _run(ctl.on_callback(f"{'txr' if step.kind == 'text' else 'exr'}:{step.id}"))
            if step.kind == "exam" and p.get("part") == "3":
                return _run(ctl.on_text(self.rnd.choice(DIALOG_LINES)))
            return _run(ctl.on_text(ACADEMIC_ANSWER if self.rnd.random() > sc.idk_rate else ""))
        if step.kind == "digest":
            self.report.digest_parts += 1
            if self.rnd.random() < 0.05:
                return _run(ctl.on_callback(f"dgl:{step.id}"))
            return _run(ctl.on_callback(f"dgn:{step.id}"))
        if step.kind == "psy":
            p = step.payload
            self.report.psy_steps += 1
            if p["kind"] == "card" and (p["answer_mode"] == "self" or "answer" in p):
                if "answer" not in p:
                    self._deliver(self._n, _run(ctl.on_callback(f"pshow:{step.id}")))
                    self.clock.advance(seconds=self.rnd.randint(3, 10))
                if sc.learner == "spaced" and p.get("purpose") != "probe":
                    r, fail = self.rnd.random(), self._fail_p(p)
                    key = "0" if r < fail else ("1" if r < fail + 0.08 else "2")
                else:
                    key = self.rnd.choice("0122222")
                res = _run(ctl.on_callback(f"pself:{step.id}:{key}"))
            else:
                if self.rnd.random() < sc.idk_rate:
                    return _run(ctl.on_callback(f"idk:{step.id}"))
                text = "Ответ своими словами по ключу темы."
                if sc.learner == "spaced" and p.get("purpose") != "probe":
                    r, fail = self.rnd.random(), self._fail_p(p)
                    text = SPACED_FAIL if r < fail else (SPACED_PART if r < fail + 0.08 else SPACED_OK)
                res = _run(ctl.on_text(text))
            for r in res.replies:
                for data in _buttons(r):
                    if data.startswith(("pwhy:", "src:")) and self.rnd.random() < sc.why_rate:
                        self.clock.advance(seconds=self.rnd.randint(20, 200))
                        _run(ctl.on_callback(data))
                        self.report.whys += 1
            return res
        if step.kind == "task":
            t = step.task()
            if t.format == INTRO:
                return _run(ctl.on_callback(f"done:{step.id}"))
            r = self.rnd.random()
            if r < sc.idk_rate:
                return _run(ctl.on_callback(f"idk:{step.id}"))
            right = t.expected[0] if t.expected else MONOLOGUE
            if r < sc.idk_rate + sc.error_rate:
                text = f"zzz {self.rnd.randint(1, 4)}"
            elif r < sc.idk_rate + sc.error_rate + sc.alt_rate:
                text = right.rstrip(".!?") + " indeed."
            else:
                text = right
            voice = sc.voice and t.voice
        elif step.kind in ("dialog", "exit_dialog"):
            text, voice = self.rnd.choice(DIALOG_LINES), sc.voice
        elif step.kind == "cp_questions":
            text = QUESTIONS
        else:
            text, voice = MONOLOGUE, sc.voice
        if voice:
            self.stt.texts.append(text)

            async def fetch() -> bytes:
                return b"ogg"

            res = _run(ctl.on_voice(sec, fetch))
        else:
            res = _run(ctl.on_text(text))
        for r in res.replies:
            for data in _buttons(r):
                if data.startswith("dispute:") and self.rnd.random() < sc.dispute_rate:
                    _run(ctl.on_callback(data))
                    self.report.disputes += 1
                elif data.startswith("why:") and self.rnd.random() < sc.why_rate:
                    self.clock.advance(seconds=self.rnd.randint(15, 200))
                    _run(ctl.on_callback(data))
                    self.report.whys += 1
        return res

    # доставка и реакция на сообщения

    def _deliver(self, n: int, res: Result) -> list[Reply]:
        replies = res.replies
        fail_at = None
        if n in self.sc.send_fail and not self._failed_today:
            fail_at = next((k for k, r in enumerate(replies) if r.document is not None), None)
        for k, r in enumerate(replies):
            if k == fail_at:
                self._failed_today = True
                self.ctl.unmark([x.mark for x in replies[k:] if x.mark is not None])
                self.report.send_failures += 1
                return replies[:k]
            self._record(n, r)
        return replies

    def _record(self, n: int, r: Reply) -> None:
        if r.mark is not None and r.mark[0] == "evening":
            self.report.evening[n] += 1
        if r.text.startswith("Сводка за неделю"):
            self.report.weekly[n] += 1
        if r.document is not None:
            self.report.copies[n] += 1
            self.report.silent_copies[n] += r.silent
            if sum(self.report.copies.values()) == 1:
                self._verify_copy(r.document.data)

    def _react(self, n: int, replies: list[Reply]) -> None:
        for r in replies:
            data = _buttons(r)
            if "start:evening" in data:
                if n in self.sc.skip:
                    self._deliver(n, _run(self.ctl.on_callback("skipday")))
                elif n not in self.sc.late and n not in self.sc.missed:
                    self._session(n, self.ctl.on_callback("start:evening"))
            elif r.text.startswith("На разбор."):
                pairs = [d for d in data if d.startswith(("disp:ok:", "var:1:"))]
                for k, d in enumerate(pairs):
                    if k % 2:
                        d = d.replace("disp:ok:", "disp:me:").replace("var:1:", "var:0:")
                    _run(self.ctl.on_callback(d))
                    self.report.reviewed += 1

    def _verify_copy(self, data: bytes) -> None:
        path = self.cfg.paths.data_dir / "copy-check.sqlite3"
        path.write_bytes(gzip.decompress(data))
        conn = sqlite3.connect(str(path))
        try:
            ok = conn.execute("PRAGMA quick_check").fetchone()[0]
            schema = conn.execute("PRAGMA user_version").fetchone()[0]
            items = conn.execute("SELECT count(*) FROM items").fetchone()[0]
        finally:
            conn.close()
            path.unlink()
        if ok != "ok" or schema != latest_version() or not items:
            self.report.violations.append(f"копия базы из Telegram не открывается как база бота ({ok}, "
                                          f"схема {schema}, позиций {items})")

    # проверки

    def _day_of(self, n: int) -> date:
        return self.sc.start + timedelta(days=n)

    def _check_day(self, n: int, day: date) -> None:
        core, sc = self.core, self.sc
        q = lambda sql, *a: self.conn.execute(sql, a).fetchone()[0]  # noqa: E731
        v = self.report.violations
        label = _dm(day)
        d = day.isoformat()
        st = core.days.status(day)
        cap = core.queue.cap(Track.EN)
        reviews = core.days.reviews_done(day, Track.EN)
        if reviews > cap:
            v.append(f"{label}: повторений {reviews} при потолке {cap}")
        limit = 0 if core.settings.is_paused(day) else core.settings.new_limit_en(day)
        new = core.queue.new_used(Track.EN, day)
        if new > limit:
            v.append(f"{label}: новых {new} при лимите {limit}")
        lo, hi = self.cal.day_start(day).isoformat(timespec="seconds"), \
            self.cal.day_end(day).isoformat(timespec="seconds")
        introduced = q("SELECT count(*) FROM item_state s JOIN items i ON i.id = s.item_id WHERE i.track = 'en' "
                       "AND i.area != 'EN5' AND s.introduced_at >= ? AND s.introduced_at < ?", lo, hi)
        if introduced != new:
            v.append(f"{label}: введено фраз {introduced}, а в учёте дня {new}")
        psy_limit = 0 if core.settings.is_paused(day) else core.settings.new_limit_psy(day)
        psy_new = core.queue.new_used(Track.PSY, day)
        if psy_new > psy_limit + 5:      # последняя позиция блока может перебрать лимит на одну виньетку
            v.append(f"{label}: нового по психологии {psy_new} единиц при лимите {psy_limit}")
        s = core.settings
        expect = (st.en_sec + st.psy_sec >= s.get("day_min_total") * 60 and st.en_sec >= s.get("day_min_en") * 60)
        if st.counted != expect:
            v.append(f"{label}: зачёт дня {st.counted} не совпадает с правилом 10/5 минут")
        in_sessions = q("SELECT COALESCE(sum(active_sec_en), 0) FROM sessions WHERE study_date = ?", d)
        if in_sessions != st.en_sec:
            v.append(f"{label}: время дня {st.en_sec} с, по сессиям {in_sessions} с")
        over = q("SELECT count(*) FROM sessions WHERE study_date = ? AND "
                 "active_sec_en + active_sec_psy > ordered_sec + ?", d, OVERSHOOT_SEC)
        if over:
            v.append(f"{label}: сессий с перебором больше 10 минут: {over}")
        if self.report.evening[n] > 1:
            v.append(f"{label}: вечерних уведомлений {self.report.evening[n]}")
        if sc.paused(n) and self.report.evening[n]:
            v.append(f"{label}: уведомление в паузу")
        bad = q("SELECT count(*) FROM item_state WHERE (step IS NOT NULL AND due_date IS NULL) "
                "OR stage NOT IN (0, 2, 3, 4) OR (closed = 1 AND stage != 4)")
        if bad:
            v.append(f"{label}: позиций с несогласованным состоянием: {bad}")
        if q("SELECT count(*) FROM sessions WHERE status IN ('active', 'paused')") > 1:
            v.append(f"{label}: открыто больше одной сессии")
        if not sc.voice and q("SELECT count(*) FROM reviews WHERE study_date = ? AND mode = 'voice'", d):
            v.append(f"{label}: в текстовом режиме есть голосовые ответы")

        row = DayRow(day, st.total_min, st.en_sec // 60, st.counted, new, reviews,
                     core.planner.speech_pending(day, self.clock.now()))
        ev = row.events
        if n in sc.missed:
            ev.append("пропуск")
        if n in sc.skip:
            ev.append("не сегодня")
            if not st.skipped:
                v.append(f"{label}: «Не сегодня» не отмечено")
        if sc.paused(n):
            ev.append("пауза")
        if n in sc.late:
            ev.append("блок в 01:30")
            late = q("SELECT count(*) FROM sessions WHERE study_date = ? AND kind = 'evening' "
                     "AND started_at >= ?", d, self.cal.at_local(day, "00:00").isoformat(timespec="seconds"))
            if not late:
                v.append(f"{label}: поздний блок не засчитан в этот день")
        if n in sc.restart:
            ev.append("перезапуск")
        if n in sc.model_down:
            ev.append("API недоступен")
            if q("SELECT count(*) FROM llm_calls WHERE study_date = ? AND ok = 1 AND id > ?", d, self._calls_before):
                v.append(f"{label}: успешные вызовы модели при недоступном API")
        if n in sc.no_model:
            ev.append("модель не настроена")
            if q("SELECT count(*) FROM llm_calls WHERE study_date = ? AND id > ?", d, self._calls_before):
                v.append(f"{label}: вызовы модели, когда она не настроена")
        if self.report.copies[n]:
            ev.append("копия базы" + (" без звука" if self.report.silent_copies[n] else ""))
        if self.report.weekly[n]:
            ev.append("сводка")
        self.report.rows.append(row)

    def _final(self) -> None:
        sc, rep, v = self.sc, self.report, self.report.violations
        conn = self.conn
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            v.append(f"целостность базы: {check}")
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            v.append(f"нарушений внешних ключей: {len(fk)}")
        daily = self.core.backups.list("daily")
        rep.backups = len(daily)
        expect = min(sc.days, self.cfg.backup.keep)
        if len(daily) != expect:
            v.append(f"ежедневных копий {len(daily)}, ожидалось {expect}")
        last_days = [(sc.start + timedelta(days=k)) for k in range(sc.days - expect, sc.days)]
        if sorted(b.day for b in daily) != last_days:
            v.append("ежедневные копии не за последние дни подряд")
        for n in range(sc.days):
            day = self._day_of(n)
            if day.weekday() != 6:
                continue
            if rep.copies[n] != 1:
                v.append(f"{_dm(day)}: копий базы в Telegram {rep.copies[n]}, нужна одна")
            quiet = sc.paused(n) or n in sc.skip
            if rep.weekly[n] != (0 if quiet else 1):
                v.append(f"{_dm(day)}: сводок {rep.weekly[n]}")
            if quiet and rep.copies[n] and not rep.silent_copies[n]:
                v.append(f"{_dm(day)}: копия в тихое воскресенье ушла со звуком")
        if sc.send_fail and rep.send_failures != len([n for n in sc.send_fail if n < sc.days]):
            v.append(f"сорванных отправок {rep.send_failures}, по сценарию {len(sc.send_fail)}")
        restarts = len([n for n in sc.restart if n < sc.days and not sc.paused(n) and n not in sc.missed])
        if rep.restarts_ok != restarts:
            v.append(f"перезапусков с продолжением {rep.restarts_ok} из {restarts}")
        rep.stages = self.core.english.stage_counts()
        rep.model_calls, rep.cost_usd = conn.execute(
            "SELECT count(*), COALESCE(sum(cost_usd), 0) FROM llm_calls").fetchone()
        errors = conn.execute("SELECT count(*) FROM errors").fetchone()[0]
        pending = conn.execute("SELECT count(*) FROM disputes WHERE resolved_at IS NULL").fetchone()[0]
        rep.notes.append(f"Журнал ошибок: {errors}; оспоренных без разбора: {pending}.")


def simulate(cfg: Config, banks: list[Path], workdir: Path, scenario: Scenario | None = None) -> SimReport:
    return Simulation(cfg, banks, workdir, scenario).run()
