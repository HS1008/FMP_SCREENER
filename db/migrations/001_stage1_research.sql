-- Stage 1 research schema. Idempotent. Does not drop existing backtest rows.

CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS backtests (
    id BIGSERIAL PRIMARY KEY,
    backtest_id VARCHAR(100) UNIQUE NOT NULL,
    strategy_id VARCHAR(100) NOT NULL,
    qc_project_id VARCHAR(100),
    name VARCHAR(255),
    status VARCHAR(50),
    created_at TIMESTAMPTZ,
    sharpe_ratio NUMERIC,
    sortino_ratio NUMERIC,
    alpha NUMERIC,
    beta NUMERIC,
    cagr NUMERIC,
    max_drawdown NUMERIC,
    net_profit NUMERIC,
    win_rate NUMERIC,
    loss_rate NUMERIC,
    trade_count INTEGER,
    psr NUMERIC,
    synced_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE backtests ADD COLUMN IF NOT EXISTS research_suite_version VARCHAR(32);
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS research_run_id VARCHAR(128);
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS research_experiment_id VARCHAR(128);
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS research_test_type VARCHAR(64);
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS research_phase VARCHAR(32);
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS research_window_id VARCHAR(64);
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS research_git_commit VARCHAR(80);
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS research_is_holdout BOOLEAN;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS research_dirty BOOLEAN;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS train_start DATE;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS train_end DATE;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS test_start DATE;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS test_end DATE;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS parameters_json JSONB;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS objective_name VARCHAR(64);
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS objective_value NUMERIC;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS raw_statistics_json JSONB;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS research_guide_json JSONB;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS backtest_start TIMESTAMPTZ;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS backtest_end TIMESTAMPTZ;
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS error_message TEXT;

CREATE INDEX IF NOT EXISTS idx_backtests_strategy_id ON backtests (strategy_id);
CREATE INDEX IF NOT EXISTS idx_backtests_research_run_id ON backtests (research_run_id);
CREATE INDEX IF NOT EXISTS idx_backtests_research_test_type ON backtests (research_test_type);
CREATE INDEX IF NOT EXISTS idx_backtests_research_git_commit ON backtests (research_git_commit);
CREATE INDEX IF NOT EXISTS idx_backtests_backtest_id ON backtests (backtest_id);

CREATE TABLE IF NOT EXISTS backtest_equity_points (
    id BIGSERIAL PRIMARY KEY,
    backtest_id VARCHAR(100) NOT NULL,
    strategy_id VARCHAR(100) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    equity NUMERIC,
    period_return NUMERIC NULL,
    series_name VARCHAR(64) NOT NULL DEFAULT 'Equity',
    UNIQUE (backtest_id, timestamp, series_name)
);

CREATE INDEX IF NOT EXISTS idx_backtest_equity_backtest_time
    ON backtest_equity_points (backtest_id, timestamp);

CREATE TABLE IF NOT EXISTS research_runs (
    research_run_id VARCHAR(128) PRIMARY KEY,
    strategy_id VARCHAR(100) NOT NULL,
    suite_version VARCHAR(32),
    git_commit VARCHAR(80),
    dirty BOOLEAN,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    holdout_accessed BOOLEAN NOT NULL DEFAULT FALSE,
    holdout_access_count INTEGER NOT NULL DEFAULT 0,
    config_json JSONB,
    metadata_json JSONB
);

CREATE INDEX IF NOT EXISTS idx_research_runs_strategy ON research_runs (strategy_id);
CREATE INDEX IF NOT EXISTS idx_research_runs_git_commit ON research_runs (git_commit);
