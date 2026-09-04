-- Additive platform research schema. Does not drop or rename Stage 1 / Stage 2 objects.

ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS research_mode VARCHAR(32);
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS asset_class VARCHAR(64);
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS strategy_family_id VARCHAR(128);
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS strategy_spec_hash VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_research_runs_mode_family
    ON research_runs (research_mode, strategy_family_id);

CREATE TABLE IF NOT EXISTS strategy_specs (
    strategy_spec_hash VARCHAR(64) PRIMARY KEY,
    strategy_id VARCHAR(128) NOT NULL,
    strategy_family_id VARCHAR(128),
    research_lineage_id VARCHAR(128),
    research_mode VARCHAR(32),
    asset_class VARCHAR(64),
    spec_json JSONB NOT NULL,
    git_sha VARCHAR(80),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS research_experiments (
    id BIGSERIAL PRIMARY KEY,
    research_run_id VARCHAR(128) NOT NULL,
    experiment_id VARCHAR(128) NOT NULL,
    research_mode VARCHAR(32),
    window_id VARCHAR(64),
    status VARCHAR(32),
    metadata_json JSONB,
    UNIQUE (research_run_id, experiment_id)
);

CREATE INDEX IF NOT EXISTS idx_research_experiments_run
    ON research_experiments (research_run_id);

CREATE TABLE IF NOT EXISTS research_trials (
    id BIGSERIAL PRIMARY KEY,
    research_run_id VARCHAR(128) NOT NULL,
    trial_id VARCHAR(128) NOT NULL,
    model_family VARCHAR(64),
    selected BOOLEAN NOT NULL DEFAULT FALSE,
    rejected BOOLEAN NOT NULL DEFAULT FALSE,
    inner_score NUMERIC,
    trial_json JSONB,
    UNIQUE (research_run_id, trial_id)
);

CREATE INDEX IF NOT EXISTS idx_research_trials_run
    ON research_trials (research_run_id);

CREATE TABLE IF NOT EXISTS research_oos_windows (
    id BIGSERIAL PRIMARY KEY,
    research_run_id VARCHAR(128) NOT NULL,
    outer_window_id VARCHAR(64) NOT NULL,
    oos_start DATE,
    oos_end DATE,
    metrics_json JSONB,
    UNIQUE (research_run_id, outer_window_id)
);

CREATE TABLE IF NOT EXISTS research_risk_metrics (
    id BIGSERIAL PRIMARY KEY,
    research_run_id VARCHAR(128) NOT NULL,
    metric_name VARCHAR(128) NOT NULL,
    metric_value NUMERIC,
    available BOOLEAN NOT NULL DEFAULT TRUE,
    source TEXT,
    UNIQUE (research_run_id, metric_name)
);

CREATE TABLE IF NOT EXISTS research_pair_diagnostics (
    id BIGSERIAL PRIMARY KEY,
    research_run_id VARCHAR(128) NOT NULL,
    pair_left VARCHAR(32) NOT NULL,
    pair_right VARCHAR(32) NOT NULL,
    hedge_ratio_method VARCHAR(64),
    hedge_ratio NUMERIC,
    correlation NUMERIC,
    half_life NUMERIC,
    selection_used_oos BOOLEAN NOT NULL DEFAULT FALSE,
    diagnostics_json JSONB,
    UNIQUE (research_run_id, pair_left, pair_right)
);

CREATE TABLE IF NOT EXISTS research_fixed_income_metrics (
    id BIGSERIAL PRIMARY KEY,
    research_run_id VARCHAR(128) NOT NULL,
    metric_name VARCHAR(128) NOT NULL,
    metric_value NUMERIC,
    available BOOLEAN NOT NULL DEFAULT FALSE,
    source TEXT,
    UNIQUE (research_run_id, metric_name)
);
