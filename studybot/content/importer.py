"""Импорт md-файла: разбор → план с отчётом → применение по кнопке.

analyze() ничего не пишет в базу. apply() применяет план одной транзакцией.
Удаление считается внутри охвата файла: блоки английского или буквенные
группы психологии, которые в файле есть. Позиция, пропавшая из своего блока,
уходит в архив с историей; блок, целиком убранный из файла, не трогается —
так блоки можно держать в разных файлах.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ..clock import Clock, StudyCalendar, to_iso
from ..db import transaction
from ..enums import ItemKind, Track
from .common import Issue, Issues, clean_text, file_hash
from .english import ParsedEnglish, parse_english
from .digests import ParsedDigests, is_digests, parse_digests
from .reference import ParsedReference, is_reference, parse_reference
from .terms import build_context
from .psychology import ParsedPsy, ParsedTopics, area_from_name, parse_psy_bank, parse_topic_map
from .sources import SourceLibrary, build_fragment


@dataclass
class ItemRow:
    """Позиция в том виде, в каком она ляжет в таблицу items."""
    code: str
    unit: str
    grp: str
    kind: str
    level: int | None
    is_core: int
    prompt: str
    answer: str
    variants: list[str]
    notes: list[str]
    sound: str | None
    audio_text: str | None
    links: list[str]
    extra: dict
    source_ref: str | None
    fragment: str | None
    fragment_status: str
    prompt_hash: str
    answer_hash: str
    sort_key: int
    topics: list[str] = field(default_factory=list)


@dataclass
class ImportPlan:
    file_name: str
    sha256: str
    kind: str                  # 'en', 'psy', 'topics', 'ref'
    area: str
    scope: list[str]           # блоки или группы, которых касается файл
    item_scope: list[str] | None = None   # en: блоки, где позиции уходят в архив; None — как scope
    rows: list[ItemRow] = field(default_factory=list)
    notes: list = field(default_factory=list)
    topics: list = field(default_factory=list)
    rehearsals: list = field(default_factory=list)          # en: репетиции блока сборки
    rehearsal_rules: str = ""                                 # en: общие правила раздела «Репетиции»
    records: list = field(default_factory=list)              # en: записи модулей 2–6 (истории, слушание…)
    terms: list = field(default_factory=list)                # en: модуль 5, единицы на узнавание
    block_titles: dict = field(default_factory=dict)         # en: блок → название
    term_context: dict = field(default_factory=dict)         # код → абзац статьи
    terms_archived: list[str] = field(default_factory=list)
    records_archived: list[str] = field(default_factory=list)
    rehearsals_changed: list[str] = field(default_factory=list)
    rehearsals_archived: list[str] = field(default_factory=list)
    reference: ParsedReference | None = None
    digests: ParsedDigests | None = None
    digests_replaced: list[str] = field(default_factory=list)   # темы, у которых разбор уже был
    topics_without_digest: list[str] = field(default_factory=list)  # psy: темы банка без разбора
    issues: Issues = field(default_factory=Issues)
    new: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    changed_prompt: list[str] = field(default_factory=list)
    changed_answer: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    fragments: dict[str, list[str]] = field(default_factory=dict)   # статус → коды
    fragment_problems: dict[str, list[str]] = field(default_factory=dict)
    same_as_last: bool = False

    @property
    def ok(self) -> bool:
        return not self.issues.errors

    @property
    def archive_scope(self) -> list[str]:
        """Блоки или группы, внутри которых пропавшая позиция уходит в архив."""
        return self.scope if self.item_scope is None else self.item_scope

    @property
    def track(self) -> str:
        return Track.EN.value if self.kind in ("en", "ref") else Track.PSY.value


def detect_kind(text: str) -> str | None:
    first = next((l for l in text.split("\n") if l.startswith("# ")), "")
    if is_reference(text):
        return "ref"
    if is_digests(text):
        return "digest"
    if first.startswith("# Английский. Модуль"):
        return "en"
    if first.startswith("# Карта тем"):
        return "topics"
    if first.startswith("# Банк вопросов"):
        return "psy"
    return None


class Importer:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, calendar: StudyCalendar,
                 library: SourceLibrary) -> None:
        self.conn = conn
        self.clock = clock
        self.calendar = calendar
        self.library = library

    # анализ

    def analyze(self, file_name: str, data: bytes) -> ImportPlan:
        sha = file_hash(data)
        try:
            text = clean_text(data.decode("utf-8"))
        except UnicodeDecodeError:
            plan = ImportPlan(file_name, sha, "?", "?", [])
            plan.issues.error("файл", "не UTF-8: сохраните файл в кодировке UTF-8")
            return plan
        kind = detect_kind(text)
        if kind is None:
            plan = ImportPlan(file_name, sha, "?", "?", [])
            plan.issues.error("заголовок", "файл не узнан: ожидается «# Английский. Модуль N», "
                                           "«# Английский. Справочник», «# Банк вопросов …», "
                                           "«# Карта тем …» или «# Разборы тем …»")
            return plan
        if kind == "en":
            plan = self._analyze_en(file_name, sha, text)
        elif kind == "ref":
            plan = self._analyze_ref(file_name, sha, text)
        else:
            area = area_from_name(file_name)
            if area is None:
                plan = ImportPlan(file_name, sha, kind, "?", [])
                plan.issues.error("имя файла", "имя должно начинаться с области: PATO_…, PSY_…")
                return plan
            if kind == "topics":
                plan = self._analyze_topics(file_name, sha, text, area)
            elif kind == "digest":
                plan = self._analyze_digests(file_name, sha, text, area)
            else:
                plan = self._analyze_psy(file_name, sha, text, area)
        last = self.conn.execute(
            "SELECT file_sha256 FROM content_versions WHERE file_name = ? ORDER BY id DESC LIMIT 1",
            (file_name,)).fetchone()
        plan.same_as_last = bool(last and last[0] == sha)
        if plan.ok and plan.kind in ("en", "psy"):
            self._diff(plan)
        if plan.ok and plan.kind == "en":
            self._diff_rehearsals(plan)
            codes = {r.code for r in plan.records}
            plan.records_archived = sorted(r[0] for r in self.conn.execute(
                "SELECT code FROM records WHERE file_name = ? AND archived = 0", (file_name,)) if r[0] not in codes)
            tcodes = {t.code for t in plan.terms}
            plan.terms_archived = sorted(r[0] for r in self.conn.execute(
                "SELECT code FROM terms WHERE file_name = ? AND archived = 0", (file_name,)) if r[0] not in tcodes)
        if plan.ok and plan.kind == "psy":
            units = sorted({r.unit for r in plan.rows}, key=lambda c: c)
            have = {r[0] for r in self.conn.execute("SELECT topic_code FROM digests")}
            plan.topics_without_digest = [u for u in units if u not in have]
        return plan

    def _analyze_digests(self, file_name: str, sha: str, text: str, area: str) -> ImportPlan:
        topics = {r[0]: r[1] for r in self.conn.execute("SELECT code, area FROM topics WHERE archived = 0")}
        items = {r[0]: r[1] for r in self.conn.execute(
            "SELECT code, unit FROM items WHERE track = 'psy' AND archived = 0")}
        parsed = parse_digests(text, area, topics, items, self.library)
        plan = ImportPlan(file_name, sha, "digest", area, [d.code for d in parsed.digests])
        plan.issues.extend(parsed.issues)
        plan.digests = parsed
        have = {r[0] for r in self.conn.execute("SELECT topic_code FROM digests")}
        plan.digests_replaced = [d.code for d in parsed.digests if d.code in have]
        return plan
    def _analyze_ref(self, file_name: str, sha: str, text: str) -> ImportPlan:
        known = {r[0] for r in self.conn.execute(
            "SELECT code FROM items WHERE track = 'en' AND archived = 0")}
        parsed = parse_reference(text, known)
        plan = ImportPlan(file_name, sha, "ref", "EN", [f"{m}.{b}" for m, b in parsed.sections])
        plan.issues.extend(parsed.issues)
        plan.reference = parsed
        return plan

    def _diff_rehearsals(self, plan: ImportPlan) -> None:
        old = {r["code"]: r for r in self.conn.execute(
            "SELECT code, block, body, title, archived FROM rehearsals WHERE area = ?", (plan.area,))}
        codes = set()
        for r in plan.rehearsals:
            codes.add(r.code)
            o = old.get(r.code)
            if o is None or o["archived"] or o["body"] != r.body or o["title"] != r.title:
                plan.rehearsals_changed.append(r.code)
        plan.rehearsals_archived = sorted(
            c for c, o in old.items() if c not in codes and not o["archived"] and o["block"] in plan.archive_scope)

    def _analyze_en(self, file_name: str, sha: str, text: str) -> ImportPlan:
        known_notes = {r[0] for r in self.conn.execute("SELECT code FROM notes WHERE track = 'en'")}
        known_codes = {r[0] for r in self.conn.execute(
            "SELECT code FROM items WHERE track = 'en' AND archived = 0")} | {
            r[0] for r in self.conn.execute("SELECT code FROM terms WHERE archived = 0")}
        parsed: ParsedEnglish = parse_english(text, known_notes, known_codes)
        plan = ImportPlan(file_name, sha, "en", parsed.area, sorted(parsed.blocks, key=int))
        plan.issues.extend(parsed.issues)
        plan.notes = parsed.notes
        plan.rehearsals = parsed.rehearsals
        plan.records = parsed.records
        plan.terms = parsed.terms
        if parsed.exam_questions:
            # банк вопросов беседы прогонов — служебная запись Э0
            from .english import EnRecord
            plan.records = list(parsed.records) + [EnRecord(
                "Э0", "exam_bank", "Банк вопросов беседы", "\n".join(parsed.exam_questions), "9",
                {"questions": parsed.exam_questions}, 599999, 0)]
        plan.block_titles = dict(parsed.blocks)
        if not parsed.items and (parsed.records or parsed.terms):
            # файл только с записями или терминами (слушание модуля 6, тексты и прогоны модуля 5):
            # блоки — для отчёта; фразы и репетиции этих блоков живут в файлах банка и в архив не уходят
            plan.scope = sorted({r.block for r in parsed.records + parsed.terms if r.block}, key=int)
            plan.item_scope = []
        missing_ctx = []
        for t in parsed.terms:
            ctx, problem = build_context(t.source, t.sentence, self.library)
            plan.term_context[t.code] = ctx
            if ctx is None:
                missing_ctx.append(t.code)
        if missing_ctx:
            plan.issues.warn("контекст", f"абзац статьи не собран у {len(missing_ctx)} терминов "
                                         f"({', '.join(missing_ctx[:6])}): кнопка «Контекст» покажет только перевод")
        plan.rehearsal_rules = parsed.rehearsal_preamble
        for it in parsed.items:
            plan.rows.append(ItemRow(
                code=it.code, unit=it.block, grp=parsed.module,
                kind=(ItemKind.ASSEMBLY if it.assembly else ItemKind.PHRASE).value,
                level=None, is_core=1, prompt=it.prompt, answer=it.target,
                variants=it.variants, notes=it.notes, sound=it.sound, audio_text=it.audio_text,
                links=it.links, extra=it.extra, source_ref=None, fragment=None,
                fragment_status="n/a", prompt_hash=it.prompt_hash, answer_hash=it.answer_hash,
                sort_key=it.sort_key))
        return plan

    def _analyze_topics(self, file_name: str, sha: str, text: str, area: str) -> ImportPlan:
        parsed: ParsedTopics = parse_topic_map(text, area)
        plan = ImportPlan(file_name, sha, "topics", area, sorted({t.grp for t in parsed.topics}))
        plan.issues.extend(parsed.issues)
        plan.topics = parsed.topics
        existing = {r[0] for r in self.conn.execute(
            "SELECT code FROM topics WHERE area = ? AND archived = 0", (area,))}
        codes = {t.code for t in parsed.topics}
        plan.new = sorted(codes - existing)
        plan.unchanged = sorted(codes & existing)
        plan.archived = sorted(existing - codes)
        used = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT it.topic_code FROM item_topics it JOIN items i ON i.id = it.item_id "
            "WHERE i.archived = 0")}
        for code in sorted((existing - codes) & used):
            plan.issues.error(code, "тема убрана из карты, но на неё ссылаются позиции банка")
        return plan

    def _analyze_psy(self, file_name: str, sha: str, text: str, area: str) -> ImportPlan:
        parsed: ParsedPsy = parse_psy_bank(text, area)
        plan = ImportPlan(file_name, sha, "psy", area, sorted(parsed.groups))
        plan.issues.extend(parsed.issues)
        topics = {r[0] for r in self.conn.execute(
            "SELECT code FROM topics WHERE archived = 0")}
        if not topics:
            plan.issues.error("карта тем", "сначала импортируйте карту тем области")
        else:
            for it in parsed.items:
                unknown = [t for t in it.topics if t not in topics]
                if unknown:
                    plan.issues.error(it.code, f"темы нет в карте: {', '.join(unknown)}")

        item_frags = {
            (a, c): f for a, c, f in self.conn.execute(
                "SELECT area, code, fragment FROM items WHERE track = 'psy' AND archived = 0")}
        frag_ok, frag_tr, frag_miss, frag_key = [], [], [], []
        for it in parsed.items:
            frag = build_fragment(area, it.source, self.library, item_frags,
                                  query=" ".join([it.prompt, it.answer] + it.extra.get("questions", [])))
            {"ok": frag_ok, "truncated": frag_tr, "missing": frag_miss, "n/a": frag_key}[frag.status].append(it.code)
            if frag.problems:
                plan.fragment_problems[it.code] = frag.problems
                if frag.status == "ok" and frag.text:
                    for note in frag.problems:
                        plan.issues.warn(it.code, note)
            plan.rows.append(ItemRow(
                code=it.code, unit=it.topics[0] if it.topics else "?", grp=it.grp,
                kind=it.kind.value, level=it.level, is_core=int(it.is_core),
                prompt=it.prompt, answer=it.answer, variants=[], notes=[], sound=None,
                audio_text=None, links=[], extra=it.extra, source_ref=it.source,
                fragment=frag.text, fragment_status=frag.status,
                prompt_hash=it.prompt_hash, answer_hash=it.answer_hash,
                sort_key=it.sort_key, topics=it.topics))
        plan.fragments = {"ok": frag_ok, "truncated": frag_tr, "missing": frag_miss, "n/a": frag_key}
        return plan

    def _diff(self, plan: ImportPlan) -> None:
        existing = {
            r["code"]: r for r in self.conn.execute(
                "SELECT code, unit, grp, area, prompt_hash, answer_hash, archived FROM items "
                "WHERE track = ?", (plan.track,))}
        codes = set()
        for row in plan.rows:
            codes.add(row.code)
            old = existing.get(row.code)
            if old is None:
                plan.new.append(row.code)
            elif old["archived"]:
                plan.restored.append(row.code)
            elif old["answer_hash"] != row.answer_hash:
                plan.changed_answer.append(row.code)
            elif old["prompt_hash"] != row.prompt_hash:
                plan.changed_prompt.append(row.code)
            else:
                plan.unchanged.append(row.code)
        scope_col = "unit" if plan.kind == "en" else "grp"
        for code, old in existing.items():
            if code not in codes and not old["archived"] and old[scope_col] in plan.archive_scope:
                if old["area"] != plan.area:
                    continue                  # блок 2 модуля 2 не трогает блок 2 модуля 1
                plan.archived.append(code)

    def _area_of(self, code: str) -> str | None:
        row = self.conn.execute("SELECT area FROM items WHERE code = ? AND track = 'psy'",
                                (code,)).fetchone()
        return row[0] if row else None

    # применение

    def apply(self, plan: ImportPlan) -> int:
        """Применить план. Возвращает номер версии контента."""
        if not plan.ok:
            raise ValueError("план с ошибками формата не применяется")
        now = self.clock.now()
        today = self.calendar.study_date(now)
        with transaction(self.conn):
            version = self.conn.execute(
                "INSERT INTO content_versions (imported_at, track, file_name, file_sha256, summary_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (to_iso(now), plan.track, plan.file_name, plan.sha256,
                 json.dumps(self._summary(plan), ensure_ascii=False))).lastrowid
            if plan.kind == "topics":
                self._apply_topics(plan, version)
            elif plan.kind == "ref":
                self._apply_reference(plan, version)
            elif plan.kind == "digest":
                self._apply_digests(plan, version)
            else:
                self._apply_items(plan, version, today)
                if plan.kind == "en":
                    self._apply_rehearsals(plan, version)
                    self._apply_records(plan, version)
                    for b, title in plan.block_titles.items():
                        self.conn.execute("INSERT INTO en_block_titles (area, block, title) VALUES (?, ?, ?) "
                                          "ON CONFLICT(area, block) DO UPDATE SET title = excluded.title",
                                          (plan.area, b, title))
                    self._apply_terms(plan, version)
        return version

    def _apply_reference(self, plan: ImportPlan, version: int) -> None:
        """Справочник целиком заменяет прежний: прогресса к нему не привязано."""
        ref = plan.reference
        self.conn.execute("DELETE FROM ref_pages")
        self.conn.execute("DELETE FROM vocab")
        for p in ref.pages:
            self.conn.execute("INSERT INTO ref_pages (code, title, body, sort_key, content_version) "
                              "VALUES (?, ?, ?, ?, ?)", (p.code, p.title, p.body, p.sort_key, version))
        for v in ref.vocab:
            self.conn.execute(
                "INSERT INTO vocab (module, block, word, translation, where_code, note, sort_key, "
                "content_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (v.module, v.block, v.word, v.translation, v.where, v.note, v.sort_key, version))

    def _apply_digests(self, plan: ImportPlan, version: int) -> None:
        """Разборы к прогрессу не привязаны: темы файла заменяются целиком."""
        for d in plan.digests.digests:
            self.conn.execute("DELETE FROM digest_parts WHERE topic_code = ?", (d.code,))
            self.conn.execute(
                "INSERT INTO digests (topic_code, area, title, file_name, parts, content_version) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(topic_code) DO UPDATE SET area = excluded.area, "
                "title = excluded.title, file_name = excluded.file_name, parts = excluded.parts, "
                "content_version = excluded.content_version",
                (d.code, plan.area, d.title, plan.file_name, len(d.parts), version))
            for p in d.parts:
                self.conn.execute("INSERT INTO digest_parts (topic_code, num, title, body, positions_json) "
                                  "VALUES (?, ?, ?, ?, ?)",
                                  (d.code, p.num, p.title, p.body, json.dumps(p.positions, ensure_ascii=False)))
            # прочитанная часть могла исчезнуть: чтение остаётся на последней существующей
            self.conn.execute("UPDATE digest_reads SET part = MIN(part, ?) WHERE topic_code = ?",
                              (len(d.parts), d.code))

    def _apply_terms(self, plan: ImportPlan, version: int) -> None:
        """Термины модуля 5: прогресс привязан к коду; смена значения или предложения — показ завтра."""
        for t in plan.terms:
            old = self.conn.execute("SELECT id, answer_hash FROM terms WHERE code = ?", (t.code,)).fetchone()
            vals = (t.block, t.section, t.term, t.meaning, json.dumps(t.alternatives, ensure_ascii=False),
                    t.sentence, t.translation, t.analysis, t.source, plan.term_context.get(t.code),
                    json.dumps(t.notes, ensure_ascii=False), t.sound, t.answer_hash, t.sort_key, plan.file_name, version)
            if old is None:
                self.conn.execute(
                    "INSERT INTO terms (block, section, term, meaning, alt_json, sentence, translation, analysis, "
                    "source_ref, context, notes_json, sound, answer_hash, sort_key, file_name, content_version, code) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*vals, t.code))
            else:
                self.conn.execute(
                    "UPDATE terms SET block = ?, section = ?, term = ?, meaning = ?, alt_json = ?, sentence = ?, "
                    "translation = ?, analysis = ?, source_ref = ?, context = ?, notes_json = ?, sound = ?, "
                    "answer_hash = ?, sort_key = ?, file_name = ?, content_version = ?, archived = 0 WHERE code = ?",
                    (*vals, t.code))
                if old["answer_hash"] != t.answer_hash:
                    self.conn.execute("UPDATE term_state SET due_date = MIN(COALESCE(due_date, '9999'), ?) "
                                      "WHERE term_id = ? AND step IS NOT NULL",
                                      ((self.calendar.study_date(self.clock.now()).isoformat()), old["id"]))
        for code in plan.terms_archived:
            self.conn.execute("UPDATE terms SET archived = 1 WHERE code = ?", (code,))

    def _apply_records(self, plan: ImportPlan, version: int) -> None:
        for r in plan.records:
            self.conn.execute(
                "INSERT INTO records (code, area, block, kind, title, fields_json, body, file_name, sort_key, "
                "content_version, archived) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) ON CONFLICT(code) DO UPDATE SET "
                "area = excluded.area, block = excluded.block, kind = excluded.kind, title = excluded.title, "
                "fields_json = excluded.fields_json, body = excluded.body, file_name = excluded.file_name, "
                "sort_key = excluded.sort_key, content_version = excluded.content_version, archived = 0",
                (r.code, plan.area, r.block, r.kind, r.title, json.dumps(r.fields, ensure_ascii=False), r.body,
                 plan.file_name, r.sort_key, version))
        for code in plan.records_archived:
            self.conn.execute("UPDATE records SET archived = 1 WHERE code = ?", (code,))

    def _apply_rehearsals(self, plan: ImportPlan, version: int) -> None:
        for r in plan.rehearsals:
            self.conn.execute(
                "INSERT INTO rehearsals (code, area, block, kind, title, body, data_json, sort_key, "
                "content_version, archived) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0) ON CONFLICT(code) DO UPDATE SET "
                "area = excluded.area, block = excluded.block, kind = excluded.kind, title = excluded.title, "
                "body = excluded.body, data_json = excluded.data_json, sort_key = excluded.sort_key, "
                "content_version = excluded.content_version, archived = 0",
                (r.code, plan.area, r.block, r.kind, r.title, r.body,
                 json.dumps({**r.data, "rules": plan.rehearsal_rules}, ensure_ascii=False),
                 r.sort_key, version))
            self.conn.execute("INSERT OR IGNORE INTO rehearsal_state (code) VALUES (?)", (r.code,))
        for code in plan.rehearsals_archived:
            self.conn.execute("UPDATE rehearsals SET archived = 1 WHERE code = ?", (code,))

    def _apply_topics(self, plan: ImportPlan, version: int) -> None:
        for t in plan.topics:
            self.conn.execute(
                "INSERT INTO topics (code, area, grp, title, target_level, sort_key, content_version, archived) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0) ON CONFLICT(code) DO UPDATE SET "
                "area = excluded.area, grp = excluded.grp, title = excluded.title, "
                "target_level = excluded.target_level, sort_key = excluded.sort_key, "
                "content_version = excluded.content_version, archived = 0",
                (t.code, plan.area, t.grp, t.title, t.target_level, t.sort_key, version))
            self.conn.execute("INSERT OR IGNORE INTO topic_state (topic_code) VALUES (?)", (t.code,))
        for code in plan.archived:
            self.conn.execute("UPDATE topics SET archived = 1 WHERE code = ?", (code,))

    def _apply_items(self, plan: ImportPlan, version: int, today: date) -> None:
        for n in plan.notes:
            self.conn.execute(
                "INSERT INTO notes (track, code, title, body, content_version) VALUES ('en', ?, ?, ?, ?) "
                "ON CONFLICT(track, code) DO UPDATE SET title = excluded.title, body = excluded.body, "
                "content_version = excluded.content_version",
                (n.code, n.title, n.body, version))
        cols = ("track, area, code, unit, grp, kind, level, is_core, prompt, answer, variants_json, "
                "notes_json, sound, audio_text, links_json, extra_json, source_ref, fragment, "
                "fragment_status, prompt_hash, answer_hash, sort_key, content_version, archived")
        update = ", ".join(f"{c.strip()} = excluded.{c.strip()}" for c in cols.split(",")
                           if c.strip() not in ("track", "code"))
        for r in plan.rows:
            values = (plan.track, plan.area, r.code, r.unit, r.grp, r.kind, r.level, r.is_core,
                      r.prompt, r.answer, json.dumps(r.variants, ensure_ascii=False),
                      json.dumps(r.notes, ensure_ascii=False), r.sound, r.audio_text,
                      json.dumps(r.links, ensure_ascii=False), json.dumps(r.extra, ensure_ascii=False),
                      r.source_ref, r.fragment, r.fragment_status, r.prompt_hash, r.answer_hash,
                      r.sort_key, version, 0)
            self.conn.execute(
                f"INSERT INTO items ({cols}) VALUES ({', '.join('?' * len(values))}) "
                f"ON CONFLICT(track, code) DO UPDATE SET {update}", values)
            item_id = self.conn.execute("SELECT id FROM items WHERE track = ? AND code = ?",
                                        (plan.track, r.code)).fetchone()[0]
            self.conn.execute("DELETE FROM item_topics WHERE item_id = ?", (item_id,))
            for i, t in enumerate(r.topics):
                self.conn.execute("INSERT INTO item_topics (item_id, topic_code, is_primary) VALUES (?, ?, ?)",
                                  (item_id, t, int(i == 0)))
        # изменённый ответ: ближайший показ — завтра, чтобы новая версия была увидена
        tomorrow = (today + timedelta(days=1)).isoformat()
        for code in plan.changed_answer:
            self.conn.execute(
                "UPDATE item_state SET due_date = ? WHERE due_date IS NOT NULL AND due_date > ? "
                "AND item_id = (SELECT id FROM items WHERE track = ? AND code = ?)",
                (tomorrow, tomorrow, plan.track, code))
        for code in plan.archived:
            self.conn.execute("UPDATE items SET archived = 1 WHERE track = ? AND code = ?",
                              (plan.track, code))

    @staticmethod
    def _summary(plan: ImportPlan) -> dict:
        if plan.kind == "ref":
            ref = plan.reference
            return {"kind": "ref", "pages": len(ref.pages), "vocab": len(ref.vocab),
                    "rejected": len(ref.rejected), "warnings": len(plan.issues.warnings)}
        if plan.kind == "digest":
            dg = plan.digests
            return {"kind": "digest", "area": plan.area, "topics": [d.code for d in dg.digests],
                    "parts": sum(len(d.parts) for d in dg.digests), "rejected": len(dg.rejected),
                    "warnings": len(plan.issues.warnings)}
        return {
            "kind": plan.kind, "area": plan.area, "scope": plan.scope,
            "new": len(plan.new), "restored": len(plan.restored),
            "changed_prompt": len(plan.changed_prompt), "changed_answer": len(plan.changed_answer),
            "unchanged": len(plan.unchanged), "archived": plan.archived,
            "fragments": {k: len(v) for k, v in plan.fragments.items()},
            "warnings": len(plan.issues.warnings),
        }


# отчёт

KIND_TITLE = {"en": "банк английского", "psy": "банк психологии", "topics": "карта тем",
              "ref": "справочник английского", "digest": "разборы тем"}


def _codes(codes: list[str], limit: int = 12) -> str:
    shown = ", ".join(codes[:limit])
    return shown + (f" и ещё {len(codes) - limit}" if len(codes) > limit else "")


def _issue_lines(issues: list[Issue], limit: int) -> list[str]:
    out = [f"— {i}" for i in issues[:limit]]
    if len(issues) > limit:
        out.append(f"— и ещё {len(issues) - limit}")
    return out


def render_report(plan: ImportPlan, max_lines: int = 15) -> str:
    """Отчёт для сообщения в Telegram: сначала итог, потом детали."""
    head = f"{plan.file_name} — {KIND_TITLE.get(plan.kind, 'не узнан')}"
    if plan.area != "?":
        head += f", {plan.area}"
    if plan.scope and plan.kind not in ("ref", "digest"):
        head += f", {'блоки' if plan.kind == 'en' else 'группы'} {', '.join(plan.scope)}"
    lines = [head]
    if plan.same_as_last:
        lines.append("Файл совпадает с последним импортом, изменений нет.")
    if not plan.ok:
        lines.append(f"Ошибки формата: {len(plan.issues.errors)}. Импорт невозможен, исправьте файл.")
        lines += _issue_lines(plan.issues.errors, max_lines)
        return "\n".join(lines)

    if plan.kind == "topics":
        lines.append(f"Тем {len(plan.topics)}: новых {len(plan.new)}, "
                     f"уже были {len(plan.unchanged)}, в архив {len(plan.archived)}.")
    elif plan.kind == "digest":
        dg = plan.digests
        parts = sum(len(d.parts) for d in dg.digests)
        lines.append(f"Тем с разборами {len(dg.digests)} ({_codes([d.code for d in dg.digests], 12)}), "
                     f"частей {parts}. Темы файла заменяются целиком"
                     + (f"; уже были: {_codes(plan.digests_replaced, 8)}." if plan.digests_replaced else "."))
        for code, why in dg.rejected:
            lines.append(f"Отклонена тема {code}: {why}.")
    elif plan.kind == "ref":
        ref = plan.reference
        blocks = len({(v.module, v.block) for v in ref.vocab})
        lines.append(f"Обзорных страниц {len(ref.pages)}, словарей блоков {blocks}, слов и сочетаний "
                     f"{len(ref.vocab)}. Новая версия целиком заменяет прежнюю.")
        if ref.rejected:
            lines.append(f"Отклонено строк словаря: {len(ref.rejected)} — их единиц нет в боте. "
                         f"Справочник загружается после банков; отклонённое придёт с повторной загрузкой.")
            reasons: dict[str, list[str]] = {}
            for v, why in ref.rejected:
                mod = f"модуль {v.module}"
                reasons.setdefault(mod, []).append(v.word)
            for mod, words in sorted(reasons.items()):
                lines.append(f"— {mod}: {len(words)} ({_codes(words, 5)})")
    else:
        total = len(plan.rows)
        lines.append(
            f"Позиций {total}: новых {len(plan.new)}, изменена формулировка {len(plan.changed_prompt)}, "
            f"изменён ответ {len(plan.changed_answer)}, без изменений {len(plan.unchanged)}.")
        if plan.restored:
            lines.append(f"Возвращены из архива: {_codes(plan.restored)}.")
        if plan.changed_answer:
            lines.append(f"Изменён ответ, показ завтра: {_codes(plan.changed_answer)}.")
        if plan.archived:
            lines.append(f"Уходят в архив: {_codes(plan.archived)}.")
        if plan.kind == "en" and (plan.terms or plan.terms_archived):
            by_block: dict[str, int] = {}
            for t in plan.terms:
                by_block[t.block] = by_block.get(t.block, 0) + 1
            lines.append("Единицы на узнавание: " + ", ".join(f"блок {b} — {n}" for b, n in sorted(by_block.items()))
                         + "." + (f" В архив: {_codes(plan.terms_archived)}." if plan.terms_archived else ""))
        if plan.kind == "en" and (plan.records or plan.records_archived):
            kinds: dict[str, list[str]] = {}
            for r in plan.records:
                kinds.setdefault(r.kind, []).append(r.code)
            titles = {"story": "истории", "listening": "слушание", "explain": "объяснение", "compare": "сравнение",
                      "advice": "совет", "statement": "утверждения", "topic": "темы разговора", "text": "тексты",
                      "exam": "прогоны", "roleplay": "ролевые карточки", "discussion": "карточки обсуждения"}
            lines.append("Записи: " + "; ".join(f"{titles.get(k, k)} {_codes(v, 4)}" for k, v in kinds.items()) + ".")
            if plan.records_archived:
                lines.append(f"Записи в архив: {_codes(plan.records_archived)}.")
        if plan.kind == "en" and (plan.rehearsals or plan.rehearsals_archived):
            mono = [r.code for r in plan.rehearsals if r.kind == "monologue"]
            scen = [r.code for r in plan.rehearsals if r.kind == "scenario"]
            line = f"Репетиции: планы монолога {', '.join(mono) or 'нет'}, сценарии {', '.join(scen) or 'нет'}"
            if plan.rehearsals_changed:
                line += f"; новые или изменённые: {', '.join(plan.rehearsals_changed)}"
            lines.append(line + ".")
            if plan.rehearsals_archived:
                lines.append(f"Репетиции в архив: {', '.join(plan.rehearsals_archived)}.")
        if plan.kind == "psy" and plan.topics_without_digest:
            lines.append(f"Тем без разбора: {len(plan.topics_without_digest)} "
                         f"({_codes(plan.topics_without_digest, 8)}) — пойдут сразу с вопросов, "
                         f"пока разборы не загружены.")
        if plan.kind == "psy":
            f = plan.fragments
            lines.append(f"Фрагменты источников: целиком {len(f['ok'])}, сжаты по ключу "
                         f"{len(f['truncated'])}, не найдены {len(f['missing'])}.")
            if f.get("n/a"):
                lines.append(f"Без источника в папке источников: {len(f['n/a'])} — эти позиции "
                             f"показываются и проверяются по ключу, кнопки «Источник» у них нет.")
            if f["missing"]:
                lines.append("Без фрагмента позиция импортируется, но не показывается. Причины:")
                reasons: dict[str, list[str]] = {}
                for code in f["missing"]:
                    for why in plan.fragment_problems.get(code, []):
                        reasons.setdefault(why, []).append(code)
                for why, codes in sorted(reasons.items(), key=lambda kv: -len(kv[1]))[:max_lines]:
                    lines.append(f"— {why}: {len(codes)} ({_codes(codes, 5)})")
    if plan.issues.warnings:
        lines.append(f"Предупреждения: {len(plan.issues.warnings)}.")
        lines += _issue_lines(plan.issues.warnings, max_lines)
    return "\n".join(lines)


def import_file(importer: Importer, path: Path, apply: bool) -> tuple[ImportPlan, int | None]:
    plan = importer.analyze(path.name, path.read_bytes())
    version = importer.apply(plan) if apply and plan.ok else None
    return plan, version
