-- Additive Stage 2 artifact transport metadata. Does not drop Stage 1 objects.

ALTER TABLE research_artifacts ADD COLUMN IF NOT EXISTS transport VARCHAR(64);
ALTER TABLE research_artifacts ADD COLUMN IF NOT EXISTS logical_path TEXT;

CREATE INDEX IF NOT EXISTS idx_research_artifacts_transport
    ON research_artifacts (transport);
