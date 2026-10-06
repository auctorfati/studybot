-- Слой К8.1: справочник английского, репетиции блока сборки, «Почему так?»,
-- критерий выхода модуля, учёт чтения.

-- Справочник: обзорные страницы. Новая версия файла целиком заменяет прежнюю.
CREATE TABLE ref_pages (
    code          TEXT PRIMARY KEY,             -- 'С1'
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    sort_key      INTEGER NOT NULL,
    content_version INTEGER NOT NULL REFERENCES content_versions(id)
);

-- Справочник: словари блоков. «Где» — код единицы, в которой слово встретилось впервые.
CREATE TABLE vocab (
    id            INTEGER PRIMARY KEY,
    module        TEXT NOT NULL,                -- '1'
    block         TEXT NOT NULL,                -- '0'
    word          TEXT NOT NULL,
    translation   TEXT NOT NULL,
    where_code    TEXT NOT NULL,                -- '1.07', '2.3.15'
    note          TEXT,
    sort_key      INTEGER NOT NULL,
    content_version INTEGER NOT NULL REFERENCES content_versions(id)
);
CREATE INDEX vocab_block ON vocab(module, block);

-- Репетиции блока сборки: план монолога (М) и сценарии диалога (Д), тексты блока.
CREATE TABLE rehearsals (
    code          TEXT PRIMARY KEY,             -- 'М1', 'Д1'
    area          TEXT NOT NULL,                -- 'EN1'
    block         TEXT NOT NULL,                -- блок сборки: '6'
    kind          TEXT NOT NULL,                -- 'monologue', 'scenario'; виды карты пути — позже
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    data_json     TEXT NOT NULL DEFAULT '{}',   -- части и ключевые слова; ситуация, реплики, вопросы
    sort_key      INTEGER NOT NULL,
    content_version INTEGER NOT NULL REFERENCES content_versions(id),
    archived      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX rehearsals_block ON rehearsals(area, block, archived);

-- Ход репетиций: ступень монолога (1 — по ключевым словам, 2 — по названиям частей,
-- 3 — без опоры) и очередь сценариев по кругу.
CREATE TABLE rehearsal_state (
    code          TEXT PRIMARY KEY,
    stage         INTEGER NOT NULL DEFAULT 1 CHECK (stage BETWEEN 1 AND 3),
    attempts      INTEGER NOT NULL DEFAULT 0,
    passes        INTEGER NOT NULL DEFAULT 0,
    uses          INTEGER NOT NULL DEFAULT 0,
    last_at       TEXT
);

-- Аудио реплик сценариев: вопросы бота в критерии выхода звучат только звуком.
-- Ключ — 'Д1:0' (первая реплика), 'Д1:3' (третий вопрос бота).
CREATE TABLE line_audio (
    key           TEXT NOT NULL,
    speed         TEXT NOT NULL CHECK (speed IN ('slow', 'normal')),
    path          TEXT NOT NULL,
    text_hash     TEXT NOT NULL,
    tg_file_id    TEXT,
    PRIMARY KEY (key, speed)
);

-- Критерий выхода модуля: каждая попытка части.
CREATE TABLE exit_attempts (
    id            INTEGER PRIMARY KEY,
    area          TEXT NOT NULL,                -- 'EN1'
    part          TEXT NOT NULL CHECK (part IN ('monologue', 'dialog')),
    ts            TEXT NOT NULL,
    study_date    TEXT NOT NULL,
    session_id    INTEGER REFERENCES sessions(id),
    rehearsal     TEXT,                         -- 'М1' или 'Д2'
    passed        INTEGER NOT NULL,
    verdict_json  TEXT
);
CREATE INDEX exit_attempts_area ON exit_attempts(area, part, study_date);

CREATE TABLE en_modules (
    area          TEXT PRIMARY KEY,
    closed_at     TEXT                          -- критерий выхода сдан
);

-- «Почему так?»: объяснение запрашивается один раз на пару «единица и нормализованный
-- ответ», без ответа — один раз на единицу.
CREATE TABLE explain_cache (
    item_id       INTEGER NOT NULL REFERENCES items(id),
    normalized    TEXT NOT NULL,                -- '' — без ответа
    context       TEXT NOT NULL,                -- версия ответа единицы и исходная фраза превращения
    text          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (item_id, normalized, context)
);

-- Открытое чтение (объяснение, позже разбор темы): время идёт в сессию при следующем
-- действии, не больше предела по длине текста.
ALTER TABLE sessions ADD COLUMN reading_json TEXT;
