-- IBKR Windows-local collector: quote provenance columns, collector heartbeat, curated views.
-- Additive. FRED/FINRA/research tables are untouched. No view/table drops.

ALTER TABLE mi_market_quotes ADD COLUMN IF NOT EXISTS con_id BIGINT;
ALTER TABLE mi_market_quotes ADD COLUMN IF NOT EXISTS market_data_type VARCHAR(32);
ALTER TABLE mi_market_quotes ADD COLUMN IF NOT EXISTS provenance JSONB;
ALTER TABLE mi_market_quotes ADD COLUMN IF NOT EXISTS source_ts TIMESTAMPTZ;
ALTER TABLE mi_market_quotes ADD COLUMN IF NOT EXISTS last_size NUMERIC;
ALTER TABLE mi_market_quotes ADD COLUMN IF NOT EXISTS close_price NUMERIC;
ALTER TABLE mi_market_quotes ADD COLUMN IF NOT EXISTS record_id VARCHAR(128);

ALTER TABLE mi_market_instruments ADD COLUMN IF NOT EXISTS con_id BIGINT;
ALTER TABLE mi_market_instruments ADD COLUMN IF NOT EXISTS primary_exchange VARCHAR(32);

CREATE UNIQUE INDEX IF NOT EXISTS mi_market_quotes_record_id_uidx
    ON mi_market_quotes (source_id, record_id)
    WHERE record_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS mi_collector_status (
    collector_id VARCHAR(64) PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL REFERENCES mi_source_registry (source_id),
    reported_state VARCHAR(32) NOT NULL,
    last_heartbeat_at TIMESTAMPTZ NOT NULL,
    last_socket_ok_at TIMESTAMPTZ,
    last_api_handshake_at TIMESTAMPTZ,
    last_tws_connect_at TIMESTAMPTZ,
    last_quote_at TIMESTAMPTZ,
    last_ingest_ok_at TIMESTAMPTZ,
    last_delivery_error_redacted TEXT,
    market_data_type VARCHAR(32),
    client_id INTEGER,
    watchlist_json JSONB,
    details_json JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- observed_state is computed from the database clock so an offline collector cannot
-- report itself healthy. 90s matches the collector's default heartbeat interval * 6.
CREATE OR REPLACE VIEW mi_v_ibkr_collector_status AS
SELECT
    collector_id,
    source_id,
    reported_state,
    CASE
        WHEN last_heartbeat_at < (NOW() - INTERVAL '90 seconds') THEN 'COLLECTOR_OFFLINE'
        ELSE reported_state
    END AS observed_state,
    EXTRACT(EPOCH FROM (NOW() - last_heartbeat_at))::INTEGER AS heartbeat_age_seconds,
    last_heartbeat_at,
    last_socket_ok_at,
    last_api_handshake_at,
    last_tws_connect_at,
    last_quote_at,
    last_ingest_ok_at,
    last_delivery_error_redacted,
    market_data_type,
    client_id,
    watchlist_json,
    details_json,
    updated_at,
    NOW() AS evaluated_at
FROM mi_collector_status;

CREATE OR REPLACE VIEW mi_v_ibkr_quotes_latest AS
SELECT DISTINCT ON (q.instrument_id)
    q.instrument_id,
    i.display_name,
    i.asset_type,
    i.security_type,
    i.con_id,
    i.currency,
    i.exchange,
    i.primary_exchange,
    q.source_id,
    q.quote_ts,
    q.source_ts,
    q.retrieved_at,
    q.bid,
    q.ask,
    q.last_price,
    q.mid,
    q.bid_size,
    q.ask_size,
    q.last_size,
    q.close_price,
    q.market_data_type,
    q.delay_status,
    q.quote_status,
    q.provenance,
    q.record_id,
    q.ingestion_run_id
FROM mi_market_quotes q
JOIN mi_market_instruments i ON i.instrument_id = q.instrument_id
WHERE q.source_id = 'IBKR_MARKET_DATA'
ORDER BY q.instrument_id, q.quote_ts DESC, q.id DESC;
