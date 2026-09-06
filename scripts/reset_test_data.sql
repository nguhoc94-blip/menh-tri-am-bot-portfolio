-- ============================================================
-- reset_test_data.sql
-- Xóa toàn bộ dữ liệu user/session để bắt đầu test lại.
-- GIỮ NGUYÊN: schema_migrations, app_config, admin_users,
--             admin_audit_log, campaigns, orders (nếu muốn giữ)
--
-- Chạy trên Render PostgreSQL console hoặc qua psql:
--   psql $DATABASE_URL -f scripts/reset_test_data.sql
-- ============================================================

BEGIN;

-- 1. Job queue — xóa pending/running render jobs
TRUNCATE TABLE background_jobs RESTART IDENTITY CASCADE;

-- 2. Assets — ảnh render đã lưu
TRUNCATE TABLE assets RESTART IDENTITY CASCADE;

-- 3. Readings — bản luận giải
TRUNCATE TABLE readings RESTART IDENTITY CASCADE;

-- 4. Memories — ký ức dài hạn của user
TRUNCATE TABLE user_memories RESTART IDENTITY CASCADE;

-- 5. Sessions — lịch sử chat + birth_data
TRUNCATE TABLE messenger_sessions RESTART IDENTITY CASCADE;

-- 6. User profiles — hồ sơ tổng hợp
TRUNCATE TABLE user_profiles RESTART IDENTITY CASCADE;

-- 7. Daily counters
TRUNCATE TABLE user_daily_counters RESTART IDENTITY CASCADE;

-- 8. Webhook dedup — để không bị skip message cũ
TRUNCATE TABLE webhook_deliveries RESTART IDENTITY CASCADE;

-- 9. Funnel + tracking events
TRUNCATE TABLE funnel_events RESTART IDENTITY CASCADE;
TRUNCATE TABLE tracking_events RESTART IDENTITY CASCADE;

-- 10. Bot activity log
TRUNCATE TABLE bot_activity_log RESTART IDENTITY CASCADE;

-- 11. Donate reports + abuse flags + cost ledger + support tickets
TRUNCATE TABLE donate_reports RESTART IDENTITY CASCADE;
TRUNCATE TABLE abuse_flags RESTART IDENTITY CASCADE;
TRUNCATE TABLE cost_ledger RESTART IDENTITY CASCADE;
TRUNCATE TABLE support_tickets RESTART IDENTITY CASCADE;

-- ── KHÔNG XÓA ──────────────────────────────────────────────
-- schema_migrations  → giữ để migration không chạy lại
-- app_config         → giữ config bot (CTA, disclaimer, v.v.)
-- admin_users        → giữ tài khoản admin
-- admin_audit_log    → giữ audit trail
-- campaigns          → giữ campaign config
-- orders             → comment dòng dưới nếu muốn giữ orders

-- Nếu muốn xóa cả orders:
-- TRUNCATE TABLE orders RESTART IDENTITY CASCADE;

COMMIT;

-- Verify
SELECT
    'messenger_sessions' AS tbl, COUNT(*) FROM messenger_sessions
UNION ALL SELECT 'readings',         COUNT(*) FROM readings
UNION ALL SELECT 'assets',           COUNT(*) FROM assets
UNION ALL SELECT 'user_memories',    COUNT(*) FROM user_memories
UNION ALL SELECT 'background_jobs',  COUNT(*) FROM background_jobs
UNION ALL SELECT 'webhook_deliveries', COUNT(*) FROM webhook_deliveries;
