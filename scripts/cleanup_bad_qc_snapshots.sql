-- One-time cleanup for the prior QuantConnect live portfolio parser bug.
--
-- DO NOT run this on a schedule. It is not part of cron / GitHub Actions /
-- jobs/sync_quantconnect.py.
--
-- Background:
--   parse_portfolio() used to read live holdings with long field names
--   (quantity, price, marketValue). QuantConnect live holdings actually
--   use compact fields (q, p, v). Holdings parsed as $0, so equity
--   collapsed to leftover cash (~$388) while SPYTrend was still Running
--   with ~$5,000 of equity.
--
-- Conservative rule (does not wipe all history):
--   Delete SPYTrend live_snapshots where equity < 1000 AND status = 'Running'.
--   That matches the cash-only collapse, not the real ~$5,000 paper account.
--
-- Positions:
--   The old parser often inserted zero-quantity / zero-value rows in the
--   same window. Those are removed only when they sit in the malformed
--   timestamp range and have essentially no market value.

BEGIN;

CREATE TEMP TABLE doomed_spytrend_snapshots AS
SELECT
    s.timestamp
FROM live_snapshots s
JOIN strategies st
  ON st.strategy_id = s.strategy_id
WHERE (st.strategy_id = 'SPYTrend' OR st.name = 'SPYTrend')
  AND s.status = 'Running'
  AND s.equity < 1000;

DELETE FROM positions p
USING strategies st
WHERE p.strategy_id = st.strategy_id
  AND (st.strategy_id = 'SPYTrend' OR st.name = 'SPYTrend')
  AND EXISTS (
      SELECT 1
      FROM doomed_spytrend_snapshots d
      WHERE p.timestamp BETWEEN d.timestamp - INTERVAL '5 seconds'
                            AND d.timestamp + INTERVAL '5 seconds'
  )
  AND COALESCE(ABS(p.market_value), 0) < 1;

DELETE FROM live_snapshots s
USING strategies st
WHERE s.strategy_id = st.strategy_id
  AND (st.strategy_id = 'SPYTrend' OR st.name = 'SPYTrend')
  AND s.status = 'Running'
  AND s.equity < 1000;

COMMIT;
