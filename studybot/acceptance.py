"""Приёмка перед запуском — одной командой на сервере.

    python -m studybot --config config.toml acceptance [ПАПКА_С_БАНКАМИ]

Что проверяется: конфиг и права; целостность рабочей базы; импорт всех банков
папки (по умолчанию content/) без ошибок формата и фрагменты источников;
инструкции для моделей; резервное копирование и восстановление на временной
копии; прогон 30 условных дней через бота в текстовом режиме с днями без
модели; автоматические тесты, если установлен pytest. Рабочая база, Telegram
и модель не затрагиваются: импорт идёт в базу в памяти, прогон — во временной
папке, копия — во временный файл.
"""

from __future__ import annotations

import json

import gzip
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .backup import BackupError, copy_database, inspect_copy, restore_database
from .clock import SystemClock
from .config import Config, insecure_permissions
from .content.importer import Importer, render_report
from .content.sources import SourceLibrary
from .db import latest_version, open_db, pending_migrations, schema_version
from .llm.prompts import NAMES, Prompts, PromptError
from .simulate import Scenario, order_banks, simulate

CODE_ROOT = Path(__file__).resolve().parents[1]
MARK = {"ok": "[ok] ", "warn": "[!]  ", "fail": "[нет]", "later": "[К9] "}


@dataclass
class Check:
    status: str                  # ok, warn, fail, later
    title: str
    lines: list[str] = field(default_factory=list)


def check_config(cfg: Config) -> Check:
    lines, status = [], "ok"
    if not cfg.token or cfg.owner_id <= 0:
        status = "fail"
        lines.append("для запуска нужны telegram.token и telegram.owner_id")
    if cfg.source_file and insecure_permissions(cfg.source_file):
        status = "fail"
        lines.append("config.toml читается не только владельцем: chmod 600 config.toml")
    if cfg.stt_provider == "stub":
        lines.append("распознавание не подключено: бот работает в текстовом режиме")
    if not cfg.llm.configured:
        status = "warn" if status == "ok" else status
        lines.append("модель не настроена: бот будет работать только форматами с проверкой кодом")
    else:
        p = cfg.llm.prices
        if not any((p.cheap_input, p.cheap_output, p.flagship_input, p.flagship_output)):
            status = "warn" if status == "ok" else status
            lines.append("цены моделей не внесены в [llm.prices]: расход в журнале будет нулевым")
        if not cfg.llm.daily_budget_usd:
            status = "warn" if status == "ok" else status
            lines.append("дневной предел расхода не задан (llm.daily_budget_usd = 0)")
        else:
            lines.append(f"модель настроена, дневной предел ${cfg.llm.daily_budget_usd:.2f}")
    return Check(status, "Конфиг", lines)


def check_database(cfg: Config) -> Check:
    path = cfg.paths.db_file
    if not path.exists():
        return Check("warn", "Рабочая база", [f"базы ещё нет: {path} создастся при запуске"])
    conn = sqlite3.connect(str(path))
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        version = schema_version(conn)
        pending = pending_migrations(conn)
    finally:
        conn.close()
    lines = [f"целостность: {integrity}; нарушений внешних ключей: {len(fk)}; схема {version} "
             f"из {latest_version()}"]
    status = "ok" if integrity == "ok" and not fk else "fail"
    if pending:
        lines.append(f"при запуске применятся миграции {', '.join(pending)}; перед ними бот сделает копию")
        status = "warn" if status == "ok" else status
    return Check(status, "Рабочая база", lines)


def check_content(cfg: Config, folder: Path) -> tuple[Check, Check, list[Path]]:
    files = sorted(folder.glob("*.md")) if folder.exists() else []
    if not files:
        msg = f"в {folder} нет md-файлов: пришли банки боту документом или укажи папку"
        return Check("fail", "Импорт банков", [msg]), Check("fail", "Фрагменты источников", []), []
    ordered, skipped = order_banks(files)
    conn = open_db(":memory:")
    clock = SystemClock()
    imp = Importer(conn, clock, cfg.calendar(), SourceLibrary(cfg.paths.sources_dir))
    errors, status = [], "ok"
    reasons: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    ref_lines: list[str] = []
    try:
        for p in ordered:
            plan = imp.analyze(p.name, p.read_bytes())
            if not plan.ok:
                errors.append(render_report(plan, 5))
                continue
            imp.apply(plan)
            if plan.kind == "ref":
                ref = plan.reference
                by_mod: dict[str, int] = defaultdict(int)
                for v, _ in ref.rejected:
                    by_mod[v.module] += 1
                ref_lines.append(f"справочник: страниц {len(ref.pages)}, слов и сочетаний {len(ref.vocab)}"
                                 + (", отклонено строк " + ", ".join(f"модуль {m} — {n}" for m, n in
                                                                      sorted(by_mod.items()))
                                    + " (их единиц нет в боте)" if by_mod else ""))
            for code in plan.fragments.get("missing", []):
                for why in plan.fragment_problems.get(code, []):
                    reasons[plan.area][why].add(code)
        rows = conn.execute(
            "SELECT area, count(*), sum(is_core), sum(kind = 'vignette'), sum(fragment_status = 'ok'), "
            "sum(fragment_status = 'truncated'), sum(fragment_status = 'missing'), sum(fragment_status = 'n/a') FROM items "
            "WHERE archived = 0 GROUP BY area ORDER BY area").fetchall()
        topics = conn.execute("SELECT count(*) FROM topics").fetchone()[0]
        # ссылки вперёд в модулях 2–6 разрешаются, когда загружены все файлы: сверка целиком
        codes = {r[0] for r in conn.execute("SELECT code FROM items WHERE track = 'en' AND archived = 0")} | {
            r[0] for r in conn.execute("SELECT code FROM terms WHERE archived = 0")}
        notes = {r[0] for r in conn.execute("SELECT code FROM notes WHERE track = 'en'")}
        bad: list[str] = []
        for r in conn.execute("SELECT code, notes_json, links_json, extra_json FROM items WHERE track = 'en' "
                              "AND archived = 0"):
            refs = json.loads(r[2]) + json.loads(r[3] or "{}").get("time_links", [])
            bad += [f"{r[0]}→{x}" for x in refs if x not in codes]
            bad += [f"{r[0]}→{n}" for n in json.loads(r[1]) if n not in notes]
        for r in conn.execute("SELECT code, notes_json FROM terms WHERE archived = 0"):
            bad += [f"{r[0]}→{n}" for n in json.loads(r[1]) if n not in notes]
        if bad:
            ref_lines.append(f"ссылки английского без адресата: {len(bad)} ({', '.join(bad[:6])})")
        dg = conn.execute("SELECT count(*), COALESCE(sum(parts), 0) FROM digests").fetchone()
        no_dg = [r[0] for r in conn.execute("SELECT code FROM topics WHERE archived = 0 AND code NOT IN "
                                            "(SELECT topic_code FROM digests) ORDER BY code")]
        ref_lines.append(f"разборы: тем {dg[0]}, частей {dg[1]}"
                         + (f"; без разбора {len(no_dg)}: {', '.join(no_dg[:8])}" if no_dg else "; у всех тем есть"))
        reh = conn.execute("SELECT area, block, sum(kind = 'monologue'), sum(kind = 'scenario') FROM rehearsals "
                           "WHERE archived = 0 GROUP BY area, block ORDER BY area, block").fetchall()
        ref_lines += [f"репетиции {r[0]}, блок {r[1]}: планов монолога {r[2]}, сценариев {r[3]}" for r in reh]
        rec = conn.execute("SELECT count(*) FROM records WHERE archived = 0").fetchone()[0]
        terms = conn.execute("SELECT count(*), sum(context IS NOT NULL) FROM terms WHERE archived = 0").fetchone()
        ref_lines.append(f"записи модулей 2–6: {rec}; единицы на узнавание модуля 5: {terms[0]}, "
                         f"с абзацем статьи {terms[1] or 0}")
    finally:
        conn.close()

    lines = [f"файлов {len(ordered)}" + (f"; не банки, пропущены: {', '.join(skipped)}" if skipped else "")]
    for r in rows:
        if r[0].startswith("EN"):
            lines.append(f"{r[0]}: позиций {r[1]}")
        else:
            lines.append(f"{r[0]}: позиций {r[1]}, ядро {r[2]}, виньеток {r[3]}")
    lines.append(f"тем в картах: {topics}")
    lines += ref_lines
    if any("отклонено" in l or "без адресата" in l for l in ref_lines):
        status = "warn" if status == "ok" else status
    if errors:
        status = "fail"
        lines.append(f"ошибки формата в {len(errors)} файлах:")
        lines += errors
    areas = {r[0] for r in rows}
    if not any(a.startswith("EN") for a in areas):
        status = "fail"
        lines.append("нет банка английского: без него первый релиз не запускается")
    if not areas - {a for a in areas if a.startswith("EN")}:
        status = "warn" if status == "ok" else status
        lines.append("банков психологии нет: их разбор не проверен")
    content = Check(status, "Импорт банков без ошибок формата", lines)

    frag_lines, frag_status = [], "ok"
    for r in rows:
        if r[0].startswith("EN"):
            continue
        frag_lines.append(f"{r[0]}: целиком {r[4]}, сжаты по ключу {r[5]}, не найдены {r[6]}"
                          + (f", источника нет — проверка по ключу {r[7]}" if r[7] else ""))
        for why, codes in sorted(reasons[r[0]].items(), key=lambda kv: -len(kv[1]))[:6]:
            frag_lines.append(f"  — {why}: {len(codes)}")
        if r[6]:
            frag_status = "warn"
    if frag_status == "warn":
        frag_lines.append("позиции с ненайденной страницей импортируются, но не показываются: "
                          "проверь ссылку «Ист» или файл источника в sources/")
    if not frag_lines:
        frag_lines.append("банков психологии нет — фрагменты не строились")
    return content, Check(frag_status, "Фрагменты источников", frag_lines), ordered


def check_prompts(cfg: Config) -> Check:
    try:
        Prompts(cfg.paths.prompts_dir)
    except PromptError as exc:
        return Check("fail", "Инструкции для моделей", [str(exc)])
    return Check("ok", "Инструкции для моделей", [f"{len(NAMES)} из {len(NAMES)} в {cfg.paths.prompts_dir}"])


def check_backups(cfg: Config) -> Check:
    lines, status = [], "ok"
    bdir = cfg.paths.backup_dir
    try:
        bdir.mkdir(parents=True, exist_ok=True)
        probe = bdir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        lines.append(f"папка {bdir} доступна на запись")
    except OSError as exc:
        return Check("fail", "Резервные копии", [f"папка {bdir} недоступна: {exc}"])
    daily = sorted(p.name for p in bdir.glob("daily-*.sqlite3"))
    lines.append(f"ежедневных копий: {len(daily)}" + (f", последняя {daily[-1]}" if daily else
                                                         f"; первая будет в {cfg.backup.time}"))
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        src = sqlite3.connect(str(cfg.paths.db_file)) if cfg.paths.db_file.exists() else open_db(":memory:")
        try:
            copy_database(src, work / "copy.sqlite3")
        finally:
            src.close()
        gz = work / "copy.sqlite3.gz"
        with open(work / "copy.sqlite3", "rb") as fin, gzip.open(gz, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        try:
            info = inspect_copy(work / "copy.sqlite3")
            target = work / "restored.sqlite3"
            res = restore_database(gz, target, work / "backups", cfg.calendar(), SystemClock(), cfg.backup,
                                   apply=True)
            conn = sqlite3.connect(str(target))
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            restored = conn.execute("SELECT count(*) FROM reviews").fetchone()[0]
            conn.close()
        except BackupError as exc:
            return Check("fail", "Резервные копии", lines + [f"пробное восстановление: {exc}"])
        if check != "ok" or restored != info.reviews or res.info.reviews != info.reviews:
            status = "fail"
            lines.append(f"пробное восстановление расходится: проверка {check}, ответов {restored} "
                         f"из {info.reviews}")
        else:
            lines.append(f"пробная копия, сжатие и восстановление прошли: ответов {info.reviews}, "
                         f"схема {info.schema}")
    if not cfg.backup.weekly_to_telegram:
        status = "warn" if status == "ok" else status
        lines.append("еженедельная копия в Telegram выключена (backup.weekly_to_telegram)")
    return Check(status, "Резервные копии", lines)


def check_simulation(cfg: Config, banks: list[Path], days: int) -> Check:
    if not banks:
        return Check("fail", f"Прогон {days} дней", ["нет банков для прогона"])
    with tempfile.TemporaryDirectory() as tmp:
        try:
            rep = simulate(cfg, banks, Path(tmp), Scenario(days=days))
        except ValueError as exc:
            return Check("fail", f"Прогон {days} дней", [str(exc)])
    tail = rep.text().split("\n")
    summary = [l for l in tail if l.startswith(("Засчитано", "Фразы", "Уведомлений", "Резервных"))]
    lines = ["текстовый режим без распознавания и озвучки; API недоступен 2 дня, модель не настроена "
             "1 день — сессии шли форматами кода"] + summary
    if rep.violations:
        lines += [f"нарушение: {v}" for v in rep.violations[:10]]
        return Check("fail", f"Прогон {days} условных дней", lines)
    lines.append("потолки, лимиты, учёт дня, уведомления и копии в норме; таблица по дням — команда simulate")
    return Check("ok", f"Прогон {days} условных дней", lines)


def check_tests() -> Check:
    tests = CODE_ROOT / "tests"
    if not tests.exists():
        return Check("warn", "Автотесты", ["папки tests нет рядом с кодом"])
    if importlib.util.find_spec("pytest") is None:
        return Check("warn", "Автотесты", ["pytest не установлен: pip install -r requirements-dev.txt"])
    env = {k: v for k, v in os.environ.items() if not k.startswith("STUDYBOT_")}
    try:
        out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tests)],
                             cwd=CODE_ROOT, capture_output=True, text=True, timeout=600, env=env)
    except subprocess.TimeoutExpired:
        return Check("fail", "Автотесты", ["не уложились в 10 минут"])
    last = [l for l in out.stdout.strip().split("\n") if l.strip()][-1:] or ["нет вывода"]
    return Check("ok" if out.returncode == 0 else "fail", "Автотесты", last)


def run_acceptance(cfg: Config, folder: Path | None = None, days: int = 30,
                   with_tests: bool = True) -> list[Check]:
    content, fragments, banks = check_content(cfg, folder or cfg.paths.content_dir)
    checks = [check_config(cfg), check_database(cfg), content, fragments, check_prompts(cfg),
              check_backups(cfg), check_simulation(cfg, banks, days)]
    if with_tests:
        checks.append(check_tests())
    return checks


def render(checks: list[Check]) -> str:
    out = ["Приёмка перед запуском."]
    for c in checks:
        out.append(f"{MARK[c.status]} {c.title}")
        out += [f"       {l}" for l in c.lines]
    counts = Counter(c.status for c in checks)
    if counts["fail"]:
        out.append(f"Итог: не готово, провалено проверок: {counts['fail']}.")
    elif counts["warn"]:
        out.append(f"Итог: готово к запуску текстового режима, замечаний: {counts['warn']}.")
    else:
        out.append("Итог: готово к запуску.")
    return "\n".join(out)


def passed(checks: list[Check]) -> bool:
    return not any(c.status == "fail" for c in checks)
