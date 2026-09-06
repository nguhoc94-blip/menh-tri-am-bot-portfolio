CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    sender_id TEXT NOT NULL,
    campaign_id BIGINT REFERENCES campaigns (id),
    status TEXT NOT NULL DEFAULT 'draft',
    amount_cents INTEGER,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS orders_sender_created_idx
    ON orders (sender_id, created_at DESC);
