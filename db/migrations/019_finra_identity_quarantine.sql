-- Additive FINRA identity versioning and rejected-row quarantine.
-- Does not drop revision history. Incomplete-grain current rows are marked in application code.

ALTER TABLE mi_finra_aggregate_observations
    ADD COLUMN IF NOT EXISTS identity_status VARCHAR(32) NOT NULL DEFAULT 'CURRENT';

CREATE TABLE IF NOT EXISTS mi_finra_aggregate_quarantine (
    id BIGSERIAL PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    dataset VARCHAR(128) NOT NULL,
    observation_date DATE,
    category_key VARCHAR(160),
    reason TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    ingestion_run_id VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS mi_finra_aggregate_quarantine_dataset_idx
    ON mi_finra_aggregate_quarantine (dataset, created_at DESC);

CREATE OR REPLACE VIEW mi_v_finra_aggregate_quarantine AS
SELECT
    id,
    source_id,
    dataset,
    observation_date,
    category_key,
    reason,
    retrieved_at,
    ingestion_run_id,
    created_at
FROM mi_finra_aggregate_quarantine;

CREATE OR REPLACE VIEW mi_v_finra_aggregate_current AS
SELECT
    source_id,
    dataset,
    observation_date,
    category_key,
    grain_json,
    metrics_json,
    units_note,
    volume_is_capped,
    payload_hash,
    retrieved_at,
    source_published_at,
    ingestion_run_id,
    revision_seq,
    last_seen_at,
    identity_status
FROM mi_finra_aggregate_observations
WHERE is_current;

CREATE OR REPLACE VIEW mi_v_finra_aggregate_history AS
SELECT
    source_id,
    dataset,
    observation_date,
    category_key,
    grain_json,
    metrics_json,
    units_note,
    volume_is_capped,
    payload_hash,
    retrieved_at,
    source_published_at,
    ingestion_run_id,
    revision_seq,
    is_current,
    superseded_at,
    last_seen_at,
    identity_status
FROM mi_finra_aggregate_observations;

COMMENT ON TABLE mi_finra_aggregate_quarantine IS
    'Rejected FINRA aggregate rows kept for retry/diagnosis; never published as current observations.';
