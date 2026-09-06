-- 041_support_ticket_hardening.sql — idempotent handoff tickets + analyze_fail trigger

ALTER TABLE support_tickets
    ADD COLUMN IF NOT EXISTS idempotency_key text,
    ADD COLUMN IF NOT EXISTS handoff_takeover_completed_at timestamptz,
    ADD COLUMN IF NOT EXISTS handoff_ack_sent_at timestamptz;

CREATE UNIQUE INDEX IF NOT EXISTS uq_support_tickets_idempotency_key
    ON support_tickets (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

ALTER TABLE support_tickets DROP CONSTRAINT IF EXISTS support_tickets_trigger_type_check;
ALTER TABLE support_tickets ADD CONSTRAINT support_tickets_trigger_type_check
    CHECK (trigger_type IN (
        'render_fail', 'image_fail', 'backend_fail', 'analyze_fail',
        'abuse', 'cost', 'user_request', 'other'
    ));
