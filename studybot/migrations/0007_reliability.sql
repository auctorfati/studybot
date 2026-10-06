-- Слой К8: отметка еженедельной копии базы в Telegram. Копия уходит со сводкой;
-- если сводки в это воскресенье нет (пауза, «не сегодня»), копия идёт одна, без звука.
-- CHECK в SQLite не меняется на месте, поэтому таблица пересоздаётся.
CREATE TABLE sent_marks_new (
    kind          TEXT NOT NULL CHECK (kind IN ('evening', 'weekly', 'weekly_db')),
    mark          TEXT NOT NULL,                -- учебный день или понедельник недели
    sent_at       TEXT NOT NULL,
    PRIMARY KEY (kind, mark)
);
INSERT INTO sent_marks_new (kind, mark, sent_at) SELECT kind, mark, sent_at FROM sent_marks;
DROP TABLE sent_marks;
ALTER TABLE sent_marks_new RENAME TO sent_marks;
