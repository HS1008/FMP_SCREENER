-- Curated, holdout-filtered research views for the read-only AI gateway.
-- Additive only. Does not change Stage 1/Stage 2 methodology or unseal 2025+ holdout.

CREATE OR REPLACE VIEW mi_v_strategy_experiments AS
SELECT
    b.strategy_id,
    b.research_run_id,
    b.research_lineage_id,
    b.research_experiment_id,
    b.research_test_type,
    b.research_phase,
    b.research_window_id,
    b.backtest_id,
    b.name,
    b.status,
    b.sharpe_ratio,
    b.sortino_ratio,
    b.cagr,
    b.max_drawdown,
    b.alpha,
    b.trade_count,
    b.test_start,
    b.test_end,
    b.created_at,
    b.synced_at
FROM backtests b
WHERE COALESCE(b.research_is_holdout, FALSE) = FALSE
  AND COALESCE(b.research_test_type, '') NOT IN (
        'ML_FINAL_HOLDOUT',
        'FINAL_HOLDOUT',
        'POST_HOLDOUT_ML_TRAIN',
        'POST_HOLDOUT_ML_OOS',
        'POST_HOLDOUT_WFO_TRAIN',
        'POST_HOLDOUT_WFO_TEST'
      )
  AND COALESCE(b.research_phase, '') <> 'HOLDOUT'
  AND (b.test_end IS NULL OR b.test_end < DATE '2025-01-01');

CREATE OR REPLACE VIEW mi_v_strategy_oos_windows AS
SELECT
    r.strategy_id,
    r.research_kind,
    r.research_lineage_id,
    w.research_run_id,
    w.outer_window_id,
    w.oos_start,
    w.oos_end,
    w.metrics_json
FROM research_oos_windows w
JOIN research_runs r ON r.research_run_id = w.research_run_id
WHERE (w.oos_end IS NULL OR w.oos_end < DATE '2025-01-01');

CREATE OR REPLACE VIEW mi_v_strategy_artifact_status AS
SELECT
    r.strategy_id,
    a.research_run_id,
    a.research_experiment_id,
    a.artifact_type,
    a.sha256 AS artifact_sha256,
    a.transport,
    a.logical_path,
    a.created_at,
    a.synced_at,
    CASE WHEN a.sha256 IS NOT NULL AND char_length(a.sha256) = 64 THEN TRUE ELSE FALSE END AS artifact_valid
FROM research_artifacts a
LEFT JOIN research_runs r ON r.research_run_id = a.research_run_id
WHERE COALESCE(a.artifact_type, '') NOT IN ('model', 'model.pkl', 'model_binary')
  AND COALESCE(a.logical_path, '') NOT ILIKE '%model.pkl%';
