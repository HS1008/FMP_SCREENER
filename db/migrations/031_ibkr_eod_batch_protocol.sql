-- IBKR EQUITY_EOD multi-chunk batch protocol and coverage provenance.
-- Additive. Does not edit applied SQL. Does not touch Stage 1 / Stage 2 / holdout tables.
-- DigitalOcean never opens a TWS socket; this table records collector-push batches only.

ALTER TABLE mi_market_bars
    ADD COLUMN IF NOT EXISTS provider VARCHAR(32);

COMMENT ON COLUMN mi_market_bars.provider IS
    'Canonical bar producer identity (IBKR, YAHOO, FIXTURE). Never mix providers in one derived snapshot.';

CREATE INDEX IF NOT EXISTS mi_market_bars_provider_idx
    ON mi_market_bars (source_id, provider, instrument_id, bar_date);

ALTER TABLE mi_data_freshness
    ADD COLUMN IF NOT EXISTS coverage_status VARCHAR(32),
    ADD COLUMN IF NOT EXISTS coverage_ratio NUMERIC,
    ADD COLUMN IF NOT EXISTS coverage_json JSONB;

COMMENT ON COLUMN mi_data_freshness.coverage_status IS
    'Universe coverage completeness: COMPLETE, PARTIAL, EMPTY, or NULL when not applicable. Independent of transport_status and observation freshness.';

CREATE TABLE IF NOT EXISTS mi_equity_eod_batches (
    batch_id VARCHAR(64) PRIMARY KEY,
    collector_id VARCHAR(64) NOT NULL,
    source_id VARCHAR(64) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    what_to_show VARCHAR(32) NOT NULL,
    adjustment_basis VARCHAR(64) NOT NULL,
    request_mode VARCHAR(32) NOT NULL,
    chunk_count INTEGER NOT NULL,
    bars_expected INTEGER,
    symbols_expected INTEGER,
    state VARCHAR(32) NOT NULL DEFAULT 'OPEN',
    coverage_json JSONB,
    coverage_ratio NUMERIC,
    coverage_status VARCHAR(32),
    chunk_hashes_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_chunks INTEGER NOT NULL DEFAULT 0,
    bars_received INTEGER NOT NULL DEFAULT 0,
    ingestion_run_id VARCHAR(64),
    finalized_run_id VARCHAR(64),
    error_redacted TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finalized_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS mi_equity_eod_batches_state_idx
    ON mi_equity_eod_batches (state, updated_at DESC);

CREATE TABLE IF NOT EXISTS mi_equity_eod_batch_chunks (
    batch_id VARCHAR(64) NOT NULL REFERENCES mi_equity_eod_batches (batch_id),
    chunk_index INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    chunk_hash VARCHAR(64) NOT NULL,
    bar_count INTEGER NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (batch_id, chunk_index)
);

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
    f.updated_at AS freshness_updated_at,
    f.expected_cadence AS dataset_cadence,
    f.freshness_policy_version,
    f.latest_observation_retrieved_at,
    f.metadata_status,
    f.coverage_status,
    f.coverage_ratio,
    f.coverage_json
FROM mi_source_registry r
LEFT JOIN mi_data_freshness f ON f.source_id = r.source_id;

CREATE OR REPLACE VIEW mi_v_equity_eod_coverage AS
SELECT
    batch_id,
    collector_id,
    source_id,
    provider,
    what_to_show,
    adjustment_basis,
    request_mode,
    chunk_count,
    received_chunks,
    state,
    coverage_status,
    coverage_ratio,
    coverage_json,
    bars_received,
    created_at,
    updated_at,
    finalized_at
FROM mi_equity_eod_batches;
