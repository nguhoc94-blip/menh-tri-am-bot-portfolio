-- 034_user_daily_tokens.sql — Daily OpenAI token counter per sender_id

ALTER TABLE user_daily_counters
    ADD COLUMN IF NOT EXISTS tokens bigint NOT NULL DEFAULT 0;
