-- 022_abuse_flags.sql — Abuse detection log
-- Slice 1 · V9.2 §8.2

CREATE TABLE IF NOT EXISTS abuse_flags (
    id              bigserial PRIMARY KEY,
    sender_id       text NOT NULL,
    session_id      text NOT NULL,
    flag_type       text NOT NULL
                    CHECK (flag_type IN ('prompt_attack', 'spam', 'image_abuse', 'consent_bypass', 'other')),
    severity        text NOT NULL DEFAULT 'soft'
                    CHECK (severity IN ('soft', 'hard')),
    detail          text,
    resolved        boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_abuse_flags_sender ON abuse_flags (sender_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_abuse_flags_session ON abuse_flags (session_id);
CREATE INDEX IF NOT EXISTS idx_abuse_flags_type ON abuse_flags (flag_type, created_at DESC);
