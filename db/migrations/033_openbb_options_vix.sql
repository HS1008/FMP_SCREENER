-- OpenBB / Cboe delayed options chains and VX_EOD curve.
-- Additive. Compatible with a previous app version if application code is rolled back.

CREATE TABLE IF NOT EXISTS mi_openbb_snapshots (
    snapshot_id VARCHAR(64) PRIMARY KEY,
    source_id VARCHAR(32) NOT NULL,
    dataset VARCHAR(64) NOT NULL,
    underlying VARCHAR(16) NOT NULL,
    session_date DATE,
    observation_time_utc TIMESTAMPTZ,
    observation_precision VARCHAR(16) NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL,
    source_timestamp_utc TIMESTAMPTZ,
    source_timestamp_precision VARCHAR(16),
    content_hash VARCHAR(64) NOT NULL,
    normalization_version VARCHAR(32) NOT NULL,
    openbb_version VARCHAR(32),
    provider VARCHAR(32) NOT NULL,
    endpoint_category VARCHAR(64),
    run_id VARCHAR(64),
    publication_status VARCHAR(32) NOT NULL,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    revision_seq INTEGER NOT NULL DEFAULT 1,
    quality_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    coverage_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    permitted_use_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    extra_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    export_scope VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT mi_openbb_snapshots_hash_uidx UNIQUE (source_id, dataset, underlying, content_hash, revision_seq)
);

COMMENT ON TABLE mi_openbb_snapshots IS
    'OpenBB/Cboe snapshot envelope. content_hash excludes fetch time and run id.';
COMMENT ON COLUMN mi_openbb_snapshots.observation_precision IS
    'timestamp | date | month | unknown. Unknown never invents an observation time.';

CREATE UNIQUE INDEX IF NOT EXISTS mi_openbb_snapshots_current_uidx
    ON mi_openbb_snapshots (source_id, dataset, underlying)
    WHERE is_current AND publication_status = 'COMPLETE';

CREATE INDEX IF NOT EXISTS mi_openbb_snapshots_session_idx
    ON mi_openbb_snapshots (dataset, underlying, session_date, collected_at DESC);

CREATE TABLE IF NOT EXISTS mi_openbb_option_contracts (
    snapshot_id VARCHAR(64) NOT NULL REFERENCES mi_openbb_snapshots (snapshot_id),
    contract_symbol VARCHAR(64) NOT NULL,
    expiration DATE,
    expiration_precision VARCHAR(16),
    dte_session INTEGER,
    strike NUMERIC,
    option_type VARCHAR(8),
    currency VARCHAR(8),
    multiplier NUMERIC,
    multiplier_rule VARCHAR(32),
    underlying_price NUMERIC,
    bid NUMERIC,
    bid_size INTEGER,
    ask NUMERIC,
    ask_size INTEGER,
    last NUMERIC,
    last_trade_time TIMESTAMPTZ,
    open_interest INTEGER,
    volume INTEGER,
    iv_decimal NUMERIC,
    delta NUMERIC,
    gamma NUMERIC,
    theta NUMERIC,
    vega NUMERIC,
    rho NUMERIC,
    theoretical_price NUMERIC,
    quote_quality VARCHAR(16),
    is_adjusted BOOLEAN NOT NULL DEFAULT FALSE,
    identity_ok BOOLEAN NOT NULL DEFAULT TRUE,
    flags_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (snapshot_id, contract_symbol)
);

COMMENT ON COLUMN mi_openbb_option_contracts.iv_decimal IS
    'OpenBB/Cboe implied volatility as a decimal, not percent.';

CREATE INDEX IF NOT EXISTS mi_openbb_option_contracts_expiry_idx
    ON mi_openbb_option_contracts (snapshot_id, expiration, strike);

CREATE TABLE IF NOT EXISTS mi_openbb_options_metrics (
    snapshot_id VARCHAR(64) NOT NULL REFERENCES mi_openbb_snapshots (snapshot_id),
    method_version VARCHAR(64) NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    coverage_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (snapshot_id, method_version)
);

CREATE TABLE IF NOT EXISTS mi_openbb_vix_points (
    snapshot_id VARCHAR(64) NOT NULL REFERENCES mi_openbb_snapshots (snapshot_id),
    point_index INTEGER NOT NULL,
    expiration_label VARCHAR(32) NOT NULL,
    expiration_precision VARCHAR(16) NOT NULL,
    price NUMERIC,
    contract_symbol VARCHAR(32),
    observation_date DATE,
    flags_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (snapshot_id, expiration_label)
);

CREATE TABLE IF NOT EXISTS mi_openbb_vix_metrics (
    snapshot_id VARCHAR(64) NOT NULL REFERENCES mi_openbb_snapshots (snapshot_id),
    method_version VARCHAR(64) NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (snapshot_id, method_version)
);

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
    'CBOE_DELAYED'::varchar AS delay_label,
    s.export_scope,
    s.normalization_version,
    s.openbb_version,
    s.quality_json,
    s.permitted_use_json AS provenance_json,
    m.method_version,
    m.metrics_json,
    m.coverage_json,
    '{}'::jsonb AS unavailable_json,
    (SELECT COUNT(*) FROM mi_openbb_option_contracts c WHERE c.snapshot_id = s.snapshot_id) AS contract_count
FROM mi_openbb_snapshots s
LEFT JOIN mi_openbb_options_metrics m ON m.snapshot_id = s.snapshot_id
WHERE s.dataset = 'options_chain' AND s.is_current AND s.publication_status = 'COMPLETE';

CREATE OR REPLACE VIEW mi_v_options_contracts_latest AS
SELECT
    s.underlying AS underlying_symbol,
    s.snapshot_id,
    s.session_date,
    s.observation_time_utc AS observation_time,
    'CBOE_DELAYED'::varchar AS delay_label,
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
    c.flags_json AS quote_flags_json
FROM mi_openbb_snapshots s
JOIN mi_openbb_option_contracts c ON c.snapshot_id = s.snapshot_id
WHERE s.dataset = 'options_chain' AND s.is_current AND s.publication_status = 'COMPLETE';

CREATE OR REPLACE VIEW mi_v_vix_curve_latest AS
SELECT
    s.snapshot_id,
    s.source_id,
    s.provider,
    COALESCE((SELECT MIN(p.observation_date) FROM mi_openbb_vix_points p WHERE p.snapshot_id = s.snapshot_id), s.session_date) AS observation_date,
    s.observation_precision,
    'cboe_4pm_et'::varchar AS level_type,
    s.collected_at,
    s.content_hash AS payload_hash,
    'CBOE_EOD'::varchar AS delay_label,
    s.export_scope AS permitted_use,
    s.quality_json,
    m.method_version,
    m.metrics_json
FROM mi_openbb_snapshots s
LEFT JOIN mi_openbb_vix_metrics m ON m.snapshot_id = s.snapshot_id
WHERE s.dataset = 'vix_eod_curve' AND s.is_current AND s.publication_status = 'COMPLETE';

CREATE OR REPLACE VIEW mi_v_openbb_last_attempt AS
SELECT
    s.source_id,
    s.dataset,
    s.underlying AS symbol,
    s.collected_at AS attempted_at,
    s.publication_status AS status,
    s.quality_json ->> 'error' AS error_category,
    s.quality_json ->> 'error' AS error_redacted
FROM mi_openbb_snapshots s
WHERE s.collected_at = (
    SELECT MAX(s2.collected_at) FROM mi_openbb_snapshots s2
    WHERE s2.source_id = s.source_id AND s2.dataset = s.dataset AND s2.underlying = s.underlying
);
