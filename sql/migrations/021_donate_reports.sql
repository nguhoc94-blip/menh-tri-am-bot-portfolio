-- 021_donate_reports.sql — Donate lifecycle: reported → verified/rejected
-- Slice 1 · V9 §6.3, V9.1 §6.6, V9.2 §8.2

CREATE TABLE IF NOT EXISTS donate_reports (
    id              bigserial PRIMARY KEY,
    sender_id       text NOT NULL,
    session_id      text NOT NULL,
    amount_claimed  bigint,
    transfer_note   text,
    status          text NOT NULL DEFAULT 'reported'
                    CHECK (status IN ('reported', 'verified', 'rejected')),
    admin_note      text,
    verified_by     text,
    verified_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_donate_reports_sender ON donate_reports (sender_id);
CREATE INDEX IF NOT EXISTS idx_donate_reports_status ON donate_reports (status, created_at DESC);
