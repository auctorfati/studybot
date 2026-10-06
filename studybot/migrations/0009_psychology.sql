-- Слой К9: разборы тем психологии в импорте и трек психологии.

-- Разборы тем: новая версия файла целиком заменяет разборы тех тем, которые в ней есть.
CREATE TABLE digests (
    topic_code    TEXT PRIMARY KEY,             -- 'ПС30'
    area          TEXT NOT NULL,                -- 'PSY'
    title         TEXT NOT NULL,
    file_name     TEXT NOT NULL,
    parts         INTEGER NOT NULL,
    content_version INTEGER NOT NULL REFERENCES content_versions(id)
);

CREATE TABLE digest_parts (
    topic_code    TEXT NOT NULL REFERENCES digests(topic_code) ON DELETE CASCADE,
    num           INTEGER NOT NULL,             -- номер части с 1
    title         TEXT NOT NULL,                -- 'Коротко'
    body          TEXT NOT NULL,                -- без строки «Позиции»
    positions_json TEXT NOT NULL DEFAULT '[]',  -- коды позиций банка, которые часть объясняет
    PRIMARY KEY (topic_code, num)
);

-- Чтение разборов: где остановился, отложен ли, прочитан ли (прогресса тем не касается).
CREATE TABLE digest_reads (
    topic_code    TEXT PRIMARY KEY,
    part          INTEGER NOT NULL DEFAULT 0,   -- последняя показанная часть
    status        TEXT NOT NULL DEFAULT 'reading'
                    CHECK (status IN ('reading', 'deferred', 'read')),
    started_at    TEXT,
    updated_at    TEXT,
    read_at       TEXT
);

-- Кнопка источника: фрагмент в русском пересказе, один раз на позицию и версию фрагмента.
CREATE TABLE source_retell (
    item_id       INTEGER NOT NULL REFERENCES items(id),
    fragment_hash TEXT NOT NULL,
    text          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (item_id, fragment_hash)
);
