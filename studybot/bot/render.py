"""Шаг сессии и итог ответа → сообщение. Только текст, без разметки и эмодзи."""

from __future__ import annotations

from ..english.track import (ASK, DICTATION, FIND_ERROR, HEAR_ANSWER, HEAR_REPEAT, INTRO,
                             READ_ANSWER, REPEAT, RU_EN, TRANSFORM, Task)
from ..sessions.engine import Feedback
from ..sessions.planner import Step
from .ui import AudioRef, Button, Reply

# callback_data
CB_DONE = "done"          # знакомство: «Готово»
CB_IDK = "idk"            # «Не знаю»: пустой ответ, проверка кодом
CB_SKIP = "skip"          # отложить шаг до следующей сессии
CB_SLOW = "slow"          # аудио в медленном темпе
CB_END = "end"            # закончить сессию
CB_DISPUTE = "dispute"
CB_WHY = "why"            # «Почему так?» — объяснение по шагу
CB_SLOW_LINE = "slowl"    # медленная запись реплики сценария


def why_button(step_id: int) -> Button:
    return Button("Почему так?", f"{CB_WHY}:{step_id}")


def _typed(instruction: str, voice: bool) -> str:
    """В текстовом режиме «скажи» становится «напиши»."""
    if voice:
        return instruction
    return (instruction.replace("Послушай и повтори", "Послушай и запиши")
            .replace("Скажи", "Напиши", 1))


def task_text(task: Task) -> str:
    instr = _typed(task.instruction, task.voice)
    control = "Зачёт блока. " if task.meta.get("control") else ""
    f = task.format
    if f == INTRO:
        lines = []
        bi = task.meta.get("block_intro")
        if bi:
            head = f"Новый блок {bi['block']}" + (f" модуля {bi['module']}" if bi["module"] != "1" else "")
            head += (f" «{bi['title']}»" if bi["title"] else "") + f": {bi['phrases']} фраз."
            lines.append(head)
            if bi["rules"]:
                lines.append("Правила блока: " + "; ".join(bi["rules"]) + ". Каждое придёт целиком перед "
                             "первой фразой, где оно нужно; все — в «Ещё» → «Правила».")
            lines.append("")
        for n in task.meta.get("rules", []):
            lines += [f"Сначала правило. {n['code']} — {n['title']}", n["body"], ""]
        lines += ["Новая фраза", task.prompt_ru or "", task.shown_en or ""]
        if task.meta.get("sound"):
            lines.append(f"Произношение: {task.meta['sound']}")
        lines.append(instr + ".")
    elif f in (TRANSFORM, ASK):
        lines = [instr + ":", task.shown_en or "", f"По-русски: {task.prompt_ru}"]
    elif f == REPEAT:
        lines = [instr + ":", task.shown_en or task.prompt_ru or ""]
    elif f in (HEAR_ANSWER, HEAR_REPEAT, DICTATION):
        lines = [instr + "."]
    elif f == READ_ANSWER:
        lines = [instr + ":", task.shown_en or ""]
    elif f == RU_EN:
        lines = [instr + ":", task.prompt_ru or ""]
    elif f == FIND_ERROR:
        lines = [instr + ":", task.shown_en or "", f"По-русски: {task.prompt_ru}"]
    else:
        lines = [instr, task.prompt_ru or "", task.shown_en or ""]
    text = "\n".join(lines) if f == INTRO else "\n".join(l for l in lines if l)
    text = text.replace("\n\n\n", "\n\n").strip()
    return control + text


def step_buttons(step: Step) -> list[list[Button]]:
    sid = step.id
    end = Button("Закончить", CB_END)
    if step.kind == "task":
        task = step.task()
        if task.format == INTRO:
            rows = [[Button("Готово", f"{CB_DONE}:{sid}"), end], [why_button(sid)]]
        else:
            rows = [[Button("Не знаю", f"{CB_IDK}:{sid}"), end]]
        if task.audio_item:
            rows.insert(0, [Button("Медленнее", f"{CB_SLOW}:{task.audio_item}"),
                            Button("Сейчас не могу слушать", f"{CB_SKIP}:{sid}")])
        return rows
    if step.kind == "exit_dialog" and step.payload.get("audio_line"):
        return [[Button("Медленнее", f"{CB_SLOW_LINE}:{step.payload['audio_line']}")],
                [Button("Пропустить", f"{CB_SKIP}:{sid}"), end]]
    return [[Button("Пропустить", f"{CB_SKIP}:{sid}"), end]]


def step_reply(step: Step, audio: AudioRef | None = None) -> Reply:
    p = step.payload
    if step.kind == "task":
        text = task_text(step.task())
    elif step.kind == "dialog" and p.get("scenario"):
        text = (f"Мини-диалог {p['scenario']} — {p['topic']}, {p['turns']} реплик.\n"
                f"Ситуация: {p['situation']}\nОтвечай по-английски коротко и сам задавай вопросы.\n\n"
                f"{p.get('opening', '')}").strip()
    elif step.kind == "dialog":
        text = (f"Мини-диалог, {p['turns']} реплик, тема: {p['topic']}. Отвечай по-английски, коротко.\n\n"
                f"{p.get('opening', '')}").strip()
    elif step.kind == "exit_dialog":
        text = (f"Критерий выхода модуля, диалог {p['scenario']} — {p['topic']}: {p['turns']} обменов голосом.\n"
                f"Ситуация: {p['situation']}\nВопросы собеседника приходят только звуком. Не расслышал — "
                f"спроси по-английски или нажми «Медленнее». Свои вопросы задавай сам.")
    elif step.kind == "monologue" and p.get("rehearsal"):
        text = monologue_text(p, f"Монолог {p['rehearsal']} — {p['topic']}, ступень «{p['stage_title']}».")
    elif step.kind == "monologue":
        text = "Монолог голосом, четыре-пять предложений."
        if p.get("task_ru"):
            text += f"\n{p['task_ru']}"
    elif step.kind == "cp_monologue" and p.get("rehearsal"):
        text = monologue_text(p, f"Зачёт блока: монолог {p['rehearsal']} — {p['topic']}, опора — названия частей.")
    elif step.kind == "cp_monologue":
        text = "Зачёт блока: мини-монолог голосом, четыре-пять предложений."
        if p.get("task_ru"):
            text += f"\n{p['task_ru']}"
    elif step.kind == "exit_monologue":
        text = monologue_text(p, f"Критерий выхода модуля: монолог {p['rehearsal']} — {p['topic']}, без опоры, "
                                 f"голосом, одной записью.")
    elif step.kind == "cp_questions" and p.get("situation"):
        text = (f"Зачёт блока: задай {p['need']} своих вопросов собеседнику. Ситуация: {p['situation']} "
                f"Одним сообщением, каждый вопрос со своим знаком вопроса.")
    elif step.kind == "cp_questions":
        text = (f"Зачёт блока: задай {p['need']} своих вопросов собеседнику, тема: {p['topic']}. "
                f"Одним сообщением, каждый вопрос со своим знаком вопроса.")
    else:
        text = step.kind
    return Reply(text, step_buttons(step), audio=audio)


def monologue_text(p: dict, head: str) -> str:
    lines = [head]
    if p.get("support"):
        lines.append("Опора:")
        lines += p["support"]
    else:
        lines.append("Без опоры: рассказ целиком по памяти.")
    if p.get("min_sentences"):
        lines.append(f"Не меньше {p['min_sentences']} предложений. Запиши одним голосовым.")
    return "\n".join(lines)


def exit_text(res: dict) -> str:
    if res.get("postponed"):
        return "Проверка недоступна: попытка не засчитана ни в плюс, ни в минус."
    part = "монолог" if res["part"] == "monologue" else "диалог"
    head = f"Критерий выхода, {part}: {'сдан' if res['passed'] else 'не сдан'}. Итог: {res['summary']}."
    if res.get("closed"):
        module = res["area"][2:] if res.get("area", "").startswith("EN") else res.get("area", "")
        nxt = {"1": "2", "2": "3", "3": "4", "4": "6"}.get(module)
        return head + f" Модуль {module} закрыт по критерию выхода." + (
            f" Открыт модуль {nxt}: новые фразы — из него, повторения прошлых модулей идут дальше." if nxt else "")
    if res.get("next"):
        head += f" Следующая попытка этой части — не раньше {res['next'].day}.{res['next'].month:02d}."
    return head


def feedback_reply(fb: Feedback, transcript: str | None = None, kind: str = "task") -> Reply:
    lines: list[str] = []
    if transcript is not None:
        lines.append(f"Распознано: {transcript}")
    if fb.reply:
        lines.append(fb.reply)
    if kind in ("dialog", "exit_dialog") and fb.finished_step:
        lines.append("Диалог окончен.")
    if fb.deferred is None and fb.finished_step:
        yes, no = ("Верно.", "Неверно.") if kind == "task" else ("Засчитано.", "Не засчитано.")
        if fb.correct is True:
            lines.append(yes)
        elif fb.correct is False:
            lines.append(no)
    lines += fb.lines
    if fb.deferred and not fb.lines:
        lines.append("Сервис проверки недоступен, задание вернётся в следующей сессии.")
    for n in fb.notes:
        lines.append(f"Заметка {n['code']} — {n['title']}\n{n['body']}")
    if fb.control_result:
        lines.append(control_text(fb.control_result))
    if fb.exit_result:
        lines.append(exit_text(fb.exit_result))
    row = []
    if fb.explainable and fb.step_id and fb.correct is not None:
        row.append(why_button(fb.step_id))
    if fb.disputable and fb.review_id:
        row.append(Button("Оспорить", f"{CB_DISPUTE}:{fb.review_id}"))
    return Reply("\n".join(lines), [row] if row else [])


def control_text(res: dict) -> str:
    if res.get("postponed"):
        return (f"Зачёт блока {res['block']} отложен: проверка была недоступна. "
                f"Попытка не засчитана как проваленная.")
    head = f"Зачёт блока {res['block']}: {'сдан' if res['passed'] else 'не сдан'}."
    return (f"{head} С русского {res['ru'][0]} из {res['ru'][1]}, на слух {res['hearing'][0]} "
            f"из {res['hearing'][1]}, своих вопросов {res['questions']}, "
            f"монолог {'засчитан' if res['monologue'] else 'не засчитан'}.")
