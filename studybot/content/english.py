"""Разбор банка английского (формат — раздел 2 файла банка).

Связи хранятся так, как записаны в файле (у меньшего номера). Граф в обе
стороны строит трек английского при выдаче заданий: так связь между файлами
разных блоков не требует правки уже импортированных позиций.

Раздел «Репетиции» (слой К8.1) читается как тексты, привязанные к последнему
блоку файла — блоку сборки: «### М1 — …» — план монолога (части с ключевыми
словами, условия зачёта), «### Д1 — …» — сценарий диалога (ситуация, первая
реплика бота, вопросы бота, что спрашивает ученик). Прочие подразделы раздела
(карточки спора, темы разговора и другие виды карты пути) пока пропускаются
с предупреждением.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .common import Issues, cell, short_hash

COLUMNS = ["ID", "Подсказка", "Целевая фраза", "Допустимые варианты",
           "Грамматика", "Звук", "Связи", "Время"]       # «Время» — восьмой, с модуля 2
# модуль 5, блоки 1–4: единицы на узнавание (банк модуля 5, раздел 1)
TERM_COLUMNS = ["ID", "Раздел", "Термин", "Значение", "Допустимые значения", "Предложение", "Перевод",
                "Разбор", "Ист", "Грамматика", "Звук"]
TERM_REQUIRED = {"ID", "Термин", "Значение", "Предложение", "Перевод"}
SECTION_RE = re.compile(r"^## \d+\. Раздел (\d+)\. (.+?)(?: — (\d+) терм\w*)?\s*$")
REQUIRED = {"ID", "Подсказка", "Целевая фраза"}

TITLE_RE = re.compile(r"^# Английский\. Модуль (\d+)\b")
BLOCK_RE = re.compile(r"^## \d+\. Блок (\d+)\. (.+?)(?: — (\d+) единиц\w*)?\s*$")
NOTE_RE = re.compile(r"^### (З\d+) — (.+?)\s*$")
REH_SECTION_RE = re.compile(r"^## (?:\d+\. )?Репетиции\s*$")
REH_RE = re.compile(r"^### ([МД]\d+) — (.+?)\s*$")
REC_RE = re.compile(r"^### ((?:СР|СВ|[РЛОУТЧЭД])\d+) — (.+?)\s*$")
FIELD_RE = re.compile(r"^([А-ЯЁ][а-яё]+(?: [а-яё]+){0,3})\.(?:\s+(.*))?$")
REC_KIND = {"Р": "story", "Л": "listening", "О": "explain", "СР": "compare", "СВ": "advice",
            "У": "statement", "Т": "topic", "Ч": "text", "Э": "exam"}
REC_NEED = {"story": ("Текст", "Вопросы", "Ключ"), "listening": ("Текст",), "explain": ("Задание", "Ключ"),
            "compare": ("Задание", "Ключ"), "advice": ("Ситуация", "Ключ"), "statement": ("Утверждение",),
            "topic": ("Тема",), "text": ("Текст",), "roleplay": ("Роль бота", "Первая реплика"),
            "discussion": ("Позиция бота", "Первая реплика")}
PART_RE = re.compile(r"^([^.:;!?]{1,40}):\s+(\S.*)$")
LABEL_RE = re.compile(r"^(Ситуация|Первая реплика бота|Вопросы бота[^:]*|Что спрашивает[^:]*):\s*(.*)$")
QUESTION_RE = re.compile(r"[^?]+\?")
NUMBER_WORDS = {"двух": 2, "трёх": 3, "трех": 3, "четырёх": 4, "четырех": 4, "пяти": 5, "шести": 6,
                "семи": 7, "восьми": 8, "девяти": 9, "десяти": 10}
ID_RE = re.compile(r"^(\d+)\.(\d{2,3})$")                 # модуль 1: блок.номер
ID3_RE = re.compile(r"^([2-6])\.(\d+)\.(\d{2,3})$")      # модули 2–6: модуль.блок.номер


def split_id(code: str) -> tuple[str, str, int] | None:
    """«4.01» → ('1', '4', 1); «2.3.15» → ('2', '3', 15). Не ID — None."""
    m = ID3_RE.match(code)
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    m = ID_RE.match(code)
    if m:
        return "1", m.group(1), int(m.group(2))
    return None


def sort_key_of(code: str) -> int:
    mod, block, num = split_id(code)
    return int(mod) * 1_000_000 + int(block) * 1000 + num
NOTE_CODE_RE = re.compile(r"З\d+")
MEANING_RE = re.compile(r"^по смыслу\s*:?\s*(.*)$")


@dataclass
class EnItem:
    code: str
    block: str
    prompt: str
    target: str
    variants: list[str]
    notes: list[str]
    sound: str | None
    audio_text: str | None
    links: list[str]
    assembly: bool
    extra: dict
    sort_key: int
    line: int

    @property
    def prompt_hash(self) -> str:
        return short_hash(self.prompt, self.notes, self.sound, self.audio_text, self.links)

    @property
    def answer_hash(self) -> str:
        return short_hash(self.target, self.variants, self.assembly, self.extra.get("meaning"))


@dataclass
class EnNote:
    code: str
    title: str
    body: str


@dataclass
class EnRehearsal:
    """План монолога (kind='monologue') или сценарий диалога (kind='scenario') блока сборки."""
    code: str
    kind: str
    title: str
    body: str
    block: str
    data: dict
    sort_key: int
    line: int

    @property
    def hash(self) -> str:
        return short_hash(self.kind, self.title, self.body)


@dataclass
class EnRecord:
    """Запись банка модулей 2–6: история, запись для слушания, тема, карточка и т.п."""
    code: str
    kind: str
    title: str
    body: str
    block: str | None
    fields: dict
    sort_key: int
    line: int


def parse_fields(body: str) -> dict[str, str]:
    """Поля записи строками «Поле. текст»; текст поля — до следующего поля."""
    out: dict[str, list[str]] = {}
    cur: str | None = None
    for line in body.split("\n"):
        m = FIELD_RE.match(line.strip())
        if m and not line.startswith(" "):
            cur = m.group(1)
            out.setdefault(cur, [])
            if m.group(2):
                out[cur].append(m.group(2))
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


@dataclass
class EnTerm:
    code: str
    block: str
    section: str | None
    term: str
    meaning: str
    alternatives: list[str]
    sentence: str
    translation: str
    analysis: str | None
    source: str | None
    notes: list[str]
    sound: str | None
    sort_key: int
    line: int

    @property
    def answer_hash(self) -> str:
        return short_hash(self.term, self.meaning, self.alternatives, self.sentence, self.translation)


@dataclass
class ParsedEnglish:
    module: str
    items: list[EnItem] = field(default_factory=list)
    notes: list[EnNote] = field(default_factory=list)
    blocks: dict[str, str] = field(default_factory=dict)   # номер → название
    rehearsals: list[EnRehearsal] = field(default_factory=list)
    records: list[EnRecord] = field(default_factory=list)
    terms: list[EnTerm] = field(default_factory=list)
    exam_questions: list[str] = field(default_factory=list)   # модуль 5: банк вопросов беседы
    rehearsal_preamble: str = ""                           # общие правила раздела «Репетиции»
    issues: Issues = field(default_factory=Issues)

    @property
    def area(self) -> str:
        return f"EN{self.module}"


def _split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def parse_english(text: str, known_notes: set[str] | None = None,
                  known_codes: set[str] | None = None) -> ParsedEnglish:
    """known_notes, known_codes — уже импортированные заметки и позиции других файлов."""
    lines = text.split("\n")
    title = next((l for l in lines if l.startswith("# ")), "")
    m = TITLE_RE.match(title)
    parsed = ParsedEnglish(module=m.group(1) if m else "?")
    iss = parsed.issues
    if not m:
        iss.error("заголовок", "первая строка должна быть «# Английский. Модуль N …»")

    block: str | None = None
    last_block: str | None = None
    declared: dict[str, int] = {}
    header: list[str] | None = None
    note: EnNote | None = None
    seen: dict[str, int] = {}
    in_reh = False
    in_bank = False
    reh: list | None = None                    # [code, title, строки, номер строки]
    rec: list | None = None                    # запись: [code, title, строки, номер строки]
    preamble: list[str] = []
    skipped_reh: list[str] = []

    def close_note() -> None:
        nonlocal note
        if note is not None:
            note.body = note.body.strip()
            if not note.body:
                iss.error(note.code, "пустая заметка")
            parsed.notes.append(note)
            note = None

    def close_reh() -> None:
        nonlocal reh, rec
        if reh is not None:
            _add_rehearsal(parsed, reh[0], reh[1], "\n".join(reh[2]).strip(), last_block, reh[3])
            reh = None
        if rec is not None:
            _add_record(parsed, rec[0], rec[1], "\n".join(rec[2]).strip(), last_block, rec[3])
            rec = None

    for no, line in enumerate(lines, start=1):
        if rec is not None and line.startswith("#### "):
            rec[2].append(line)                 # части прогона Э — внутри записи
            continue
        if line.startswith("#"):
            close_note()
            close_reh()
            header = None
            bm = BLOCK_RE.match(line)
            nm = NOTE_RE.match(line)
            sm = SECTION_RE.match(line) if parsed.module == "5" else None
            if line.startswith("## "):
                in_reh = bool(REH_SECTION_RE.match(line))
                in_bank = parsed.module == "5" and "Банк вопросов беседы" in line
            if sm:
                block = last_block = "4"           # блок 4 модуля 5 разбит на разделы словаря
                parsed.blocks["4"] = "Словарь специальности"
                continue
            if bm:
                block = last_block = bm.group(1)
                parsed.blocks[block] = bm.group(2).strip()
                if bm.group(3):
                    declared[block] = int(bm.group(3))
            elif line.startswith("## "):
                block = None
            if in_reh and line.startswith("### "):
                rm = REH_RE.match(line)
                cm = REC_RE.match(line)
                if rm:
                    reh = [rm.group(1), rm.group(2), [], no]
                elif cm:
                    rec = [cm.group(1), cm.group(2), [], no]
                else:
                    skipped_reh.append(line[4:].split(" — ")[0].strip())
                continue
            cm = REC_RE.match(line)
            if cm and not nm:
                rec = [cm.group(1), cm.group(2), [], no]
                continue
            if nm:
                note = EnNote(nm.group(1), nm.group(2), "")
            elif line.startswith("### З"):
                iss.error(f"строка {no}", "заголовок заметки должен быть «### З1 — название»")
            continue
        if note is not None:
            note.body += line + "\n"
            continue
        if reh is not None:
            reh[2].append(line)
            continue
        if rec is not None:
            rec[2].append(line)
            continue
        if in_bank:
            qm = re.match(r"^\d+\.\s+(.+?)(?:\s+—\s+.*)?$", line.strip())
            if qm:
                parsed.exam_questions.append(qm.group(1).strip())
            continue
        if in_reh:
            if not skipped_reh and line.strip():
                preamble.append(line.strip())
            continue
        if block is None or not line.lstrip().startswith("|"):
            header = None
            continue

        cells = _split_row(line)
        if header is None and cells and cells[0] != "ID":
            header = ["__skip__"]               # сводная таблица раздела, не единицы — пропускается
            continue
        if header == ["__skip__"]:
            continue
        if header is None:
            header = cells
            if "Термин" in header:
                unknown = [c for c in header if c not in TERM_COLUMNS]
                missing = TERM_REQUIRED - set(header)
            else:
                unknown = [c for c in header if c not in COLUMNS]
                missing = REQUIRED - set(header)
            if unknown:
                iss.error(f"строка {no}", f"неизвестные столбцы: {', '.join(unknown)}")
            if missing:
                iss.error(f"строка {no}", f"нет столбцов: {', '.join(sorted(missing))}")
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue  # разделитель шапки
        if len(cells) != len(header):
            iss.error(f"строка {no}", f"в строке {len(cells)} ячеек, в шапке {len(header)}")
            continue
        row = {h: cell(v) for h, v in zip(header, cells)}
        if "Термин" in header:
            _row_to_term(parsed, row, block, no, seen)
        else:
            _row_to_item(parsed, row, block, no, seen)

    close_note()
    close_reh()
    parsed.rehearsal_preamble = "\n".join(preamble)
    if skipped_reh:
        iss.warn("репетиции", "подразделы пока не читаются (доработки под карту пути): "
                 + ", ".join(dict.fromkeys(skipped_reh)))
    codes_seen: dict[str, int] = {}
    for r in parsed.rehearsals:
        if r.code in codes_seen:
            iss.error(r.code, f"репетиция встречается дважды, первый раз в строке {codes_seen[r.code]}")
        codes_seen.setdefault(r.code, r.line)

    counts: dict[str, int] = {}
    for it in parsed.items + parsed.terms:
        counts[it.block] = counts.get(it.block, 0) + 1
    for b, n in declared.items():
        if counts.get(b, 0) != n:
            iss.warn(f"блок {b}", f"в заголовке {n} единиц, в таблице {counts.get(b, 0)}")
    if not parsed.items and not parsed.records and not parsed.terms:
        iss.error("файл", "не найдено ни одной единицы")
    rec_seen: dict[str, int] = {}
    for r in parsed.records:
        if r.code in rec_seen:
            iss.error(r.code, f"запись встречается дважды, первый раз в строке {rec_seen[r.code]}")
        rec_seen.setdefault(r.code, r.line)

    # ссылки на заметки и связи
    note_codes = {n.code for n in parsed.notes} | (known_notes or set())
    codes = {it.code for it in parsed.items} | {t.code for t in parsed.terms} | (known_codes or set())
    dup_notes = {n.code for n in parsed.notes if sum(x.code == n.code for x in parsed.notes) > 1}
    for c in sorted(dup_notes):
        iss.error(c, "заметка встречается дважды")
    # Модуль 1 собран без ссылок вперёд: там неизвестная ссылка — ошибка. В модулях 2–6 банк
    # блоков 0–3 ссылается на заметки и единицы следующих файлов модуля: такая ссылка — предупреждение,
    # она разрешается, когда придёт файл; полную сверку ссылок делает приёмка после всех файлов.
    forward = parsed.module != "1"
    def unresolved(where: str, text: str, ref: str, ref_module: str | None) -> None:
        if forward and (ref_module is None or int(ref_module) >= int(parsed.module)):
            iss.warn(where, f"{text} — пока не загружена, проверится с файлом, где она есть")
        else:
            iss.error(where, text)
    for t in parsed.terms:
        for n in t.notes:
            if n not in note_codes:
                unresolved(t.code, f"ссылка на несуществующую заметку {n}", n, None)
    for it in parsed.items:
        for n in it.notes:
            if n not in note_codes:
                unresolved(it.code, f"ссылка на несуществующую заметку {n}", n, None)
        for link in it.links + it.extra.get("time_links", []):
            if link == it.code:
                iss.error(it.code, "связь сама на себя")
            elif link not in codes:
                sid = split_id(link)
                unresolved(it.code, f"связь на несуществующую единицу {link}", link, sid[0] if sid else None)
    return parsed


def _row_to_item(parsed: ParsedEnglish, row: dict[str, str], block: str,
                 no: int, seen: dict[str, int]) -> None:
    iss = parsed.issues
    code = row.get("ID", "")
    where = code or f"строка {no}"
    sid = split_id(code)
    if sid is None or (parsed.module == "1") != bool(ID_RE.match(code)):
        iss.error(where, "ID должен быть вида 1.07 (модуль 1) или 2.3.15 (модули 2–6)")
        return
    if sid[0] != parsed.module:
        iss.error(code, f"ID модуля {sid[0]} в файле модуля {parsed.module}")
    if sid[1] != block:
        iss.error(code, f"ID из блока {sid[1]} стоит в таблице блока {block}")
    if code in seen:
        iss.error(code, f"повтор ID, первый раз в строке {seen[code]}")
        return
    seen[code] = no

    prompt, target = row.get("Подсказка", ""), row.get("Целевая фраза", "")
    if not prompt:
        iss.error(code, "пустая подсказка")
    if not target:
        iss.error(code, "пустая целевая фраза")

    extra: dict = {}
    variants_cell = row.get("Допустимые варианты", "")
    mm = MEANING_RE.match(variants_cell)
    assembly = bool(mm)
    variants: list[str] = []
    if assembly:
        extra["meaning"] = mm.group(1).strip()
    elif variants_cell:
        variants = [v.strip() for v in variants_cell.split(" / ") if v.strip()]

    grammar = row.get("Грамматика", "")
    notes = NOTE_CODE_RE.findall(grammar)
    if "готовый кусок" in grammar:
        extra["ready_chunk"] = True
    if "…" in target:
        extra["wildcard"] = True

    sound_cell = row.get("Звук", "")
    if sound_cell.startswith("аудио:"):
        audio_text, sound = sound_cell[len("аудио:"):].strip(), None
        if not audio_text:
            iss.error(code, "«аудио:» без текста")
    else:
        audio_text, sound = target, (sound_cell or None)

    links = _id_list(row.get("Связи", ""), code, "связь", iss)
    time_links = _id_list(row.get("Время", ""), code, "связь по времени", iss)
    if time_links:
        extra["time_links"] = time_links

    parsed.items.append(EnItem(
        code=code, block=block, prompt=prompt, target=target, variants=variants,
        notes=notes, sound=sound, audio_text=audio_text, links=links,
        assembly=assembly, extra=extra, sort_key=sort_key_of(code),
        line=no,
    ))


def _row_to_term(parsed: ParsedEnglish, row: dict[str, str], block: str, no: int, seen: dict[str, int]) -> None:
    iss = parsed.issues
    code = row.get("ID", "")
    sid = split_id(code)
    if sid is None or sid[0] != "5":
        iss.error(code or f"строка {no}", "ID единицы на узнавание должен быть вида 5.1.01")
        return
    if sid[1] != block:
        iss.error(code, f"ID из блока {sid[1]} стоит в таблице блока {block}")
    if code in seen:
        iss.error(code, f"повтор ID, первый раз в строке {seen[code]}")
        return
    seen[code] = no
    for f in ("Термин", "Значение", "Предложение", "Перевод"):
        if not row.get(f):
            iss.error(code, f"пустое поле «{f}»")
    alts = [v.strip() for v in row.get("Допустимые значения", "").split(" / ") if v.strip()]
    parsed.terms.append(EnTerm(
        code, block, row.get("Раздел") or None, row.get("Термин", ""), row.get("Значение", ""), alts,
        row.get("Предложение", ""), row.get("Перевод", ""), row.get("Разбор") or None, row.get("Ист") or None,
        NOTE_CODE_RE.findall(row.get("Грамматика", "")), row.get("Звук") or None, sort_key_of(code), no))


def _add_record(parsed: ParsedEnglish, code: str, title: str, body: str, block: str | None, line: int) -> None:
    iss = parsed.issues
    if not body:
        iss.error(code, "пустая запись")
        return
    fields = parse_fields(re.split(r"^#### ", body, flags=re.M)[0])
    prefix = re.match(r"^(СР|СВ|[А-ЯЁ])", code).group(1)
    if prefix == "Э":
        # прогон экзамена: части «#### Часть N. название» со своими полями
        parts = {}
        for chunk in re.split(r"^#### ", body, flags=re.M)[1:]:
            head, _, rest = chunk.partition("\n")
            pm = re.match(r"^Часть (\d)\. (.+)$", head.strip())
            if pm:
                pf = parse_fields(rest)
                if not pf:
                    pf = {"Вопросы по статье": "\n".join(
                        q.group(1) for q in re.finditer(r"^\d+\.\s+(.+?)(?:\s+—\s+.*)?$", rest, re.M))}
                parts[pm.group(1)] = {"title": pm.group(2).strip(), **pf}
        fields["parts"] = parts
        if len(parts) != 4:
            iss.error(code, f"в прогоне {len(parts)} частей, нужно четыре «#### Часть N. …»")
    if prefix == "Д":
        kind = "roleplay" if "Скрытая ситуация" in fields else "discussion"
    else:
        kind = REC_KIND[prefix]
    bf = fields.get("Блок", "")
    bm = re.match(r"^(?:(\d)\.)?(\d+)", bf)
    if bm:
        if bm.group(1) and bm.group(1) != parsed.module:
            iss.warn(code, f"«Блок.» {bf} — из модуля {bm.group(1)}, файл модуля {parsed.module}")
        block = bm.group(2)
    missing = [f for f in REC_NEED.get(kind, ()) if not fields.get(f)]
    if missing:
        iss.warn(code, "нет полей: " + ", ".join(f"«{f}.»" for f in missing))
    parsed.records.append(EnRecord(code, kind, title, body, block, fields,
                                   int(parsed.module) * 100000 + len(parsed.records) + 1, line))


def _id_list(cell_text: str, code: str, what: str, iss: Issues) -> list[str]:
    out: list[str] = []
    for part in cell_text.split(","):
        part = part.strip()
        if not part or part in ("—", "-"):
            continue
        if split_id(part) is None:
            iss.error(code, f"{what} {part!r} не похожа на ID")
            continue
        out.append(part)
    return out


def _number(text: str) -> int | None:
    m = re.search(r"не меньше (\d+|\w+)", text)
    if not m:
        return None
    word = m.group(1)
    return int(word) if word.isdigit() else NUMBER_WORDS.get(word)


def _questions(text: str) -> list[str]:
    return [q.strip() for q in QUESTION_RE.findall(text) if len(q.strip()) > 1]


def _add_rehearsal(parsed: ParsedEnglish, code: str, title: str, body: str,
                   block: str | None, line: int) -> None:
    iss = parsed.issues
    if block is None:
        iss.error(code, "репетиция стоит раньше таблицы блока: не к чему привязать")
        return
    if not body:
        iss.error(code, "пустая репетиция")
        return
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    sort_key = len(parsed.rehearsals) + 1
    if code.startswith("М"):
        parts, about = [], []
        for p in paragraphs:
            for ln in p.split("\n"):
                m = PART_RE.match(ln.strip())
                if m and not m.group(1).startswith("Зачёт"):
                    words = [w.strip(" .") for w in m.group(2).split(" — ") if w.strip(" .")]
                    parts.append({"name": m.group(1).strip(), "keywords": words})
                elif ln.strip():
                    about.append(ln.strip())
        if not parts:
            iss.error(code, "в плане монолога нет частей вида «Название: ключевые слова — …»")
            return
        criteria = " ".join(p for p in about if "Зачёт" in p)
        data = {"parts": parts, "about": " ".join(about), "criteria": criteria,
                "min_sentences": None}
        ms = re.search(r"не меньше (\d+) предложени", criteria or " ".join(about))
        if ms:
            data["min_sentences"] = int(ms.group(1))
        parsed.rehearsals.append(EnRehearsal(code, "monologue", title, body, block, data, sort_key, line))
        return
    fields: dict[str, str] = {}
    for p in paragraphs:
        m = LABEL_RE.match(p.replace("\n", " "))
        if m:
            fields[m.group(1)] = m.group(2).strip()
    if "Первая реплика бота" not in fields:
        if "Позиция бота." in body or "Скрытая ситуация." in body or "Роль бота." in body:
            _add_record(parsed, code, title, body, block, line)    # карточка сценария — запись
        else:
            iss.error(code, "в сценарии нет строки «Первая реплика бота: …»")
        return
    bot_key = next((k for k in fields if k.startswith("Вопросы бота")), None)
    ask_key = next((k for k in fields if k.startswith("Что спрашивает")), None)
    situation = fields.get("Ситуация", "")
    bot_q = _questions(fields[bot_key]) if bot_key else []
    ask_q = _questions(fields[ask_key]) if ask_key else []
    if not situation:
        iss.error(code, "в сценарии нет строки «Ситуация: …»")
        return
    if len(bot_q) < 3:
        iss.error(code, "в сценарии меньше трёх вопросов бота")
        return
    data = {"situation": situation, "opening": fields["Первая реплика бота"], "bot_questions": bot_q,
            "learner_questions": ask_q, "learner_min": (_number(ask_key) if ask_key else None) or 5}
    parsed.rehearsals.append(EnRehearsal(code, "scenario", title, body, block, data, sort_key, line))
