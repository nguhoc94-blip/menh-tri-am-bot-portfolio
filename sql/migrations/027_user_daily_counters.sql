-- 027_user_daily_counters.sql — Per-user daily usage counters for fast cost enforcement
-- Slice 5 · V9.2 §8.1 / docs/ARCHITECTURE/10_cost_and_abuse.md

CREATE TABLE IF NOT EXISTS user_daily_counters (
    sender_id       text NOT NULL,
    date_utc        date NOT NULL DEFAULT CURRENT_DATE,
    model_calls     integer NOT NULL DEFAULT 0,
    image_analyzes  integer NOT NULL DEFAULT 0,
    renders         integer NOT NULL DEFAULT 0,
    cost_vnd        bigint  NOT NULL DEFAULT 0,
    msg_count       integer NOT NULL DEFAULT 0,        -- total messages today (spam guard)
    abuse_flags     integer NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sender_id, date_utc)
);

CREATE INDEX IF NOT EXISTS idx_daily_counters_date ON user_daily_counters (date_utc);

-- Recent-window abuse tracking (message bursts per 10 min)
CREATE TABLE IF NOT EXISTS message_burst_log (
    id          bigserial PRIMARY KEY,
    sender_id   text NOT NULL,
    ts          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_burst_log_sender_ts ON message_burst_log (sender_id, ts DESC);

-- Auto-clean burst log older than 1 hour (called by cleanup_worker)
-- No trigger needed — cleanup_worker deletes WHERE ts < now() - interval '1 hour'
