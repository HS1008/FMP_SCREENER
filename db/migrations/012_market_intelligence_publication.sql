-- Market Intelligence follow-up: metadata publication gate, observation quarantine,
-- freshness policy provenance, morning-snapshot supersession, lineage-friendly views.
-- Additive only. 008-011 are left untouched because they may already be applied.
-- Plain SQL only: jobs/apply_migrations.py splits on lines ending in ';'.

-- Publication gate on series metadata (catalog v2). Observations retrieved while a series
-- is not VALIDATED go to mi_macro_observation_quarantine, never to current observations.
ALTER TABLE mi_macro_series ADD COLUMN IF NOT EXISTS publication_status VARCHAR(32) NOT NULL DEFAULT 'UNVALIDATED';
ALTER TABLE mi_macro_series ADD COLUMN IF NOT EXISTS publication_reason TEXT;
ALTER TABLE mi_macro_series ADD COLUMN IF NOT EXISTS catalog_units VARCHAR(64);
ALTER TABLE mi_macro_series ADD COLUMN IF NOT EXISTS catalog_label TEXT;
ALTER TABLE mi_macro_series ADD COLUMN IF NOT EXISTS aggregation VARCHAR(48);
ALTER TABLE mi_macro_series ADD COLUMN IF NOT EXISTS display_divisor NUMERIC;
ALTER TABLE mi_macro_series ADD COLUMN IF NOT EXISTS display_units VARCHAR(32);
ALTER TABLE mi_macro_series ADD COLUMN IF NOT EXISTS last_validated_at TIMESTAMPTZ;
ALTER TABLE mi_macro_series ADD COLUMN IF NOT EXISTS last_quarantined_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS mi_macro_observation_quarantine (
    id BIGSERIAL PRIMARY KEY,
    series_id VARCHAR(64) NOT NULL,
    observation_date DATE,
    raw_value TEXT,
    realtime_start DATE,
    realtime_end DATE,
    retrieved_at TIMESTAMPTZ NOT NULL,
    ingestion_run_id VARCHAR(64),
    reason VARCHAR(64) NOT NULL,
    metadata_status VARCHAR(32),
    detail_json JSONB
);

CREATE INDEX IF NOT EXISTS mi_macro_observation_quarantine_series_idx
    ON mi_macro_observation_quarantine (series_id, retrieved_at DESC);

-- Freshness: keep the policy version and the retrieval time of the latest observation so
-- current health can be recomputed against an explicit clock at read time.
ALTER TABLE mi_data_freshness ADD COLUMN IF NOT EXISTS freshness_policy_version VARCHAR(32);
ALTER TABLE mi_data_freshness ADD COLUMN IF NOT EXISTS latest_observation_retrieved_at TIMESTAMPTZ;
ALTER TABLE mi_data_freshness ADD COLUMN IF NOT EXISTS metadata_status VARCHAR(32);

-- Morning snapshots stay immutable; supersession and quality are separate metadata columns.
ALTER TABLE mi_morning_context_snapshots ADD COLUMN IF NOT EXISTS content_sha256 VARCHAR(64);
ALTER TABLE mi_morning_context_snapshots ADD COLUMN IF NOT EXISTS superseded_by VARCHAR(64);
ALTER TABLE mi_morning_context_snapshots ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;
ALTER TABLE mi_morning_context_snapshots ADD COLUMN IF NOT EXISTS quality_status VARCHAR(32) NOT NULL DEFAULT 'OK';
ALTER TABLE mi_morning_context_snapshots ADD COLUMN IF NOT EXISTS quality_note TEXT;

-- Metric snapshots: record which observation revisions fed a metric (recompute audit).
ALTER TABLE mi_metric_snapshots ADD COLUMN IF NOT EXISTS inputs_retrieved_max TIMESTAMPTZ;

-- Views: CREATE OR REPLACE may only append columns, so every existing column keeps its position.

CREATE OR REPLACE VIEW mi_v_source_health AS
SELECT
    r.source_id,
    r.provider,
    r.dataset,
    r.enabled,
    r.access_status,
    r.expected_cadence,
    r.usage_scope,
    r.attribution,
    r.catalog_version,
    f.dataset AS freshness_dataset,
    f.last_attempt_at,
    f.last_success_at,
    f.latest_observation_date,
    f.tolerance_days,
    f.expected_next_release,  -- stale-after bound (column name kept: views may not rename columns); readers expose it as stale_after_estimate
    f.transport_status,
    f.freshness_status,
    f.last_error_redacted,
    f.last_run_id,
    f.updated_at AS freshness_updated_at,
    f.expected_cadence AS dataset_cadence,
    f.freshness_policy_version,
    f.latest_observation_retrieved_at,
    f.metadata_status
FROM mi_source_registry r
LEFT JOIN mi_data_freshness f ON f.source_id = r.source_id;

CREATE OR REPLACE VIEW mi_v_macro_series AS
SELECT
    series_id,
    source_id,
    provider_series_id,
    title,
    units,
    units_short,
    frequency,
    frequency_short,
    seasonal_adjustment_short,
    category,
    subcategory,
    catalog_version,
    source_url,
    source_notes,
    provider_last_updated,
    provider_observation_start,
    provider_observation_end,
    metadata_status,
    vintage_kind,
    pit_safe,
    export_scope,
    updated_at,
    publication_status,
    publication_reason,
    catalog_units,
    catalog_label,
    aggregation,
    display_divisor,
    display_units,
    last_validated_at,
    last_quarantined_at
FROM mi_macro_series;

CREATE OR REPLACE VIEW mi_v_macro_latest AS
SELECT
    s.series_id,
    s.title,
    s.category,
    s.subcategory,
    s.units,
    s.units_short,
    s.frequency_short,
    s.seasonal_adjustment_short,
    s.source_url,
    s.vintage_kind,
    s.pit_safe,
    s.export_scope,
    s.metadata_status,
    o.observation_date,
    o.value,
    o.retrieved_at,
    o.revision_seq,
    o.ingestion_run_id,
    s.publication_status,
    s.publication_reason,
    s.catalog_units,
    s.aggregation,
    s.display_divisor,
    s.display_units
FROM mi_macro_series s
LEFT JOIN LATERAL (
    SELECT observation_date, value, retrieved_at, revision_seq, ingestion_run_id
    FROM mi_macro_observations x
    WHERE x.series_id = s.series_id AND x.is_current AND x.value IS NOT NULL
    ORDER BY x.observation_date DESC
    LIMIT 1
) o ON TRUE;

CREATE OR REPLACE VIEW mi_v_metric_latest AS
SELECT DISTINCT ON (m.metric_id)
    m.metric_id,
    m.series_id,
    m.category,
    m.as_of,
    m.value,
    m.units,
    m.transform_version,
    m.status,
    m.detail_json,
    m.computed_at,
    s.export_scope,
    m.inputs_retrieved_max,
    s.publication_status
FROM mi_metric_snapshots m
LEFT JOIN mi_macro_series s ON s.series_id = m.series_id
ORDER BY m.metric_id, m.as_of DESC, m.computed_at DESC;

CREATE OR REPLACE VIEW mi_v_metric_history AS
SELECT
    m.metric_id,
    m.series_id,
    m.category,
    m.as_of,
    m.value,
    m.units,
    m.transform_version,
    m.status,
    s.export_scope,
    m.computed_at
FROM mi_metric_snapshots m
LEFT JOIN mi_macro_series s ON s.series_id = m.series_id;

CREATE OR REPLACE VIEW mi_v_morning_context_latest AS
SELECT
    snapshot_id,
    schema_version,
    generated_at,
    cutoff_at,
    as_of_date,
    generation_params,
    input_refs,
    sections_status,
    snapshot_json,
    snapshot_sha256,
    completeness,
    publication_state,
    created_at,
    content_sha256,
    quality_status,
    quality_note
FROM mi_morning_context_snapshots
WHERE publication_state = 'PUBLISHED'
ORDER BY cutoff_at DESC, generated_at DESC, created_at DESC
LIMIT 1;

CREATE OR REPLACE VIEW mi_v_morning_context_index AS
SELECT
    snapshot_id,
    schema_version,
    generated_at,
    cutoff_at,
    as_of_date,
    snapshot_sha256,
    completeness,
    publication_state,
    created_at,
    content_sha256,
    superseded_by,
    superseded_at,
    quality_status,
    quality_note
FROM mi_morning_context_snapshots;

-- Diagnostic view of quarantined payloads (counts only; raw values stay in the table for the writer).
CREATE OR REPLACE VIEW mi_v_macro_quarantine_summary AS
SELECT
    series_id,
    reason,
    metadata_status,
    COUNT(*) AS quarantined_rows,
    MIN(observation_date) AS first_observation_date,
    MAX(observation_date) AS last_observation_date,
    MAX(retrieved_at) AS last_retrieved_at
FROM mi_macro_observation_quarantine
GROUP BY series_id, reason, metadata_status;
