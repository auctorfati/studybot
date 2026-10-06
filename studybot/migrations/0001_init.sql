-- Схема ядра, версия 1.
-- Моменты — TEXT в UTC ISO 8601; учебные дни — TEXT 'YYYY-MM-DD'.
-- JSON-поля — TEXT с JSON; флаги — INTEGER 0/1.

-- Импорты контента: каждый принятый файл получает номер версии.
CREATE TABLE content_versions (
    id            INTEGER PRIMARY KEY,
    imported_at   TEXT NOT NULL,
    track         TEXT NOT NULL CHECK (track IN ('en', 'psy')),
    file_name     TEXT NOT NULL,
    file_sha256   TEXT NOT NULL,
    summary_json  TEXT NOT NULL DEFAULT '{}'   -- новые, изменённые, удалённые, предупреждения
);

-- Темы психологии из карты тем; порядок тем — sort_key.
CREATE TABLE topics (
    code          TEXT PRIMARY KEY,             -- 'П16'
    track         TEXT NOT NULL DEFAULT 'psy' CHECK (track IN ('psy')),
    area          TEXT NOT NULL,                -- 'PATO', 'PSY', ...
    grp           TEXT NOT NULL,                -- блок карты: 'Д'
    title         TEXT NOT NULL,
    target_level  INTEGER NOT NULL CHECK (target_level BETWEEN 1 AND 4),
    sort_key      INTEGER NOT NULL,
    content_version INTEGER REFERENCES content_versions(id),
    archived      INTEGER NOT NULL DEFAULT 0
);

-- Позиции контента обоих треков.
CREATE TABLE items (
    id            INTEGER PRIMARY KEY,
    track         TEXT NOT NULL CHECK (track IN ('en', 'psy')),
    code          TEXT NOT NULL,                -- '1.07', 'Д-1-01'
    unit          TEXT NOT NULL,                -- en: блок '1'; psy: тема 'П16'
    grp           TEXT,                         -- en: модуль '1'; psy: блок 'Д'
    kind          TEXT NOT NULL CHECK (kind IN
                    ('phrase', 'assembly', 'card', 'open', 'distinction', 'vignette')),
    level         INTEGER CHECK (level IS NULL OR level BETWEEN 1 AND 4),
    is_core       INTEGER NOT NULL DEFAULT 1,   -- psy: ядро/резерв
    prompt        TEXT NOT NULL,                -- en: подсказка; psy: В или текст виньетки
    answer        TEXT NOT NULL,                -- en: целевая фраза; psy: О или ключ
    variants_json TEXT NOT NULL DEFAULT '[]',   -- en: допустимые варианты
    notes_json    TEXT NOT NULL DEFAULT '[]',   -- en: коды заметок ['З1']
    sound         TEXT,                         -- en: столбец «Звук»
    audio_text    TEXT,                         -- en: текст для озвучки
    links_json    TEXT NOT NULL DEFAULT '[]',   -- en: связи, уже двусторонние
    extra_json    TEXT NOT NULL DEFAULT '{}',   -- psy: вопросы виньетки, угол наставника
    source_ref    TEXT,                         -- psy: строка «Ист» как в файле
    fragment      TEXT,                         -- psy: текст страниц источника
    fragment_status TEXT NOT NULL DEFAULT 'n/a'
                    CHECK (fragment_status IN ('n/a', 'ok', 'truncated', 'missing')),
    prompt_hash   TEXT NOT NULL,                -- смена формулировки: прогресс сохраняется
    answer_hash   TEXT NOT NULL,                -- смена ответа: ближайший показ — завтра
    sort_key      INTEGER NOT NULL,             -- порядок внутри файла
    content_version INTEGER NOT NULL REFERENCES content_versions(id),
    archived      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (track, code)
);
CREATE INDEX items_unit ON items(track, unit, archived);

-- Грамматические заметки английского.
CREATE TABLE notes (
    track         TEXT NOT NULL CHECK (track IN ('en')),
    code          TEXT NOT NULL,                -- 'З1'
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    content_version INTEGER NOT NULL REFERENCES content_versions(id),
    PRIMARY KEY (track, code)
);

-- Аудио: два темпа на фразу; после первой отправки храним file_id Telegram.
CREATE TABLE audio_files (
    item_id       INTEGER NOT NULL REFERENCES items(id),
    speed         TEXT NOT NULL CHECK (speed IN ('slow', 'normal')),
    path          TEXT NOT NULL,
    text_hash     TEXT NOT NULL,                -- от какого текста сгенерировано
    tg_file_id    TEXT,
    PRIMARY KEY (item_id, speed)
);

-- Прогресс по позиции. Привязан к items.id, то есть к коду позиции.
CREATE TABLE item_state (
    item_id       INTEGER PRIMARY KEY REFERENCES items(id),
    stage         INTEGER NOT NULL DEFAULT 0,   -- en: 0 не введена, 2–4 ступени; psy: 0
    streak        INTEGER NOT NULL DEFAULT 0,   -- верных подряд на ступенях 2–3
    step          INTEGER,                      -- индекс в сетке интервалов; NULL — вне сетки
    due_date      TEXT,                         -- учебный день ближайшего показа
    reps          INTEGER NOT NULL DEFAULT 0,
    lapses        INTEGER NOT NULL DEFAULT 0,
    spoken_in_dialog INTEGER NOT NULL DEFAULT 0,
    closed        INTEGER NOT NULL DEFAULT 0,   -- en: фраза закрыта; psy: позиция в редком повторении
    introduced_at TEXT,
    last_review_at TEXT,
    deferred_until TEXT                         -- «сейчас не могу слушать», отказ сервиса
);
CREATE INDEX item_state_due ON item_state(due_date);

-- Прогресс по теме психологии.
CREATE TABLE topic_state (
    topic_code    TEXT PRIMARY KEY REFERENCES topics(code),
    state         TEXT NOT NULL DEFAULT 'not_started'
                    CHECK (state IN ('not_started', 'probe', 'study', 'closed', 'rare')),
    level         INTEGER NOT NULL DEFAULT 0,   -- достигнутый уровень
    probe_json    TEXT NOT NULL DEFAULT '{}',   -- ход среза сверху вниз
    updated_at    TEXT
);

-- Сессии.
CREATE TABLE sessions (
    id            INTEGER PRIMARY KEY,
    study_date    TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('5', '15', '30', 'evening', 'long')),
    ordered_sec   INTEGER NOT NULL,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'done')),
    active_sec_en INTEGER NOT NULL DEFAULT 0,
    active_sec_psy INTEGER NOT NULL DEFAULT 0,
    voice_sec     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX sessions_day ON sessions(study_date);

-- Каждый ответ. Формат совместим с FSRS: позиция, момент, оценка 1–4, длительность.
CREATE TABLE reviews (
    id            INTEGER PRIMARY KEY,
    item_id       INTEGER NOT NULL REFERENCES items(id),
    session_id    INTEGER REFERENCES sessions(id),
    ts            TEXT NOT NULL,
    study_date    TEXT NOT NULL,
    format        TEXT NOT NULL,                -- 'intro', 'transform', 'listen', 'speak', 'card', ...
    mode          TEXT NOT NULL CHECK (mode IN ('voice', 'text')),
    answer        TEXT,                         -- текст или расшифровка
    verdict       TEXT NOT NULL,                -- 'correct', 'hard', 'wrong', 'partial', 'deferred', 'shown'
    rating        INTEGER CHECK (rating IS NULL OR rating BETWEEN 1 AND 4),
    judge         TEXT NOT NULL CHECK (judge IN ('code', 'self', 'cheap', 'flagship')),
    schedules     INTEGER NOT NULL DEFAULT 1,   -- двигает ли интервал (текст на ступени 4 — нет)
    stage_before  INTEGER,
    stage_after   INTEGER,
    step_before   INTEGER,
    step_after    INTEGER,
    elapsed_sec   INTEGER NOT NULL DEFAULT 0,   -- учтённое активное время
    raw_sec       INTEGER NOT NULL DEFAULT 0,   -- фактическое, для калибровки оценок
    verdict_json  TEXT,                         -- ответ модели целиком
    disputed      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX reviews_item ON reviews(item_id, ts);
CREATE INDEX reviews_day ON reviews(study_date);

-- Итог дня.
CREATE TABLE days (
    study_date    TEXT PRIMARY KEY,
    sec_en        INTEGER NOT NULL DEFAULT 0,
    sec_psy       INTEGER NOT NULL DEFAULT 0,
    sec_voice     INTEGER NOT NULL DEFAULT 0,
    new_en        INTEGER NOT NULL DEFAULT 0,   -- фразы
    new_psy_units INTEGER NOT NULL DEFAULT 0,   -- единицы лимита
    reviews_en    INTEGER NOT NULL DEFAULT 0,
    reviews_psy   INTEGER NOT NULL DEFAULT 0,
    counted       INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,   -- кнопка «не сегодня»
    speech_held   INTEGER NOT NULL DEFAULT 0    -- речевая часть придержана на вечер
);

-- Журнал ошибок английского и пробелов психологии.
CREATE TABLE errors (
    id            INTEGER PRIMARY KEY,
    track         TEXT NOT NULL CHECK (track IN ('en', 'psy')),
    item_id       INTEGER NOT NULL REFERENCES items(id),
    review_id     INTEGER REFERENCES reviews(id),
    ts            TEXT NOT NULL,
    study_date    TEXT NOT NULL,
    tag           TEXT NOT NULL,                -- ключ из enums.EN_ERROR_TAGS / PSY_GAP_TAGS
    detail        TEXT,                         -- psy 'confusion': какие конструкты
    answer        TEXT,
    explanation   TEXT,
    correction    TEXT,
    used_count    INTEGER NOT NULL DEFAULT 0    -- сколько раз показан в «найди ошибку»
);
CREATE INDEX errors_tag ON errors(track, tag);

-- Оспоренные вердикты.
CREATE TABLE disputes (
    id            INTEGER PRIMARY KEY,
    review_id     INTEGER NOT NULL UNIQUE REFERENCES reviews(id),
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    resolution    TEXT CHECK (resolution IS NULL OR resolution IN
                    ('verdict_changed', 'key_changed', 'variants_changed', 'upheld'))
);

-- Кандидаты в допустимые варианты английского.
CREATE TABLE variants_pending (
    id            INTEGER PRIMARY KEY,
    item_id       INTEGER NOT NULL REFERENCES items(id),
    normalized    TEXT NOT NULL,
    raw_answer    TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    seen_count    INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
    UNIQUE (item_id, normalized)
);

-- Кэш вердиктов модели: пара «позиция + нормализованный ответ» при той же версии ответа.
CREATE TABLE verdict_cache (
    item_id       INTEGER NOT NULL REFERENCES items(id),
    normalized    TEXT NOT NULL,
    answer_hash   TEXT NOT NULL,
    verdict_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (item_id, normalized, answer_hash)
);

-- Вызовы моделей с токенами и стоимостью.
CREATE TABLE llm_calls (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    study_date    TEXT NOT NULL,
    tier          TEXT NOT NULL CHECK (tier IN ('cheap', 'flagship')),
    task          TEXT NOT NULL,
    model         TEXT NOT NULL,
    tokens_in     INTEGER NOT NULL DEFAULT 0,
    tokens_out    INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL,
    error         TEXT
);
CREATE INDEX llm_calls_day ON llm_calls(study_date);

-- Сбои внешних сервисов — для команды «состояние».
CREATE TABLE service_events (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    service       TEXT NOT NULL CHECK (service IN ('llm', 'stt', 'tts', 'telegram', 'backup', 'import')),
    level         TEXT NOT NULL CHECK (level IN ('warning', 'error')),
    message       TEXT NOT NULL
);

-- Настройки, которые меняются из бота.
CREATE TABLE settings (
    key           TEXT PRIMARY KEY,
    value_json    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
