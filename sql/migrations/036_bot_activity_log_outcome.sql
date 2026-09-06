-- PR-006b AM-07: persist real delivery outcome + provider message id.
-- Additive, idempotent — safe to run multiple times.
ALTER TABLE bot_activity_log ADD COLUMN IF NOT EXISTS send_outcome TEXT;
ALTER TABLE bot_activity_log ADD COLUMN IF NOT EXISTS provider_message_id TEXT;

CREATE INDEX IF NOT EXISTS bot_activity_log_send_outcome_idx
    ON bot_activity_log (send_outcome) WHERE send_outcome IS NOT NULL;
