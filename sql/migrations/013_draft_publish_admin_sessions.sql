-- Additive: draft vs published (published = existing config_value / config_json).

ALTER TABLE app_config ADD COLUMN IF NOT EXISTS draft_value JSONB;

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS draft_config_json JSONB;

CREATE TABLE IF NOT EXISTS admin_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_user_id BIGINT NOT NULL REFERENCES admin_users (id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS admin_sessions_expires_idx ON admin_sessions (expires_at);
CREATE INDEX IF NOT EXISTS admin_sessions_user_idx ON admin_sessions (admin_user_id);
