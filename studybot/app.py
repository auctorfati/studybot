"""Сборка всех частей ядра из конфига — одно место, где объекты связываются."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .backup import Backups
from .checking.english import EnglishChecker
from .checking.psychology import PsyChecker
from .checking.talk import EnglishTalk
from .clock import Clock, StudyCalendar
from .config import Config
from .content.importer import Importer
from .content.sources import SourceLibrary
from .english.blocks import Blocks
from .english.academic import Academic
from .english.explain import Explainer
from .english.rehearsals import Rehearsals
from .english.track import EnglishTrack
from .psychology.planner import PsyPlanner
from .psychology.retell import SourceRetell
from .psychology.track import PsyTrack
from .llm.client import LLM, Transport
from .llm.prompts import Prompts
from .llm.runner import ModelRunner
from .review_queue import ReviewQueue
from .scheduler import Scheduler
from .sessions.day import DayBook
from .sessions.engine import SessionEngine
from .sessions.estimates import Estimates
from .sessions.planner import EnglishPlanner
from .settings import Settings


@dataclass
class Core:
    conn: sqlite3.Connection
    clock: Clock
    calendar: StudyCalendar
    settings: Settings
    scheduler: Scheduler
    queue: ReviewQueue
    importer: Importer
    llm: LLM
    runner: ModelRunner
    english: EnglishTrack
    en_checker: EnglishChecker
    psy_checker: PsyChecker
    talk: EnglishTalk
    blocks: Blocks
    days: DayBook
    estimates: Estimates
    planner: EnglishPlanner
    engine: SessionEngine
    backups: Backups
    rehearsals: Rehearsals
    explainer: Explainer
    psy: PsyTrack
    psy_planner: PsyPlanner
    retell: SourceRetell
    academic: Academic


def build_core(cfg: Config, conn: sqlite3.Connection, clock: Clock,
               transport: Transport | None = None) -> Core:
    cal = cfg.calendar()
    settings = Settings(conn, clock)
    scheduler = Scheduler(conn, settings)
    queue = ReviewQueue(conn, settings)
    importer = Importer(conn, clock, cal, SourceLibrary(cfg.paths.sources_dir))
    llm = LLM(conn, cfg.llm, clock, cal, transport)
    runner = ModelRunner(llm, Prompts(cfg.paths.prompts_dir))
    english = EnglishTrack(conn, settings, scheduler, queue)
    en_checker = EnglishChecker(conn, runner)
    psy_checker = PsyChecker(conn, runner)
    talk = EnglishTalk(runner)
    blocks = Blocks(conn, cal)
    days = DayBook(conn, settings)
    estimates = Estimates(conn, settings)
    rehearsals = Rehearsals(conn, passes_to_advance=lambda: settings.get("monologue_passes"))
    planner = EnglishPlanner(conn, settings, english, queue, blocks, estimates, days, cal, rehearsals)
    psy = PsyTrack(conn, scheduler, settings)
    academic = Academic(conn, scheduler, settings)
    psy_planner = PsyPlanner(conn, psy, queue, estimates)
    engine = SessionEngine(conn, settings, clock, cal, english, queue, en_checker, talk, blocks,
                           estimates, days, planner, model_ok=runner.available, rehearsals=rehearsals,
                           psy_track=psy, psy_planner=psy_planner, psy_checker=psy_checker, llm=llm,
                           academic=academic, runner=runner)
    backups = Backups(conn, cfg.paths.backup_dir, cal, clock, cfg.backup)
    explainer = Explainer(conn, runner)
    return Core(conn, clock, cal, settings, scheduler, queue, importer, llm, runner, english,
                en_checker, psy_checker, talk, blocks, days, estimates, planner, engine, backups,
                rehearsals, explainer, psy, psy_planner, SourceRetell(conn, runner), academic)
