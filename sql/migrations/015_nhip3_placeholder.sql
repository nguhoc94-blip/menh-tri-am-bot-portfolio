-- Nhịp 3: no new DDL. webhook_dedupe retention (24h) is enforced in application code
-- (app.services.webhook_dedupe_cleanup). Index webhook_dedupe_created_at_idx exists from 002.
SELECT 1;
