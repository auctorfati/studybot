"""Лестница фразы (программа модуля 1, раздел 6).

Ступени в item_state.stage: 0 — не введена; 2 — конструкция; 3 — на слух;
4 — своя речь. Ступень 1 проходится за один показ при знакомстве:
аудио и текст, повтор, затем одна трансформация по связям. После знакомства
фраза на ступени 2. Ступени 2 и 3 — по двум верным подряд, показ на следующий
день. Ступень 4 идёт по сетке интервалов; ответ текстом её не двигает.
Фраза закрыта, когда выдержан интервал 30 дней без срыва и она прозвучала
в диалоге. Переходы решает код; вердикт по ответу приходит готовым.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..clock import to_iso
from ..enums import Judge, Mode, Rating, Track
from ..progress import snapshot
from ..review_queue import ReviewQueue
from ..scheduler import Change, Scheduler
from ..settings import Settings
from .graph import INSTRUCTION, QUESTION, LinkGraph, Node, phrase_kind, transform_label
from .normalize import contains_phrase, matches

# Форматы заданий (программа, раздел 7)
INTRO = "intro"              # слушай и повторяй, знакомство
TRANSFORM = "transform"      # трансформация по связи
ASK = "ask"                  # вопрос к ответу
REPEAT = "repeat"            # ступень 2 у фразы без связей
HEAR_ANSWER = "hear_answer"  # вопрос на слух, ответ по смыслу
HEAR_REPEAT = "hear_repeat"  # утверждение на слух, повтор голосом
DICTATION = "dictation"      # утверждение на слух, запись
READ_ANSWER = "read_answer"  # запасной: вопрос текстом, пока нет аудио
RU_EN = "ru_en"              # с русского на английский
FIND_ERROR = "find_error"    # свой неверный ответ из журнала


@dataclass
class Task:
    item_id: int
    code: str
    format: str
    stage: int
    instruction: str                     # что сделать, по-русски
    prompt_ru: str | None = None         # русская подсказка
    shown_en: str | None = None          # английский текст на экране
    audio_item: int | None = None        # чьё аудио проиграть
    needs_audio: bool = False
    voice: bool = False                  # ответ ожидается голосом
    expected: list[str] = field(default_factory=list)   # целевые фразы (любая засчитывается)
    variants: list[str] = field(default_factory=list)
    by_meaning: bool = False             # при несовпадении — модель по смыслу
    schedules: bool = True               # двигает ли ответ лестницу или интервал
    notes: list[str] = field(default_factory=list)      # коды заметок показать после задания
    meta: dict = field(default_factory=dict)

    def code_match(self, answer: str) -> bool:
        """Проверка кодом: совпадение с любой ожидаемой фразой или вариантом."""
        cands = [(t, self.variants) for t in self.expected] + list(self.meta.get("extra_expected", []))
        return any(matches(answer, t, v) for t, v in cands)


@dataclass
class Outcome:
    stage_before: int
    stage_after: int
    change: Change | None = None
    closed: bool = False
    review_id: int | None = None


class EnglishTrack:
    def __init__(self, conn: sqlite3.Connection, settings: Settings,
                 scheduler: Scheduler, queue: ReviewQueue) -> None:
        self.conn = conn
        self.settings = settings
        self.scheduler = scheduler
        self.queue = queue
        self._graph: LinkGraph | None = None

    @property
    def graph(self) -> LinkGraph:
        if self._graph is None:
            self._graph = LinkGraph.load(self.conn)
        return self._graph

    def reload(self) -> None:
        """После импорта: граф связей строится заново."""
        self._graph = None

    # данные

    def _item(self, item_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    def _state(self, item_id: int) -> sqlite3.Row:
        self.scheduler.ensure_state(item_id)
        return self.conn.execute("SELECT * FROM item_state WHERE item_id = ?", (item_id,)).fetchone()

    def has_audio(self, item_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM audio_files WHERE item_id = ? LIMIT 1",
                                 (item_id,)).fetchone() is not None

    def stage(self, item_id: int) -> int:
        row = self.conn.execute("SELECT stage FROM item_state WHERE item_id = ?", (item_id,)).fetchone()
        return row[0] if row else 0

    # новое

    def next_new(self, day: date, limit: int | None = None) -> list[int]:
        """Следующие фразы к знакомству: по порядку банка, в пределах лимита дня."""
        budget = self.queue.new_budget(Track.EN, day)
        if limit is not None:
            budget = min(budget, limit)
        if budget <= 0:
            return []
        from .modules import Modules
        cond, areas = Modules(self.conn).sql_filter("i")
        rows = self.conn.execute(
            "SELECT i.id FROM items i LEFT JOIN item_state s ON s.item_id = i.id "
            "WHERE i.track = 'en' AND i.archived = 0 AND i.kind = 'phrase' AND " + cond +
            " AND COALESCE(s.stage, 0) = 0 ORDER BY i.sort_key LIMIT ?", (*areas, budget)).fetchall()
        return [r[0] for r in rows]

    def intro_tasks(self, item_id: int, mode: Mode) -> list[Task]:
        """Знакомство за один показ: повтор за аудио, затем одна трансформация по связям."""
        it = self._item(item_id)
        voice = mode == Mode.VOICE
        audio = self.has_audio(item_id)
        tasks = [Task(
            item_id, it["code"], INTRO, 1,
            instruction="Послушай и повтори вслух" if voice else "Прочитай и запомни",
            prompt_ru=it["prompt"], shown_en=it["answer"],
            audio_item=item_id if audio else None, voice=voice,
            expected=[it["answer"]], variants=json.loads(it["variants_json"]),
            schedules=False, notes=self._unseen_notes(it),
            meta={"intro": True, **self._lesson(it)},
        )]
        follow = self._transform(item_id, mode, intro=True)
        if follow is not None:
            follow.schedules = False
            tasks.append(follow)
        return tasks

    def _lesson(self, it: sqlite3.Row) -> dict:
        """Сначала материал: на карточке знакомства — план нового блока, правило целиком
        и произношение: бот учит, а не только проверяет."""
        out: dict = {}
        unseen = self._unseen_notes(it)
        if unseen:
            marks = ",".join("?" * len(unseen))
            out["rules"] = [dict(r) for r in self.conn.execute(
                f"SELECT code, title, body FROM notes WHERE track = 'en' AND code IN ({marks}) ORDER BY "
                f"CAST(substr(code, 2) AS INTEGER)", unseen)]
        if it["sound"]:
            out["sound"] = it["sound"]
        opened = self.conn.execute("SELECT 1 FROM en_blocks WHERE area = ? AND block = ? AND opened_at IS NOT NULL",
                                   (it["area"], it["unit"])).fetchone()
        if not opened:
            title = self.conn.execute("SELECT title FROM en_block_titles WHERE area = ? AND block = ?",
                                      (it["area"], it["unit"])).fetchone()
            from ..bot.rules import note_blocks
            codes = note_blocks(self.conn).get(((it["area"] or "EN1")[2:], it["unit"]), [])
            titles = []
            if codes:
                marks = ",".join("?" * len(codes))
                found = {r[0]: r[1] for r in self.conn.execute(
                    f"SELECT code, title FROM notes WHERE track = 'en' AND code IN ({marks})", codes)}
                titles = [f"{c} — {found[c]}" for c in codes if c in found]
            total = self.conn.execute("SELECT count(*) FROM items WHERE track = 'en' AND archived = 0 AND area = ? "
                                      "AND unit = ? AND kind = 'phrase'", (it["area"], it["unit"])).fetchone()[0]
            out["block_intro"] = {"module": (it["area"] or "EN1")[2:], "block": it["unit"],
                                  "title": title[0] if title else "", "rules": titles, "phrases": total}
        return out

    def _unseen_notes(self, it: sqlite3.Row) -> list[str]:
        codes = json.loads(it["notes_json"])
        if not codes:
            return []
        marks = ",".join("?" * len(codes))
        return [r[0] for r in self.conn.execute(
            f"SELECT code FROM notes WHERE track = 'en' AND shown_at IS NULL AND code IN ({marks}) "
            f"ORDER BY code", codes)]

    def mark_notes_shown(self, codes: list[str], now: datetime) -> None:
        for c in codes:
            self.conn.execute("UPDATE notes SET shown_at = ? WHERE track = 'en' AND code = ? "
                              "AND shown_at IS NULL", (to_iso(now), c))

    # задания по ступеням

    def _transform(self, item_id: int, mode: Mode, intro: bool = False) -> Task | None:
        """Трансформация: английская фраза одной единицы и русская подсказка другой.

        Если среди соседей есть введённые, текущая превращается в одного из них;
        если нет (и всегда при знакомстве) — сосед превращается в текущую, чтобы
        ответом была фраза, которую ученик уже слышал.
        Соседи чередуются по числу повторений, чтобы задания не повторялись.
        """
        g = self.graph
        me = g.nodes.get(item_id)
        if me is None:
            return None
        neigh = [n for n in g.neighbors(item_id) if n.kind == "phrase"]
        if not neigh:
            return None
        reps = self._state(item_id)["reps"]
        known = [n for n in neigh if self.stage(n.item_id) >= 2]
        if known and not intro:
            source, target = me, known[reps % len(known)]
        else:
            source, target = neigh[reps % len(neigh)], me
        label = "time" if g.is_time(source.item_id, target.item_id) else transform_label(source.target, target.target)
        fmt = ASK if label == QUESTION and phrase_kind(source.target) != QUESTION else TRANSFORM
        trow = self._item(target.item_id)
        return Task(
            item_id, me.code, fmt, 2,
            instruction=INSTRUCTION[label], prompt_ru=target.prompt, shown_en=source.target,
            voice=mode == Mode.VOICE, expected=[target.target],
            variants=json.loads(trow["variants_json"]),
            meta={"source": source.code, "target": target.code, "intro": intro},
        )

    def task_for(self, item_id: int, mode: Mode) -> Task | None:
        """Задание для повторения по текущей ступени."""
        it = self._item(item_id)
        st = self._state(item_id)
        stage = st["stage"]
        voice = mode == Mode.VOICE
        variants = json.loads(it["variants_json"])
        if stage == 2:
            t = self._transform(item_id, mode)
            if t is not None:
                return t
            return Task(item_id, it["code"], REPEAT, 2,
                        instruction="Повтори за аудио" if voice and self.has_audio(item_id)
                        else "Скажи по-английски",
                        prompt_ru=it["prompt"],
                        audio_item=item_id if voice and self.has_audio(item_id) else None,
                        shown_en=it["answer"] if voice and self.has_audio(item_id) else None,
                        voice=voice, expected=[it["answer"]], variants=variants)
        if stage == 3:
            return self._hearing(it, mode)
        if stage == 4:
            return Task(item_id, it["code"], RU_EN, 4, instruction="Скажи по-английски",
                        prompt_ru=it["prompt"], voice=voice, expected=[it["answer"]],
                        variants=variants, schedules=voice,
                        meta={} if voice else {"writing_only": True})
        return None

    def control_task(self, item_id: int, kind: str, mode: Mode) -> Task:
        """Задание контрольной точки: ru — с русского голосом, hear — вопрос блока на слух."""
        it = self._item(item_id)
        if kind == "hear":
            t = self._hearing(it, mode)
        else:
            t = Task(item_id, it["code"], RU_EN, 4, instruction="Скажи по-английски",
                     prompt_ru=it["prompt"], voice=mode == Mode.VOICE, expected=[it["answer"]],
                     variants=json.loads(it["variants_json"]))
        t.schedules = False
        t.meta["control"] = kind
        return t

    def _hearing(self, it: sqlite3.Row, mode: Mode) -> Task:
        """Ступень 3: аудио без текста. Без аудио — запасной формат текстом."""
        item_id, voice = it["id"], mode == Mode.VOICE
        audio = self.has_audio(item_id)
        variants = json.loads(it["variants_json"])
        if phrase_kind(it["answer"]) == QUESTION:
            answers = self.graph.answers_to(item_id)
            extra = [(n.target, json.loads(self._item(n.item_id)["variants_json"])) for n in answers]
            return Task(
                item_id, it["code"], HEAR_ANSWER if audio else READ_ANSWER, 3,
                instruction="Ответь на вопрос" + (" голосом" if voice else ""),
                shown_en=None if audio else it["answer"],
                audio_item=item_id if audio else None, needs_audio=audio, voice=voice,
                expected=[], by_meaning=True,
                meta={"question": it["answer"], "extra_expected": extra},
            )
        if not audio:
            return Task(item_id, it["code"], RU_EN, 3, instruction="Скажи по-английски",
                        prompt_ru=it["prompt"], voice=voice, expected=[it["answer"]],
                        variants=variants, meta={"no_audio": True})
        return Task(item_id, it["code"], HEAR_REPEAT if voice else DICTATION, 3,
                    instruction="Послушай и повтори" if voice else "Послушай и запиши",
                    audio_item=item_id, needs_audio=True, voice=voice,
                    expected=[it["answer"]], variants=variants)

    # запись результата

    def record(self, task: Task, correct: bool | None, *, judge: Judge, mode: Mode,
               day: date, now: datetime, answer: str | None = None,
               elapsed_sec: int = 0, raw_sec: int = 0, session_id: int | None = None,
               verdict_json: dict | None = None) -> Outcome:
        """Записать ответ и сдвинуть лестницу. correct=None — задание отложено без оценки."""
        st = self._state(task.item_id)
        snap = snapshot(self.conn, task.item_id)
        stage = st["stage"]
        out = Outcome(stage, stage)
        rating: Rating | None = None
        verdict = "deferred"
        schedules = 0

        if correct is not None:
            rating = Rating.GOOD if correct else Rating.AGAIN
            verdict = "correct" if correct else "wrong"

        if task.format == INTRO:
            verdict, rating = "shown", None
            self._introduce(task.item_id, day, now)
            self.mark_notes_shown(task.notes, now)
            out.stage_after = 2
        elif correct is None or self.is_practice(task):
            pass                                  # отложено, трансформация при знакомстве, зачёт, «найди ошибку»
        elif stage in (2, 3):
            schedules = 1
            out.stage_after = self._ladder(task.item_id, stage, st["streak"], correct, day, now)
        elif stage == 4:
            schedules = int(task.schedules)
            out.change = self.scheduler.grade(task.item_id, rating, day, now, schedules=bool(schedules))
            out.closed = self._maybe_close(task.item_id)

        out.review_id = self.conn.execute(
            "INSERT INTO reviews (item_id, session_id, ts, study_date, format, mode, answer, verdict, "
            "rating, judge, schedules, stage_before, stage_after, step_before, step_after, "
            "elapsed_sec, raw_sec, verdict_json, state_before_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task.item_id, session_id, to_iso(now), day.isoformat(), task.format, mode.value, answer,
             verdict, int(rating) if rating else None, judge.value, schedules,
             stage, out.stage_after,
             out.change.step_before if out.change else st["step"],
             out.change.step_after if out.change else st["step"],
             elapsed_sec, raw_sec,
             json.dumps(verdict_json, ensure_ascii=False) if verdict_json else None,
             snap)).lastrowid
        return out

    @staticmethod
    def is_practice(task: Task) -> bool:
        """Ответ учитывается, но лестницу и интервал не двигает."""
        return bool(task.meta.get("intro") or task.meta.get("control")) or task.format == FIND_ERROR

    def _introduce(self, item_id: int, day: date, now: datetime) -> None:
        st = self._state(item_id)
        if st["stage"] != 0:
            return
        self.conn.execute(
            "UPDATE item_state SET stage = 2, streak = 0, due_date = ?, introduced_at = ? WHERE item_id = ?",
            ((day + timedelta(days=1)).isoformat(), to_iso(now), item_id))
        it = self._item(item_id)
        if it["area"] == "EN5":
            # фразы блоков 7–8 модуля 5 — свой лимит академической ветки, не лимит разговорной
            self.conn.execute("INSERT OR IGNORE INTO days (study_date) VALUES (?)", (day.isoformat(),))
            self.conn.execute("UPDATE days SET new_acad_phrases = new_acad_phrases + 1 WHERE study_date = ?",
                              (day.isoformat(),))
        else:
            self.queue.spend_new(Track.EN, day)
        self._block_opened(it["area"], it["unit"], now)

    def _ladder(self, item_id: int, stage: int, streak: int, correct: bool,
                day: date, now: datetime) -> int:
        tomorrow = (day + timedelta(days=1)).isoformat()
        if not correct:
            self.conn.execute("UPDATE item_state SET streak = 0, due_date = ?, reps = reps + 1, "
                              "lapses = lapses + 1, last_review_at = ? WHERE item_id = ?",
                              (tomorrow, to_iso(now), item_id))
            return stage
        streak += 1
        if streak < 2:
            self.conn.execute("UPDATE item_state SET streak = ?, due_date = ?, reps = reps + 1, "
                              "last_review_at = ? WHERE item_id = ?",
                              (streak, tomorrow, to_iso(now), item_id))
            return stage
        new = stage + 1
        self.conn.execute("UPDATE item_state SET stage = ?, streak = 0, reps = reps + 1, "
                          "last_review_at = ?, due_date = ? WHERE item_id = ?",
                          (new, to_iso(now), tomorrow, item_id))
        if new == 4:
            self.scheduler.enter_grid(item_id, day, now)
        return new

    def _maybe_close(self, item_id: int) -> bool:
        st = self._state(item_id)
        if st["closed"] or not st["spoken_in_dialog"] or st["step"] is None:
            return False
        if st["step"] > self.scheduler.step_of(30):
            self.conn.execute("UPDATE item_state SET closed = 1 WHERE item_id = ?", (item_id,))
            return True
        return False

    # диалог

    def mark_spoken(self, utterances: list[str]) -> list[str]:
        """Отметить фразы, прозвучавшие в репликах ученика. Возвращает коды отмеченных."""
        rows = self.conn.execute(
            "SELECT i.id, i.code, i.answer, i.variants_json, i.extra_json FROM items i "
            "JOIN item_state s ON s.item_id = i.id WHERE i.track = 'en' AND i.archived = 0 "
            "AND i.kind = 'phrase' AND s.stage >= 2 AND s.spoken_in_dialog = 0").fetchall()
        marked = []
        for r in rows:
            if json.loads(r["extra_json"]).get("wildcard"):
                continue
            cands = [r["answer"], *json.loads(r["variants_json"])]
            if any(contains_phrase(u, c) for u in utterances for c in cands):
                self.conn.execute("UPDATE item_state SET spoken_in_dialog = 1 WHERE item_id = ?", (r["id"],))
                self._maybe_close(r["id"])
                marked.append(r["code"])
        return marked

    def dialog_vocabulary(self) -> list[str]:
        """Пройденные фразы — допустимый словарь для мини-диалога."""
        return [r[0] for r in self.conn.execute(
            "SELECT i.answer FROM items i JOIN item_state s ON s.item_id = i.id "
            "WHERE i.track = 'en' AND i.archived = 0 AND s.stage >= 2 ORDER BY i.sort_key")]

    # журнал ошибок → «найди ошибку»

    def find_error_tasks(self, limit: int) -> list[Task]:
        """Свои неверные ответы по меткам, набравшим порог ошибок; модель не нужна."""
        threshold = self.settings.get("error_tag_threshold")
        rows = self.conn.execute(
            "SELECT e.id, e.item_id, e.answer, e.tag, i.code, i.prompt, i.answer AS target, i.variants_json "
            "FROM errors e JOIN items i ON i.id = e.item_id "
            "WHERE e.track = 'en' AND i.archived = 0 AND e.answer IS NOT NULL AND e.used_count < 2 "
            "AND e.tag IN (SELECT tag FROM errors WHERE track = 'en' GROUP BY tag HAVING count(*) >= ?) "
            "ORDER BY e.used_count, e.ts DESC LIMIT ?", (threshold, limit)).fetchall()
        return [Task(r["item_id"], r["code"], FIND_ERROR, 0,
                     instruction="Найди и исправь ошибку", prompt_ru=r["prompt"], shown_en=r["answer"],
                     expected=[r["target"]], variants=json.loads(r["variants_json"]),
                     schedules=False, meta={"error_id": r["id"], "tag": r["tag"]})
                for r in rows]

    def mark_error_used(self, error_id: int) -> None:
        self.conn.execute("UPDATE errors SET used_count = used_count + 1 WHERE id = ?", (error_id,))

    # блоки

    def _block_opened(self, area: str, block: str, now: datetime) -> None:
        self.conn.execute("INSERT OR IGNORE INTO en_blocks (area, block, opened_at) VALUES (?, ?, ?)",
                          (area, block, to_iso(now)))
        left = self.conn.execute(
            "SELECT count(*) FROM items i LEFT JOIN item_state s ON s.item_id = i.id "
            "WHERE i.track = 'en' AND i.archived = 0 AND i.kind = 'phrase' AND i.area = ? AND i.unit = ? "
            "AND COALESCE(s.stage, 0) = 0", (area, block)).fetchone()[0]
        if left == 0:
            self.conn.execute("UPDATE en_blocks SET introduced_at = COALESCE(introduced_at, ?) "
                              "WHERE area = ? AND block = ?", (to_iso(now), area, block))

    def stage_counts(self) -> dict[int, int]:
        """Фразы по ступеням — для недельной сводки. 5 — закрытые."""
        out = {0: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        for stage, closed, n in self.conn.execute(
                "SELECT COALESCE(s.stage, 0), COALESCE(s.closed, 0), count(*) FROM items i "
                "LEFT JOIN item_state s ON s.item_id = i.id WHERE i.track = 'en' AND i.archived = 0 "
                "AND i.kind = 'phrase' GROUP BY 1, 2"):
            out[5 if closed else stage] += n
        return out
