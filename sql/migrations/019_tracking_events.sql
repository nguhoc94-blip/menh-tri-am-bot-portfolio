-- 019_tracking_events.sql — Event log for analytics & tracking
-- Slice 1 · V9.1 §9, V9.2 §7.1

CREATE TABLE IF NOT EXISTS tracking_events (
    id                  bigserial PRIMARY KEY,
    event_name          text NOT NULL,
    user_id_hash        text NOT NULL,
    session_id          text NOT NULL,
    mode                text NOT NULL DEFAULT 'system',
    step                text NOT NULL DEFAULT 'system',
    status              text NOT NULL DEFAULT 'completed',
    event_timestamp     timestamptz NOT NULL DEFAULT now(),
    source              text NOT NULL DEFAULT 'system',
    cost_estimate       numeric(12, 6),
    model_call_count    integer,
    asset_id            text,
    error_code          text,
    cohort_label        text NOT NULL DEFAULT 'new',
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tracking_events_user_hash
    ON tracking_events (user_id_hash);

CREATE INDEX IF NOT EXISTS idx_tracking_events_session
    ON tracking_events (session_id);

CREATE INDEX IF NOT EXISTS idx_tracking_events_name_ts
    ON tracking_events (event_name, event_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_tracking_events_mode
    ON tracking_events (mode, event_timestamp DESC);
