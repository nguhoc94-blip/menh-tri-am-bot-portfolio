-- 042_premium_interpretations.sql — Premium Interpretation Layer (Phase A)
-- One row per sender + generation + workflow version; GPT payload stored on completion.

CREATE TABLE IF NOT EXISTS premium_interpretations (
    id               bigserial PRIMARY KEY,
    sender_id        text NOT NULL,
    generation_id    uuid NOT NULL,
    workflow_version text NOT NULL DEFAULT 'v1',
    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    attempt          integer NOT NULL DEFAULT 0,
    payload          jsonb,
    error_detail     text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (sender_id, generation_id, workflow_version)
);

CREATE INDEX IF NOT EXISTS idx_premium_interp_sender_status
    ON premium_interpretations (sender_id, status);
