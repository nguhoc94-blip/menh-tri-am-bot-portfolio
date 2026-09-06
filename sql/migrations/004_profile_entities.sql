CREATE TABLE IF NOT EXISTS profile_entities (
    id BIGSERIAL PRIMARY KEY,
    sender_id TEXT NOT NULL REFERENCES user_profiles (sender_id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS profile_entities_sender_idx
    ON profile_entities (sender_id);
