-- Server-authoritative EQUITY_EOD batch evidence.
-- Additive. Does not edit applied SQL. Does not touch Stage 1 / Stage 2 / holdout tables.
-- Collector coverage is a claim. Completeness is proven from this batch's observations
-- on the latest received date, never from older canonical mi_market_bars rows.

ALTER TABLE mi_equity_eod_batches
    ADD COLUMN IF NOT EXISTS expected_symbols_json JSONB,
    ADD COLUMN IF NOT EXISTS expected_symbols_sha256 VARCHAR(64),
    ADD COLUMN IF NOT EXISTS universe_version VARCHAR(64);

COMMENT ON COLUMN mi_equity_eod_batches.expected_symbols_json IS
    'Frozen canonical UNIVERSE_SYMBOLS at batch creation. Collector cannot substitute a smaller expected universe.';
COMMENT ON COLUMN mi_equity_eod_batches.expected_symbols_sha256 IS
    'SHA-256 of the canonical sorted JSON serialization of expected_symbols_json.';
COMMENT ON COLUMN mi_equity_eod_batches.universe_version IS
    'TAXONOMY_VERSION frozen when the batch started so later universe edits remain auditable.';

CREATE TABLE IF NOT EXISTS mi_equity_eod_batch_observations (
    batch_id VARCHAR(64) NOT NULL REFERENCES mi_equity_eod_batches (batch_id),
    symbol VARCHAR(32) NOT NULL,
    bar_date DATE NOT NULL,
    chunk_index INTEGER NOT NULL,
    row_hash VARCHAR(64) NOT NULL,
    con_id BIGINT,
    provider VARCHAR(32) NOT NULL,
    what_to_show VARCHAR(32) NOT NULL,
    adjustment_basis VARCHAR(64) NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (batch_id, symbol, bar_date)
);

COMMENT ON TABLE mi_equity_eod_batch_observations IS
    'Observations actually received in one logical IBKR EQUITY_EOD batch. Authoritative for COMPLETE.';

CREATE INDEX IF NOT EXISTS mi_equity_eod_batch_obs_date_idx
    ON mi_equity_eod_batch_observations (batch_id, bar_date, symbol);

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
    finalized_at,
    expected_symbols_sha256,
    universe_version
FROM mi_equity_eod_batches;
