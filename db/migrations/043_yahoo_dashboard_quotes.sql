-- Dashboard market quotes from Yahoo. Additive. IBKR quote rows stay in place.
-- Streamlit reads this source through mi_v_live_quotes_by_source.

INSERT INTO mi_source_registry (
    source_id, provider, dataset, enabled, access_status, source_url, expected_cadence, usage_scope,
    attribution, terms_notes, units_metadata, catalog_version, updated_at
) VALUES (
    'YAHOO_DASHBOARD',
    'Yahoo Finance (yfinance, unofficial)',
    'dashboard_quotes',
    TRUE,
    'COLLECTOR_ACTIVE',
    '',
    'INTRADAY',
    'INTERNAL_ONLY',
    'Yahoo Finance via yfinance (unofficial; no SLA).',
    'Dashboard market quotes. Not a guaranteed real-time feed. Not an IBKR quote.',
    '{"price":"last_trade"}'::jsonb,
    'yahoo_dashboard_v1',
    NOW()
)
ON CONFLICT (source_id) DO UPDATE SET
    terms_notes = EXCLUDED.terms_notes,
    attribution = EXCLUDED.attribution,
    enabled = TRUE,
    updated_at = NOW();

CREATE OR REPLACE VIEW mi_v_live_quotes_by_source AS
SELECT DISTINCT ON (symbol, q.source_id)
    UPPER(COALESCE(sym.id_value, NULLIF(i.display_name, ''), q.instrument_id)) AS symbol,
    q.instrument_id,
    i.display_name,
    i.asset_type,
    i.security_type,
    i.con_id,
    i.currency,
    i.exchange,
    i.primary_exchange,
    q.source_id,
    CASE
        WHEN q.source_id = 'IBKR_MARKET_DATA' THEN 'IBKR'
        WHEN q.source_id IN ('YAHOO_LIVE', 'YAHOO_DASHBOARD') THEN 'YAHOO'
        ELSE q.source_id
    END AS provider,
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
LEFT JOIN LATERAL (
    SELECT id_value
    FROM mi_instrument_identifiers
    WHERE instrument_id = i.instrument_id AND id_type = 'SYMBOL'
    ORDER BY id DESC
    LIMIT 1
) sym ON TRUE
WHERE q.source_id IN ('IBKR_MARKET_DATA', 'YAHOO_LIVE', 'YAHOO_DASHBOARD')
  AND q.last_price IS NOT NULL
ORDER BY symbol, q.source_id, q.quote_ts DESC, q.id DESC;

COMMENT ON VIEW mi_v_live_quotes_by_source IS
    'Latest last_price quote per symbol and source. Dashboard quotes use YAHOO_DASHBOARD.';
