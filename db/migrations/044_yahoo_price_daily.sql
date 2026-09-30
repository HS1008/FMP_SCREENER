-- Split-adjusted daily closes for dashboard price returns.
-- A separate source_id leaves MARKET_MONITOR_EOD chart history unchanged.

INSERT INTO mi_source_registry (
    source_id, provider, dataset, enabled, access_status, source_url, expected_cadence, usage_scope,
    attribution, terms_notes, units_metadata, catalog_version, updated_at
) VALUES (
    'YAHOO_PRICE_DAILY',
    'Yahoo Finance (yfinance, unofficial)',
    'dashboard_price_history',
    TRUE,
    'COLLECTOR_ACTIVE',
    '',
    'D',
    'INTERNAL_ONLY',
    'Yahoo Finance via yfinance (unofficial; no SLA).',
    'Split-adjusted daily closes for dashboard price returns. Cash dividends are excluded. Not MARKET_MONITOR_EOD and not EQUITY_EOD.',
    '{"price":"split_adjusted_close","dividends":"excluded"}'::jsonb,
    'yahoo_price_daily_v1',
    NOW()
)
ON CONFLICT (source_id) DO UPDATE SET
    terms_notes = EXCLUDED.terms_notes,
    attribution = EXCLUDED.attribution,
    units_metadata = EXCLUDED.units_metadata,
    enabled = TRUE,
    updated_at = NOW();

ALTER TABLE mi_market_bars
    ADD COLUMN IF NOT EXISTS bar_quality VARCHAR(16);

CREATE OR REPLACE VIEW mi_v_yahoo_price_daily AS
SELECT
    b.instrument_id AS symbol,
    b.bar_date,
    b.bar_ts,
    b.open_price,
    b.high_price,
    b.low_price,
    b.close_price,
    b.volume,
    b.adjustment_basis,
    b.bar_quality,
    b.provider,
    b.provider_symbol,
    b.retrieved_at,
    b.source_id
FROM mi_market_bars b
WHERE b.bar_interval = '1D'
  AND b.source_id = 'YAHOO_PRICE_DAILY';

COMMENT ON VIEW mi_v_yahoo_price_daily IS
    'Yahoo split-adjusted daily closes for 1W-1Y price returns. Does not include MARKET_MONITOR_EOD or EQUITY_EOD.';
