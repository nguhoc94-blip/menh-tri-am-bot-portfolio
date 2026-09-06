-- 024_support_tickets.sql — Support handoff tickets
-- Slice 1 · V9 §9, V9.1 §10.4, V9.2 §3.4

CREATE TABLE IF NOT EXISTS support_tickets (
    id              bigserial PRIMARY KEY,
    sender_id       text NOT NULL,
    session_id      text NOT NULL,
    trigger_type    text NOT NULL
                    CHECK (trigger_type IN (
                        'render_fail', 'image_fail', 'backend_fail',
                        'abuse', 'cost', 'user_request', 'other'
                    )),
    context_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
    status          text NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'in_progress', 'resolved', 'closed')),
    assigned_to     text,
    resolution_note text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tickets_sender ON support_tickets (sender_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON support_tickets (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_trigger ON support_tickets (trigger_type, created_at DESC);
