"""Резервные копии базы.

Ежедневно в 03:30 по Москве — копия средствами SQLite (backup API): она
согласована даже при работающем боте. Хранятся 14 последних ежедневных;
копии перед миграцией, перед восстановлением и сделанные вручную — по пять
последних каждого вида. Раз в неделю сжатая копия уходит владельцу в Telegram
вместе со сводкой: копия вне сервера без настройки облака.

Файл копии — самостоятельная база в режиме журнала DELETE, права 600, после
записи проходит quick_check. Имя несёт дату по Москве: ротация и «сделана ли
сегодняшняя копия» решаются по имени, а не по времени файла, поэтому прогон
на ускоренных часах ведёт себя так же, как сервер.

Восстановление — только при остановленном боте (замок data/studybot.lock).
Исходный файл не трогается: работа идёт с его копией; текущая база перед
заменой сохраняется копией pre-restore; после замены применяются миграции.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .clock import Clock, StudyCalendar, parse_hhmm
from .config import BackupConfig
from .db import connect, latest_version, migrate, schema_version

KINDS = ("daily", "manual", "pre-migrate", "pre-restore")
KEEP_OTHER = 5                          # копий каждого вида, кроме ежедневных
NAME_RE = re.compile(r"^(daily|manual|pre-migrate|pre-restore)-(\d{4}-\d{2}-\d{2})(?:-(\d{6}))?\.sqlite3$")
TELEGRAM_MAX = 45 * 1024 * 1024         # предел документа у Bot API — 50 МБ, берём с запасом
STALE_PART = timedelta(hours=1)
REQUIRED_TABLES = {"items", "item_state", "reviews", "days", "settings"}


class BackupError(RuntimeError):
    pass


def human_size(n: int) -> str:
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} КБ"
    return f"{n / 1024 / 1024:.1f} МБ".replace(".", ",")


def copy_database(src: sqlite3.Connection, dest: Path) -> None:
    """Согласованная копия через backup API, журнал DELETE, проверка, права 600."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    out = sqlite3.connect(str(dest))
    try:
        src.backup(out)
        out.execute("PRAGMA journal_mode = DELETE")
        check = out.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        out.close()
    os.chmod(dest, 0o600)
    if check != "ok":
        dest.unlink(missing_ok=True)
        raise BackupError(f"копия не прошла проверку: {check}")


@dataclass(frozen=True)
class BackupFile:
    path: Path
    kind: str
    day: date
    stamp: str          # 'YYYY-MM-DD' или 'YYYY-MM-DD-HHMMSS': порядок внутри вида
    size: int

    @property
    def when(self) -> str:
        """Дата и время по Москве из имени: «28.09 03:30» или «28.09 14:05»."""
        if len(self.stamp) > 10:
            t = self.stamp[11:]
            return f"{self.day:%d.%m} {t[:2]}:{t[2:4]}"
        return f"{self.day:%d.%m}"


def _parse(p: Path) -> BackupFile | None:
    m = NAME_RE.match(p.name)
    if not m:
        return None
    try:
        size = p.stat().st_size
    except FileNotFoundError:
        return None
    stamp = m.group(2) + (f"-{m.group(3)}" if m.group(3) else "")
    return BackupFile(p, m.group(1), date.fromisoformat(m.group(2)), stamp, size)


class Backups:
    def __init__(self, conn: sqlite3.Connection, directory: Path, calendar: StudyCalendar,
                 clock: Clock, cfg: BackupConfig) -> None:
        self.conn = conn
        self.dir = Path(directory)
        self.calendar = calendar
        self.clock = clock
        self.cfg = cfg

    # список и ротация

    def list(self, kind: str | None = None) -> list[BackupFile]:
        """Копии, новые первыми."""
        if not self.dir.exists():
            return []
        out = [b for b in map(_parse, self.dir.iterdir()) if b is not None
               and (kind is None or b.kind == kind)]
        out.sort(key=lambda b: (b.stamp, b.kind), reverse=True)
        return out

    def last(self, kind: str = "daily") -> BackupFile | None:
        found = self.list(kind)
        return found[0] if found else None

    def rotate(self) -> list[Path]:
        removed = []
        for kind in KINDS:
            keep = self.cfg.keep if kind == "daily" else KEEP_OTHER
            for b in self.list(kind)[keep:]:
                b.path.unlink(missing_ok=True)
                removed.append(b.path)
        limit = datetime.now().timestamp() - STALE_PART.total_seconds()
        for p in self.dir.glob("*.part"):
            try:
                if p.stat().st_mtime < limit:
                    p.unlink(missing_ok=True)
                    removed.append(p)
            except FileNotFoundError:
                pass
        return removed

    # создание

    def _name(self, kind: str, now: datetime) -> str:
        loc = self.calendar.local(now)
        if kind == "daily":
            return f"daily-{loc:%Y-%m-%d}.sqlite3"
        return f"{kind}-{loc:%Y-%m-%d-%H%M%S}.sqlite3"

    def make(self, kind: str = "manual") -> BackupFile:
        if kind not in KINDS:
            raise ValueError(f"неизвестный вид копии: {kind}")
        final = self.dir / self._name(kind, self.clock.now())
        part = final.with_name(final.name + f".{uuid.uuid4().hex[:6]}.part")
        try:
            copy_database(self.conn, part)
            os.replace(part, final)
        finally:
            part.unlink(missing_ok=True)
        self.rotate()
        found = _parse(final)
        if found is None:                      # имя всегда по шаблону; на случай правки шаблона
            raise BackupError(f"копия записана под неожиданным именем: {final.name}")
        return found

    def due(self, now: datetime) -> bool:
        """Пора ли ежедневной копии: местное время после backup.time, сегодняшней ещё нет."""
        loc = self.calendar.local(now)
        if loc.time() < parse_hhmm(self.cfg.time):
            return False
        return not (self.dir / f"daily-{loc:%Y-%m-%d}.sqlite3").exists()

    def snapshot_gz(self) -> tuple[str, bytes]:
        """Свежая копия, сжатая для отправки в Telegram: имя файла и содержимое."""
        loc = self.calendar.local(self.clock.now())
        tmp = self.dir / f"snapshot-{uuid.uuid4().hex[:8]}.part"
        try:
            copy_database(self.conn, tmp)
            data = gzip.compress(tmp.read_bytes(), compresslevel=6)
        finally:
            tmp.unlink(missing_ok=True)
        return f"studybot-{loc:%Y-%m-%d}.sqlite3.gz", data


# проверка и восстановление

@dataclass
class CopyInfo:
    schema: int
    check: str
    items: int
    reviews: int
    days_counted: int
    last_day: str | None
    content_version: int | None

    def lines(self) -> list[str]:
        return [f"проверка целостности: {self.check}",
                f"схема базы: {self.schema} (код ждёт {latest_version()})",
                f"позиций контента: {self.items}, версия контента: {self.content_version or 'нет'}",
                f"ответов: {self.reviews}, засчитанных дней: {self.days_counted}, "
                f"последний учебный день: {self.last_day or 'нет'}"]


def unpack(src: Path, workdir: Path) -> Path:
    """Рабочая копия файла: .gz распаковывается, обычный файл копируется. Исходник не трогается."""
    if not src.exists():
        raise BackupError(f"нет файла {src}")
    workdir.mkdir(parents=True, exist_ok=True)
    tmp = workdir / f"restore-{uuid.uuid4().hex[:8]}.part"
    try:
        if src.suffix == ".gz":
            with gzip.open(src, "rb") as fin, open(tmp, "wb") as fout:
                shutil.copyfileobj(fin, fout)
        else:
            shutil.copyfile(src, tmp)
    except (OSError, EOFError) as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"файл не читается: {exc}") from exc
    os.chmod(tmp, 0o600)
    return tmp


def inspect_copy(path: Path) -> CopyInfo:
    try:
        conn = sqlite3.connect(str(path))
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not REQUIRED_TABLES <= tables:
                raise BackupError("это не база учебного бота: нет нужных таблиц")
            q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
            return CopyInfo(
                schema=q("PRAGMA user_version"), check=check,
                items=q("SELECT count(*) FROM items"), reviews=q("SELECT count(*) FROM reviews"),
                days_counted=q("SELECT count(*) FROM days WHERE counted = 1"),
                last_day=q("SELECT max(study_date) FROM reviews"),
                content_version=q("SELECT max(id) FROM content_versions"))
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"файл не является базой SQLite: {exc}") from exc


@dataclass
class RestoreResult:
    info: CopyInfo
    safety: BackupFile | None
    migrations: list[str]


def restore_database(src: Path, db_file: Path, backups_dir: Path, calendar: StudyCalendar,
                     clock: Clock, cfg: BackupConfig, apply: bool) -> RestoreResult:
    """Проверить копию и, если apply, заменить ею базу. Замок держит вызывающий."""
    tmp = unpack(src, backups_dir)
    try:
        info = inspect_copy(tmp)
        if info.check != "ok":
            raise BackupError(f"копия повреждена: {info.check}")
        if info.schema > latest_version():
            raise BackupError(f"копия от более новой версии кода (схема {info.schema}, "
                              f"код знает до {latest_version()}): сначала обнови код")
        if not apply:
            return RestoreResult(info, None, [])
        conn = connect(db_file)
        try:
            safety = None
            if schema_version(conn) > 0:
                safety = Backups(conn, backups_dir, calendar, clock, cfg).make("pre-restore")
            source = sqlite3.connect(str(tmp))
            try:
                source.backup(conn)
            finally:
                source.close()
            applied = migrate(conn)
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise BackupError(f"после восстановления база не прошла проверку: {check}")
        finally:
            conn.close()
        return RestoreResult(info, safety, applied)
    finally:
        tmp.unlink(missing_ok=True)


def backup_before_migrations(conn: sqlite3.Connection, backups_dir: Path, calendar: StudyCalendar,
                             clock: Clock, cfg: BackupConfig) -> BackupFile | None:
    """Перед миграциями существующей базы — копия pre-migrate: откат к ней вручную командой restore."""
    if schema_version(conn) == 0 or schema_version(conn) >= latest_version():
        return None
    return Backups(conn, backups_dir, calendar, clock, cfg).make("pre-migrate")
