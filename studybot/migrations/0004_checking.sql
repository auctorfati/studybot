-- Слой К5: проверка ответов.
-- Снимок состояния позиции до ответа: оспоренный вердикт откатывается к нему,
-- поэтому «до разбора срывом не считается».
ALTER TABLE reviews ADD COLUMN state_before_json TEXT;
-- Вызов модели привязан к ответу, когда он был.
ALTER TABLE llm_calls ADD COLUMN review_id INTEGER;
