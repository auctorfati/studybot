"""Строгий JSON ответов модели: разбор и проверка.

Невалидный ответ — один повтор с напоминанием, затем задание откладывается
без оценки. Модель не придумывает новых меток: только закрытые списки.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from ..enums import EN_ERROR_TAGS, PSY_GAP_TAGS


class SchemaError(ValueError):
    pass


def extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < start:
        raise SchemaError("в ответе нет JSON-объекта")
    try:
        data = json.loads(t[start:end + 1])
    except json.JSONDecodeError as exc:
        raise SchemaError(f"JSON не разбирается: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise SchemaError("ожидается объект")
    return data


def _str(d: dict, key: str, required: bool = True, empty_ok: bool = False) -> str | None:
    v = d.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or (not empty_ok and not v.strip()):
        raise SchemaError(f"поле {key} должно быть строкой")
    return v.strip()


def _choice(d: dict, key: str, options, required: bool = True) -> str | None:
    v = d.get(key)
    if v is None and not required:
        return None
    if v not in options:
        raise SchemaError(f"поле {key}: {v!r} не из {sorted(options)}")
    return v


def _str_list(d: dict, key: str, max_len: int | None = None) -> list[str]:
    v = d.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise SchemaError(f"поле {key} должно быть списком строк")
    if max_len is not None and len(v) > max_len:
        raise SchemaError(f"в {key} больше {max_len} элементов")
    return [x.strip() for x in v if x.strip()]


def check_en(d: dict) -> dict:
    verdict = _choice(d, "verdict", {"acceptable", "error"})
    out = {"verdict": verdict, "explanation": _str(d, "explanation", empty_ok=True) or "",
           "correction": _str(d, "correction", required=False, empty_ok=True) or ""}
    if verdict == "error":
        out["tag"] = _choice(d, "tag", EN_ERROR_TAGS)
        if not out["explanation"]:
            raise SchemaError("у ошибки нужно объяснение")
    else:
        out["tag"] = None
    return out


def check_psy(d: dict) -> dict:
    verdict = _choice(d, "verdict", {"passed", "partial", "failed"})
    out = {"verdict": verdict, "present": _str_list(d, "present"), "missing": _str_list(d, "missing"),
           "outside_source": _str_list(d, "outside_source"), "comment": _str(d, "comment", empty_ok=True) or "",
           "tag": _choice(d, "tag", PSY_GAP_TAGS, required=False),
           "confusion": _str(d, "confusion", required=False, empty_ok=True)}
    if verdict != "passed" and out["tag"] is None:
        raise SchemaError("при незачёте нужна метка пробела")
    return out


def check_vignette(d: dict) -> dict:
    out = check_psy(d)
    m = d.get("mentor")
    if not isinstance(m, dict) or not isinstance(m.get("matches"), bool):
        raise SchemaError("нужно поле mentor с matches: true/false")
    out["mentor"] = {"matches": m["matches"], "divergence": _str(m, "divergence", required=False,
                                                                  empty_ok=True) or ""}
    return out


def dialog_turn(d: dict) -> dict:
    end = d.get("end", False)
    if not isinstance(end, bool):
        raise SchemaError("end должно быть true/false")
    return {"reply": _str(d, "reply"), "end": end}


def dialog_corrections(d: dict) -> dict:
    items = d.get("corrections", [])
    if not isinstance(items, list) or len(items) > 3:
        raise SchemaError("corrections — список не длиннее трёх")
    out = []
    for c in items:
        if not isinstance(c, dict):
            raise SchemaError("исправление должно быть объектом")
        out.append({"said": _str(c, "said"), "better": _str(c, "better"),
                    "tag": _choice(c, "tag", EN_ERROR_TAGS), "why": _str(c, "why")})
    return {"corrections": out}


def monologue(d: dict) -> dict:
    return {"verdict": _choice(d, "verdict", {"passed", "failed"}),
            "covered": _str_list(d, "covered"), "missing": _str_list(d, "missing"),
            "errors": dialog_corrections({"corrections": d.get("errors", [])})["corrections"],
            "comment": _str(d, "comment", empty_ok=True) or ""}


def questions(d: dict) -> dict:
    items = d.get("items")
    if not isinstance(items, list):
        raise SchemaError("items — список")
    out = []
    for q in items:
        if not isinstance(q, dict) or not isinstance(q.get("ok"), bool):
            raise SchemaError("у вопроса нужно ok: true/false")
        out.append({"question": _str(q, "question"), "ok": q["ok"],
                    "fix": _str(q, "fix", required=False, empty_ok=True) or ""})
    return {"items": out, "accepted": sum(q["ok"] for q in out)}


def scenario_turn(d: dict) -> dict:
    """Реплика бота в сценарии: отклик или ответ, номер вопроса из списка сценария (с 1) или null."""
    end = d.get("end", False)
    if not isinstance(end, bool):
        raise SchemaError("end должно быть true/false")
    q = d.get("question")
    if q is not None and (isinstance(q, bool) or not isinstance(q, int) or q < 1):
        raise SchemaError("question — номер вопроса из списка (с 1) или null")
    reply = _str(d, "reply", required=False, empty_ok=True) or ""
    if not reply and q is None and not end:
        raise SchemaError("нужна реплика или номер вопроса")
    return {"reply": reply, "question": q, "end": end}


def exit_dialog(d: dict) -> dict:
    for key in ("answered_all", "russian_used"):
        if not isinstance(d.get(key), bool):
            raise SchemaError(f"{key} должно быть true/false")
    n = d.get("own_questions_ok")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise SchemaError("own_questions_ok — целое число")
    return {"verdict": _choice(d, "verdict", {"passed", "failed"}), "answered_all": d["answered_all"],
            "own_questions_ok": n, "russian_used": d["russian_used"],
            "unanswered": _str_list(d, "unanswered"),
            "errors": dialog_corrections({"corrections": d.get("errors", [])})["corrections"],
            "comment": _str(d, "comment", empty_ok=True) or ""}


def check_term(d: dict) -> dict:
    return {"verdict": _choice(d, "verdict", {"passed", "partial", "failed"}),
            "comment": _str(d, "comment", empty_ok=True) or ""}


def check_text(d: dict) -> dict:
    n = d.get("theses_ok")
    if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 0):
        raise SchemaError("theses_ok — целое число или null")
    return {"verdict": _choice(d, "verdict", {"passed", "partial", "failed"}),
            "comment": _str(d, "comment", empty_ok=True) or "", "errors": _str_list(d, "errors"),
            "calques": _str_list(d, "calques"), "theses_ok": n}


def _opt_int(d: dict, key: str) -> int | None:
    n = d.get(key)
    if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 0):
        raise SchemaError(f"{key} — целое число или null")
    return n


def exam_check(d: dict) -> dict:
    return {"verdict": _choice(d, "verdict", {"passed", "failed"}),
            "comment": _str(d, "comment", empty_ok=True) or "", "errors": _str_list(d, "errors"),
            "inaccuracies": _opt_int(d, "inaccuracies"), "theses_ok": _opt_int(d, "theses_ok"),
            "answers_ok": _opt_int(d, "answers_ok")}


def exam_talk(d: dict) -> dict:
    if "verdict" in d:
        if not isinstance(d.get("answered_all"), bool):
            raise SchemaError("answered_all должно быть true/false")
        return {"verdict": _choice(d, "verdict", {"passed", "failed"}), "answered_all": d["answered_all"],
                "errors": _str_list(d, "errors"), "comment": _str(d, "comment", empty_ok=True) or ""}
    end = d.get("end", False)
    if not isinstance(end, bool):
        raise SchemaError("end должно быть true/false")
    return {"question": _str(d, "question"), "end": end}


def speech_partner(d: dict) -> dict:
    end = d.get("end", False)
    if not isinstance(end, bool):
        raise SchemaError("end должно быть true/false")
    return {"reply": _str(d, "reply"), "end": end}


def speech_review(d: dict) -> dict:
    return {"verdict": _choice(d, "verdict", {"passed", "partial", "failed"}),
            "covered": _str_list(d, "covered"), "missing": _str_list(d, "missing"),
            "corrections": _free_corrections(d.get("corrections", [])),
            "comment": _str(d, "comment", empty_ok=True) or ""}


def _free_corrections(items) -> list[dict]:
    if not isinstance(items, list):
        raise SchemaError("corrections — список")
    out = []
    for c in items[:3]:
        if isinstance(c, dict) and c.get("said") and c.get("better"):
            out.append({"said": str(c["said"]), "better": str(c["better"]), "why": str(c.get("why", ""))})
    return out


def listen_review(d: dict) -> dict:
    out = {}
    for key in ("answers_ok", "points_ok"):
        n = d.get(key)
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise SchemaError(f"{key} — целое число")
        out[key] = n
    wrong = d.get("wrong", [])
    if not isinstance(wrong, list):
        raise SchemaError("wrong — список номеров")
    out.update(wrong=[w for w in wrong if isinstance(w, int)], corrections=_free_corrections(d.get("corrections", [])),
               comment=_str(d, "comment", empty_ok=True) or "")
    return out


VALIDATORS: dict[str, Callable[[dict], dict]] = {
    "listen_review": listen_review,
    "speech_partner": speech_partner, "speech_review": speech_review, "exit_review": speech_review,
    "check_term": check_term, "check_text": check_text, "exam_check": exam_check, "exam_talk": exam_talk,
    "scenario_en": scenario_turn, "exit_dialog_en": exit_dialog,
    "check_en": check_en, "check_psy": check_psy, "check_vignette": check_vignette,
    "dialog_en": dialog_turn, "dialog_corrections": dialog_corrections,
    "monologue_en": monologue, "questions_en": questions,
}


def validate(task: str, text: str) -> dict[str, Any]:
    return VALIDATORS[task](extract_json(text))
