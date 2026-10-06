-- Слой К2: область знаний у позиции и связь позиции с несколькими темами.
-- Виньетка может закрывать две темы («Тема: П12, П15»), срез сверху вниз
-- ищет виньетку темы по этой таблице.

ALTER TABLE items ADD COLUMN area TEXT;         -- 'EN1' для модуля английского, 'PATO', 'PSY', ...
CREATE INDEX items_area ON items(track, area, archived);

CREATE TABLE item_topics (
    item_id       INTEGER NOT NULL REFERENCES items(id),
    topic_code    TEXT NOT NULL,
    is_primary    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, topic_code)
);
CREATE INDEX item_topics_topic ON item_topics(topic_code);
