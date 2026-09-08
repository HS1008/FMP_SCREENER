-- Market Intelligence analytics, sector snapshots, morning context, curated read views.
-- Additive. Plain SQL only (statement splitter in jobs/apply_migrations.py).

CREATE TABLE IF NOT EXISTS mi_metric_snapshots (
    id BIGSERIAL PRIMARY KEY,
    metric_id VARCHAR(96) NOT NULL,
    series_id VARCHAR(64),
    category VARCHAR(32),
    as_of DATE NOT NULL,
    value NUMERIC,
    units VARCHAR(32) NOT NULL,
    transform_version VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'OK',
    detail_json JSONB,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingestion_run_id VARCHAR(64),
    UNIQUE (metric_id, as_of, transform_version)
);

CREATE INDEX IF NOT EXISTS mi_metric_snapshots_latest_idx
    ON mi_metric_snapshots (metric_id, as_of DESC);

CREATE TABLE IF NOT EXISTS mi_credit_index_snapshots (
    id BIGSERIAL PRIMARY KEY,
    series_id VARCHAR(64) NOT NULL,
    bucket VARCHAR(32) NOT NULL,
    as_of DATE NOT NULL,
    oas_bps NUMERIC,
    change_1d_bps NUMERIC,
    change_1w_bps NUMERIC,
    change_1m_bps NUMERIC,
    change_3m_bps NUMERIC,
    percentile_window VARCHAR(16),
    percentile NUMERIC,
    zscore NUMERIC,
    window_observations INTEGER,
    history_first_date DATE,
    history_status VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN',
    units VARCHAR(16) NOT NULL DEFAULT 'bps',
    transform_version VARCHAR(32) NOT NULL,
    export_scope VARCHAR(32) NOT NULL DEFAULT 'RESTRICTED_REDISTRIBUTION',
    source_refs JSONB,
    detail_json JSONB,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (series_id, as_of, transform_version)
);

CREATE TABLE IF NOT EXISTS mi_sector_snapshots (
    id BIGSERIAL PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    dataset VARCHAR(64) NOT NULL,
    entity_kind VARCHAR(16) NOT NULL DEFAULT 'SECTOR',
    sector_key VARCHAR(64) NOT NULL,
    canonical_sector VARCHAR(64),
    provider_label VARCHAR(64),
    instrument_id VARCHAR(64),
    as_of DATE NOT NULL,
    schema_version VARCHAR(32) NOT NULL,
    methodology_version VARCHAR(32) NOT NULL,
    benchmark VARCHAR(16),
    return_basis VARCHAR(64),
    value_basis VARCHAR(16),
    universe_method VARCHAR(64),
    research_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    metrics_json JSONB NOT NULL,
    coverage_json JSONB,
    provenance_json JSONB,
    source_refs JSONB,
    artifact_sha256 VARCHAR(64) NOT NULL,
    ingestion_run_id VARCHAR(64),
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, dataset, sector_key, as_of, methodology_version)
);

CREATE INDEX IF NOT EXISTS mi_sector_snapshots_latest_idx
    ON mi_sector_snapshots (dataset, sector_key, as_of DESC);

CREATE TABLE IF NOT EXISTS mi_industry_snapshots (
    id BIGSERIAL PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    dataset VARCHAR(64) NOT NULL,
    parent_sector_key VARCHAR(64) NOT NULL,
    industry_key VARCHAR(96) NOT NULL,
    instrument_id VARCHAR(64),
    as_of DATE NOT NULL,
    schema_version VARCHAR(32) NOT NULL,
    methodology_version VARCHAR(32) NOT NULL,
    benchmark VARCHAR(16),
    return_basis VARCHAR(64),
    value_basis VARCHAR(16),
    metrics_json JSONB NOT NULL,
    provenance_json JSONB,
    artifact_sha256 VARCHAR(64) NOT NULL,
    ingestion_run_id VARCHAR(64),
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, dataset, parent_sector_key, industry_key, as_of, methodology_version)
);

CREATE INDEX IF NOT EXISTS mi_industry_snapshots_latest_idx
    ON mi_industry_snapshots (dataset, parent_sector_key, as_of DESC);

CREATE TABLE IF NOT EXISTS mi_morning_context_snapshots (
    snapshot_id VARCHAR(64) PRIMARY KEY,
    schema_version VARCHAR(32) NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    cutoff_at TIMESTAMPTZ NOT NULL,
    as_of_date DATE NOT NULL,
    generation_params JSONB NOT NULL,
    input_refs JSONB NOT NULL,
    sections_status JSONB NOT NULL,
    snapshot_json JSONB NOT NULL,
    snapshot_sha256 VARCHAR(64) NOT NULL,
    completeness VARCHAR(16) NOT NULL,
    publication_state VARCHAR(16) NOT NULL DEFAULT 'PUBLISHED',
    created_by VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS mi_morning_context_snapshots_latest_idx
    ON mi_morning_context_snapshots (publication_state, generated_at DESC);

-- Curated read views. The read-only role receives SELECT on these views only.

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
    f.expected_next_release,
    f.transport_status,
    f.freshness_status,
    f.last_error_redacted,
    f.last_run_id,
    f.updated_at AS freshness_updated_at
FROM mi_source_registry r
LEFT JOIN mi_data_freshness f ON f.source_id = r.source_id;

CREATE OR REPLACE VIEW mi_v_ingestion_runs_recent AS
SELECT
    run_id,
    parent_run_id,
    source_id,
    dataset,
    started_at,
    finished_at,
    status,
    rows_received,
    rows_inserted,
    rows_revised,
    rows_unchanged,
    rows_rejected,
    error_redacted,
    code_version,
    catalog_version,
    request_window_start,
    request_window_end,
    retry_count,
    dry_run
FROM mi_ingestion_runs
WHERE started_at >= NOW() - INTERVAL '30 days';

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
    updated_at
FROM mi_macro_series;

CREATE OR REPLACE VIEW mi_v_macro_observations_current AS
SELECT
    o.series_id,
    o.observation_date,
    o.value,
    o.realtime_start,
    o.realtime_end,
    o.retrieved_at,
    o.revision_seq,
    s.units,
    s.frequency_short,
    s.category,
    s.export_scope,
    s.vintage_kind
FROM mi_macro_observations o
JOIN mi_macro_series s ON s.series_id = o.series_id
WHERE o.is_current;

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
    o.retrieved_at
FROM mi_macro_series s
LEFT JOIN LATERAL (
    SELECT observation_date, value, retrieved_at
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
    s.export_scope
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
    s.export_scope
FROM mi_metric_snapshots m
LEFT JOIN mi_macro_series s ON s.series_id = m.series_id;

CREATE OR REPLACE VIEW mi_v_credit_latest AS
SELECT DISTINCT ON (series_id)
    series_id,
    bucket,
    as_of,
    oas_bps,
    change_1d_bps,
    change_1w_bps,
    change_1m_bps,
    change_3m_bps,
    percentile_window,
    percentile,
    zscore,
    window_observations,
    history_first_date,
    history_status,
    units,
    transform_version,
    export_scope,
    source_refs,
    detail_json,
    computed_at
FROM mi_credit_index_snapshots
ORDER BY series_id, as_of DESC, computed_at DESC;

CREATE OR REPLACE VIEW mi_v_sector_latest AS
SELECT DISTINCT ON (dataset, sector_key)
    source_id,
    dataset,
    entity_kind,
    sector_key,
    canonical_sector,
    provider_label,
    instrument_id,
    as_of,
    schema_version,
    methodology_version,
    benchmark,
    return_basis,
    value_basis,
    universe_method,
    research_eligible,
    metrics_json,
    coverage_json,
    provenance_json,
    source_refs,
    artifact_sha256,
    ingested_at
FROM mi_sector_snapshots
ORDER BY dataset, sector_key, as_of DESC, ingested_at DESC;

CREATE OR REPLACE VIEW mi_v_industry_latest AS
SELECT DISTINCT ON (dataset, parent_sector_key, industry_key)
    source_id,
    dataset,
    parent_sector_key,
    industry_key,
    instrument_id,
    as_of,
    schema_version,
    methodology_version,
    benchmark,
    return_basis,
    value_basis,
    metrics_json,
    provenance_json,
    artifact_sha256,
    ingested_at
FROM mi_industry_snapshots
ORDER BY dataset, parent_sector_key, industry_key, as_of DESC, ingested_at DESC;

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
    created_at
FROM mi_morning_context_snapshots
WHERE publication_state = 'PUBLISHED'
ORDER BY generated_at DESC
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
    created_at
FROM mi_morning_context_snapshots;

CREATE OR REPLACE VIEW mi_v_strategy_research_summary AS
SELECT DISTINCT ON (strategy_id)
    strategy_id,
    research_run_id,
    research_lineage_id,
    research_kind,
    research_mode,
    asset_class,
    strategy_family_id,
    run_status,
    economic_gate,
    promotion_gate,
    holdout_status,
    holdout_exposure_status,
    delivery_status,
    expected_experiment_count,
    synced_experiment_count,
    completed_count,
    failed_count,
    skipped_count,
    git_commit,
    first_seen_at,
    last_seen_at
FROM research_runs
ORDER BY strategy_id, last_seen_at DESC;
