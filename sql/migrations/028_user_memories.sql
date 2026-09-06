-- 028_user_memories.sql — Long-term conversational memory store (Demo Bot)
-- Layer 2 of the 3-layer memory architecture:
--   Layer 1 = chart_json + cached full_reading (always in system prompt)
--   Layer 2 = user_memories (selective retrieval, this table)
--   Layer 3 = MAX_HISTORY=8 sliding window (already exists)
--
-- Each row is a structured fact the user has shared (life event, concern,
-- preference, relationship, goal). Extraction runs after each bot reply using
-- gpt-4o-mini; retrieval combines keyword match + importance + recency.

CREATE TABLE IF NOT EXISTS user_memories (
    id              bigserial PRIMARY KEY,
    sender_id       text        NOT NULL,
    category        text        NOT NULL,
    content         text        NOT NULL,
    importance      smallint    NOT NULL DEFAULT 5,
    keywords        text[]      NOT NULL DEFAULT '{}',
    mention_count   integer     NOT NULL DEFAULT 1,
    last_seen       timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_memories_sender_importance
    ON user_memories (sender_id, importance DESC, last_seen DESC);

CREATE INDEX IF NOT EXISTS idx_user_memories_sender_recent
    ON user_memories (sender_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_user_memories_keywords
    ON user_memories USING GIN (keywords);
