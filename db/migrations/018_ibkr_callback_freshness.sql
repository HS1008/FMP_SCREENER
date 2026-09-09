-- IBKR quote callback freshness. Additive; views only append columns.

ALTER TABLE mi_market_quotes ADD COLUMN IF NOT EXISTS last_callback_at TIMESTAMPTZ;
ALTER TABLE mi_collector_status ADD COLUMN IF NOT EXISTS last_callback_at TIMESTAMPTZ;
ALTER TABLE mi_collector_status ADD COLUMN IF NOT EXISTS queue_overflow_count INTEGER NOT NULL DEFAULT 0;

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
    q.ingestion_run_id,
    q.last_callback_at
FROM mi_market_quotes q
JOIN mi_market_instruments i ON i.instrument_id = q.instrument_id
WHERE q.source_id = 'IBKR_MARKET_DATA'
ORDER BY q.instrument_id, q.quote_ts DESC, q.id DESC;

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
    NOW() AS evaluated_at,
    last_callback_at,
    queue_overflow_count
FROM mi_collector_status;
