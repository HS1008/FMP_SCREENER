-- Public CFTC COT watchlist plus EIA energy observations.
-- Additive. No research-table changes.

CREATE TABLE IF NOT EXISTS mi_cftc_cot_observations (
    id BIGSERIAL PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    market VARCHAR(160) NOT NULL,
    report_date DATE NOT NULL,
    open_interest INTEGER,
    noncomm_long INTEGER,
    noncomm_short INTEGER,
    noncomm_net INTEGER,
    comm_long INTEGER,
    comm_short INTEGER,
    commodity_name TEXT,
    report_type VARCHAR(32),
    payload_hash VARCHAR(64) NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingestion_run_id VARCHAR(64),
    UNIQUE (source_id, market, report_date)
);

CREATE INDEX IF NOT EXISTS mi_cftc_cot_report_idx
    ON mi_cftc_cot_observations (report_date DESC, market);

CREATE OR REPLACE VIEW mi_v_cftc_cot_current AS
SELECT DISTINCT ON (market)
    source_id,
    market,
    report_date,
    open_interest,
    noncomm_long,
    noncomm_short,
    noncomm_net,
    comm_long,
    comm_short,
    commodity_name,
    report_type,
    retrieved_at
FROM mi_cftc_cot_observations
ORDER BY market, report_date DESC;

CREATE TABLE IF NOT EXISTS mi_eia_observations (
    id BIGSERIAL PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    series_id VARCHAR(128) NOT NULL,
    observation_date DATE NOT NULL,
    value NUMERIC,
    units TEXT,
    payload_hash VARCHAR(64) NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingestion_run_id VARCHAR(64),
    UNIQUE (source_id, series_id, observation_date)
);

CREATE OR REPLACE VIEW mi_v_eia_latest AS
SELECT DISTINCT ON (series_id)
    source_id,
    series_id,
    observation_date,
    value,
    units,
    retrieved_at
FROM mi_eia_observations
ORDER BY series_id, observation_date DESC;
