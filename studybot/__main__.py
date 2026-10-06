"""Запуск бота и служебные команды.

    python -m studybot --config config.toml run       — запустить бота (systemd вызывает её)
    python -m studybot --config config.toml check     — проверить конфиг и права
    python -m studybot --config config.toml init-db   — создать или обновить базу
    python -m studybot --config config.toml settings  — показать настройки
    python -m studybot --config config.toml import FILE…|DIR [--apply]
                                                      — отчёт импорта; с --apply — применить;
                                                        папка или несколько файлов — в порядке
                                                        карты → банки → разборы → английский
    python -m studybot --config config.toml sources   — какие источники найдены
    python -m studybot --config config.toml extract-pages PDF OUT "Заголовок"
                                                      — постраничный текст книги
    python -m studybot --config config.toml backup    — резервная копия сейчас (можно при работающем боте)
    python -m studybot --config config.toml backups   — список копий
    python -m studybot --config config.toml restore FILE [--apply]
                                                      — проверить копию; с --apply — восстановить
                                                        (бот должен быть остановлен)
    python -m studybot --config config.toml simulate [--days 30] [--voice] [--content DIR]
                                                      — прогон условных дней, таблица по дням
    python -m studybot --config config.toml acceptance [DIR] [--no-tests]
                                                      — приёмка перед запуском
    python -m studybot --config config.toml ping-model — один короткий вызов каждой модели
    python -m studybot --config config.toml voice [--apply] — что озвучено; --apply — озвучить недостающее
                                                          (бот с озвучкой делает это сам в фоне)

Коды выхода: 0 — успешно, 1 — проверка не прошла, 2 — ошибка конфига или аргументов,
3 — бот уже запущен (замок data/studybot.lock занят).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from . import __version__
from .clock import SystemClock
from .config import ConfigError, insecure_permissions, load_config
from .db import connect, migrate, schema_version
from .lock import InstanceLock
from .settings import SPEC, Settings
from .speech import STTConfigError, make_stt


def _lock_busy(lock: InstanceLock) -> int:
    print(f"Бот уже запущен: замок {lock.path} держит процесс {lock.holder()}. "
          f"Остановить: sudo systemctl stop studybot", file=sys.stderr)
    return 3


def _pre_migrate(cfg, conn, log=print) -> None:
    from .backup import backup_before_migrations
    made = backup_before_migrations(conn, cfg.paths.backup_dir, cfg.calendar(), SystemClock(), cfg.backup)
    if made is not None:
        log(f"копия перед миграциями: {made.path}")


def run(cfg) -> int:
    """Запуск бота: проверки, замок, копия перед миграциями, опрос Telegram до остановки сервиса."""
    import asyncio
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        cfg.require_telegram()
        make_stt(cfg.stt_provider, cfg)
    except (ConfigError, STTConfigError) as exc:
        print(f"Конфиг: {exc}", file=sys.stderr)
        return 2
    if cfg.source_file and insecure_permissions(cfg.source_file):
        print("Конфиг читается не только владельцем: chmod 600 config.toml", file=sys.stderr)
        return 2
    cfg.paths.ensure()
    lock = InstanceLock(cfg.paths.lock_file)
    if not lock.acquire():
        return _lock_busy(lock)
    conn = connect(cfg.paths.db_file)
    try:
        _pre_migrate(cfg, conn, logging.info)
        applied = migrate(conn)
        if applied:
            logging.info("миграции: %s", ", ".join(applied))
        from aiogram.utils.token import TokenValidationError
        from .bot.telegram import run_bot
        try:
            asyncio.run(run_bot(cfg, conn))
        except TokenValidationError:
            print("Конфиг: telegram.token неверного вида", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            pass
    finally:
        conn.close()
        lock.release()
    return 0


def cmd_check(cfg) -> int:
    print(f"studybot {__version__}")
    print(f"конфиг: {cfg.source_file}")
    print(f"база: {cfg.paths.db_file}")
    print(f"сутки: {cfg.timezone}, граница {cfg.day_boundary}")
    print(f"telegram: {'задан' if cfg.token else 'нет токена'}, "
          f"владелец {'задан' if cfg.owner_id > 0 else 'не задан'}")
    print(f"модель: {'настроена' if cfg.llm.configured else 'не настроена'}, "
          f"дневной предел {cfg.llm.daily_budget_usd} USD")
    print(f"распознавание: {cfg.stt_provider}; озвучка: {cfg.tts_provider}")
    print(f"копии: в {cfg.backup.time}, хранить {cfg.backup.keep}, "
          f"в Telegram {'да' if cfg.backup.weekly_to_telegram else 'нет'}")
    try:
        make_stt(cfg.stt_provider, cfg)
    except STTConfigError as exc:
        print(f"ВНИМАНИЕ: {exc}")
        return 1
    if cfg.source_file and insecure_permissions(cfg.source_file):
        print("ВНИМАНИЕ: конфиг читается не только владельцем, нужно chmod 600")
        return 1
    return 0


def cmd_backup(cfg) -> int:
    from .backup import BackupError, Backups, human_size
    if not cfg.paths.db_file.exists():
        print("Базы ещё нет.", file=sys.stderr)
        return 1
    conn = connect(cfg.paths.db_file)
    try:
        made = Backups(conn, cfg.paths.backup_dir, cfg.calendar(), SystemClock(), cfg.backup).make("manual")
    except (BackupError, OSError) as exc:
        print(f"Копия не сделана: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"копия: {made.path} ({human_size(made.size)}), проверка целостности пройдена")
    return 0


def cmd_backups(cfg) -> int:
    from .backup import KEEP_OTHER, Backups, human_size
    b = Backups(None, cfg.paths.backup_dir, cfg.calendar(), SystemClock(), cfg.backup)  # type: ignore[arg-type]
    found = b.list()
    if not found:
        print(f"в {cfg.paths.backup_dir} копий нет")
        return 0
    for f in found:
        print(f"{f.path.name:<42} {human_size(f.size):>9}")
    print(f"хранится: ежедневных {cfg.backup.keep}, остальных по {KEEP_OTHER} каждого вида")
    return 0


def cmd_restore(cfg, args, apply: bool) -> int:
    from .backup import BackupError, restore_database
    if len(args) != 1:
        print("нужно: restore FILE [--apply]", file=sys.stderr)
        return 2
    src = Path(args[0])
    lock = InstanceLock(cfg.paths.lock_file)
    if apply and not lock.acquire():
        return _lock_busy(lock)
    try:
        res = restore_database(src, cfg.paths.db_file, cfg.paths.backup_dir, cfg.calendar(), SystemClock(),
                               cfg.backup, apply)
    except BackupError as exc:
        print(f"Восстановление: {exc}", file=sys.stderr)
        return 1
    finally:
        lock.release()
    print(f"копия {src.name}:")
    for line in res.info.lines():
        print(f"  {line}")
    if not apply:
        print("Копия годится. Восстановить: остановить бота (sudo systemctl stop studybot), "
              "повторить команду с --apply, запустить бота.")
        return 0
    if res.safety is not None:
        print(f"прежняя база сохранена: {res.safety.path}")
    if res.migrations:
        print(f"применены миграции: {', '.join(res.migrations)}")
    print("База восстановлена. Запустить бота: sudo systemctl start studybot")
    return 0


def cmd_simulate(cfg, days: int, voice: bool, content: str | None) -> int:
    from .simulate import Scenario, simulate
    folder = Path(content) if content else cfg.paths.content_dir
    banks = sorted(folder.glob("*.md")) if folder.exists() else []
    if not banks:
        print(f"в {folder} нет банков: укажи папку через --content", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        try:
            rep = simulate(cfg, banks, Path(tmp), Scenario(days=days, voice=voice))
        except ValueError as exc:
            print(f"Прогон: {exc}", file=sys.stderr)
            return 1
    print(rep.text())
    return 0 if rep.ok else 1


def cmd_acceptance(cfg, args, days: int, tests: bool) -> int:
    from .acceptance import passed, render, run_acceptance
    folder = Path(args[0]) if args else None
    checks = run_acceptance(cfg, folder, days, with_tests=tests)
    print(render(checks))
    return 0 if passed(checks) else 1


def cmd_voice(cfg, apply: bool) -> int:
    """Озвучка заранее: сколько файлов готово; с --apply — озвучить недостающее."""
    import asyncio
    from . import voicing
    from .app import build_core
    from .clock import SystemClock
    from .db import connect
    from .deepgram import TTS_PRICE_PER_1K
    from .speech import make_tts
    if not cfg.paths.db_file.exists():
        print("Базы ещё нет.", file=sys.stderr)
        return 1
    conn = connect(cfg.paths.db_file)
    try:
        core = build_core(cfg, conn, SystemClock())
        jobs = voicing.plan(conn, core.rehearsals, cfg.deepgram)
        todo = voicing.pending(conn, cfg.paths.audio_dir, jobs)
        n, chars = voicing.summary(todo)
        total = 2 * len(jobs)
        print(f"озвучено {total - n} из {total} файлов; к озвучке {n}, знаков {chars} "
              f"(около ${chars / 1000 * TTS_PRICE_PER_1K:.2f} в Deepgram)")
        if not apply or not n:
            return 0
        tts = make_tts(cfg)
        if tts is None:
            print("озвучка не подключена: в конфиге [tts] provider = \"deepgram\"", file=sys.stderr)
            return 2
        st = asyncio.run(voicing.run(conn, cfg.paths.audio_dir, tts, todo, cfg.deepgram.slow_speed,
                                     progress=lambda s: print(f"  готово {s.done} из {n}", flush=True)))
        print(f"готово {st.done}, сбоев {st.failed}" + (f"; остановлено: {st.stopped}" if st.stopped else ""))
        return 0 if not st.failed else 1
    finally:
        conn.close()


def cmd_ping(cfg) -> int:
    """Проверка поставщика на серверном шаге: адрес, ключ, названия моделей, режим JSON, цены.
    Два коротких вызова, оба пишутся в журнал расходов рабочей базы."""
    import asyncio
    import time as _time
    from .llm.client import CHEAP, FLAGSHIP, LLM, LLMUnavailable
    if not cfg.llm.configured:
        print("Модель не настроена: нужны llm.api_key, llm.cheap_model, llm.flagship_model.", file=sys.stderr)
        return 2
    cfg.paths.ensure()
    conn = connect(cfg.paths.db_file)
    migrate(conn)
    llm = LLM(conn, cfg.llm, SystemClock(), cfg.calendar())
    system = 'Ответь только JSON-объектом {"ok": true}.'

    async def ping() -> int:
        status = 0
        for tier in (CHEAP, FLAGSHIP):
            model = cfg.llm.cheap_model if tier == CHEAP else cfg.llm.flagship_model
            extra = cfg.llm.cheap_extra if tier == CHEAP else cfg.llm.flagship_extra
            if extra:
                model += " " + ", ".join(f"{k}={v}" for k, v in extra.items())
            t0 = _time.monotonic()
            try:
                reply = await llm.call(tier, "ping", system, "ping")
            except LLMUnavailable as exc:
                status = 1
                print(f"{tier} ({model}): не отвечает — {exc}")
                if "400" in str(exc) and cfg.llm.json_mode:
                    print("  возможно, модель не принимает response_format: попробуй json_mode = false")
                continue
            took = _time.monotonic() - t0
            ok_json = '"ok":true' in reply.text.replace(" ", "").lower()
            cost = llm.cost(tier, reply.tokens_in, reply.tokens_out)
            print(f"{tier} ({model}): ответ за {took:.1f} с, модель {reply.model}, токены "
                  f"{reply.tokens_in}/{reply.tokens_out}, стоимость ${cost:.5f}, "
                  f"JSON {'да' if ok_json else 'нет'}: {reply.text[:80]!r}")
            if not ok_json:
                status = 1
        return status

    try:
        return asyncio.run(ping())
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="studybot")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("command", choices=["run", "check", "init-db", "settings", "import", "sources",
                                            "extract-pages", "backup", "backups", "restore", "simulate",
                                            "acceptance", "ping-model", "voice"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--voice", action="store_true")
    parser.add_argument("--content")
    parser.add_argument("--no-tests", action="store_true")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Конфиг: {exc}", file=sys.stderr)
        return 2
    if not 1 <= args.days <= 365:
        print("--days: от 1 до 365", file=sys.stderr)
        return 2

    if args.command == "check":
        return cmd_check(cfg)
    if args.command == "run":
        return run(cfg)
    if args.command == "backup":
        return cmd_backup(cfg)
    if args.command == "backups":
        return cmd_backups(cfg)
    if args.command == "restore":
        return cmd_restore(cfg, args.args, args.apply)
    if args.command == "simulate":
        return cmd_simulate(cfg, args.days, args.voice, args.content)
    if args.command == "acceptance":
        return cmd_acceptance(cfg, args.args, args.days, not args.no_tests)
    if args.command == "ping-model":
        return cmd_ping(cfg)
    if args.command == "voice":
        return cmd_voice(cfg, args.apply)

    if args.command == "extract-pages":
        from .content.extract_pages import ExtractError, extract_pages
        if len(args.args) != 3:
            print("нужно: extract-pages PDF OUT \"Заголовок\"", file=sys.stderr)
            return 2
        try:
            n = extract_pages(Path(args.args[0]), Path(args.args[1]), args.args[2])
        except ExtractError as exc:
            print(f"Извлечение: {exc}", file=sys.stderr)
            return 1
        print(f"страниц: {n}")
        return 0

    if args.command == "sources":
        from .content.sources import SourceLibrary
        found = SourceLibrary(cfg.paths.sources_dir).describe()
        print("\n".join(found) if found else f"в {cfg.paths.sources_dir} источников нет")
        return 0

    cfg.paths.ensure()
    conn = connect(cfg.paths.db_file)
    try:
        if args.command == "init-db":
            _pre_migrate(cfg, conn)
            applied = migrate(conn)
            print(f"схема версии {schema_version(conn)}; применено: {', '.join(applied) or 'ничего'}")
        elif args.command == "settings":
            migrate(conn)
            st = Settings(conn, SystemClock())
            for key, value in st.all().items():
                mark = "" if value == SPEC[key].default else "  (изменено)"
                print(f"{key} = {value!r} — {SPEC[key].title}{mark}")
        elif args.command == "import":
            from .content.importer import Importer, import_file, render_report
            from .content.sources import SourceLibrary
            migrate(conn)
            clock = SystemClock()
            from .simulate import order_banks
            importer = Importer(conn, clock, cfg.calendar(), SourceLibrary(cfg.paths.sources_dir))
            paths: list[Path] = []
            for name in args.args:
                p = Path(name)
                paths += sorted(p.glob("*.md")) if p.is_dir() else [p]
            if not paths:
                print("нужно: import FILE… или import ПАПКА", file=sys.stderr)
                return 2
            many = len(paths) > 1
            if many:
                paths, skipped = order_banks(paths)
                for name in skipped:
                    print(f"{name}: не банк, не карта и не разбор — пропущен")
            status, accepted = 0, 0
            for path in paths:
                plan, version = import_file(importer, path, args.apply)
                if many and plan.ok:
                    head = render_report(plan).split("\n")[0]
                    print(head + (f" — принято, версия {version}" if version is not None else " — без ошибок"))
                else:
                    print(render_report(plan))
                    print()
                if version is not None:
                    accepted += 1
                    if not many:
                        print(f"Принято, версия контента {version}.")
                elif not plan.ok:
                    status = 1
            if many:
                print(f"Файлов {len(paths)}, принято {accepted}" + ("" if args.apply else " (отчёт без --apply)") + ".")
            return status
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
