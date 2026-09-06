CREATE TABLE IF NOT EXISTS webhook_dedupe (
    dedupe_key TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL,
    payload_excerpt TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS webhook_dedupe_created_at_idx
    ON webhook_dedupe (created_at);
