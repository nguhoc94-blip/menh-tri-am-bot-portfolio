-- 029_assets_data_bytes.sql — Persist rendered JPEG bytes in DB
-- Fixes send_asset StorageError when worker restarts (ephemeral disk).

ALTER TABLE assets ADD COLUMN IF NOT EXISTS data_bytes BYTEA;
