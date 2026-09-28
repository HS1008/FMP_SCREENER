-- Yahoo FX, futures-proxy, and crypto sources, plus long-form CFTC positions.
-- Additive. Legacy mi_cftc_cot_observations and equity bar rows are not deleted.
-- Yahoo bars reuse mi_market_bars under new source ids.

INSERT INTO mi_source_registry (
    source_id, provider, dataset, enabled, access_status, source_url, expected_cadence,
    units_metadata, usage_scope, terms_notes, attribution, catalog_version, updated_at
) VALUES
(
    'YAHOO_FX',
    'Yahoo Finance',
    'fx_daily',
    TRUE,
    'AVAILABLE',
    'https://finance.yahoo.com/',
    'D',
    '{}'::jsonb,
    'ATTRIBUTION_REQUIRED',
    'Yahoo daily FX closes, including DX-Y.NYB. Weekday observations. Not an equity session calendar.',
    'Yahoo Finance daily FX history.',
    'cross_asset_v1',
    NOW()
),
(
    'YAHOO_FUTURES_PROXY',
    'Yahoo Finance',
    'commodity_futures_proxy',
    TRUE,
    'AVAILABLE',
    'https://finance.yahoo.com/',
    'D',
    '{}'::jsonb,
    'ATTRIBUTION_REQUIRED',
    'Yahoo futures proxies for market monitoring. Not an official continuous settlement or roll history.',
    'Yahoo Finance futures-proxy daily history.',
    'cross_asset_v1',
    NOW()
),
(
    'YAHOO_CRYPTO',
    'Yahoo Finance',
    'crypto_daily',
    TRUE,
    'AVAILABLE',
    'https://finance.yahoo.com/',
    'D',
    '{}'::jsonb,
    'ATTRIBUTION_REQUIRED',
    'Yahoo BTC-USD and ETH-USD daily bars. Observation date is UTC and includes weekends.',
    'Yahoo Finance crypto daily history.',
    'cross_asset_v1',
    NOW()
)
ON CONFLICT (source_id) DO UPDATE SET
    provider = EXCLUDED.provider,
    dataset = EXCLUDED.dataset,
    enabled = EXCLUDED.enabled,
    access_status = EXCLUDED.access_status,
    source_url = EXCLUDED.source_url,
    expected_cadence = EXCLUDED.expected_cadence,
    terms_notes = EXCLUDED.terms_notes,
    attribution = EXCLUDED.attribution,
    catalog_version = EXCLUDED.catalog_version,
    updated_at = NOW();

INSERT INTO mi_source_registry (
    source_id, provider, dataset, enabled, access_status, source_url, expected_cadence,
    units_metadata, usage_scope, terms_notes, attribution, catalog_version, updated_at
) VALUES (
    'CFTC_COT',
    'CFTC',
    'commitment_of_traders',
    TRUE,
    'AVAILABLE',
    'https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm',
    'W',
    '{}'::jsonb,
    'ATTRIBUTION_REQUIRED',
    'Legacy futures-only resource 6dca-aqww remains stored. Dashboard positioning uses TFF futures-only gpe5-46if and Disaggregated futures-only 72hh-3qpy. Position date is Tuesday. The record has no publication timestamp; Friday is the regular release day.',
    'CFTC Public Reporting Environment.',
    'cross_asset_v1',
    NOW()
)
ON CONFLICT (source_id) DO UPDATE SET
    terms_notes = EXCLUDED.terms_notes,
    attribution = EXCLUDED.attribution,
    catalog_version = EXCLUDED.catalog_version,
    updated_at = NOW();

CREATE TABLE IF NOT EXISTS mi_cftc_position_observations (
    id BIGSERIAL PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    report_family VARCHAR(32) NOT NULL,
    cftc_contract_market_code VARCHAR(16) NOT NULL,
    market_key VARCHAR(64) NOT NULL,
    market_name TEXT NOT NULL,
    asset_group VARCHAR(32) NOT NULL,
    position_date DATE NOT NULL,
    scheduled_publication_date DATE,
    trader_category VARCHAR(64) NOT NULL,
    long_contracts INTEGER,
    short_contracts INTEGER,
    open_interest INTEGER,
    payload_hash VARCHAR(64) NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingestion_run_id VARCHAR(64)
);

CREATE UNIQUE INDEX IF NOT EXISTS mi_cftc_position_identity_idx
    ON mi_cftc_position_observations (source_id, report_family, cftc_contract_market_code, position_date, trader_category);

CREATE INDEX IF NOT EXISTS mi_cftc_position_market_idx
    ON mi_cftc_position_observations (market_key, trader_category, position_date);

CREATE OR REPLACE VIEW mi_v_cftc_position_latest AS
SELECT DISTINCT ON (market_key, trader_category)
    source_id,
    report_family,
    cftc_contract_market_code,
    market_key,
    market_name,
    asset_group,
    position_date,
    scheduled_publication_date,
    trader_category,
    long_contracts,
    short_contracts,
    open_interest,
    retrieved_at
FROM mi_cftc_position_observations
ORDER BY market_key, trader_category, position_date DESC;

COMMENT ON TABLE mi_cftc_position_observations IS
    'TFF and Disaggregated COT category rows. Does not replace mi_cftc_cot_observations.';
