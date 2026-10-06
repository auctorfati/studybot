"""Шаги модуля 5 в сессии: карточка знакомства с термином, «Термин», «Разбор предложения».

Подмешивается в SessionEngine. Время идёт в английский и отдельно в долю академической ветки.
Модель недоступна при несовпадении кодом — задание откладывается без оценки.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from ..db import transaction
from ..enums import Rating, Track
from ..llm.client import CHEAP, LLMUnavailable
from .academic import TermTask

VERDICT = {"passed": Rating.GOOD, "partial": Rating.HARD, "failed": Rating.AGAIN}
TITLE = {"passed": "Верно.", "partial": "Близко, но неточно.", "failed": "Неверно."}


@dataclass
class TermFeedback:
    step_id: int
    lines: list[str] = field(default_factory=list)
    deferred: str | None = None
    closed: bool = False
    has_context: bool = False
    has_note: bool = False


class TermSteps:
    def _term_row(self, step_id: int):
        row = self.conn.execute("SELECT * FROM session_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None or row["kind"] != "term" or row["status"] != "active":
            return None, None, None
        s = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (row["session_id"],)).fetchone()
        return row, TermTask(**json.loads(row["payload_json"])), s

    def _acad_account(self, s, day: date, sec: int) -> None:
        self._account(s["id"], day, Track.EN.value, sec, 0)
        self.conn.execute("UPDATE days SET sec_en_acad = sec_en_acad + ? WHERE study_date = ?", (sec, day.isoformat()))

    def _term_fb(self, row, task: TermTask) -> TermFeedback:
        t = self.conn.execute("SELECT context, notes_json FROM terms WHERE id = ?", (task.term_id,)).fetchone()
        return TermFeedback(row["id"], has_context=bool(t["context"]), has_note=bool(json.loads(t["notes_json"])))

    def term_intro_done(self, step_id: int) -> TermFeedback | None:
        row, task, s = self._term_row(step_id)
        if row is None:
            return None
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        elapsed, _ = self._elapsed(row, "term_intro", now, None)
        with transaction(self.conn):
            self.academic.record(task, None, judge="code", answer=None, verdict=None, day=day, now=now,
                                 session_id=s["id"], elapsed=elapsed)
            self._acad_account(s, day, elapsed)
            self._mark(step_id, "done", now)
        return self._term_fb(row, task)

    async def term_answer(self, step_id: int, text: str) -> TermFeedback | None:
        self.settle_reading()
        row, task, s = self._term_row(step_id)
        if row is None:
            return None
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        fb = self._term_fb(row, task)
        elapsed, _ = self._elapsed(row, "term_" + task.kind, now, None)
        t = self.conn.execute("SELECT * FROM terms WHERE id = ?", (task.term_id,)).fetchone()
        rating, judge, verdict = None, "code", None
        if task.kind == "term" and self.academic.check_code(task.term_id, text or ""):
            rating, verdict = Rating.GOOD, {"verdict": "passed", "comment": ""}
        elif not (text or "").strip():
            rating, verdict = Rating.AGAIN, {"verdict": "failed", "comment": ""}
        else:
            try:
                res = await self.runner.run("check_term", CHEAP, {
                    "mode": task.kind, "term": t["term"], "meaning": t["meaning"],
                    "alternatives": json.loads(t["alt_json"]), "sentence": t["sentence"],
                    "translation": t["translation"], "analysis": t["analysis"] or "", "answer": text.strip()})
                verdict, judge = res.data, "cheap"
                rating = VERDICT[verdict["verdict"]]
            except LLMUnavailable as exc:
                fb.deferred = str(exc)
        with transaction(self.conn):
            if rating is None:
                self.conn.execute("UPDATE term_state SET due_date = ? WHERE term_id = ?",
                                  (day.isoformat(), task.term_id))
                fb.lines.append(f"Проверка недоступна ({fb.deferred}): термин вернётся без оценки.")
            else:
                out = self.academic.record(task, rating, judge=judge, answer=text, verdict=verdict, day=day, now=now,
                                           session_id=s["id"], elapsed=elapsed)
                fb.closed = out["closed"]
                fb.lines.append(TITLE[verdict["verdict"]])
                if verdict.get("comment"):
                    fb.lines.append(verdict["comment"])
            alts = json.loads(t["alt_json"])
            if task.kind == "parse":
                fb.lines.append(f"Разбор: {t['analysis']}")
            else:
                fb.lines.append(f"{t['term']} — {t['meaning']}" + (f" (также: {' / '.join(alts)})" if alts else ""))
            fb.lines.append(f"Перевод: {t['translation']}")
            if fb.closed:
                fb.lines.append("Термин закрыт: 30 дней без срыва.")
            self._acad_account(s, day, elapsed)
            self._mark(step_id, "done", now)
        return fb


# тексты Ч: чтение на время → вопросы → предложение → абзац → пересказ (банк блоков 5–6, раздел 1)

TEXT_PHASES = ("read", "q", "sentence", "paragraph", "retell")


@dataclass
class TextFeedback:
    step_id: int
    lines: list[str] = field(default_factory=list)
    done: bool = False


class TextSteps:
    def _text_row(self, step_id: int):
        row = self.conn.execute("SELECT * FROM session_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None or row["kind"] != "text" or row["status"] != "active":
            return None, None, None
        s = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (row["session_id"],)).fetchone()
        return row, json.loads(row["payload_json"]), s

    def _text_save(self, row, p: dict, now) -> None:
        self.conn.execute("UPDATE session_steps SET payload_json = ?, sent_at = ? WHERE id = ?",
                          (json.dumps(p, ensure_ascii=False), now.isoformat(), row["id"]))

    def text_read_done(self, step_id: int) -> TextFeedback | None:
        row, p, s = self._text_row(step_id)
        if row is None or p["phase"] != "read":
            return None
        from ..clock import from_iso
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        f = self.texts.fields(p["code"])
        raw = int((now - from_iso(row["sent_at"])).total_seconds()) if row["sent_at"] else 0
        chars = len(f.get("Текст", ""))
        counted = min(raw, max(60, round(chars * 60 / 300)), 900)      # не медленнее 300 знаков в минуту в учёт
        fb = TextFeedback(step_id)
        if raw >= 10:
            speed = round(chars * 60 / max(raw, 1))
            p["speed"] = speed
            fb.lines.append(f"Скорость чтения: {speed} знаков в минуту. Ориентир экзамена — 500 (1500 знаков "
                            f"за 3 минуты).")
        p["phase"], p["qi"] = "q", 0
        with transaction(self.conn):
            self._acad_account(s, day, counted)
            self._text_save(row, p, now)
        return fb

    async def text_answer(self, step_id: int, text: str) -> TextFeedback | None:
        self.settle_reading()
        row, p, s = self._text_row(step_id)
        if row is None or p["phase"] == "read":
            return None
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        f = self.texts.fields(p["code"])
        from .academic import numbered, paragraph_of
        questions, keys = numbered(f.get("Вопросы", "")), numbered(f.get("Ключ", ""))
        phase = p["phase"]
        payload = {"mode": {"q": "answer"}.get(phase, phase), "answer": (text or "").strip()}
        if phase == "q":
            k = p["qi"]
            payload.update(question=questions[k] if k < len(questions) else "", key=keys[k] if k < len(keys) else "")
        elif phase == "sentence":
            payload.update(sentence=f.get("Предложение", ""), key=f.get("Перевод предложения", ""),
                           analysis=f.get("Разбор", ""))
        elif phase == "paragraph":
            payload.update(source=paragraph_of(f.get("Текст", ""), f.get("Абзац", "")), key=f.get("Эталон", ""))
        else:
            payload.update(theses=numbered(f.get("Тезисы", "")))
        fb = TextFeedback(step_id)
        elapsed, _ = self._elapsed(row, "text_" + phase, now, None)
        try:
            v = (await self.runner.run("check_text", CHEAP, payload)).data if payload["answer"] else \
                {"verdict": "failed", "comment": "", "errors": [], "calques": [], "theses_ok": 0}
        except LLMUnavailable as exc:
            v = None
            fb.lines.append(f"Проверка недоступна ({exc}): ответ записан без оценки.")
        if v is not None:
            fb.lines.append(TITLE[v["verdict"]] + (f" {v['comment']}" if v["comment"] else ""))
            fb.lines += [f"Ошибка: {e}" for e in v["errors"][:3]] + [f"Калька: {c}" for c in v["calques"][:3]]
        p.setdefault("results", {}).setdefault(phase, []).append(v["verdict"] if v else None)
        if phase == "q":
            fb.lines.append(f"Ключ: {payload['key']}")
            p["qi"] += 1
            if p["qi"] >= len(questions):
                p["phase"] = "sentence"
                units = self._open_units(f.get("Единицы", ""))
                if units:
                    fb.lines.append("Термины текста, которые ещё учишь: " + "; ".join(units[:8]) + ".")
        elif phase == "sentence":
            fb.lines += [f"Разбор: {f.get('Разбор', '')}", f"Перевод: {f.get('Перевод предложения', '')}"]
            p["phase"] = "paragraph"
        elif phase == "paragraph":
            fb.lines.append(f"Эталон: {f.get('Эталон', '')}")
            p["paragraph"] = v["verdict"] if v else None
            p["phase"] = "retell"
        else:
            p["theses_ok"] = v["theses_ok"] if v else None
            fb.lines.append("Тезисы: " + " ".join(f"{k}. {t}" for k, t in enumerate(numbered(f.get('Тезисы', '')), 1)))
            answers = p["results"].get("q", [])
            share = sum(a == "passed" for a in answers) / max(1, len(answers))
            result = {"share": round(share, 2), "paragraph": p.get("paragraph"), "theses_ok": p.get("theses_ok"),
                      "speed": p.get("speed")}
            with transaction(self.conn):
                event = self.texts.finish(p["code"], p.get("stage", 0), result, day, now)
            fb.lines.append({"repeat7": "Текст пройден. Повтор — через 7 дней.",
                             "repeat30": "Повтор пройден. Следующий — через 30 дней.",
                             "closed": "Текст закрыт.",
                             "again7": "Для закрытия не хватило: ещё один повтор через 7 дней."}[event])
            fb.done = True
        with transaction(self.conn):
            self._acad_account(s, day, elapsed)
            if fb.done:
                self._mark(step_id, "done", now)
            else:
                self._text_save(row, p, now)
        return fb

    def _open_units(self, units: str) -> list[str]:
        codes = [c.strip(" .") for c in units.split(",") if c.strip(" .")]
        if not codes:
            return []
        marks = ",".join("?" * len(codes))
        return [f"{r['term']} — {r['meaning']}" for r in self.conn.execute(
            f"SELECT t.term, t.meaning FROM terms t LEFT JOIN term_state s ON s.term_id = t.id WHERE t.code IN ({marks}) "
            f"AND COALESCE(s.closed, 0) = 0 ORDER BY t.sort_key", codes)]


# прогоны экзамена Э (банк блока 9)

EXAM_EST = {"1": 3600, "2": 360, "3": 480, "4": 900}
TALK_TURNS = 7


class ExamSteps:
    def _exam_row(self, step_id: int):
        row = self.conn.execute("SELECT * FROM session_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None or row["kind"] != "exam" or row["status"] != "active":
            return None, None, None
        s = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (row["session_id"],)).fetchone()
        return row, json.loads(row["payload_json"]), s

    def _exam_time(self, row, p: dict, now) -> int:
        """Длинный формат: предел четырёх минут не действует, учёт — по факту, не больше полутора оценок части."""
        from ..clock import from_iso
        raw = int((now - from_iso(row["sent_at"])).total_seconds()) if row["sent_at"] else 0
        return max(0, min(raw, int(EXAM_EST[p["part"]] * 1.5)))

    def exam_read_done(self, step_id: int) -> dict | None:
        row, p, s = self._exam_row(step_id)
        if row is None or p["phase"] != "read":
            return None
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        p["phase"] = "retell" if p["part"] == "2" else "q"
        p.setdefault("answers", [])
        with transaction(self.conn):
            self._acad_account(s, day, self._exam_time(row, p, now))
            self.conn.execute("UPDATE session_steps SET payload_json = ?, sent_at = ? WHERE id = ?",
                              (json.dumps(p, ensure_ascii=False), now.isoformat(), step_id))
        return p

    async def exam_answer(self, step_id: int, text: str) -> TextFeedback | None:
        self.settle_reading()
        row, p, s = self._exam_row(step_id)
        if row is None or p["phase"] == "read":
            return None
        from .academic import numbered
        from ..llm.client import FLAGSHIP
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        f = self.exams.fields(p["code"])["parts"][p["part"]]
        fb = TextFeedback(step_id)
        text = (text or "").strip()
        elapsed = self._exam_time(row, p, now)
        verdict, passed = None, None
        try:
            if p["part"] == "1":
                verdict = (await self.runner.run("exam_check", FLAGSHIP, {
                    "mode": "translation", "source": f.get("Текст", ""), "key": f.get("Эталон", ""),
                    "terms": f.get("Термины", ""), "answer": text})).data
                passed = verdict["verdict"] == "passed"
            elif p["part"] == "2":
                verdict = (await self.runner.run("exam_check", FLAGSHIP, {
                    "mode": "retell", "theses": numbered(f.get("Тезисы", "")), "answer": text})).data
                passed = verdict["verdict"] == "passed" and (verdict["theses_ok"] or 0) >= 4
            elif p["part"] == "4" and p["phase"] == "q":
                p["answers"].append(text)
                if len(p["answers"]) >= len(numbered(f.get("Вопросы", ""))):
                    p["phase"] = "retell"
            elif p["part"] == "4":
                verdict = (await self.runner.run("exam_check", FLAGSHIP, {
                    "mode": "reading", "questions": numbered(f.get("Вопросы", "")), "keys": numbered(f.get("Ключ", "")),
                    "answers": p["answers"], "retell": text, "theses": numbered(f.get("Тезисы", ""))})).data
                passed = (verdict["verdict"] == "passed" and (verdict["answers_ok"] or 0) >= 4
                          and (verdict["theses_ok"] or 0) >= 4)
            else:                                                   # часть 3: беседа
                p.setdefault("history", []).append({"role": "user", "content": text})
                done = len([h for h in p["history"] if h["role"] == "user"]) >= TALK_TURNS
                payload = {"bank": self.exams.bank(), "article_questions": numbered(f.get("Вопросы по статье", "")),
                           "history": p["history"], "exchanges_done": len(p["history"]) // 2}
                if not done:
                    turn = (await self.runner.run("exam_talk", FLAGSHIP, {**payload, "mode": "turn",
                                                                          "finish_now": False})).data
                    done = turn["end"]
                    if not done:
                        p["history"].append({"role": "assistant", "content": turn["question"]})
                        fb.lines.append(turn["question"])
                if done:
                    verdict = (await self.runner.run("exam_talk", FLAGSHIP, {**payload, "mode": "grade"})).data
                    passed = verdict["verdict"] == "passed" and verdict["answered_all"]
        except LLMUnavailable as exc:
            fb.lines.append(f"Проверка недоступна ({exc}): часть прогона не засчитана ни в плюс, ни в минус, "
                            f"вернётся позже.")
            with transaction(self.conn):
                self._acad_account(s, day, elapsed)
                self._mark(step_id, "skipped", now)
            fb.done = True
            return fb
        with transaction(self.conn):
            self._acad_account(s, day, elapsed)
            if passed is None:
                self.conn.execute("UPDATE session_steps SET payload_json = ?, sent_at = ? WHERE id = ?",
                                  (json.dumps(p, ensure_ascii=False), now.isoformat(), step_id))
                return fb
            out = self.exams.record_part(p["run_id"], p["part"], passed, verdict, day)
            from .exams import PART_TITLE
            fb.lines.append(f"Часть {p['part']} ({PART_TITLE[p['part']]}): {'сдана' if passed else 'не сдана'}.")
            if verdict.get("comment"):
                fb.lines.append(verdict["comment"])
            fb.lines += [f"— {e}" for e in verdict.get("errors", [])[:3]]
            if p["part"] == "1":
                fb.lines.append(f"Эталон: {f.get('Эталон', '')}")
            if out["finished"]:
                fb.lines.append(f"Прогон {p['code']} {'засчитан' if out['passed'] else 'не засчитан'}.")
            if out["closed"]:
                fb.lines.append("Модуль 5 закрыт по критерию выхода: два засчитанных прогона на резервных статьях.")
            self._mark(step_id, "done", now)
        fb.done = True
        return fb
