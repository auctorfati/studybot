-- Слой К6: план сессии хранится в базе — перезапуск бота сессию не теряет.
CREATE TABLE session_steps (
    id            INTEGER PRIMARY KEY,
    session_id    INTEGER NOT NULL REFERENCES sessions(id),
    seq           INTEGER NOT NULL,
    track         TEXT NOT NULL CHECK (track IN ('en', 'psy')),
    kind          TEXT NOT NULL,                -- task, dialog, monologue, cp_questions, cp_monologue
    slot          TEXT NOT NULL,                -- review, new, hearing, talk, control, topup
    item_id       INTEGER REFERENCES items(id),
    est_sec       INTEGER NOT NULL,
    payload_json  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'planned'
                    CHECK (status IN ('planned', 'active', 'done', 'deferred', 'skipped')),
    sent_at       TEXT,
    done_at       TEXT,
    UNIQUE (session_id, seq)
);
CREATE INDEX session_steps_open ON session_steps(session_id, status);

ALTER TABLE sessions ADD COLUMN last_activity_at TEXT;
ALTER TABLE sessions ADD COLUMN mode TEXT;
