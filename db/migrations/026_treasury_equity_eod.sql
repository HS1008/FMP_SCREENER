-- Additive: official Treasury XML raw observations + equity EOD provenance columns.
-- Does not edit applied SQL. Does not touch Stage 1 / Stage 2 / holdout tables.

CREATE TABLE IF NOT EXISTS mi_provider_observations (
    id BIGSERIAL PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    provider_series_id VARCHAR(64) NOT NULL,
    canonical_series_id VARCHAR(64),
    fred_equivalent VARCHAR(64),
    observation_date DATE NOT NULL,
    value NUMERIC,
    raw_value TEXT,
    units VARCHAR(32),
    curve VARCHAR(16),
    tenor VARCHAR(16),
    retrieved_at TIMESTAMPTZ NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ,
    release_time TIMESTAMPTZ,
    revision_seq INTEGER NOT NULL DEFAULT 1,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    selection_reason TEXT,
    ingestion_run_id VARCHAR(64),
    details_json JSONB,
    UNIQUE (source_id, provider_series_id, observation_date, revision_seq)
);

CREATE INDEX IF NOT EXISTS mi_provider_observations_canonical_idx
    ON mi_provider_observations (canonical_series_id, observation_date DESC);

ALTER TABLE mi_market_bars
    ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS provider_symbol VARCHAR(32);

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
    s.display_units,
    s.source_id
FROM mi_macro_series s
LEFT JOIN LATERAL (
    SELECT observation_date, value, retrieved_at, revision_seq, ingestion_run_id
    FROM mi_macro_observations x
    WHERE x.series_id = s.series_id AND x.is_current AND x.value IS NOT NULL
    ORDER BY x.observation_date DESC
    LIMIT 1
) o ON TRUE;

CREATE OR REPLACE VIEW mi_v_provider_observations_current AS
SELECT
    source_id,
    provider_series_id,
    canonical_series_id,
    fred_equivalent,
    observation_date,
    value,
    raw_value,
    units,
    curve,
    tenor,
    retrieved_at,
    first_seen_at,
    last_seen_at,
    release_time,
    revision_seq,
    selection_reason
FROM mi_provider_observations
WHERE is_current;
