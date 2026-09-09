-- FINRA Query API aggregate activity for the Order Flow page.
-- Additive. Does not invent individual TRACE trades. No view/table drops.

CREATE TABLE IF NOT EXISTS mi_finra_dataset_capability (
    group_name VARCHAR(64) NOT NULL,
    dataset VARCHAR(128) NOT NULL,
    source_id VARCHAR(64) NOT NULL,
    environment VARCHAR(32) NOT NULL DEFAULT 'production',
    capability_status VARCHAR(32) NOT NULL,
    http_status INTEGER,
    record_count INTEGER,
    schema_fields JSONB,
    coverage_note TEXT,
    latest_observation_date DATE,
    last_probe_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_success_at TIMESTAMPTZ,
    error_redacted TEXT,
    details_json JSONB,
    PRIMARY KEY (group_name, dataset, environment)
);

CREATE TABLE IF NOT EXISTS mi_finra_ingest_checkpoint (
    dataset VARCHAR(128) PRIMARY KEY,
    last_committed_observation_date DATE,
    last_success_at TIMESTAMPTZ,
    overlap_days INTEGER NOT NULL DEFAULT 5,
    details_json JSONB
);

CREATE TABLE IF NOT EXISTS mi_finra_aggregate_observations (
    id BIGSERIAL PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    dataset VARCHAR(128) NOT NULL,
    observation_date DATE NOT NULL,
    category_key VARCHAR(160) NOT NULL,
    grain_json JSONB NOT NULL,
    metrics_json JSONB NOT NULL,
    units_note TEXT,
    volume_is_capped BOOLEAN NOT NULL DEFAULT FALSE,
    payload_hash VARCHAR(64) NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    source_published_at TIMESTAMPTZ,
    ingestion_run_id VARCHAR(64),
    revision_seq INTEGER NOT NULL DEFAULT 1,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    superseded_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, dataset, observation_date, category_key, revision_seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS mi_finra_aggregate_current_uidx
    ON mi_finra_aggregate_observations (source_id, dataset, observation_date, category_key)
    WHERE is_current;

CREATE INDEX IF NOT EXISTS mi_finra_aggregate_dataset_date_idx
    ON mi_finra_aggregate_observations (dataset, observation_date DESC, category_key);

CREATE OR REPLACE VIEW mi_v_finra_dataset_capability AS
SELECT
    group_name,
    dataset,
    source_id,
    environment,
    capability_status,
    http_status,
    record_count,
    schema_fields,
    coverage_note,
    latest_observation_date,
    last_probe_at,
    last_success_at,
    error_redacted,
    details_json
FROM mi_finra_dataset_capability;

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
    last_seen_at
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
    last_seen_at
FROM mi_finra_aggregate_observations;

-- Individual TRACE tape rows (empty unless a licensed transaction product is later stored).
CREATE OR REPLACE VIEW mi_v_trace_individual_trades AS
SELECT
    t.source_id,
    t.source_trade_id,
    t.bond_id,
    t.trade_ts,
    t.report_ts,
    t.price,
    t.price_kind,
    t.volume,
    t.volume_units,
    t.volume_capped,
    t.correction_kind,
    t.corrects_trade_id,
    t.retrieved_at
FROM mi_bond_trades t
WHERE t.source_id = 'FINRA_TRACE';

CREATE OR REPLACE VIEW mi_v_order_flow_coverage AS
SELECT
    c.group_name,
    c.dataset,
    c.source_id,
    c.environment,
    c.capability_status,
    c.http_status,
    c.record_count AS probe_record_count,
    c.coverage_note,
    c.schema_fields,
    c.latest_observation_date AS probe_latest_observation_date,
    c.last_probe_at,
    c.last_success_at AS probe_last_success_at,
    c.error_redacted,
    f.last_success_at AS ingest_last_success_at,
    f.latest_observation_date AS ingest_latest_observation_date,
    f.latest_observation_retrieved_at,
    f.transport_status,
    f.freshness_status,
    f.expected_cadence AS dataset_cadence,
    r.access_status AS source_access_status,
    r.enabled AS source_enabled,
    r.attribution,
    r.terms_notes
FROM mi_finra_dataset_capability c
LEFT JOIN mi_data_freshness f
    ON f.source_id = c.source_id AND f.dataset = c.dataset
LEFT JOIN mi_source_registry r
    ON r.source_id = c.source_id;
