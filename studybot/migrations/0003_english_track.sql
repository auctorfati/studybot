-- Слой К4: трек английского.

-- Заметка показывается после первого знакомства с фразой, которая на неё ссылается.
ALTER TABLE notes ADD COLUMN shown_at TEXT;

-- Блоки модуля: открытие, полное введение, контрольная точка.
CREATE TABLE en_blocks (
    area          TEXT NOT NULL,                -- 'EN1'
    block         TEXT NOT NULL,                -- '0', '1', ...
    opened_at     TEXT,                         -- первая фраза введена
    introduced_at TEXT,                         -- все фразы блока введены
    cp_attempts   INTEGER NOT NULL DEFAULT 0,
    cp_passed_at  TEXT,
    cp_last_json  TEXT,                         -- итог последней попытки
    PRIMARY KEY (area, block)
);
