-- 018_jobs.sql — Postgres-as-queue (SKIP LOCKED pattern)
-- Slice 1 · V9.3 §4.3

CREATE TABLE IF NOT EXISTS jobs (
    id              bigserial PRIMARY KEY,
    kind            text NOT NULL,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'dead')),
    attempt         integer NOT NULL DEFAULT 0,
    max_attempts    integer NOT NULL DEFAULT 3,
    locked_by       text,
    locked_until    timestamptz,
    run_at          timestamptz NOT NULL DEFAULT now(),
    last_error      text,
    idempotency_key text UNIQUE,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_run_at ON jobs (status, run_at)
    WHERE status IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS idx_jobs_kind ON jobs (kind);

CREATE INDEX IF NOT EXISTS idx_jobs_locked_until ON jobs (locked_until)
    WHERE status = 'running';
