-- 023_cost_ledger.sql — Cost tracking per model call
-- Slice 1 · V9.2 §8.1, V9 §10.4

CREATE TABLE IF NOT EXISTS cost_ledger (
    id                  bigserial PRIMARY KEY,
    sender_id           text NOT NULL,
    session_id          text NOT NULL,
    mode                text NOT NULL DEFAULT 'system',
    model_name          text NOT NULL,
    prompt_tokens       integer,
    completion_tokens   integer,
    cost_usd            numeric(12, 8) NOT NULL DEFAULT 0,
    cost_vnd_estimate   bigint,
    is_retry            boolean NOT NULL DEFAULT false,
    job_id              bigint REFERENCES jobs(id) ON DELETE SET NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cost_ledger_sender ON cost_ledger (sender_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cost_ledger_session ON cost_ledger (session_id);
CREATE INDEX IF NOT EXISTS idx_cost_ledger_mode ON cost_ledger (mode, created_at DESC);
