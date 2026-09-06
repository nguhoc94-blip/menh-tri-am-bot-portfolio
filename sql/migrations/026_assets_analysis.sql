-- 026_assets_analysis.sql — Add analysis result fields to assets table
-- Slice 3 · docs/ARCHITECTURE/02_state_machine.md (ANALYZING state)

ALTER TABLE assets
    ADD COLUMN IF NOT EXISTS analysis_status  text    NOT NULL DEFAULT 'pending'
        CHECK (analysis_status IN ('pending', 'running', 'done', 'failed', 'guardrail_blocked')),
    ADD COLUMN IF NOT EXISTS analysis_result  jsonb,
    ADD COLUMN IF NOT EXISTS analysis_model   text,
    ADD COLUMN IF NOT EXISTS analysis_tokens  integer,
    ADD COLUMN IF NOT EXISTS analysis_cost_usd numeric(10,6),
    ADD COLUMN IF NOT EXISTS analyzed_at      timestamptz;

CREATE INDEX IF NOT EXISTS idx_assets_analysis_status ON assets (analysis_status)
    WHERE analysis_status IN ('pending', 'running');
