-- Доработки под карту пути, часть 2: модуль 5 — академическая ветка (карта пути, раздел 6;
-- банк модуля 5, раздел 1). Единица на узнавание: термин в предложении из статьи,
-- ответ — значение по-русски. Своя очередь, лимит нового и потолок.

CREATE TABLE terms (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,         -- '5.1.01', '5.4.001'
    block         TEXT NOT NULL,                -- '1'…'4'
    section       TEXT,                         -- блок 4: раздел словаря специальности
    term          TEXT NOT NULL,
    meaning       TEXT NOT NULL,
    alt_json      TEXT NOT NULL DEFAULT '[]',   -- допустимые значения
    sentence      TEXT NOT NULL,
    translation   TEXT NOT NULL,
    analysis      TEXT,                         -- блок 3: разбор предложения
    source_ref    TEXT,                         -- 'PATO S10, с. 3'
    context       TEXT,                         -- абзац статьи вокруг предложения (кнопка «Контекст»)
    notes_json    TEXT NOT NULL DEFAULT '[]',
    sound         TEXT,
    answer_hash   TEXT NOT NULL,
    sort_key      INTEGER NOT NULL,
    file_name     TEXT NOT NULL,
    content_version INTEGER NOT NULL REFERENCES content_versions(id),
    archived      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE term_state (
    term_id       INTEGER PRIMARY KEY REFERENCES terms(id),
    step          INTEGER,                      -- шаг сетки; NULL — не введён
    due_date      TEXT,
    introduced_at TEXT,
    reps          INTEGER NOT NULL DEFAULT 0,
    lapses        INTEGER NOT NULL DEFAULT 0,
    closed        INTEGER NOT NULL DEFAULT 0,   -- 30 дней без срыва
    last_review_at TEXT
);
CREATE INDEX term_state_due ON term_state(due_date);

CREATE TABLE term_reviews (
    id            INTEGER PRIMARY KEY,
    term_id       INTEGER NOT NULL REFERENCES terms(id),
    session_id    INTEGER REFERENCES sessions(id),
    ts            TEXT NOT NULL,
    study_date    TEXT NOT NULL,
    kind          TEXT NOT NULL,                -- intro, term, parse
    answer        TEXT,
    verdict       TEXT NOT NULL,                -- correct, wrong, partial, shown, deferred
    rating        INTEGER,
    judge         TEXT NOT NULL,
    step_before   INTEGER,
    step_after    INTEGER,
    elapsed_sec   INTEGER NOT NULL DEFAULT 0,
    verdict_json  TEXT
);

-- Учёт академической ветки внутри английского времени и лимита новых терминов.
ALTER TABLE days ADD COLUMN sec_en_acad INTEGER NOT NULL DEFAULT 0;
ALTER TABLE days ADD COLUMN new_terms INTEGER NOT NULL DEFAULT 0;

-- Тексты Ч: повтор через 7 и 30 дней; закрыт по итогам повтора через 30 дней (банк блоков 5–6).
CREATE TABLE text_state (
    code          TEXT PRIMARY KEY,             -- 'Ч1'
    stage         INTEGER NOT NULL DEFAULT 0,   -- 0 — первое чтение; 1 — ждёт повтора через 7; 2 — через 30
    due_date      TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    closed        INTEGER NOT NULL DEFAULT 0,
    last_json     TEXT,
    updated_at    TEXT
);

-- Фразы блоков 7–8 модуля 5 идут по обычной лестнице своим лимитом (с модуля 2).
ALTER TABLE days ADD COLUMN new_acad_phrases INTEGER NOT NULL DEFAULT 0;

-- Прогоны экзамена Э (банк блока 9): части 1–4, по одной за раз; прогон засчитан, если сданы все.
CREATE TABLE exam_runs (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL,                -- 'Э1'
    started_at    TEXT NOT NULL,
    parts_json    TEXT NOT NULL DEFAULT '{}',   -- {"1": {"passed": true, "verdict": {...}}, …}
    finished_date TEXT,                         -- учебный день, когда сдана последняя часть
    passed        INTEGER
);
