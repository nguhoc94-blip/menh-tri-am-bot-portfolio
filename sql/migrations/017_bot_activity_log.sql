-- Nhịp 2 — bot activity log (timeline / Gate Output evidence). Additive.
CREATE TABLE IF NOT EXISTS bot_activity_log (
    id BIGSERIAL PRIMARY KEY,
    sender_id TEXT NOT NULL,
    request_id TEXT,
    event_kind TEXT NOT NULL,
    state_before TEXT,
    state_after TEXT,
    outbound_class TEXT,
    validator_verdict TEXT,
    blocked_reason TEXT,
    order_status TEXT,
    ai_status TEXT,
    structured_payload TEXT,
    message_excerpt TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS bot_activity_log_sender_created_idx
    ON bot_activity_log (sender_id, created_at DESC);

CREATE INDEX IF NOT EXISTS bot_activity_log_request_idx
    ON bot_activity_log (request_id);
