-- Provider-neutral delay labels for stored option snapshots.
-- CREATE OR REPLACE cannot rename or reorder columns from 033; new fields append.
-- Existing OpenBB rows keep CBOE_DELAYED when quality_json has no delay_label.

CREATE OR REPLACE VIEW mi_v_options_latest AS
SELECT
    s.snapshot_id,
    s.source_id,
    s.provider,
    s.underlying AS underlying_symbol,
    s.observation_time_utc AS observation_time,
    s.session_date AS observation_date,
    s.observation_precision,
    s.session_date,
    s.collected_at,
    s.content_hash AS payload_hash,
    s.revision_seq,
    s.publication_status,
    COALESCE(
        NULLIF(s.quality_json->>'delay_label', ''),
        CASE
            WHEN s.source_id = 'OPENBB_CBOE_OPTIONS' THEN 'CBOE_DELAYED'
            WHEN s.source_id = 'IBKR_OPTIONS' THEN 'IBKR_UNAVAILABLE'
            ELSE s.source_id
        END
    )::varchar AS delay_label,
    s.export_scope,
    s.normalization_version,
    s.openbb_version,
    s.quality_json,
    s.permitted_use_json AS provenance_json,
    m.method_version,
    m.metrics_json,
    m.coverage_json,
    '{}'::jsonb AS unavailable_json,
    (SELECT COUNT(*) FROM mi_openbb_option_contracts c WHERE c.snapshot_id = s.snapshot_id) AS contract_count,
    s.quality_json->>'market_data_type' AS market_data_type
FROM mi_openbb_snapshots s
LEFT JOIN mi_openbb_options_metrics m ON m.snapshot_id = s.snapshot_id
WHERE s.dataset = 'options_chain' AND s.is_current AND s.publication_status = 'COMPLETE';

CREATE OR REPLACE VIEW mi_v_options_contracts_latest AS
SELECT
    s.underlying AS underlying_symbol,
    s.snapshot_id,
    s.session_date,
    s.observation_time_utc AS observation_time,
    COALESCE(
        NULLIF(s.quality_json->>'delay_label', ''),
        CASE
            WHEN s.source_id = 'OPENBB_CBOE_OPTIONS' THEN 'CBOE_DELAYED'
            WHEN s.source_id = 'IBKR_OPTIONS' THEN 'IBKR_UNAVAILABLE'
            ELSE s.source_id
        END
    )::varchar AS delay_label,
    c.contract_symbol,
    c.expiration,
    c.strike,
    CASE WHEN c.option_type = 'call' THEN 'C' WHEN c.option_type = 'put' THEN 'P' ELSE NULL END AS call_put,
    c.bid,
    c.ask,
    c.open_interest,
    c.volume,
    c.iv_decimal AS implied_volatility,
    c.delta,
    c.gamma,
    (NOT c.is_adjusted AND c.identity_ok AND c.gamma IS NOT NULL AND c.open_interest IS NOT NULL) AS eligible_for_dollar_gex,
    c.is_adjusted,
    c.flags_json AS quote_flags_json,
    s.quality_json->>'market_data_type' AS market_data_type
FROM mi_openbb_snapshots s
JOIN mi_openbb_option_contracts c ON c.snapshot_id = s.snapshot_id
WHERE s.dataset = 'options_chain' AND s.is_current AND s.publication_status = 'COMPLETE';
