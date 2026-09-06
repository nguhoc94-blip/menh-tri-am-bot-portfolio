-- 032_messenger_session_generation_id.sql — invalidate stale async jobs (Phase 2)
--
-- Rollout: DEFAULT allows legacy app inserts between migration and deploy.
-- gen_random_uuid() requires pgcrypto (used elsewhere, e.g. 013 admin_sessions).
--
-- Local dev: if version 032 was applied before this file gained DEFAULT, either:
--   DELETE FROM schema_migrations WHERE version = '032'; then re-run migrations, OR
--   ALTER TABLE messenger_sessions
--     ALTER COLUMN generation_id SET DEFAULT gen_random_uuid();

ALTER TABLE messenger_sessions
    ADD COLUMN IF NOT EXISTS generation_id UUID
    DEFAULT gen_random_uuid();

UPDATE messenger_sessions
    SET generation_id = gen_random_uuid()
    WHERE generation_id IS NULL;

ALTER TABLE messenger_sessions
    ALTER COLUMN generation_id SET NOT NULL;
