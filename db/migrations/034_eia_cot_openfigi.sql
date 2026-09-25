-- Official EIA v2, CFTC COT, and OpenFIGI mapping tables.
-- Additive. Does not edit applied SQL. Does not touch research/holdout tables.

CREATE TABLE IF NOT EXISTS mi_eia_series (
    series_id VARCHAR(96) PRIMARY KEY,
    route VARCHAR(128) NOT NULL,
    facets_json JSONB NOT NULL,
    frequency VARCHAR(16) NOT NULL,
    units TEXT,
    geographic_scope TEXT,
    catalog_version VARCHAR(32) NOT NULL,
    source_url TEXT,
    dashboard_eligible BOOLEAN NOT NULL DEFAULT TRUE,
    research_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    export_scope VARCHAR(32) NOT NULL DEFAULT 'ATTRIBUTION_REQUIRED',
    status VARCHAR(32) NOT NULL DEFAULT 'CONFIGURED'
);

CREATE TABLE IF NOT EXISTS mi_eia_observations (
    series_id VARCHAR(96) NOT NULL REFERENCES mi_eia_series (series_id),
    period VARCHAR(32) NOT NULL,
    observation_date DATE,
    value NUMERIC,
    units TEXT,
    revision_seq INTEGER NOT NULL DEFAULT 1,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    available_at TIMESTAMPTZ,
    available_at_basis VARCHAR(32) NOT NULL DEFAULT 'FIRST_SEEN',
    retrieved_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingestion_run_id VARCHAR(64),
    payload_hash VARCHAR(64) NOT NULL,
    missing_reason TEXT,
    PRIMARY KEY (series_id, period, revision_seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS mi_eia_observations_current_uidx
    ON mi_eia_observations (series_id, period)
    WHERE is_current;

CREATE TABLE IF NOT EXISTS mi_cot_contracts (
    contract_code VARCHAR(32) PRIMARY KEY,
    market_name TEXT,
    report_family VARCHAR(32) NOT NULL,
    futonly_or_combined VARCHAR(16) NOT NULL,
    exchange VARCHAR(32),
    mapped_future_root VARCHAR(16),
    export_scope VARCHAR(32) NOT NULL DEFAULT 'ATTRIBUTION_REQUIRED'
);

CREATE TABLE IF NOT EXISTS mi_cot_positions (
    report_family VARCHAR(32) NOT NULL,
    futonly_or_combined VARCHAR(16) NOT NULL,
    contract_code VARCHAR(32) NOT NULL,
    position_date DATE NOT NULL,
    trader_category VARCHAR(64) NOT NULL,
    long_position NUMERIC,
    short_position NUMERIC,
    spreading_position NUMERIC,
    open_interest NUMERIC,
    change_long NUMERIC,
    change_short NUMERIC,
    pct_oi_long NUMERIC,
    pct_oi_short NUMERIC,
    concentration_top4_long NUMERIC,
    concentration_top8_long NUMERIC,
    revision_seq INTEGER NOT NULL DEFAULT 1,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    published_at TIMESTAMPTZ,
    available_at TIMESTAMPTZ,
    available_at_basis VARCHAR(32) NOT NULL DEFAULT 'SCHEDULE_FRIDAY_1530_ET',
    retrieved_at TIMESTAMPTZ NOT NULL,
    ingestion_run_id VARCHAR(64),
    payload_hash VARCHAR(64) NOT NULL,
    PRIMARY KEY (report_family, futonly_or_combined, contract_code, position_date, trader_category, revision_seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS mi_cot_positions_current_uidx
    ON mi_cot_positions (report_family, futonly_or_combined, contract_code, position_date, trader_category)
    WHERE is_current;

CREATE TABLE IF NOT EXISTS mi_openfigi_cache (
    request_hash VARCHAR(64) PRIMARY KEY,
    id_type VARCHAR(32) NOT NULL,
    id_value VARCHAR(64) NOT NULL,
    exch_code VARCHAR(16),
    currency VARCHAR(8),
    market_sec_des VARCHAR(32),
    status VARCHAR(32) NOT NULL,
    figi VARCHAR(32),
    composite_figi VARCHAR(32),
    share_class_figi VARCHAR(32),
    ticker VARCHAR(32),
    name TEXT,
    security_type VARCHAR(64),
    catalog_version VARCHAR(32) NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    response_hash VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS mi_openfigi_candidates (
    request_hash VARCHAR(64) NOT NULL REFERENCES mi_openfigi_cache (request_hash),
    candidate_index INTEGER NOT NULL,
    figi VARCHAR(32),
    ticker VARCHAR(32),
    exch_code VARCHAR(16),
    security_type VARCHAR(64),
    name TEXT,
    payload_json JSONB NOT NULL,
    PRIMARY KEY (request_hash, candidate_index)
);

CREATE OR REPLACE VIEW mi_v_eia_latest AS
SELECT DISTINCT ON (s.series_id)
    s.series_id, s.route, s.frequency, s.units, s.geographic_scope, s.export_scope, s.status,
    o.period, o.observation_date, o.value, o.available_at, o.available_at_basis, o.retrieved_at, o.missing_reason
FROM mi_eia_series s
LEFT JOIN mi_eia_observations o
  ON o.series_id = s.series_id AND o.is_current
ORDER BY s.series_id, o.observation_date DESC NULLS LAST;

CREATE OR REPLACE VIEW mi_v_cot_latest AS
SELECT contract_code, report_family, futonly_or_combined, position_date, trader_category,
       long_position, short_position, spreading_position, open_interest,
       published_at, available_at, available_at_basis
FROM mi_cot_positions
WHERE is_current;

CREATE OR REPLACE VIEW mi_v_openfigi_resolution AS
SELECT request_hash, id_type, id_value, exch_code, currency, status, figi, ticker, security_type,
       catalog_version, retrieved_at, expires_at
FROM mi_openfigi_cache;
