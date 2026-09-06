-- 039_messenger_sessions_busy_gate.sql — foreground operation BUSY gate

ALTER TABLE messenger_sessions
    ADD COLUMN IF NOT EXISTS busy_owner_id UUID,
    ADD COLUMN IF NOT EXISTS busy_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS busy_notice_sent BOOLEAN NOT NULL DEFAULT false;
