"""Шаги свободной речи в сессии: подмешивается в SessionEngine (английский)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from ..db import transaction
from ..enums import Track
from ..llm.client import CHEAP, LLMUnavailable
from .speech import CORE, FORMATS, TASK_TEXT

FIRST_LINE = ("Первая реплика", "Первый вопрос")


@dataclass
class SpeechFeedback:
    step_id: int
    lines: list[str] = field(default_factory=list)
    reply: str | None = None
    done: bool = False


class SpeechSteps:
    def speech_open(self, step) -> None:
        """Активация шага: запись — в счётчик круга, первая реплика бота — из карточки, без модели."""
        p = step.payload
        self.speech.mark_used(p.get("record"), self.clock.now())
        if p["format"] in ("story", "listening") and "phase" not in p:
            p["phase"] = "listen"
            self.conn.execute("UPDATE session_steps SET payload_json = ? WHERE id = ?",
                              (json.dumps(p, ensure_ascii=False), step.id))
            return
        line = next((p["card"].get(k) for k in FIRST_LINE if p["card"].get(k)), None)
        if line and p["turns"] and not p["history"]:
            p["history"].append({"role": "assistant", "content": line})
            self.conn.execute("UPDATE session_steps SET payload_json = ? WHERE id = ?",
                              (json.dumps(p, ensure_ascii=False), step.id))

    async def speech_answer(self, step_id: int, text: str, voice_sec: int | None = None) -> SpeechFeedback | None:
        self.settle_reading()
        row = self.conn.execute("SELECT * FROM session_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None or row["kind"] != "speech" or row["status"] != "active":
            return None
        s = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (row["session_id"],)).fetchone()
        p = json.loads(row["payload_json"])
        if p["format"] in ("story", "listening"):
            if p.get("phase") == "listen":
                return None
            return await self.listen_answer(row, p, s, (text or "").strip(), voice_sec)
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        fb = SpeechFeedback(step_id)
        text = (text or "").strip()
        elapsed, _ = self._elapsed(row, "dialog", now, voice_sec)
        elapsed = min(elapsed, 240)
        base = {"format": p["format"], "card": p["card"], "allowed_level": CORE.get(p["module"], ""),
                "history": p["history"]}
        try:
            if text.lower().startswith("как сказать"):
                res = await self.runner.run("speech_partner", CHEAP, {**base, "mode": "how_to_say",
                                                                      "learner_said": text})
                fb.reply = f"Можно так: {res.data['reply']}"
                with transaction(self.conn):
                    self._account(s["id"], day, Track.EN.value, elapsed, voice_sec or 0)
                    self.conn.execute("UPDATE session_steps SET sent_at = ? WHERE id = ?", (now.isoformat(), step_id))
                return fb
            p["answers"].append(text)
            p["history"].append({"role": "user", "content": text})
            turns = p["turns"]
            done = len(p["answers"]) > turns if p["format"] == "why" else len(p["answers"]) >= max(1, turns)
            if not done:
                res = await self.runner.run("speech_partner", CHEAP, {
                    **base, "mode": "turn", "learner_said": text, "exchanges_done": len(p["answers"]),
                    "exchanges_total": turns, "finish_now": len(p["answers"]) + 1 >= turns and p["format"] != "why"})
                p["history"].append({"role": "assistant", "content": res.data["reply"]})
                fb.reply = res.data["reply"]
                done = res.data["end"]
            if done:
                v = (await self.runner.run("speech_review", CHEAP, {
                    "format": p["format"], "task": TASK_TEXT[p["format"]], "card": p["card"],
                    "module_core": CORE.get(p["module"], ""), "answer": "\n".join(p["answers"]),
                    "history": p["history"] if turns else []})).data
                fb.lines.append({"passed": "Получилось.", "partial": "Наполовину.", "failed": "Пока не вышло."}
                                [v["verdict"]] + (f" {v['comment']}" if v["comment"] else ""))
                if v["covered"]:
                    fb.lines.append("Есть: " + "; ".join(v["covered"]) + ".")
                if v["missing"]:
                    fb.lines.append("Не хватило: " + "; ".join(v["missing"]) + ".")
                fb.lines += [f"{c['said']} → {c['better']}" + (f" ({c['why']})" if c["why"] else "")
                             for c in v["corrections"]]
                fb.done = True
        except LLMUnavailable as exc:
            fb.lines.append(f"Модель недоступна ({exc}): свободная речь — в следующей сессии.")
            fb.done = True
        with transaction(self.conn):
            self._account(s["id"], day, Track.EN.value, elapsed, voice_sec or 0)
            if fb.done:
                self._mark(step_id, "done", now)
            else:
                self.conn.execute("UPDATE session_steps SET payload_json = ?, sent_at = ? WHERE id = ?",
                                  (json.dumps(p, ensure_ascii=False), now.isoformat(), step_id))
        return fb


class ExitSpeechSteps:
    """Части критерия выхода модулей 2–6 вне монолога и сценария: рассказ, объяснение, сравнение
    (одна запись голосом), диалог-совет, обсуждение, разговор (реплики модели), слушание. Проверяет флагман."""

    async def exit_speech_answer(self, step_id: int, text: str, voice_sec: int | None = None):
        from ..llm.client import FLAGSHIP
        self.settle_reading()
        row = self.conn.execute("SELECT * FROM session_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None or row["kind"] != "exit_speech" or row["status"] != "active":
            return None
        s = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (row["session_id"],)).fetchone()
        p = json.loads(row["payload_json"])
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        fb = SpeechFeedback(step_id)
        text = (text or "").strip()
        raw = int((now - __import__("studybot.clock", fromlist=["x"]).from_iso(row["sent_at"])).total_seconds()) \
            if row["sent_at"] else 0
        elapsed = min(max(raw, voice_sec or 0), 900)          # длинный формат: без предела четырёх минут
        p["answers"].append(text)
        p["history"].append({"role": "user", "content": text})
        result = None
        try:
            done = p["mode"] != "talk" or len(p["answers"]) >= p["turns"]
            if not done:
                res = await self.runner.run("speech_partner", CHEAP, {
                    "format": {"advice_dialog": "roleplay"}.get(p["part"], p["part"]), "card": p["card"],
                    "allowed_level": CORE.get(p["module"], ""), "history": p["history"], "mode": "turn",
                    "learner_said": text, "exchanges_done": len(p["answers"]), "exchanges_total": p["turns"],
                    "finish_now": len(p["answers"]) + 1 >= p["turns"]})
                p["history"].append({"role": "assistant", "content": res.data["reply"]})
                fb.reply = res.data["reply"]
            else:
                result = (await self.runner.run("exit_review", FLAGSHIP, {
                    "part": p["title"], "criteria": p["criteria"], "card": p["card"],
                    "module_core": CORE.get(p["module"], ""), "answer": "\n".join(p["answers"]),
                    "history": p["history"] if p["mode"] == "talk" else [], "duration_sec": voice_sec})).data
        except LLMUnavailable:
            result, done = None, True
        with transaction(self.conn):
            self._account(s["id"], day, Track.EN.value, elapsed, voice_sec or 0)
            if result is None and not done:
                self.conn.execute("UPDATE session_steps SET payload_json = ?, sent_at = ? WHERE id = ?",
                                  (json.dumps(p, ensure_ascii=False), now.isoformat(), step_id))
                return fb
            if result is None:
                fb.lines.append("Проверка недоступна: попытка не засчитана ни в плюс, ни в минус.")
            else:
                passed = result["verdict"] == "passed"
                closed = self.reh.record_exit(p["area"], p["part"], p.get("record") or "", passed,
                                              result, day, now, s["id"])
                fb.lines.append(f"Критерий выхода, {p['title']}: {'сдан' if passed else 'не сдан'}. "
                                f"{result['comment']}".strip())
                if result["missing"]:
                    fb.lines.append("Не хватило: " + "; ".join(result["missing"]) + ".")
                fb.lines += [f"{c['said']} → {c['better']}" for c in result["corrections"]]
                fb.lines.append(f"Итог: {self.reh.exit_summary(p['area'])}.")
                if closed:
                    nxt = {"EN1": "2", "EN2": "3", "EN3": "4", "EN4": "6"}.get(p["area"])
                    fb.lines.append(f"Модуль {p['area'][2:]} закрыт по критерию выхода."
                                    + (f" Открыт модуль {nxt}." if nxt else ""))
            fb.done = True
            self._mark(step_id, "done", now)
        return fb


class ListenSteps:
    """Пересказ на слух и длинное слушание: фазы listen → q → retell (english/listening.py)."""

    def listen_heard(self, step_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM session_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None or row["kind"] != "speech" or row["status"] != "active":
            return None
        p = json.loads(row["payload_json"])
        if p.get("phase") != "listen":
            return None
        s = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (row["session_id"],)).fetchone()
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        from ..clock import from_iso
        raw = int((now - from_iso(row["sent_at"])).total_seconds()) if row["sent_at"] else 0
        p.update(phase="q", qi=1, answers=[])
        with transaction(self.conn):
            self._account(s["id"], day, Track.EN.value, min(raw, 600), 0)
            self.conn.execute("UPDATE session_steps SET payload_json = ?, sent_at = ? WHERE id = ?",
                              (json.dumps(p, ensure_ascii=False), now.isoformat(), step_id))
        return p

    async def listen_answer(self, row, p: dict, s, text: str, voice_sec: int | None) -> SpeechFeedback:
        from ..llm.client import FLAGSHIP
        from .listening import parts
        now, day = self.clock.now(), date.fromisoformat(s["study_date"])
        fb = SpeechFeedback(row["id"])
        qs, keys, points = parts(p["card"])
        elapsed, _ = self._elapsed(row, "dialog", now, voice_sec)
        elapsed = min(elapsed, 240)
        if p["phase"] == "q":
            p["answers"].append(text)
            p["qi"] += 1
            if p["qi"] > len(qs):
                p["phase"] = "retell"
            with transaction(self.conn):
                self._account(s["id"], day, Track.EN.value, elapsed, voice_sec or 0)
                self.conn.execute("UPDATE session_steps SET payload_json = ?, sent_at = ? WHERE id = ?",
                                  (json.dumps(p, ensure_ascii=False), now.isoformat(), row["id"]))
            return fb
        exam = bool(p.get("exit"))
        try:
            v = (await self.runner.run("listen_review", FLAGSHIP if exam else CHEAP, {
                "questions": qs, "keys": keys, "answers": p["answers"], "points": points, "retell": text,
                "exam": exam})).data
        except LLMUnavailable as exc:
            v = None
            fb.lines.append(f"Проверка недоступна ({exc}): попытка без оценки.")
        with transaction(self.conn):
            self._account(s["id"], day, Track.EN.value, elapsed, voice_sec or 0)
            if v is not None:
                fb.lines.append(f"Вопросы: верно {v['answers_ok']} из {len(qs)}. Пересказ: передано {v['points_ok']} "
                                f"из {len(points)}. {v['comment']}".strip())
                if v["wrong"]:
                    fb.lines.append("Ключ к неверным: " + "; ".join(
                        f"{k}. {keys[k - 1]}" for k in v["wrong"] if 0 < k <= len(keys)))
                fb.lines += [f"{c['said']} → {c['better']}" for c in v["corrections"]]
                if exam:
                    passed = v["answers_ok"] >= 8 and v["points_ok"] * 2 >= len(points)
                    closed = self.reh.record_exit(p["area"], "listening", p.get("record") or "", passed, v,
                                                  day, now, s["id"])
                    fb.lines.append(f"Критерий выхода, слушание: {'сдан' if passed else 'не сдан'}. "
                                    f"Итог: {self.reh.exit_summary(p['area'])}.")
                    if closed:
                        fb.lines.append(f"Модуль {p['area'][2:]} закрыт по критерию выхода.")
            fb.lines.append("Текст записи:\n" + p["card"].get("Текст", ""))
            fb.done = True
            self._mark(row["id"], "done", now)
        return fb
