"""Нормализация ответа перед сравнением кодом.

Приравниваются: регистр, пунктуация, кавычки и апострофы, пробелы,
сокращения и полные формы, числа словами и цифрами, американское
и британское написание. Одна функция используется и для ответа,
и для целевой фразы с вариантами — поэтому сравнение симметрично.
"""

from __future__ import annotations

import re
import unicodedata

APOSTROPHES = "\u2019\u2018\u02bc\u0060\u00b4"
WILDCARD = "…"

# Сокращения → полная форма. 's после he/she/it/that/what… читается как is:
# в ядре модуля 1 has в сокращённом виде не встречается.
CONTRACTIONS = {
    "i'm": "i am", "you're": "you are", "we're": "we are", "they're": "they are",
    "he's": "he is", "she's": "she is", "it's": "it is", "that's": "that is",
    "what's": "what is", "who's": "who is", "where's": "where is", "there's": "there is",
    "how's": "how is", "here's": "here is", "let's": "let us",
    "isn't": "is not", "aren't": "are not", "wasn't": "was not", "weren't": "were not",
    "don't": "do not", "doesn't": "does not", "didn't": "did not",
    "can't": "can not", "cannot": "can not", "couldn't": "could not",
    "won't": "will not", "wouldn't": "would not", "shouldn't": "should not",
    "haven't": "have not", "hasn't": "has not",
    "i've": "i have", "you've": "you have", "we've": "we have", "they've": "they have",
    "i'll": "i will", "you'll": "you will", "we'll": "we will", "they'll": "they will",
    "i'd": "i would", "you'd": "you would",
}

# Британское → американское написание.
SPELLING = {
    "centre": "center", "theatre": "theater", "colour": "color", "favourite": "favorite",
    "neighbour": "neighbor", "neighbourhood": "neighborhood", "travelling": "traveling",
    "travelled": "traveled", "traveller": "traveler", "grey": "gray", "realise": "realize",
    "organise": "organize", "programme": "program", "metre": "meter", "litre": "liter",
    "behaviour": "behavior", "humour": "humor", "labour": "labor", "honour": "honor",
    "analyse": "analyze", "practise": "practice", "cheque": "check", "okay": "ok",
    "mum": "mom", "maths": "math", "catalogue": "catalog", "dialogue": "dialog",
    "defence": "defense", "licence": "license", "jewellery": "jewelry",
}

UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
         "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
         "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
        "seventy": 70, "eighty": 80, "ninety": 90}


def _numbers(words: list[str]) -> list[str]:
    """«thirty six», «thirty-six» (после разбора дефиса), «a hundred» → цифры."""
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w in TENS:
            value = TENS[w]
            if i + 1 < len(words) and words[i + 1] in UNITS and 0 < UNITS[words[i + 1]] < 10:
                value += UNITS[words[i + 1]]
                i += 1
            out.append(str(value))
        elif w in UNITS:
            out.append(str(UNITS[w]))
        elif w == "hundred" and out and out[-1] in ("1", "a"):
            out[-1] = "100"
        else:
            out.append(w)
        i += 1
    return out


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    for ch in APOSTROPHES:
        t = t.replace(ch, "'")
    t = t.replace(WILDCARD, " ").replace("...", " ")
    t = re.sub(r"[\"“”«»„]", " ", t)
    # отделить слова от пунктуации, оставив апостроф внутри слова
    t = re.sub(r"[^\w\s']", " ", t)
    t = re.sub(r"(?<!\w)'|'(?!\w)", " ", t)
    words = []
    for w in t.split():
        full = CONTRACTIONS.get(w)
        if full:
            words.extend(full.split())
        else:
            words.append(SPELLING.get(w, w))
    # дефис уже снят пунктуацией: thirty-six → thirty six
    return " ".join(_numbers(words))


def wildcard_pattern(target: str) -> re.Pattern | None:
    """Целевая фраза с многоточием: «What does "…" mean?» принимает любое заполнение."""
    if WILDCARD not in target:
        return None
    parts = [normalize(p) for p in target.split(WILDCARD)]
    body = r"(?:\s+\S+)+\s*".join(re.escape(p) for p in parts)
    return re.compile(rf"^\s*{body.strip()}\s*$".replace(r"\ ", r"\s+"))


def matches(answer: str, target: str, variants: list[str]) -> bool:
    """Совпадение ответа с целевой фразой или вариантом после нормализации."""
    a = normalize(answer)
    if not a:
        return False
    pattern = wildcard_pattern(target)
    if pattern is not None:
        return bool(pattern.match(a))
    return a in {normalize(x) for x in [target, *variants]}


def contains_phrase(utterance: str, phrase: str) -> bool:
    """Звучит ли фраза внутри реплики (для отметки «прозвучала в диалоге»)."""
    u, p = f" {normalize(utterance)} ", normalize(phrase)
    return bool(p) and f" {p} " in u
