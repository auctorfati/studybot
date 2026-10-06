-- Слой К7: отметки отправленного. Вечернее уведомление и недельная сводка
-- уходят один раз даже после перезапуска бота.
CREATE TABLE sent_marks (
    kind          TEXT NOT NULL CHECK (kind IN ('evening', 'weekly')),
    mark          TEXT NOT NULL,                -- учебный день или понедельник недели
    sent_at       TEXT NOT NULL,
    PRIMARY KEY (kind, mark)
);
