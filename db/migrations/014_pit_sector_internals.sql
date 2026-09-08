-- PIT sector internals consumer: hash-verified sector_internals_v1 artifacts produced by the isolated
-- quant-strategies MarketIntelligenceResearch producer. Aggregates only (no constituents), revision
-- semantics per (decision_date, sector, method_version), date gate < effective holdout boundary.
-- Additive only. Plain SQL only: jobs/apply_migrations.py splits on lines ending in ';'.

CREATE TABLE IF NOT EXISTS mi_pit_sector_artifacts (
    artifact_sha256 VARCHAR(64) PRIMARY KEY,
    schema_version VARCHAR(32) NOT NULL,
    method_version VARCHAR(48) NOT NULL,
    producer TEXT,
    provenance VARCHAR(32) NOT NULL,
    research_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    contract_idea_id VARCHAR(64),
    contract_spec_hash VARCHAR(64),
    contract_sha256 VARCHAR(64),
    effective_holdout_start DATE NOT NULL,
    window_start DATE NOT NULL,
    window_end DATE NOT NULL,
    sessions INTEGER NOT NULL,
    row_count INTEGER NOT NULL,
    params_json JSONB NOT NULL,
    pit_json JSONB NOT NULL,
    lineage_json JSONB NOT NULL,
    coverage_json JSONB NOT NULL,
    definitions_json JSONB NOT NULL,
    units_json JSONB NOT NULL,
    generated_at TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingestion_run_id VARCHAR(64),
    source_ref TEXT
);

CREATE TABLE IF NOT EXISTS mi_pit_sector_internals (
    id BIGSERIAL PRIMARY KEY,
    decision_date DATE NOT NULL,
    sector VARCHAR(64) NOT NULL,
    method_version VARCHAR(48) NOT NULL,
    revision_seq INTEGER NOT NULL DEFAULT 1,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    artifact_sha256 VARCHAR(64) NOT NULL REFERENCES mi_pit_sector_artifacts (artifact_sha256),
    row_sha256 VARCHAR(64) NOT NULL,
    provenance VARCHAR(32) NOT NULL,
    research_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    return_sessions INTEGER NOT NULL,
    constituent_count INTEGER NOT NULL,
    priced_count INTEGER NOT NULL,
    pct_above_20d NUMERIC,
    pct_above_50d NUMERIC,
    pct_above_100d NUMERIC,
    pct_above_200d NUMERIC,
    median_return NUMERIC,
    ew_return NUMERIC,
    cw_return NUMERIC,
    cw_status VARCHAR(40),
    ew_minus_cw NUMERIC,
    dispersion NUMERIC,
    return_denominator INTEGER,
    held_ew_return NUMERIC,
    held_status VARCHAR(40),
    hhi_cap NUMERIC,
    top5_cap_share NUMERIC,
    concentration_status VARCHAR(40),
    row_json JSONB NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingestion_run_id VARCHAR(64)
);

CREATE UNIQUE INDEX IF NOT EXISTS mi_pit_sector_internals_revision_uidx
    ON mi_pit_sector_internals (decision_date, sector, method_version, revision_seq);

CREATE INDEX IF NOT EXISTS mi_pit_sector_internals_current_idx
    ON mi_pit_sector_internals (sector, decision_date DESC) WHERE is_current;

CREATE OR REPLACE VIEW mi_v_pit_sector_artifacts AS
SELECT
    artifact_sha256,
    schema_version,
    method_version,
    provenance,
    research_eligible,
    contract_idea_id,
    contract_spec_hash,
    effective_holdout_start,
    window_start,
    window_end,
    sessions,
    row_count,
    params_json,
    pit_json,
    coverage_json,
    units_json,
    generated_at,
    ingested_at,
    ingestion_run_id
FROM mi_pit_sector_artifacts;

CREATE OR REPLACE VIEW mi_v_pit_sector_internals_current AS
SELECT
    decision_date,
    sector,
    method_version,
    revision_seq,
    artifact_sha256,
    provenance,
    research_eligible,
    return_sessions,
    constituent_count,
    priced_count,
    pct_above_20d,
    pct_above_50d,
    pct_above_100d,
    pct_above_200d,
    median_return,
    ew_return,
    cw_return,
    cw_status,
    ew_minus_cw,
    dispersion,
    return_denominator,
    held_ew_return,
    held_status,
    hhi_cap,
    top5_cap_share,
    concentration_status,
    row_json,
    ingested_at
FROM mi_pit_sector_internals
WHERE is_current;

CREATE OR REPLACE VIEW mi_v_pit_sector_internals_latest AS
SELECT DISTINCT ON (sector, method_version)
    decision_date,
    sector,
    method_version,
    revision_seq,
    artifact_sha256,
    provenance,
    research_eligible,
    return_sessions,
    constituent_count,
    priced_count,
    pct_above_20d,
    pct_above_50d,
    pct_above_100d,
    pct_above_200d,
    median_return,
    ew_return,
    cw_return,
    cw_status,
    ew_minus_cw,
    dispersion,
    return_denominator,
    held_ew_return,
    held_status,
    hhi_cap,
    top5_cap_share,
    concentration_status,
    row_json,
    ingested_at
FROM mi_pit_sector_internals
WHERE is_current
ORDER BY sector, method_version, decision_date DESC;
