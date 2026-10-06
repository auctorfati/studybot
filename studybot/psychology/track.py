"""Трек психологии: темы по порядку, срез сверху вниз, уровни, разбор.

Порядок дисциплин: патопсихология, психопатология,
нейропсихология, классификации, психоаналитическая диагностика, отношения,
сексология; внутри — темы в порядке карты. Новое берётся из первой по порядку
темы, где оно сейчас есть: следующая тема начинается, когда у текущей нового
не осталось (уровни ждут повторений), а не когда она закрыта.

Сначала объяснение (настройка psy_digest_first): новая
тема с разбором открывается чтением разбора по частям, и только после него — срез.
Срез темы: верхний уровень — виньетка ядра, иначе два различения ядра.
Пройден — тема закрыта, карточки ядра уходят в повторение с шага 14 дней,
ученику предлагается разбор темы. Не пройден — разбор по частям, затем два
открытых вопроса ядра: пройдены — изучаются только уровни 3–4, затем виньетка
снова; не пройдены — тема целиком с карточек. Задания среза в лимит нового
не входят. «Частично» в срезе — не пройдено.

Изучение: уровень открывается, когда каждая позиция ядра предыдущего уровня
хотя бы раз сдана в повторении (шаг сетки ≥ 1). Тема закрывается, когда сдан
её целевой уровень (уровень из карты, но не выше имеющегося): виньетка или все
позиции уровня. Резерв закрытых тем идёт как новое после ядра текущей темы.
Редкое повторение закрытой темы — сама сетка: 30, 60, 120, 240 дней.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from ..clock import to_iso
from ..enums import Judge, Mode, Rating, Track
from ..scheduler import Scheduler

AREA_ORDER = ("PATO", "PSY", "NEURO", "KLIN", "PSAN", "REL", "SEX")
UNITS = {"card": 1, "open": 3, "distinction": 2, "vignette": 6}
LEVEL = {"card": 1, "open": 2, "distinction": 3, "vignette": 4}
CONFIRM_STEP = 3              # шаг сетки 14 дней — подтверждение среза
SELF_SAMPLE = 5               # каждая пятая карточка набирается текстом
SELF_SAMPLE_STRICT = 2        # при расхождении чаще трети за неделю — каждая вторая
MISMATCH_SHARE = 1 / 3
VERDICT = {"passed": Rating.GOOD, "partial": Rating.HARD, "failed": Rating.AGAIN}


@dataclass
class PsyTask:
    item_id: int
    code: str
    kind: str                  # card, open, distinction, vignette
    topic: str
    purpose: str               # probe, new, review
    answer_mode: str           # self — самооценка; typed — карточка набором; text — ответ текстом
    prompt: str
    source_ref: str = ""
    questions: list[str] = field(default_factory=list)
    level: int = 1
    title: str = ""            # название темы

    def dump(self) -> dict:
        return asdict(self)


def area_rank(area: str) -> int:
    return AREA_ORDER.index(area) if area in AREA_ORDER else len(AREA_ORDER)


class PsyTrack:
    def __init__(self, conn: sqlite3.Connection, scheduler: Scheduler, settings=None) -> None:
        self.conn = conn
        self.sched = scheduler
        self.settings = settings

    def _setting(self, key: str, default):
        return default if self.settings is None else self.settings.get(key)

    def confirm_step(self) -> int:
        return self.sched.step_of(self._setting("probe_confirm_days", 14))

    def digest_first(self) -> bool:
        return True if self.settings is None else bool(self.settings.get("psy_digest_first"))

    def _digest_pending(self, code: str) -> bool:
        has = self.conn.execute("SELECT 1 FROM digests WHERE topic_code = ?", (code,)).fetchone()
        read = self.conn.execute("SELECT status FROM digest_reads WHERE topic_code = ?", (code,)).fetchone()
        return bool(has) and not (read and read[0] == "read")

    # темы

    def topics(self) -> list[sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM topics WHERE archived = 0").fetchall()
        return sorted(rows, key=lambda r: (area_rank(r["area"]), r["sort_key"]))

    def state(self, code: str) -> sqlite3.Row:
        self.conn.execute("INSERT OR IGNORE INTO topic_state (topic_code) VALUES (?)", (code,))
        return self.conn.execute("SELECT * FROM topic_state WHERE topic_code = ?", (code,)).fetchone()

    def probe(self, code: str) -> dict:
        return json.loads(self.state(code)["probe_json"])

    def _set(self, code: str, now: datetime, **kw) -> None:
        self.state(code)
        if "probe" in kw:
            kw["probe_json"] = json.dumps(kw.pop("probe"), ensure_ascii=False)
        sets = ", ".join(f"{k} = ?" for k in kw)
        self.conn.execute(f"UPDATE topic_state SET {sets}, updated_at = ? WHERE topic_code = ?",
                          (*kw.values(), to_iso(now), code))

    def items(self, topic: str, core: bool | None = True) -> list[sqlite3.Row]:
        """Позиции темы, которые можно показывать (фрагмент найден или источника нет), по уровню и коду."""
        q = ("SELECT i.*, s.step, s.due_date, s.introduced_at, s.deferred_until FROM items i "
             "LEFT JOIN item_state s ON s.item_id = i.id WHERE i.track = 'psy' AND i.archived = 0 "
             "AND i.unit = ? AND i.fragment_status != 'missing'")
        args: list = [topic]
        if core is not None:
            q += " AND i.is_core = ?"
            args.append(int(core))
        return self.conn.execute(q + " ORDER BY i.level, i.sort_key", args).fetchall()

    def target_level(self, topic: sqlite3.Row) -> int:
        levels = {r["level"] for r in self.items(topic["code"])}
        if not levels:
            return 0
        return min(topic["target_level"], max(levels))

    # срез сверху вниз

    def _probe_top(self, topic: str) -> tuple[str, list[int]]:
        items = self.items(topic)
        vig = [r["id"] for r in items if r["kind"] == "vignette"]
        if vig:
            return "vignette", vig[:1]
        dist = [r["id"] for r in items if r["kind"] == "distinction"]
        if dist:
            return "distinction", dist[:2]
        opens = [r["id"] for r in items if r["kind"] == "open"]
        if opens:
            return "open", opens[:2]          # выше открытых вопросов уровней нет: они и есть верх
        return "none", []

    def _open_items(self, topic: str) -> list[int]:
        return [r["id"] for r in self.items(topic) if r["kind"] == "open"][:2]

    def start_topic(self, code: str, now: datetime) -> None:
        if self.digest_first() and self._digest_pending(code):
            # сначала материал: разбор, потом проверка
            self._set(code, now, state="probe", level=0,
                      probe={"phase": "digest", "after_digest": "top", "kind": "", "items": [], "done": {}})
            return
        self._start_top(code, now)

    def _start_top(self, code: str, now: datetime) -> None:
        kind, ids = self._probe_top(code)
        if ids:
            self._set(code, now, state="probe", level=0, probe={"phase": "top", "kind": kind, "items": ids,
                                                                 "done": {}})
        else:
            self._after_top_failed(code, now, {"phase": "top", "kind": kind, "items": [], "done": {}})

    def _after_top_failed(self, code: str, now: datetime, pr: dict) -> None:
        """Верх не пройден (или его нет): сначала разбор, затем открытые вопросы — если верхом
        были не они; иначе тема целиком с карточек."""
        pr["after_digest"] = "open" if pr.get("kind") in ("vignette", "distinction") else "study1"
        if self._digest_pending(code):
            pr.update(phase="digest")
            self._set(code, now, state="probe", probe=pr)
            return
        self._after_digest(code, now, pr)

    def _after_digest(self, code: str, now: datetime, pr: dict) -> None:
        if pr.get("after_digest") == "top":
            self._start_top(code, now)
        elif pr.get("after_digest") == "study1":
            self._study(code, now, 1, pr)
        else:
            self._to_open(code, now, pr)

    def _to_open(self, code: str, now: datetime, pr: dict) -> None:
        ids = self._open_items(code)
        if ids:
            pr.update(phase="open", items=ids, done={})
            self._set(code, now, state="probe", probe=pr)
        else:
            self._study(code, now, 1, pr)

    def _study(self, code: str, now: datetime, from_level: int, pr: dict) -> None:
        pr.update(phase="done", from_level=from_level)
        self._set(code, now, state="study", level=from_level - 1, probe=pr)

    def digest_done(self, code: str, now: datetime) -> None:
        """Разбор среза прочитан: дальше открытые вопросы."""
        st = self.state(code)
        pr = json.loads(st["probe_json"])
        if st["state"] == "probe" and pr.get("phase") == "digest":
            self._after_digest(code, now, pr)

    def record_probe(self, code: str, item_id: int, rating: Rating, day: date, now: datetime) -> str | None:
        """Итог задания среза. Возвращает событие: closed, digest, open, study1, study3 или None."""
        st = self.state(code)
        pr = json.loads(st["probe_json"])
        if st["state"] != "probe" or item_id not in pr.get("items", []):
            return None
        pr["done"][str(item_id)] = rating.value
        if len(pr["done"]) < len(pr["items"]):
            self._set(code, now, probe=pr)
            return None
        passed = all(v == Rating.GOOD.value for v in pr["done"].values())
        if pr["phase"] == "top":
            if passed:
                self.close(code, day, now, via="probe")
                return "closed"
            self._after_top_failed(code, now, pr)
            phase = json.loads(self.state(code)["probe_json"])["phase"]
            return {"digest": "digest", "open": "open"}.get(phase, "study1")
        if pr["phase"] == "open":
            if passed:
                topic = self.conn.execute("SELECT * FROM topics WHERE code = ?", (code,)).fetchone()
                if self.target_level(topic) <= 2:
                    # целевой уровень темы — открытые вопросы, и они сданы в срезе: тема закрыта
                    self.close(code, day, now, via="open")
                    return "closed"
                self._study(code, now, 3, pr)
                return "study3"
            self._study(code, now, 1, pr)
            return "study1"
        return None

    def close(self, code: str, day: date, now: datetime, via: str) -> None:
        """Тема закрыта: карточки ядра вне сетки уходят в повторение с шага 14 дней."""
        for r in self.items(code):
            if r["kind"] == "card" and r["step"] is None:
                self.sched.enter_grid(r["id"], day, now, step=self.confirm_step())
                self.conn.execute("UPDATE item_state SET introduced_at = COALESCE(introduced_at, ?) "
                                  "WHERE item_id = ?", (to_iso(now), r["id"]))
        st = self.state(code)
        pr = json.loads(st["probe_json"])
        pr["closed_via"] = via
        self._set(code, now, state="closed", level=4, probe=pr)

    # изучение по уровням

    def study_levels(self, code: str) -> list[int]:
        st = self.state(code)
        pr = json.loads(st["probe_json"])
        start = pr.get("from_level", 1)
        return [lv for lv in (1, 2, 3, 4) if lv >= start]

    def open_level(self, code: str) -> int | None:
        """Уровень, позиции которого сейчас можно вводить как новое; None — ждать повторений."""
        items = [r for r in self.items(code) if r["level"] in self.study_levels(code)]
        for lv in self.study_levels(code):
            mine = [r for r in items if r["level"] == lv]
            if not mine:
                continue
            if any(r["introduced_at"] is None for r in mine):
                return lv
            if any((r["step"] or 0) < 1 for r in mine):
                return None
        return None

    def maybe_close(self, code: str, day: date, now: datetime) -> bool:
        st = self.state(code)
        if st["state"] != "study":
            return False
        topic = self.conn.execute("SELECT * FROM topics WHERE code = ?", (code,)).fetchone()
        target = self.target_level(topic)
        if target and target < self.study_levels(code)[0]:
            # целевой уровень ниже изучаемых — он уже сдан в срезе (иначе тема ждала бы вечно)
            self.close(code, day, now, via="open")
            return True
        mine = [r for r in self.items(code) if r["level"] == target and r["level"] in self.study_levels(code)]
        if target and mine and all((r["step"] or 0) >= 1 for r in mine):
            self.close(code, day, now, via="study")
            return True
        return False

    # выбор заданий

    def _task(self, r: sqlite3.Row, purpose: str, typed: bool = False) -> PsyTask:
        extra = json.loads(r["extra_json"] or "{}")
        title = self.conn.execute("SELECT title FROM topics WHERE code = ?", (r["unit"],)).fetchone()
        mode = "text"
        if r["kind"] == "card":
            mode = "typed" if typed else "self"
        return PsyTask(r["id"], r["code"], r["kind"], r["unit"], purpose, mode, r["prompt"],
                       r["source_ref"] or "", extra.get("questions", []), r["level"] or LEVEL[r["kind"]],
                       title[0] if title else "")

    def due(self, day: date, limit: int, exclude: set[int] = frozenset(),
            kinds: tuple[str, ...] | None = None, now: datetime | None = None) -> list[PsyTask]:
        rows = self.conn.execute(
            "SELECT i.* FROM items i JOIN item_state s ON s.item_id = i.id WHERE i.track = 'psy' "
            "AND i.archived = 0 AND s.step IS NOT NULL AND s.due_date <= ? AND (s.deferred_until IS NULL "
            "OR s.deferred_until <= ?) AND i.fragment_status != 'missing' ORDER BY s.due_date, i.level, i.id",
            (day.isoformat(), day.isoformat())).fetchall()
        out = []
        for r in rows:
            if r["id"] in exclude or (kinds and r["kind"] not in kinds):
                continue
            out.append(self._task(r, "review", typed=r["kind"] == "card" and self.typed_turn(now, len(out))))
            if len(out) >= limit:
                break
        return out

    def next_new(self, budget_units: int, now: datetime, exclude: set[int] = frozenset(),
                 allow_vignette: bool = True, skip_digests: set[str] = frozenset()
                 ) -> tuple[list[PsyTask], list[str]]:
        """Новое и срез по порядку тем. Возвращает задания и темы, ждущие чтения разбора.

        Разбор — часть темы: на теме, чей разбор ждёт чтения, обход останавливается,
        если разбор не отложен в этом блоке (skip_digests)."""
        tasks: list[PsyTask] = []
        digests: list[str] = []
        units = 0
        for t in self.topics():
            code = t["code"]
            if not self.items(code, core=None):
                continue
            st = self.state(code)
            if st["state"] == "not_started":
                self.start_topic(code, now)
                st = self.state(code)
            if st["state"] == "probe":
                pr = json.loads(st["probe_json"])
                if pr["phase"] == "digest":
                    digests.append(code)
                    if code in skip_digests:
                        continue
                    return tasks, digests
                pending = [i for i in pr["items"] if str(i) not in pr["done"] and i not in exclude]
                rows = [self.conn.execute("SELECT * FROM items WHERE id = ?", (i,)).fetchone() for i in pending]
                rows = [r for r in rows if allow_vignette or r["kind"] != "vignette"]
                tasks += [self._task(r, "probe") for r in rows]
                if rows:
                    return tasks, digests                     # срез темы — до следующей темы
                if pending:
                    continue                                  # виньетка не влезла: тема ждёт длинного блока
                continue
            if st["state"] != "study":
                continue
            lv = self.open_level(code)
            if lv is None:
                continue
            for r in self.items(code):
                if r["level"] != lv or r["introduced_at"] is not None or r["id"] in exclude:
                    continue
                if r["kind"] == "vignette" and not allow_vignette:
                    continue
                cost = UNITS[r["kind"]]
                if units + cost > budget_units:
                    return tasks, digests
                units += cost
                tasks.append(self._task(r, "new", typed=r["kind"] == "card"
                                        and self.typed_turn(now, len(tasks))))
            if tasks:
                return tasks, digests
        # резерв закрытых тем — когда ядро текущих тем ждёт повторений
        for t in self.topics():
            if self.state(t["code"])["state"] != "closed":
                continue
            for r in self.items(t["code"], core=False):
                if r["introduced_at"] is not None or r["id"] in exclude:
                    continue
                if r["kind"] == "vignette" and not allow_vignette:
                    continue
                cost = UNITS[r["kind"]]
                if units + cost > budget_units:
                    return tasks, digests
                units += cost
                tasks.append(self._task(r, "new"))
        return tasks, digests

    # самооценка карточек

    def typed_turn(self, now: datetime | None, ahead: int = 0) -> bool:
        """Карточка набирается текстом: каждая пятая, при частых расхождениях — каждая вторая.
        ahead — сколько карточек уже поставлено в план до этой."""
        n = self.conn.execute("SELECT count(*) FROM reviews r JOIN items i ON i.id = r.item_id "
                              "WHERE i.kind = 'card' AND r.format IN ('self', 'typed')").fetchone()[0]
        strict = now is not None and self.mismatch_high(now)
        every = (self._setting("selfcheck_every_strict", SELF_SAMPLE_STRICT) if strict
                 else self._setting("selfcheck_every", SELF_SAMPLE))
        return (n + ahead + 1) % every == 0

    def mismatch_high(self, now: datetime, days: int = 7) -> bool:
        rows = self.conn.execute(
            "SELECT verdict_json FROM reviews WHERE format = 'typed' AND verdict_json IS NOT NULL "
            "AND ts >= ?", (to_iso(now - timedelta(days=days)),)).fetchall()
        checks = [json.loads(r[0]).get("self_mismatch") for r in rows]
        checks = [c for c in checks if c is not None]
        return bool(checks) and sum(checks) / len(checks) > self._setting("selfcheck_mismatch_share", MISMATCH_SHARE)

    # запись ответа

    def record(self, task: PsyTask, rating: Rating | None, *, judge: Judge, mode: Mode, answer: str | None,
               verdict: dict | None, day: date, now: datetime, session_id: int | None,
               elapsed: int = 0, raw: int = 0, schedules: bool = True, fmt: str | None = None) -> dict:
        """Записать ответ и сдвинуть состояние. Возвращает события: review_id, topic_event, closed."""
        self.sched.ensure_state(task.item_id)
        st = self.conn.execute("SELECT step FROM item_state WHERE item_id = ?", (task.item_id,)).fetchone()
        step_before = st["step"]
        out: dict = {"topic_event": None, "closed": False}
        step_after = step_before
        if rating is not None and schedules:
            if task.purpose == "review" and step_before is not None:
                step_after = self.sched.grade(task.item_id, rating, day, now).step_after
            elif task.purpose == "new":
                step_after = 0                    # первый показ — знакомство; уровень выше откроет повтор
                self.sched.enter_grid(task.item_id, day, now, step=step_after)
                self.conn.execute("UPDATE item_state SET introduced_at = ?, reps = reps + 1 WHERE item_id = ?",
                                  (to_iso(now), task.item_id))
        verdict_name = {Rating.GOOD: "correct", Rating.HARD: "partial", Rating.AGAIN: "wrong"}.get(
            rating, "deferred")
        rid = self.conn.execute(
            "INSERT INTO reviews (item_id, session_id, ts, study_date, format, mode, answer, verdict, rating, judge, "
            "schedules, step_before, step_after, elapsed_sec, raw_sec, verdict_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task.item_id, session_id, to_iso(now), day.isoformat(), fmt or task.answer_mode, mode.value, answer,
             verdict_name, rating.value if rating else None, judge.value, int(schedules and rating is not None),
             step_before, step_after, elapsed, raw, json.dumps(verdict, ensure_ascii=False) if verdict else None)
        ).lastrowid
        out["review_id"] = rid
        if rating is None:
            return out
        if task.purpose == "probe":
            out["topic_event"] = self.record_probe(task.topic, task.item_id, rating, day, now)
            out["closed"] = out["topic_event"] == "closed"
        else:
            out["closed"] = self.maybe_close(task.topic, day, now)
            if out["closed"]:
                out["topic_event"] = "closed"
        return out

    # сводка

    def state_counts(self) -> dict[str, int]:
        counts = {"not_started": 0, "probe": 0, "study": 0, "closed": 0}
        for t in self.topics():
            st = self.conn.execute("SELECT state FROM topic_state WHERE topic_code = ?", (t["code"],)).fetchone()
            key = st[0] if st else "not_started"
            counts["closed" if key == "rare" else key] += 1
        return counts
