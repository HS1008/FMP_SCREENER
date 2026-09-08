-- Research idea contracts (idea_spec_v2): completeness gate for approval, lineage-specific
-- effective holdout boundary, explicit economic-gate status. Additive only; 010 is untouched.
-- Plain SQL only: jobs/apply_migrations.py splits on lines ending in ';'.

ALTER TABLE mi_research_idea_versions ADD COLUMN IF NOT EXISTS spec_completeness VARCHAR(16) NOT NULL DEFAULT 'INCOMPLETE';
ALTER TABLE mi_research_idea_versions ADD COLUMN IF NOT EXISTS missing_fields_json JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE mi_research_idea_versions ADD COLUMN IF NOT EXISTS effective_holdout_start DATE NOT NULL DEFAULT DATE '2025-01-01';
ALTER TABLE mi_research_idea_versions ADD COLUMN IF NOT EXISTS economic_gate VARCHAR(24) NOT NULL DEFAULT 'NOT_DEFINED';

-- Versions written by idea_spec_v1 were never checked for completeness; they stay INCOMPLETE
-- (the default) until a human revises them, so no pre-013 version can be approved silently.

CREATE OR REPLACE VIEW mi_v_research_ideas AS
SELECT
    i.idea_id,
    i.lineage_id,
    i.title,
    i.research_type,
    i.current_state,
    i.current_version,
    i.created_by,
    i.created_at,
    i.updated_at,
    v.spec_hash AS current_spec_hash,
    v.source_snapshot_id,
    v.execution_support,
    v.conception_at,
    v.spec_completeness,
    v.missing_fields_json AS missing_fields,
    v.effective_holdout_start,
    v.economic_gate
FROM mi_research_ideas i
LEFT JOIN mi_research_idea_versions v
    ON v.idea_id = i.idea_id AND v.version = i.current_version;
