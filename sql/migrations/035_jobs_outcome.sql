-- PR-004 AM-04/AM-05: widen status CHECK + add outcome audit column
-- Idempotent: safe to run multiple times.

-- Widen the status CHECK to include 'cancelled' for superseded jobs.
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'dead', 'cancelled'));

-- Add nullable outcome column for three-way audit.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS outcome TEXT;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'jobs_outcome_check'
          AND conrelid = 'jobs'::regclass
    ) THEN
        ALTER TABLE jobs ADD CONSTRAINT jobs_outcome_check
            CHECK (outcome IS NULL OR outcome IN ('succeeded', 'failed_terminal', 'cancelled_stale'));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS jobs_outcome_idx ON jobs (outcome) WHERE outcome IS NOT NULL;
