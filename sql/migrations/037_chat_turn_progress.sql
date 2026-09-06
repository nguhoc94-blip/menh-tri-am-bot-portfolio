-- PR-L2: idempotent progress-stage tracking for chat_turn jobs.
-- Additive, idempotent — safe to run multiple times.
-- job_id is not a FK to jobs(id): this table is a pure send-log (idempotency
-- primitive for user-facing progress messages), not a referential entity, and
-- job rows can in principle be pruned/rotated independently of this log.
CREATE TABLE IF NOT EXISTS job_progress_sent (
    job_id BIGINT NOT NULL,
    stage TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (job_id, stage)
);
