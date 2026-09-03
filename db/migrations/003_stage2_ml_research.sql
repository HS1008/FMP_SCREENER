-- Stage 2 ML research schema. Idempotent. Does not drop or rename Stage 1 objects.

ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS research_kind VARCHAR(32);
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS artifact_schema_version VARCHAR(32);
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS feature_set_id VARCHAR(128);
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS feature_set_hash VARCHAR(64);
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS target_id VARCHAR(128);
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS target_hash VARCHAR(64);
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS planned_internal_trials INTEGER;
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS completed_internal_trials INTEGER;
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS planned_cv_fits INTEGER;
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS completed_cv_fits INTEGER;

CREATE INDEX IF NOT EXISTS idx_research_runs_kind
    ON research_runs (research_kind);

ALTER TABLE backtests ADD COLUMN IF NOT EXISTS model_id VARCHAR(128);
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS model_family VARCHAR(64);
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS feature_set_id VARCHAR(128);
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS target_id VARCHAR(128);
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS signal_rank_ic NUMERIC;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS signal_positive_ic_fraction NUMERIC;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS annual_turnover NUMERIC;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS base_slippage_bps NUMERIC;

CREATE INDEX IF NOT EXISTS idx_backtests_model_id ON backtests (model_id);
CREATE INDEX IF NOT EXISTS idx_backtests_feature_set ON backtests (feature_set_id);

CREATE TABLE IF NOT EXISTS ml_trials (
    id BIGSERIAL PRIMARY KEY,
    research_run_id VARCHAR(128) NOT NULL,
    outer_window_id VARCHAR(64) NOT NULL,
    trial_id VARCHAR(128) NOT NULL,
    model_family VARCHAR(64),
    hyperparameters_json JSONB,
    median_rank_ic NUMERIC,
    mean_rank_ic NUMERIC,
    icir NUMERIC,
    positive_ic_fraction NUMERIC,
    worst_fold_ic NUMERIC,
    fold_metrics_json JSONB,
    selected BOOLEAN NOT NULL DEFAULT FALSE,
    robustness_label VARCHAR(64),
    status VARCHAR(32),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (research_run_id, outer_window_id, trial_id)
);

CREATE INDEX IF NOT EXISTS idx_ml_trials_run_window
    ON ml_trials (research_run_id, outer_window_id);

CREATE TABLE IF NOT EXISTS ml_models (
    model_id VARCHAR(128) PRIMARY KEY,
    research_run_id VARCHAR(128) NOT NULL,
    outer_window_id VARCHAR(64),
    model_family VARCHAR(64),
    hyperparameters_json JSONB,
    feature_set_id VARCHAR(128),
    feature_set_hash VARCHAR(64),
    target_id VARCHAR(128),
    target_hash VARCHAR(64),
    train_start DATE,
    train_end DATE,
    object_store_key TEXT,
    model_sha256 VARCHAR(64),
    metadata_json JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ml_models_run
    ON ml_models (research_run_id, outer_window_id);

CREATE TABLE IF NOT EXISTS ml_feature_diagnostics (
    id BIGSERIAL PRIMARY KEY,
    research_run_id VARCHAR(128) NOT NULL,
    outer_window_id VARCHAR(64) NOT NULL,
    feature_name VARCHAR(128) NOT NULL,
    ridge_coefficient NUMERIC,
    coefficient_rank INTEGER,
    mean_univariate_rank_ic NUMERIC,
    median_univariate_rank_ic NUMERIC,
    positive_ic_fraction NUMERIC,
    missing_fraction NUMERIC,
    metadata_json JSONB,
    UNIQUE (research_run_id, outer_window_id, feature_name)
);

CREATE INDEX IF NOT EXISTS idx_ml_feature_diagnostics_run
    ON ml_feature_diagnostics (research_run_id, outer_window_id);

CREATE TABLE IF NOT EXISTS ml_signal_points (
    id BIGSERIAL PRIMARY KEY,
    backtest_id VARCHAR(100) NOT NULL,
    research_run_id VARCHAR(128) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    scope VARCHAR(64) NOT NULL DEFAULT 'month',
    rank_ic NUMERIC,
    n_names INTEGER,
    turnover NUMERIC,
    gross_return NUMERIC,
    net_return NUMERIC,
    stress_10bps_return NUMERIC,
    stress_20bps_return NUMERIC,
    UNIQUE (backtest_id, timestamp, scope)
);

CREATE INDEX IF NOT EXISTS idx_ml_signal_points_run
    ON ml_signal_points (research_run_id, timestamp);

CREATE TABLE IF NOT EXISTS research_artifacts (
    artifact_key TEXT PRIMARY KEY,
    research_run_id VARCHAR(128),
    research_experiment_id VARCHAR(128),
    artifact_type VARCHAR(64),
    sha256 VARCHAR(64),
    payload_json JSONB,
    created_at TIMESTAMPTZ,
    synced_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_research_artifacts_run
    ON research_artifacts (research_run_id, artifact_type);
