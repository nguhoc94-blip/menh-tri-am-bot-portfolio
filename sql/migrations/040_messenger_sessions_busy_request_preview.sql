-- 040_messenger_sessions_busy_request_preview.sql — preview text for BUSY notice

ALTER TABLE messenger_sessions
    ADD COLUMN IF NOT EXISTS busy_request_preview TEXT;
