-- 020_assets.sql — Private image asset registry
-- Slice 1 · V9.2 §7, V9 §8

CREATE TABLE IF NOT EXISTS assets (
    id              text PRIMARY KEY,
    sender_id       text NOT NULL,
    session_id      text NOT NULL,
    asset_type      text NOT NULL
                    CHECK (asset_type IN ('palm_input', 'face_input', 'render_output', 'thumbnail', 'qr_brand')),
    storage_key     text NOT NULL,
    mime_type       text NOT NULL DEFAULT 'image/jpeg',
    file_size_bytes bigint,
    width_px        integer,
    height_px       integer,
    consent_token   text,
    mode            text,
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'expired', 'deleted')),
    expires_at      timestamptz NOT NULL,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_assets_sender ON assets (sender_id);
CREATE INDEX IF NOT EXISTS idx_assets_session ON assets (session_id);
CREATE INDEX IF NOT EXISTS idx_assets_expires ON assets (expires_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_assets_type ON assets (asset_type);
