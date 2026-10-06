-- Expose the provider fetch timestamp on the Yahoo cross-asset history view so the
-- dashboard can show when an end-of-day close (MOVE, USD/CNH, ...) was collected.
-- Columns are appended; existing grants on the view are preserved by CREATE OR REPLACE.

CREATE OR REPLACE VIEW mi_v_yahoo_cross_asset_history AS
SELECT
    b.instrument_id,
    b.source_id,
    b.bar_date,
    b.close_price,
    b.adj_close_price,
    b.provider_symbol,
    b.provider,
    b.retrieved_at,
    b.last_seen_at
FROM mi_market_bars b
WHERE b.bar_interval = '1D'
  AND b.provider = 'YAHOO'
  AND b.source_id IN ('YAHOO_FX', 'YAHOO_FUTURES_PROXY', 'YAHOO_CRYPTO');

COMMENT ON VIEW mi_v_yahoo_cross_asset_history IS
    'Yahoo daily history for FX, futures proxies, crypto, and the MOVE index. Provider must be YAHOO. Equity and market-monitor bars are excluded. retrieved_at is the collection time of the close, not a quote time.';
