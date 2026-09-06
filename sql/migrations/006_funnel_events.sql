CREATE TABLE IF NOT EXISTS funnel_events (
    id BIGSERIAL PRIMARY KEY,
    sender_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    request_id TEXT,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS funnel_events_sender_created_idx
    ON funnel_events (sender_id, created_at DESC);

CREATE INDEX IF NOT EXISTS funnel_events_type_created_idx
    ON funnel_events (event_type, created_at DESC);
