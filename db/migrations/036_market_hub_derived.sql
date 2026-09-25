-- Market Hub snapshots and versioned derived analytics.
-- Additive. Does not edit applied SQL. Does not touch research/holdout tables.

CREATE TABLE IF NOT EXISTS mi_option_contracts (
    option_con_id BIGINT PRIMARY KEY,
    underlying_con_id BIGINT,
    underlying_symbol VARCHAR(32),
    local_symbol VARCHAR(64),
    expiry DATE,
    settlement_type VARCHAR(8),
        strike NUMERIC,
        put_or_call VARCHAR(4),
    multiplier NUMERIC,
    trading_class VARCHAR(32),
    exchange VARCHAR(16),
    currency VARCHAR(8),
    deliverable_note TEXT,
    retrieved_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS mi_option_snapshots (
    option_con_id BIGINT NOT NULL REFERENCES mi_option_contracts (option_con_id),
    snapshot_ts TIMESTAMPTZ NOT NULL,
    data_mode VARCHAR(16),
    bid NUMERIC,
    ask NUMERIC,
    last_price NUMERIC,
    bid_size NUMERIC,
    ask_size NUMERIC,
    volume NUMERIC,
    open_interest NUMERIC,
    oi_as_of_date DATE,
    bid_iv NUMERIC,
    ask_iv NUMERIC,
    last_iv NUMERIC,
    model_iv NUMERIC,
    delta NUMERIC,
    gamma NUMERIC,
    vega NUMERIC,
    theta NUMERIC,
    und_price NUMERIC,
    field_callback_json JSONB,
    retrieved_at TIMESTAMPTZ NOT NULL,
    ingestion_run_id VARCHAR(64),
    export_scope VARCHAR(32) NOT NULL DEFAULT 'INTERNAL_ONLY',
    PRIMARY KEY (option_con_id, snapshot_ts)
);

CREATE TABLE IF NOT EXISTS mi_futures_snapshots (
    con_id BIGINT NOT NULL,
    root VARCHAR(16) NOT NULL,
    local_symbol VARCHAR(32),
    expiry DATE,
    snapshot_ts TIMESTAMPTZ NOT NULL,
    data_mode VARCHAR(16),
    bid NUMERIC,
    ask NUMERIC,
    last_price NUMERIC,
    volume NUMERIC,
    open_interest NUMERIC,
    close_price NUMERIC,
    settlement_price NUMERIC,
    settlement_status VARCHAR(16),
    retrieved_at TIMESTAMPTZ NOT NULL,
    export_scope VARCHAR(32) NOT NULL DEFAULT 'INTERNAL_ONLY',
    PRIMARY KEY (con_id, snapshot_ts)
);

CREATE TABLE IF NOT EXISTS mi_fx_snapshots (
    pair VARCHAR(16) NOT NULL,
    snapshot_ts TIMESTAMPTZ NOT NULL,
    data_mode VARCHAR(16),
    bid NUMERIC,
    ask NUMERIC,
    mid NUMERIC,
    last_price NUMERIC,
    retrieved_at TIMESTAMPTZ NOT NULL,
    export_scope VARCHAR(32) NOT NULL DEFAULT 'INTERNAL_ONLY',
    PRIMARY KEY (pair, snapshot_ts)
);

CREATE TABLE IF NOT EXISTS mi_derived_snapshots (
    method_id VARCHAR(64) NOT NULL,
    method_version VARCHAR(32) NOT NULL,
    subject_key VARCHAR(64) NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    status VARCHAR(32) NOT NULL,
    value NUMERIC,
    units VARCHAR(32),
    coverage_json JSONB,
    inputs_json JSONB NOT NULL,
    assumptions_json JSONB,
    missing_reason TEXT,
    dashboard_eligible BOOLEAN NOT NULL DEFAULT TRUE,
    research_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    export_scope VARCHAR(32) NOT NULL DEFAULT 'INTERNAL_ONLY',
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (method_id, method_version, subject_key, as_of)
);

CREATE TABLE IF NOT EXISTS mi_retirement_evidence (
    capability_id VARCHAR(96) NOT NULL,
    as_of DATE NOT NULL,
    sessions_observed INTEGER,
    parity_json JSONB,
    soak_status VARCHAR(32) NOT NULL DEFAULT 'NOT_STARTED',
    fmp_can_be_cancelled BOOLEAN NOT NULL DEFAULT FALSE,
    notes TEXT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (capability_id, as_of)
);

CREATE OR REPLACE VIEW mi_v_option_snapshots_latest AS
SELECT DISTINCT ON (option_con_id)
    option_con_id, snapshot_ts, data_mode, bid, ask, last_price, volume, open_interest, oi_as_of_date,
    delta, gamma, vega, theta, und_price, export_scope
FROM mi_option_snapshots
ORDER BY option_con_id, snapshot_ts DESC;

CREATE OR REPLACE VIEW mi_v_futures_snapshots_latest AS
SELECT DISTINCT ON (con_id)
    con_id, root, local_symbol, expiry, snapshot_ts, data_mode, bid, ask, last_price, volume,
    open_interest, close_price, settlement_price, settlement_status, export_scope
FROM mi_futures_snapshots
ORDER BY con_id, snapshot_ts DESC;

CREATE OR REPLACE VIEW mi_v_fx_snapshots_latest AS
SELECT DISTINCT ON (pair)
    pair, snapshot_ts, data_mode, bid, ask, mid, last_price, export_scope
FROM mi_fx_snapshots
ORDER BY pair, snapshot_ts DESC;

CREATE OR REPLACE VIEW mi_v_derived_latest AS
SELECT DISTINCT ON (method_id, subject_key)
    method_id, method_version, subject_key, as_of, status, value, units, missing_reason,
    dashboard_eligible, research_eligible, export_scope
FROM mi_derived_snapshots
ORDER BY method_id, subject_key, as_of DESC;

CREATE OR REPLACE VIEW mi_v_market_hub_overview AS
SELECT
    (SELECT COUNT(*) FROM mi_v_eia_latest WHERE value IS NOT NULL) AS eia_series_with_values,
    (SELECT COUNT(*) FROM mi_v_cot_latest) AS cot_rows,
    (SELECT COUNT(*) FROM mi_v_financial_metrics_latest WHERE status = 'OK') AS sec_metrics_ok,
    (SELECT COUNT(*) FROM mi_v_derived_latest WHERE status = 'OK') AS derived_ok,
    (SELECT COUNT(*) FROM mi_v_fx_snapshots_latest) AS fx_pairs,
    (SELECT COUNT(*) FROM mi_v_futures_snapshots_latest) AS futures_contracts;
