-- IBKR daily-bar provenance on the independent EQUITY_EOD store.
-- Additive. Does not edit applied SQL. Does not touch Stage 1 / Stage 2 / holdout tables.

ALTER TABLE mi_market_bars
    ADD COLUMN IF NOT EXISTS con_id BIGINT;

COMMENT ON COLUMN mi_market_bars.con_id IS 'IBKR conId when the bar was sourced from Interactive Brokers; null for other providers.';

CREATE INDEX IF NOT EXISTS mi_market_bars_con_id_idx
    ON mi_market_bars (con_id)
    WHERE con_id IS NOT NULL;
