-- Expose Yahoo's dividend- and split-adjusted Adj Close on the dashboard price view.
-- close_price stays the split-adjusted close behind the 1W-1Y price-return columns;
-- adj_close_price feeds only the individual-stock realized volatility and Sharpe
-- columns. The ingest repairs the full stored window on a split or dividend so the
-- adjusted series stays on one Yahoo vintage. Columns are appended; existing grants
-- on the view are preserved by CREATE OR REPLACE.

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
    'Split-adjusted daily closes for dashboard price returns (cash dividends excluded) plus the dividend- and split-adjusted Adj Close used only for realized volatility and Sharpe. Not MARKET_MONITOR_EOD and not EQUITY_EOD.',
    '{"price":"split_adjusted_close","dividends":"excluded","adj_close_price":"dividend_and_split_adjusted_close","adj_close_usage":"realized_volatility_and_sharpe_only"}'::jsonb,
    'yahoo_price_daily_v2',
    NOW()
)
ON CONFLICT (source_id) DO UPDATE SET
    terms_notes = EXCLUDED.terms_notes,
    units_metadata = EXCLUDED.units_metadata,
    catalog_version = EXCLUDED.catalog_version,
    enabled = TRUE,
    updated_at = NOW();

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
    b.source_id,
    b.adj_close_price
FROM mi_market_bars b
WHERE b.bar_interval = '1D'
  AND b.source_id = 'YAHOO_PRICE_DAILY';

COMMENT ON VIEW mi_v_yahoo_price_daily IS
    'Yahoo split-adjusted daily closes for 1W-1Y price returns, plus the dividend- and split-adjusted adj_close_price for realized volatility and Sharpe. Does not include MARKET_MONITOR_EOD or EQUITY_EOD.';
