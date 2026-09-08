-- Bond reference, quotes, trades, and local analytics (bounded domain). Additive.
-- Security terms come from sources, never inferred from ticker strings.

CREATE TABLE IF NOT EXISTS mi_bond_securities (
    bond_id VARCHAR(64) PRIMARY KEY,
    issuer_name TEXT,
    issuer_cik VARCHAR(16),
    currency VARCHAR(8) NOT NULL,
    coupon_type VARCHAR(16) NOT NULL,
    coupon_rate NUMERIC,
    coupon_frequency INTEGER,
    issue_date DATE,
    maturity_date DATE,
    day_count VARCHAR(16),
    settlement_days INTEGER,
    business_day_convention VARCHAR(32),
    redemption NUMERIC,
    callable BOOLEAN,
    putable BOOLEAN,
    call_schedule_json JSONB,
    structure_flags JSONB,
    source_id VARCHAR(64) NOT NULL,
    source_identifiers JSONB,
    valid_from DATE,
    valid_to DATE,
    terms_status VARCHAR(32) NOT NULL DEFAULT 'UNVERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mi_bond_quotes (
    id BIGSERIAL PRIMARY KEY,
    bond_id VARCHAR(64) NOT NULL REFERENCES mi_bond_securities (bond_id),
    source_id VARCHAR(64) NOT NULL,
    quote_ts TIMESTAMPTZ NOT NULL,
    price_kind VARCHAR(8) NOT NULL,
    bid_price NUMERIC,
    ask_price NUMERIC,
    last_price NUMERIC,
    price_per VARCHAR(8) NOT NULL DEFAULT '100',
    delay_status VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN',
    entitlement_status VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN',
    retrieved_at TIMESTAMPTZ NOT NULL,
    source_ref TEXT,
    UNIQUE (bond_id, source_id, quote_ts, price_kind)
);

CREATE TABLE IF NOT EXISTS mi_bond_trades (
    id BIGSERIAL PRIMARY KEY,
    bond_id VARCHAR(64) NOT NULL REFERENCES mi_bond_securities (bond_id),
    source_id VARCHAR(64) NOT NULL,
    source_trade_id VARCHAR(64) NOT NULL,
    trade_ts TIMESTAMPTZ NOT NULL,
    report_ts TIMESTAMPTZ,
    price NUMERIC,
    price_kind VARCHAR(8) NOT NULL,
    price_per VARCHAR(8) NOT NULL DEFAULT '100',
    volume NUMERIC,
    volume_units VARCHAR(16),
    volume_capped BOOLEAN,
    correction_kind VARCHAR(16) NOT NULL DEFAULT 'ORIGINAL',
    corrects_trade_id VARCHAR(64),
    retrieved_at TIMESTAMPTZ NOT NULL,
    UNIQUE (source_id, source_trade_id, correction_kind)
);

CREATE TABLE IF NOT EXISTS mi_bond_analytics (
    id BIGSERIAL PRIMARY KEY,
    bond_id VARCHAR(64) NOT NULL REFERENCES mi_bond_securities (bond_id),
    as_of DATE NOT NULL,
    settlement_date DATE NOT NULL,
    price_kind VARCHAR(8) NOT NULL,
    clean_price NUMERIC,
    dirty_price NUMERIC,
    accrued_interest NUMERIC,
    ytm NUMERIC,
    macaulay_duration NUMERIC,
    modified_duration NUMERIC,
    convexity NUMERIC,
    g_spread_bps NUMERIC,
    z_spread_bps NUMERIC,
    oas_bps NUMERIC,
    analytics_version VARCHAR(32) NOT NULL,
    support_status VARCHAR(32) NOT NULL,
    detail_json JSONB,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (bond_id, as_of, price_kind, analytics_version)
);
