-- 025_messenger_sessions_v9_expand.sql — Add V9 multimodal fields to sessions
-- Slice 2 · docs/ARCHITECTURE/02_state_machine.md §7

ALTER TABLE messenger_sessions
    ADD COLUMN IF NOT EXISTS palm_asset_ids   jsonb        NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS face_asset_ids   jsonb        NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS face_consent_given boolean    NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS active_mode      text,
    ADD COLUMN IF NOT EXISTS image_retry_count integer     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS session_model_calls integer   NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cohort_label     text         NOT NULL DEFAULT 'new',
    ADD COLUMN IF NOT EXISTS abuse_flag_count integer      NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS admin_granted_combined boolean NOT NULL DEFAULT false;
