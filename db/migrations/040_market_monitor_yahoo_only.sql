-- Fail closed: dashboard market-monitor closes are Yahoo rows on
-- MARKET_MONITOR_EOD only. A non-Yahoo provider stored on that source is
-- excluded. This does not fall back to EQUITY_EOD and does not delete bars.
-- CREATE OR REPLACE preserves the existing mi_readonly grant.

CREATE OR REPLACE VIEW mi_v_market_monitor_closes AS
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
  AND b.source_id = 'MARKET_MONITOR_EOD'
  AND b.provider = 'YAHOO';

COMMENT ON VIEW mi_v_market_monitor_closes IS
    'Yahoo daily closes for US/Global Markets charts. Requires provider YAHOO on MARKET_MONITOR_EOD. Non-Yahoo rows are excluded. Does not include EQUITY_EOD.';
