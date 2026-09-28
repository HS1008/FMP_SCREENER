-- Retire CBOE and OpenBB-CBOE as active providers.
-- Historical mi_openbb_* tables, snapshots, and freshness rows are retained.
-- ON CONFLICT does not change an existing provider or dataset label.

INSERT INTO mi_source_registry (
    source_id, provider, dataset, enabled, access_status, source_url, expected_cadence,
    units_metadata, usage_scope, terms_notes, attribution, catalog_version, updated_at
) VALUES
(
    'CBOE_ALL_ACCESS',
    'Cboe (retired)',
    'cboe_all_access',
    FALSE,
    'RETIRED_OPTIONAL',
    NULL,
    'D',
    '{}'::jsonb,
    'INTERNAL_ONLY',
    'Intentionally retired. CBOE/OpenBB-CBOE is no longer an active data provider. Yahoo volatility replaced it for active dashboard use. Historical observations are retained. This is not a platform outage and does not require CBOE credentials.',
    'Retired CBOE/OpenBB-CBOE provider. Not an active integration. Index names such as the Cboe SKEW Index identify the index, not a CBOE data feed.',
    'cboe_retired_v1',
    NOW()
),
(
    'OPENBB_CBOE',
    'OpenBB / Cboe (retired)',
    'openbb_cboe',
    FALSE,
    'RETIRED_OPTIONAL',
    NULL,
    'D',
    '{}'::jsonb,
    'INTERNAL_ONLY',
    'Intentionally retired. CBOE/OpenBB-CBOE is no longer an active data provider. Yahoo volatility replaced it for active dashboard use. Historical observations are retained. This is not a platform outage and does not require CBOE credentials.',
    'Retired CBOE/OpenBB-CBOE provider. Not an active integration. Index names such as the Cboe SKEW Index identify the index, not a CBOE data feed.',
    'cboe_retired_v1',
    NOW()
),
(
    'OPENBB_CBOE_OPTIONS',
    'OpenBB / Cboe (retired)',
    'cboe_delayed_options_chains',
    FALSE,
    'RETIRED_OPTIONAL',
    NULL,
    'D',
    '{}'::jsonb,
    'INTERNAL_ONLY',
    'Intentionally retired. CBOE/OpenBB-CBOE is no longer an active data provider. Yahoo volatility replaced it for active dashboard use. Historical observations are retained. This is not a platform outage and does not require CBOE credentials.',
    'Retired CBOE/OpenBB-CBOE provider. Not an active integration. Index names such as the Cboe SKEW Index identify the index, not a CBOE data feed.',
    'cboe_retired_v1',
    NOW()
),
(
    'OPENBB_CBOE_VIX',
    'OpenBB / Cboe (retired)',
    'cboe_vx_eod_curve',
    FALSE,
    'RETIRED_OPTIONAL',
    NULL,
    'D',
    '{}'::jsonb,
    'INTERNAL_ONLY',
    'Intentionally retired. CBOE/OpenBB-CBOE is no longer an active data provider. Yahoo volatility replaced it for active dashboard use. Historical observations are retained. This is not a platform outage and does not require CBOE credentials.',
    'Retired CBOE/OpenBB-CBOE provider. Not an active integration. Index names such as the Cboe SKEW Index identify the index, not a CBOE data feed.',
    'cboe_retired_v1',
    NOW()
)
ON CONFLICT (source_id) DO UPDATE SET
    enabled = FALSE,
    access_status = 'RETIRED_OPTIONAL',
    terms_notes = EXCLUDED.terms_notes,
    attribution = EXCLUDED.attribution,
    catalog_version = EXCLUDED.catalog_version,
    updated_at = NOW();
