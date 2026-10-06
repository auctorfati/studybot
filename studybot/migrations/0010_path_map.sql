-- Доработки под карту пути английского, часть 1 (карта пути, раздел 9): записи банков
-- модулей 2–6 и открытие модулей по критерию выхода.

-- Записи банка: истории Р, слушание Л, объяснение О, сравнение СР, совет СВ, утверждения У,
-- темы разговора Т, тексты Ч, прогоны Э, карточки Д со скрытой ситуацией или позицией бота.
CREATE TABLE records (
    code          TEXT PRIMARY KEY,             -- 'Р1', 'СР10', 'Л6'
    area          TEXT NOT NULL,                -- 'EN2'
    block         TEXT,                         -- блок модуля, к которому запись относится
    kind          TEXT NOT NULL,                -- story, listening, explain, compare, advice, statement,
                                                -- topic, text, exam, roleplay, discussion
    title         TEXT NOT NULL,
    fields_json   TEXT NOT NULL DEFAULT '{}',   -- поля записи: «Текст», «Вопросы», «Ключ», …
    body          TEXT NOT NULL,
    file_name     TEXT NOT NULL,
    sort_key      INTEGER NOT NULL,
    content_version INTEGER NOT NULL REFERENCES content_versions(id),
    archived      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX records_kind ON records(kind, area, archived);

-- Модуль открыт: модуль 1 — с запуска, следующий — по сданному критерию выхода предыдущего.
ALTER TABLE en_modules ADD COLUMN opened_at TEXT;

-- Названия блоков английского: открытие нового блока начинается с его плана и правил.
CREATE TABLE en_block_titles (
    area          TEXT NOT NULL,
    block         TEXT NOT NULL,
    title         TEXT NOT NULL,
    PRIMARY KEY (area, block)
);

-- Записи в свободной речи идут по кругу: сколько раз использована и когда.
CREATE TABLE record_state (
    code          TEXT PRIMARY KEY,
    uses          INTEGER NOT NULL DEFAULT 0,
    last_at       TEXT
);

-- Части критерия выхода модулей 2–6 разные (карта пути, разделы 3–7): снять ограничение на имя части.
-- Таблица до этой миграции нигде не заполнялась (критерий выхода появился в К8.1, бот ещё не запущен).
DROP TABLE exit_attempts;
CREATE TABLE exit_attempts (
    id            INTEGER PRIMARY KEY,
    area          TEXT NOT NULL,
    part          TEXT NOT NULL,                -- monologue, dialog, story, explain, compare, advice,
                                                -- discussion, talk, listening
    ts            TEXT NOT NULL,
    study_date    TEXT NOT NULL,
    session_id    INTEGER REFERENCES sessions(id),
    rehearsal     TEXT,
    passed        INTEGER NOT NULL,
    verdict_json  TEXT
);
CREATE INDEX exit_attempts_area ON exit_attempts(area, part, study_date);
