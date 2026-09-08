-- Research idea registry (IdeaLab thin contracts). Additive; backend-only writers.
-- Ideas link to existing strategy specs / research runs; they never duplicate results.

CREATE TABLE IF NOT EXISTS mi_research_ideas (
    idea_id VARCHAR(64) PRIMARY KEY,
    lineage_id VARCHAR(128) NOT NULL UNIQUE,
    title TEXT NOT NULL,
    research_type VARCHAR(48) NOT NULL,
    current_state VARCHAR(32) NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 0,
    created_by VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mi_research_idea_versions (
    idea_id VARCHAR(64) NOT NULL REFERENCES mi_research_ideas (idea_id),
    version INTEGER NOT NULL,
    spec_json JSONB NOT NULL,
    spec_hash VARCHAR(64) NOT NULL,
    research_type VARCHAR(48) NOT NULL,
    source_snapshot_id VARCHAR(64),
    source_snapshot_hash VARCHAR(64),
    conception_at TIMESTAMPTZ NOT NULL,
    data_used_json JSONB,
    holdout_policy_json JSONB,
    execution_support VARCHAR(32) NOT NULL,
    created_by VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (idea_id, version),
    UNIQUE (idea_id, spec_hash)
);

CREATE TABLE IF NOT EXISTS mi_research_idea_transitions (
    id BIGSERIAL PRIMARY KEY,
    idea_id VARCHAR(64) NOT NULL REFERENCES mi_research_ideas (idea_id),
    version INTEGER,
    from_state VARCHAR(32),
    to_state VARCHAR(32) NOT NULL,
    actor VARCHAR(64) NOT NULL,
    actor_kind VARCHAR(16) NOT NULL DEFAULT 'HUMAN',
    reason TEXT,
    bound_spec_hash VARCHAR(64),
    transitioned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS mi_research_idea_transitions_idea_idx
    ON mi_research_idea_transitions (idea_id, transitioned_at);

CREATE TABLE IF NOT EXISTS mi_research_idea_approvals (
    id BIGSERIAL PRIMARY KEY,
    idea_id VARCHAR(64) NOT NULL REFERENCES mi_research_ideas (idea_id),
    version INTEGER NOT NULL,
    spec_hash VARCHAR(64) NOT NULL,
    scope VARCHAR(32) NOT NULL DEFAULT 'NON_HOLDOUT_RESEARCH',
    approved_by VARCHAR(64) NOT NULL,
    approved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    revoke_reason TEXT,
    UNIQUE (idea_id, version, spec_hash, scope)
);

CREATE TABLE IF NOT EXISTS mi_research_idea_tests (
    id BIGSERIAL PRIMARY KEY,
    idea_id VARCHAR(64) NOT NULL REFERENCES mi_research_ideas (idea_id),
    version INTEGER NOT NULL,
    spec_hash VARCHAR(64) NOT NULL,
    strategy_spec_hash VARCHAR(64),
    research_run_id VARCHAR(128),
    execution_status VARCHAR(32) NOT NULL,
    artifact_ref TEXT,
    artifact_sha256 VARCHAR(64),
    created_by VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes TEXT
);

CREATE INDEX IF NOT EXISTS mi_research_idea_tests_idea_idx
    ON mi_research_idea_tests (idea_id, version);

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
    v.conception_at
FROM mi_research_ideas i
LEFT JOIN mi_research_idea_versions v
    ON v.idea_id = i.idea_id AND v.version = i.current_version;
