"""Модуль 5 — академическая ветка: единицы на узнавание (банк модуля 5, раздел 1; карта пути, раздел 6).

Шкала: знакомство в предложении (карточка с термином, значением, предложением и переводом —
сначала материал), затем узнавание по интервалам сетки: бот показывает предложение с термином,
Ученик пишет значение по-русски. Код сверяет со «Значением» и «Допустимыми значениями» после
нормализации; при несовпадении решает дешёвая модель по ключу и переводу предложения.
Единица закрыта после интервала 30 дней без срыва. В блоке 3 узнавание чередуется
с «Разбором предложения»: подлежащее, сказуемое и перевод, ключ — «Разбор» и «Перевод».
Своя очередь, лимит нового (около пяти в день) и потолок — настройки бота.
Доля английского времени: четверть до конца модуля 1, дальше треть (банк модуля 5, раздел 1).
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from ..clock import to_iso
from ..enums import Rating
from ..scheduler import Scheduler

CLOSE_AFTER_DAYS = 30


def normalize_ru(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    t = re.sub(r"[«»\"“”„()\[\]]", " ", t)
    t = re.sub(r"[^\w\s-]", " ", t).replace("-", " ")
    return " ".join(t.split())


@dataclass
class TermTask:
    term_id: int
    code: str
    kind: str                 # intro, term, parse
    purpose: str              # new, review
    term: str
    sentence: str
    meaning: str = ""
    translation: str = ""
    analysis: str = ""
    section: str = ""

    def dump(self) -> dict:
        return asdict(self)


class Academic:
    def __init__(self, conn: sqlite3.Connection, scheduler: Scheduler, settings) -> None:
        self.conn = conn
        self.sched = scheduler
        self.settings = settings

    # доля и лимиты

    def share(self) -> float:
        from .modules import Modules
        m = Modules(self.conn)
        from .exams import Exams
        if Exams(self.conn).opened():
            return float(self.settings.get("acad_share_runs"))
        if not m.is_open("EN1") or m.current() not in (None, "EN1"):
            return float(self.settings.get("acad_share"))
        return float(self.settings.get("acad_share_m1"))

    def new_left(self, day: date) -> int:
        if self.settings.is_paused(day):
            return 0
        used = self.conn.execute("SELECT new_terms FROM days WHERE study_date = ?", (day.isoformat(),)).fetchone()
        return max(0, int(self.settings.get("new_terms")) - (used[0] if used else 0))

    def spend_new(self, day: date) -> None:
        self.conn.execute("INSERT OR IGNORE INTO days (study_date) VALUES (?)", (day.isoformat(),))
        self.conn.execute("UPDATE days SET new_terms = new_terms + 1 WHERE study_date = ?", (day.isoformat(),))

    def deficit_sec(self, day: date, block_sec: int) -> int:
        """Сколько из английского блока отдать академической ветке, чтобы держать долю."""
        row = self.conn.execute("SELECT sec_en, sec_en_acad FROM days WHERE study_date = ?",
                                (day.isoformat(),)).fetchone()
        en, acad = (row[0], row[1]) if row else (0, 0)
        want = self.share() * (en + block_sec) - acad
        return int(max(0, min(block_sec, want)))

    def week_deficit_sec(self, day: date, block_sec: int) -> int:
        """Недобор доли за семь дней по сегодня. Текст Ч длиннее дневной доли ветки, поэтому для него
        доля держится в среднем за неделю, а не внутри дня (иначе текст встаёт почти каждый вечер)."""
        en, acad = self.conn.execute(
            "SELECT COALESCE(sum(sec_en), 0), COALESCE(sum(sec_en_acad), 0) FROM days WHERE study_date BETWEEN ? AND ?",
            ((day - timedelta(days=6)).isoformat(), day.isoformat())).fetchone()
        return int(self.share() * (en + block_sec) - acad)

    def new_phrases(self, day: date, limit: int, exclude: set[int] = frozenset()) -> list[int]:
        """Фразы блоков 7–8 модуля 5 для знакомства: с модуля 2, своим лимитом."""
        from .modules import Modules
        if Modules(self.conn).is_open("EN1") or self.settings.is_paused(day):
            return []
        used = self.conn.execute("SELECT new_acad_phrases FROM days WHERE study_date = ?",
                                 (day.isoformat(),)).fetchone()
        left = int(self.settings.get("new_acad_phrases")) - (used[0] if used else 0)
        if left <= 0:
            return []
        rows = self.conn.execute(
            "SELECT i.id FROM items i LEFT JOIN item_state s ON s.item_id = i.id WHERE i.track = 'en' "
            "AND i.area = 'EN5' AND i.archived = 0 AND i.kind = 'phrase' AND COALESCE(s.stage, 0) = 0 "
            "ORDER BY i.sort_key LIMIT ?", (min(left, limit) + len(exclude),)).fetchall()
        return [r[0] for r in rows if r[0] not in exclude][:min(left, limit)]

    # выбор

    def _task(self, r: sqlite3.Row, purpose: str) -> TermTask:
        kind = "intro" if purpose == "new" else "term"
        if purpose == "review" and r["block"] == "3" and r["analysis"] and (r["reps"] or 0) % 2 == 1:
            kind = "parse"
        return TermTask(r["id"], r["code"], kind, purpose, r["term"], r["sentence"], r["meaning"],
                        r["translation"], r["analysis"] or "", r["section"] or "")

    def due(self, day: date, limit: int, exclude: set[int] = frozenset()) -> list[TermTask]:
        cap = int(self.settings.get("review_cap_terms"))
        done = self.conn.execute("SELECT count(*) FROM term_reviews WHERE study_date = ? AND kind != 'intro'",
                                 (day.isoformat(),)).fetchone()[0]
        left = max(0, min(limit, cap - done))
        rows = self.conn.execute(
            "SELECT t.*, s.reps FROM terms t JOIN term_state s ON s.term_id = t.id WHERE t.archived = 0 "
            "AND s.step IS NOT NULL AND s.closed = 0 AND s.due_date <= ? ORDER BY s.due_date, t.sort_key",
            (day.isoformat(),)).fetchall()
        return [self._task(r, "review") for r in rows if r["id"] not in exclude][:left]

    def new(self, day: date, limit: int, exclude: set[int] = frozenset()) -> list[TermTask]:
        n = min(limit, self.new_left(day))
        if n <= 0:
            return []
        rows = self.conn.execute(
            "SELECT t.*, 0 AS reps FROM terms t LEFT JOIN term_state s ON s.term_id = t.id WHERE t.archived = 0 "
            "AND s.term_id IS NULL ORDER BY t.sort_key LIMIT ?", (n + len(exclude),)).fetchall()
        return [self._task(r, "new") for r in rows if r["id"] not in exclude][:n]

    # проверка кодом

    def check_code(self, term_id: int, answer: str) -> bool:
        t = self.conn.execute("SELECT meaning, alt_json FROM terms WHERE id = ?", (term_id,)).fetchone()
        a = normalize_ru(answer)
        keys = [t["meaning"]] + json.loads(t["alt_json"])
        return bool(a) and any(a == normalize_ru(k) for k in keys)

    # запись

    def record(self, task: TermTask, rating: Rating | None, *, judge: str, answer: str | None, verdict: dict | None,
               day: date, now: datetime, session_id: int | None, elapsed: int = 0) -> dict:
        self.conn.execute("INSERT OR IGNORE INTO term_state (term_id) VALUES (?)", (task.term_id,))
        st = self.conn.execute("SELECT * FROM term_state WHERE term_id = ?", (task.term_id,)).fetchone()
        before, after, closed = st["step"], st["step"], False
        grid = self.sched.grid
        if task.purpose == "new":
            after = 0
            self.conn.execute("UPDATE term_state SET step = 0, due_date = ?, introduced_at = ?, last_review_at = ? "
                              "WHERE term_id = ?", (self.sched.due_for(0, day).isoformat(), to_iso(now), to_iso(now),
                                                    task.term_id))
            self.spend_new(day)
        elif rating is not None and before is not None:
            if rating == Rating.AGAIN:
                after, lapse = 0, 1
            elif rating == Rating.HARD:
                after, lapse = before, 0
            else:
                after, lapse = min(before + 1, len(grid) - 1), 0
                closed = grid[before] >= CLOSE_AFTER_DAYS
            self.conn.execute(
                "UPDATE term_state SET step = ?, due_date = ?, reps = reps + 1, lapses = lapses + ?, closed = ?, "
                "last_review_at = ? WHERE term_id = ?",
                (after, self.sched.due_for(after, day).isoformat(), lapse, int(closed), to_iso(now), task.term_id))
        name = "shown" if task.purpose == "new" else {Rating.GOOD: "correct", Rating.HARD: "partial",
                                                      Rating.AGAIN: "wrong"}.get(rating, "deferred")
        rid = self.conn.execute(
            "INSERT INTO term_reviews (term_id, session_id, ts, study_date, kind, answer, verdict, rating, judge, "
            "step_before, step_after, elapsed_sec, verdict_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task.term_id, session_id, to_iso(now), day.isoformat(), task.kind, answer, name,
             rating.value if rating else None, judge, before, after, elapsed,
             json.dumps(verdict, ensure_ascii=False) if verdict else None)).lastrowid
        return {"review_id": rid, "closed": closed}

    def counts(self) -> dict[str, int]:
        r = self.conn.execute(
            "SELECT count(*), sum(s.term_id IS NOT NULL), sum(COALESCE(s.closed, 0)) FROM terms t "
            "LEFT JOIN term_state s ON s.term_id = t.id WHERE t.archived = 0").fetchone()
        return {"total": r[0] or 0, "introduced": r[1] or 0, "closed": r[2] or 0}


# тексты Ч (банк блоков 5–6, раздел 1)

TEXT_REPEAT = (7, 30)          # повтор через 7 и 30 дней
TEXT_CLOSE_SHARE = 0.8         # на повторе через 30 дней: верных ответов не меньше 80 %
TEXT_CLOSE_THESES = 4


def numbered(text: str) -> list[str]:
    """«1. … 2. …» → список пунктов; пункт может занимать несколько строк."""
    items: list[str] = []
    for line in (text or "").split("\n"):
        m = re.match(r"^\s*(\d+)\.\s+(.*)$", line)
        if m:
            items.append(m.group(2).strip())
        elif items and line.strip():
            items[-1] += " " + line.strip()
    return items


def paragraph_of(text: str, bounds: str) -> str:
    """«От «The clinical criteria developed» до «would be desirable.»» → отрывок текста."""
    m = re.match(r"^От «(.+?)» до «(.+?)»", (bounds or "").strip())
    if not m:
        return ""
    a, b = text.find(m.group(1)), -1
    if a < 0:
        return ""
    b = text.find(m.group(2), a)
    return text[a:b + len(m.group(2))].strip() if b >= 0 else ""


class Texts:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def opened(self) -> bool:
        """Тексты блоков 5–6 открываются с модуля 2 (банк модуля 5, раздел 1)."""
        from .modules import Modules
        return not Modules(self.conn).is_open("EN1")

    def record(self, code: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM records WHERE code = ? AND kind = 'text' AND archived = 0",
                                 (code,)).fetchone()

    def fields(self, code: str) -> dict:
        r = self.record(code)
        return json.loads(r["fields_json"]) if r else {}

    def next(self, day: date) -> tuple[str, int] | None:
        """(код, этап): сначала повтор по сроку, затем новый текст по порядку."""
        due = self.conn.execute(
            "SELECT s.code, s.stage FROM text_state s JOIN records r ON r.code = s.code WHERE r.archived = 0 "
            "AND s.closed = 0 AND s.stage >= 1 AND s.due_date <= ? ORDER BY s.due_date, r.sort_key LIMIT 1",
            (day.isoformat(),)).fetchone()
        if due:
            return due[0], due[1]
        if not self.opened():
            return None
        row = self.conn.execute(
            "SELECT r.code FROM records r LEFT JOIN text_state s ON s.code = r.code WHERE r.kind = 'text' "
            "AND r.archived = 0 AND s.code IS NULL ORDER BY r.sort_key LIMIT 1").fetchone()
        return (row[0], 0) if row else None

    def finish(self, code: str, stage: int, result: dict, day: date, now: datetime) -> str:
        """Итог работы с текстом. Возвращает: repeat7, repeat30, closed, again7."""
        from datetime import timedelta
        self.conn.execute("INSERT OR IGNORE INTO text_state (code) VALUES (?)", (code,))
        ok = (result.get("share", 0) >= TEXT_CLOSE_SHARE and result.get("paragraph") == "passed"
              and (result.get("theses_ok") or 0) >= TEXT_CLOSE_THESES)
        if stage == 0:
            new_stage, days, event = 1, TEXT_REPEAT[0], "repeat7"
        elif stage == 1:
            new_stage, days, event = 2, TEXT_REPEAT[1], "repeat30"
        elif ok:
            new_stage, days, event = 2, None, "closed"
        else:
            new_stage, days, event = 2, TEXT_REPEAT[0], "again7"
        self.conn.execute(
            "UPDATE text_state SET stage = ?, due_date = ?, attempts = attempts + 1, closed = ?, last_json = ?, "
            "updated_at = ? WHERE code = ?",
            (new_stage, (day + timedelta(days=days)).isoformat() if days else None, int(event == "closed"),
             json.dumps(result, ensure_ascii=False), to_iso(now), code))
        return event
