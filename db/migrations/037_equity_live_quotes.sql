-- Live quote provenance views + Yahoo live fallback source registration.
-- Additive. Streamlit remains read-only against curated mi_v_* views.
-- Yahoo/yfinance is an unofficial INTERNAL_ONLY fallback with no SLA / redistribution claim.

INSERT INTO mi_source_registry (
    source_id, provider, dataset, enabled, access_status, source_url, expected_cadence, usage_scope,
    attribution, terms_notes, units_metadata, catalog_version, updated_at
) VALUES (
    'YAHOO_LIVE',
    'Yahoo Finance (yfinance, unofficial)',
    'live_quotes_fallback',
    FALSE,
    'OPTIONAL_FALLBACK',
    '',
    'INTRADAY',
    'INTERNAL_ONLY',
    'Yahoo Finance via yfinance (unofficial; no SLA).',
    'Optional fallback when IBKR live quotes are missing or stale (MI_YAHOO_LIVE_FALLBACK). Never labeled as IBKR. Entitlement/redistribution unverified; INTERNAL_ONLY.',
    '{"price":"last_trade_or_regular_market"}'::jsonb,
    'equity_live_v1',
    NOW()
)
ON CONFLICT (source_id) DO UPDATE SET
    terms_notes = EXCLUDED.terms_notes,
    attribution = EXCLUDED.attribution,
    updated_at = NOW();

-- Latest quote per (symbol, source). Symbol resolved via identifiers, else display_name, else instrument_id.
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
        WHEN q.source_id = 'YAHOO_LIVE' THEN 'YAHOO'
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
WHERE q.source_id IN ('IBKR_MARKET_DATA', 'YAHOO_LIVE')
  AND q.last_price IS NOT NULL
ORDER BY symbol, q.source_id, q.quote_ts DESC, q.id DESC;

COMMENT ON VIEW mi_v_live_quotes_by_source IS
    'Latest last_price quote per symbol and source (IBKR_MARKET_DATA, YAHOO_LIVE). Resolver prefers IBKR when fresh.';

-- Daily adjusted closes for dashboard charting / prior-close resolution (EQUITY_EOD only).
CREATE OR REPLACE VIEW mi_v_equity_daily_closes AS
SELECT
    b.instrument_id AS symbol,
    b.bar_date,
    b.open_price,
    b.high_price,
    b.low_price,
    b.close_price,
    b.adj_close_price,
    b.volume,
    b.provider,
    b.source_id,
    b.adjustment_basis,
    b.con_id
FROM mi_market_bars b
WHERE b.bar_interval = '1D'
  AND b.source_id = 'EQUITY_EOD';

COMMENT ON VIEW mi_v_equity_daily_closes IS
    'Curated EQUITY_EOD daily bars for read-only consumers. Provider column preserves IBKR vs Yahoo provenance.';
