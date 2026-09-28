-- Long Yahoo history for the Markets dashboard only.
-- Canonical IBKR EQUITY_EOD rows stay on source_id EQUITY_EOD. This source is a
-- separate key on mi_market_bars, so the same symbol and date can exist twice
-- without overwriting or splicing the canonical series.

INSERT INTO mi_source_registry (
    source_id, provider, dataset, enabled, access_status, source_url, expected_cadence, usage_scope,
    attribution, terms_notes, units_metadata, catalog_version, updated_at
) VALUES (
    'MARKET_MONITOR_EOD',
    'YAHOO',
    'market_monitor_etfs',
    TRUE,
    'CONFIGURED',
    '',
    'D',
    'INTERNAL_ONLY',
    'Yahoo Finance via yfinance (unofficial; no SLA).',
    'Dashboard market-monitor history only. One Yahoo adjustment methodology for every market-monitor ETF. Never written into EQUITY_EOD and never spliced with IBKR.',
    '{"price":"adjusted_close","adjustment_basis":"SPLIT_ADJUSTED_UNKNOWN_DIVIDEND"}'::jsonb,
    'market_monitor_eod_v1',
    NOW()
)
ON CONFLICT (source_id) DO UPDATE SET
    dataset = EXCLUDED.dataset,
    enabled = EXCLUDED.enabled,
    terms_notes = EXCLUDED.terms_notes,
    attribution = EXCLUDED.attribution,
    units_metadata = EXCLUDED.units_metadata,
    catalog_version = EXCLUDED.catalog_version,
    updated_at = NOW();

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
  AND b.source_id = 'MARKET_MONITOR_EOD';

COMMENT ON VIEW mi_v_market_monitor_closes IS
    'Yahoo daily closes for US/Global Markets charts. Does not include EQUITY_EOD.';
