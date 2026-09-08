-- Market Intelligence core schema (additive). Namespace prefix: mi_.
-- Does not touch Stage 1 / Stage 2 / platform research tables.
-- Plain SQL only: jobs/apply_migrations.py splits on lines ending in ';'.

CREATE TABLE IF NOT EXISTS mi_source_registry (
    source_id VARCHAR(64) PRIMARY KEY,
    provider VARCHAR(64) NOT NULL,
    dataset VARCHAR(128) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    access_status VARCHAR(32) NOT NULL DEFAULT 'CONFIGURATION_REQUIRED',
    source_url TEXT,
    expected_cadence VARCHAR(32),
    units_metadata JSONB,
    usage_scope VARCHAR(32) NOT NULL DEFAULT 'INTERNAL_ONLY',
    terms_notes TEXT,
    attribution TEXT,
    catalog_version VARCHAR(32),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mi_ingestion_runs (
    run_id VARCHAR(64) PRIMARY KEY,
    parent_run_id VARCHAR(64),
    source_id VARCHAR(64) NOT NULL,
    dataset VARCHAR(128),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status VARCHAR(32) NOT NULL,
    rows_received INTEGER,
    rows_inserted INTEGER,
    rows_revised INTEGER,
    rows_unchanged INTEGER,
    rows_rejected INTEGER,
    error_redacted TEXT,
    code_version VARCHAR(80),
    catalog_version VARCHAR(32),
    request_window_start DATE,
    request_window_end DATE,
    retry_count INTEGER NOT NULL DEFAULT 0,
    dry_run BOOLEAN NOT NULL DEFAULT FALSE,
    details_json JSONB
);

CREATE INDEX IF NOT EXISTS mi_ingestion_runs_source_idx
    ON mi_ingestion_runs (source_id, started_at DESC);

CREATE INDEX IF NOT EXISTS mi_ingestion_runs_parent_idx
    ON mi_ingestion_runs (parent_run_id);

CREATE TABLE IF NOT EXISTS mi_data_freshness (
    source_id VARCHAR(64) NOT NULL,
    dataset VARCHAR(128) NOT NULL,
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    latest_observation_date DATE,
    expected_cadence VARCHAR(32),
    tolerance_days INTEGER,
    expected_next_release DATE,
    transport_status VARCHAR(32) NOT NULL DEFAULT 'NEVER_ATTEMPTED',
    freshness_status VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN',
    last_error_redacted TEXT,
    last_run_id VARCHAR(64),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_id, dataset)
);

CREATE TABLE IF NOT EXISTS mi_macro_series (
    series_id VARCHAR(64) PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL REFERENCES mi_source_registry (source_id),
    provider_series_id VARCHAR(64) NOT NULL,
    title TEXT,
    units TEXT,
    units_short TEXT,
    frequency VARCHAR(32),
    frequency_short VARCHAR(8),
    seasonal_adjustment TEXT,
    seasonal_adjustment_short VARCHAR(8),
    category VARCHAR(32),
    subcategory VARCHAR(64),
    catalog_version VARCHAR(32),
    source_url TEXT,
    source_notes TEXT,
    provider_last_updated TEXT,
    provider_observation_start DATE,
    provider_observation_end DATE,
    metadata_status VARCHAR(32) NOT NULL DEFAULT 'UNVALIDATED',
    metadata_mismatch_json JSONB,
    vintage_kind VARCHAR(32) NOT NULL DEFAULT 'LATEST_REVISED',
    pit_safe BOOLEAN NOT NULL DEFAULT FALSE,
    export_scope VARCHAR(32) NOT NULL DEFAULT 'INTERNAL_ONLY',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, provider_series_id)
);

CREATE TABLE IF NOT EXISTS mi_macro_observations (
    id BIGSERIAL PRIMARY KEY,
    series_id VARCHAR(64) NOT NULL REFERENCES mi_macro_series (series_id),
    observation_date DATE NOT NULL,
    value NUMERIC,
    raw_value TEXT,
    realtime_start DATE,
    realtime_end DATE,
    retrieved_at TIMESTAMPTZ NOT NULL,
    ingestion_run_id VARCHAR(64),
    revision_seq INTEGER NOT NULL DEFAULT 1,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    superseded_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    UNIQUE (series_id, observation_date, revision_seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS mi_macro_observations_current_idx
    ON mi_macro_observations (series_id, observation_date)
    WHERE is_current;

CREATE INDEX IF NOT EXISTS mi_macro_observations_series_date_idx
    ON mi_macro_observations (series_id, observation_date DESC);

CREATE TABLE IF NOT EXISTS mi_market_instruments (
    instrument_id VARCHAR(64) PRIMARY KEY,
    display_name TEXT,
    asset_type VARCHAR(32) NOT NULL,
    security_type VARCHAR(32),
    currency VARCHAR(8),
    exchange VARCHAR(32),
    canonical_sector VARCHAR(64),
    classification_version VARCHAR(32),
    provider_classification JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mi_instrument_identifiers (
    id BIGSERIAL PRIMARY KEY,
    instrument_id VARCHAR(64) NOT NULL REFERENCES mi_market_instruments (instrument_id),
    id_type VARCHAR(32) NOT NULL,
    id_value VARCHAR(128) NOT NULL,
    source_id VARCHAR(64),
    valid_from DATE NOT NULL DEFAULT DATE '1900-01-01',
    valid_to DATE,
    UNIQUE (id_type, id_value, source_id, valid_from)
);

CREATE INDEX IF NOT EXISTS mi_instrument_identifiers_lookup_idx
    ON mi_instrument_identifiers (id_type, id_value);

CREATE TABLE IF NOT EXISTS mi_market_bars (
    id BIGSERIAL PRIMARY KEY,
    instrument_id VARCHAR(64) NOT NULL REFERENCES mi_market_instruments (instrument_id),
    source_id VARCHAR(64) NOT NULL,
    bar_interval VARCHAR(8) NOT NULL,
    bar_date DATE NOT NULL,
    bar_ts TIMESTAMPTZ,
    open_price NUMERIC,
    high_price NUMERIC,
    low_price NUMERIC,
    close_price NUMERIC,
    adj_close_price NUMERIC,
    volume NUMERIC,
    currency VARCHAR(8),
    adjustment_basis VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN',
    price_units VARCHAR(32),
    volume_units VARCHAR(32),
    retrieved_at TIMESTAMPTZ NOT NULL,
    ingestion_run_id VARCHAR(64),
    UNIQUE (instrument_id, source_id, bar_interval, bar_date)
);

CREATE TABLE IF NOT EXISTS mi_market_quotes (
    id BIGSERIAL PRIMARY KEY,
    instrument_id VARCHAR(64) NOT NULL REFERENCES mi_market_instruments (instrument_id),
    source_id VARCHAR(64) NOT NULL,
    quote_ts TIMESTAMPTZ NOT NULL,
    bid NUMERIC,
    ask NUMERIC,
    last_price NUMERIC,
    mid NUMERIC,
    bid_size NUMERIC,
    ask_size NUMERIC,
    currency VARCHAR(8),
    delay_status VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN',
    quote_status VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN',
    retrieved_at TIMESTAMPTZ NOT NULL,
    ingestion_run_id VARCHAR(64),
    UNIQUE (instrument_id, source_id, quote_ts)
);
