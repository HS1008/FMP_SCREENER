-- Platform identity, source policy, and checkpoints.
-- Additive. Does not edit applied SQL. Does not touch Stage 1 / Stage 2 / holdout tables.

CREATE TABLE IF NOT EXISTS mi_issuers (
    issuer_id VARCHAR(64) PRIMARY KEY,
    display_name TEXT NOT NULL,
    cik VARCHAR(10),
    lei VARCHAR(32),
    country VARCHAR(8),
    entity_type VARCHAR(32),
    source_id VARCHAR(64) NOT NULL,
    confidence VARCHAR(16) NOT NULL DEFAULT 'UNVERIFIED',
    effective_from DATE,
    effective_to DATE,
    knowledge_date DATE,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dashboard_eligible BOOLEAN NOT NULL DEFAULT TRUE,
    research_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    pit_safe BOOLEAN NOT NULL DEFAULT FALSE,
    export_scope VARCHAR(32) NOT NULL DEFAULT 'INTERNAL_ONLY'
);

CREATE INDEX IF NOT EXISTS mi_issuers_cik_idx ON mi_issuers (cik);

CREATE TABLE IF NOT EXISTS mi_securities (
    security_id VARCHAR(64) PRIMARY KEY,
    issuer_id VARCHAR(64) REFERENCES mi_issuers (issuer_id),
    instrument_id VARCHAR(64),
    instrument_type VARCHAR(16) NOT NULL,
    currency VARCHAR(8),
    share_class VARCHAR(32),
    figi VARCHAR(32),
    composite_figi VARCHAR(32),
    share_class_figi VARCHAR(32),
    isin VARCHAR(16),
    cusip VARCHAR(16),
    ibkr_con_id BIGINT,
    qc_sid VARCHAR(64),
    listing_exchange VARCHAR(32),
    ticker VARCHAR(32),
    source_id VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
    confidence VARCHAR(16) NOT NULL DEFAULT 'UNVERIFIED',
    effective_from DATE,
    effective_to DATE,
    knowledge_date DATE,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dashboard_eligible BOOLEAN NOT NULL DEFAULT TRUE,
    research_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    pit_safe BOOLEAN NOT NULL DEFAULT FALSE,
    export_scope VARCHAR(32) NOT NULL DEFAULT 'INTERNAL_ONLY',
    ambiguity_reason TEXT
);

CREATE INDEX IF NOT EXISTS mi_securities_ticker_idx ON mi_securities (ticker, listing_exchange);
CREATE INDEX IF NOT EXISTS mi_securities_instrument_idx ON mi_securities (instrument_id);
CREATE INDEX IF NOT EXISTS mi_securities_figi_idx ON mi_securities (figi);

CREATE TABLE IF NOT EXISTS mi_classification_versions (
    classification_id VARCHAR(64) PRIMARY KEY,
    security_id VARCHAR(64) REFERENCES mi_securities (security_id),
    taxonomy_name VARCHAR(64) NOT NULL,
    taxonomy_version VARCHAR(32) NOT NULL,
    sector_key VARCHAR(64),
    industry_key VARCHAR(96),
    subindustry_key VARCHAR(96),
    source_id VARCHAR(64) NOT NULL,
    effective_from DATE,
    effective_to DATE,
    knowledge_date DATE,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pit_safe BOOLEAN NOT NULL DEFAULT FALSE,
    research_eligible BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS mi_corporate_actions (
    action_id VARCHAR(64) PRIMARY KEY,
    security_id VARCHAR(64),
    issuer_id VARCHAR(64),
    action_type VARCHAR(32) NOT NULL,
    announcement_date DATE,
    available_at TIMESTAMPTZ,
    available_at_basis VARCHAR(32) NOT NULL DEFAULT 'FIRST_SEEN',
    effective_date DATE,
    ex_date DATE,
    pay_date DATE,
    factor NUMERIC,
    cash_amount NUMERIC,
    currency VARCHAR(8),
    adjustment_basis VARCHAR(32),
    source_id VARCHAR(64) NOT NULL,
    evidence_url TEXT,
    raw_json JSONB,
    content_sha256 VARCHAR(64),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dashboard_eligible BOOLEAN NOT NULL DEFAULT TRUE,
    research_eligible BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS mi_source_policies (
    policy_id VARCHAR(96) PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    dataset VARCHAR(128) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    priority VARCHAR(32) NOT NULL DEFAULT 'BROAD_BACKGROUND',
    cadence VARCHAR(32),
    market_or_release_window TEXT,
    freshness_slo TEXT,
    request_budget INTEGER,
    batch_size INTEGER,
    retry_limit INTEGER,
    retention_detail TEXT,
    retention_rollup TEXT,
    rights_scope VARCHAR(32) NOT NULL DEFAULT 'INTERNAL_ONLY',
    dependencies_json JSONB,
    catalog_version VARCHAR(32) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mi_dataset_checkpoints (
    source_id VARCHAR(64) NOT NULL,
    dataset VARCHAR(128) NOT NULL,
    next_due_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    expected_release_at TIMESTAMPTZ,
    last_accepted_observation DATE,
    last_accepted_version VARCHAR(64),
    provider_cursor TEXT,
    outcome VARCHAR(32),
    rows_accepted INTEGER,
    request_count INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_id, dataset)
);

CREATE TABLE IF NOT EXISTS mi_source_resolution_events (
    event_id BIGSERIAL PRIMARY KEY,
    dataset VARCHAR(128) NOT NULL,
    observation_date DATE,
    instrument_id VARCHAR(64),
    candidate_sources_json JSONB NOT NULL,
    chosen_source_id VARCHAR(64),
    reason TEXT NOT NULL,
    schema_version VARCHAR(32) NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mi_capability_matrix (
    capability_id VARCHAR(96) PRIMARY KEY,
    domain VARCHAR(64) NOT NULL,
    status VARCHAR(48) NOT NULL,
    implementation_path TEXT,
    intended_change TEXT,
    dependencies TEXT,
    test_gate TEXT,
    estimated_cost TEXT,
    activation_state VARCHAR(32) NOT NULL DEFAULT 'OFF',
    environment_validated VARCHAR(32),
    production_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    rights_scope VARCHAR(32),
    blocker TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE VIEW mi_v_source_policies AS
SELECT policy_id, source_id, dataset, enabled, priority, cadence, market_or_release_window,
       freshness_slo, rights_scope, catalog_version, updated_at
FROM mi_source_policies;

CREATE OR REPLACE VIEW mi_v_capability_matrix AS
SELECT capability_id, domain, status, activation_state, environment_validated,
       production_enabled, rights_scope, blocker, updated_at
FROM mi_capability_matrix;

CREATE OR REPLACE VIEW mi_v_issuers AS
SELECT issuer_id, display_name, cik, country, entity_type, source_id, confidence,
       effective_from, knowledge_date, dashboard_eligible, research_eligible, pit_safe, export_scope
FROM mi_issuers;

CREATE OR REPLACE VIEW mi_v_securities AS
SELECT security_id, issuer_id, instrument_id, instrument_type, currency, ticker, listing_exchange,
       figi, ibkr_con_id, status, confidence, dashboard_eligible, research_eligible, pit_safe, export_scope, ambiguity_reason
FROM mi_securities;
