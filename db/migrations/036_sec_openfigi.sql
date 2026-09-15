-- SEC filings/facts/events plus OpenFIGI identifier mapping cache.
-- Additive. Does not edit applied SQL. Does not touch research/holdout tables.

CREATE TABLE IF NOT EXISTS mi_sec_filings (
    accession VARCHAR(32) PRIMARY KEY,
    cik VARCHAR(10) NOT NULL,
    issuer_id VARCHAR(64),
    form VARCHAR(16) NOT NULL,
    filed_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ,
    available_at TIMESTAMPTZ,
    available_at_basis VARCHAR(32) NOT NULL DEFAULT 'ACCEPTANCE',
    period_of_report DATE,
    is_amendment BOOLEAN NOT NULL DEFAULT FALSE,
    amends_accession VARCHAR(32),
    source_url TEXT,
    retrieved_at TIMESTAMPTZ NOT NULL,
    ingestion_run_id VARCHAR(64),
    content_sha256 VARCHAR(64),
    dashboard_eligible BOOLEAN NOT NULL DEFAULT TRUE,
    research_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    export_scope VARCHAR(32) NOT NULL DEFAULT 'ATTRIBUTION_REQUIRED'
);

CREATE INDEX IF NOT EXISTS mi_sec_filings_cik_idx ON mi_sec_filings (cik, filed_at DESC);

CREATE TABLE IF NOT EXISTS mi_financial_facts (
    fact_id BIGSERIAL PRIMARY KEY,
    cik VARCHAR(10) NOT NULL,
    accession VARCHAR(32),
    taxonomy VARCHAR(32) NOT NULL,
    concept VARCHAR(128) NOT NULL,
    unit VARCHAR(32) NOT NULL,
    currency VARCHAR(8),
    period_start DATE,
    period_end DATE,
    instant_date DATE,
    duration_or_instant VARCHAR(16) NOT NULL,
    fiscal_year INTEGER,
    fiscal_period VARCHAR(8),
    value NUMERIC,
    dimensions_json JSONB,
    frame VARCHAR(64),
    original_or_amended VARCHAR(16) NOT NULL DEFAULT 'ORIGINAL',
    available_at TIMESTAMPTZ,
    available_at_basis VARCHAR(32) NOT NULL DEFAULT 'ACCEPTANCE',
    retrieved_at TIMESTAMPTZ NOT NULL,
    ingestion_run_id VARCHAR(64),
    payload_hash VARCHAR(64) NOT NULL
);

CREATE INDEX IF NOT EXISTS mi_financial_facts_concept_idx
    ON mi_financial_facts (cik, concept, period_end DESC);

CREATE TABLE IF NOT EXISTS mi_financial_metrics (
    metric_id VARCHAR(64) NOT NULL,
    cik VARCHAR(10) NOT NULL,
    as_of DATE NOT NULL,
    method_version VARCHAR(32) NOT NULL,
    value NUMERIC,
    units VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    inputs_json JSONB NOT NULL,
    missing_prerequisites TEXT,
    dashboard_eligible BOOLEAN NOT NULL DEFAULT TRUE,
    research_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    export_scope VARCHAR(32) NOT NULL DEFAULT 'ATTRIBUTION_REQUIRED',
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (metric_id, cik, as_of, method_version)
);

CREATE TABLE IF NOT EXISTS mi_corporate_events (
    event_id VARCHAR(64) PRIMARY KEY,
    cik VARCHAR(10),
    issuer_id VARCHAR(64),
    event_type VARCHAR(32) NOT NULL,
    occurrence_at TIMESTAMPTZ,
    disclosed_at TIMESTAMPTZ,
    importance VARCHAR(16),
    confidence VARCHAR(16) NOT NULL DEFAULT 'RULE',
    accession VARCHAR(32),
    source_url TEXT,
    summary TEXT,
    payload_json JSONB,
    dedupe_key VARCHAR(128),
    dashboard_eligible BOOLEAN NOT NULL DEFAULT TRUE,
    research_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    export_scope VARCHAR(32) NOT NULL DEFAULT 'ATTRIBUTION_REQUIRED',
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS mi_corporate_events_dedupe_uidx
    ON mi_corporate_events (dedupe_key)
    WHERE dedupe_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS mi_openfigi_cache (
    request_hash VARCHAR(64) PRIMARY KEY,
    id_type VARCHAR(32) NOT NULL,
    id_value VARCHAR(64) NOT NULL,
    exch_code VARCHAR(16),
    currency VARCHAR(8),
    market_sec_des VARCHAR(32),
    status VARCHAR(32) NOT NULL,
    figi VARCHAR(32),
    composite_figi VARCHAR(32),
    share_class_figi VARCHAR(32),
    ticker VARCHAR(32),
    name TEXT,
    security_type VARCHAR(64),
    catalog_version VARCHAR(32) NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    response_hash VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS mi_openfigi_candidates (
    request_hash VARCHAR(64) NOT NULL REFERENCES mi_openfigi_cache (request_hash),
    candidate_index INTEGER NOT NULL,
    figi VARCHAR(32),
    ticker VARCHAR(32),
    exch_code VARCHAR(16),
    security_type VARCHAR(64),
    name TEXT,
    payload_json JSONB NOT NULL,
    PRIMARY KEY (request_hash, candidate_index)
);

CREATE OR REPLACE VIEW mi_v_sec_filings_recent AS
SELECT accession, cik, form, filed_at, accepted_at, available_at, period_of_report,
       is_amendment, source_url, export_scope
FROM mi_sec_filings;

CREATE OR REPLACE VIEW mi_v_financial_metrics_latest AS
SELECT DISTINCT ON (cik, metric_id)
    metric_id, cik, as_of, method_version, value, units, status, missing_prerequisites,
    dashboard_eligible, research_eligible, export_scope
FROM mi_financial_metrics
ORDER BY cik, metric_id, as_of DESC, computed_at DESC;

CREATE OR REPLACE VIEW mi_v_corporate_events_recent AS
SELECT event_id, cik, event_type, occurrence_at, disclosed_at, importance, confidence,
       accession, source_url, summary, export_scope
FROM mi_corporate_events;

CREATE OR REPLACE VIEW mi_v_openfigi_resolution AS
SELECT request_hash, id_type, id_value, exch_code, currency, status, figi, ticker, security_type,
       catalog_version, retrieved_at, expires_at
FROM mi_openfigi_cache;
