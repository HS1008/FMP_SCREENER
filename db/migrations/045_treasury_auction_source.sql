-- Official Treasury Fiscal Data auctions. Idempotent registry row for gross issuance.

INSERT INTO mi_source_registry (
    source_id, provider, dataset, enabled, access_status, source_url, expected_cadence, usage_scope,
    attribution, terms_notes, units_metadata, catalog_version, updated_at
) VALUES (
    'TREASURY_FISCAL',
    'U.S. Treasury Fiscal Data',
    'auctions_query',
    TRUE,
    'COLLECTOR_ACTIVE',
    'https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query',
    'M',
    'ATTRIBUTION_REQUIRED',
    'U.S. Department of the Treasury, Fiscal Data API.',
    'Gross accepted auction amounts for bills, notes, and bonds. TIPS, floating-rate notes, and cash-management bills are excluded.',
    '{"issuance":"gross_accepted_dollars","frequency":"monthly"}'::jsonb,
    'fred_catalog_v2',
    NOW()
)
ON CONFLICT (source_id) DO UPDATE SET
    terms_notes = EXCLUDED.terms_notes,
    attribution = EXCLUDED.attribution,
    units_metadata = EXCLUDED.units_metadata,
    source_url = EXCLUDED.source_url,
    enabled = TRUE,
    access_status = EXCLUDED.access_status,
    updated_at = NOW();
