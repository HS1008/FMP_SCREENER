-- Curated, holdout-fail-closed research views for the read-only AI gateway.
-- Additive only. Does not change Stage 1 / Stage 2 methodology, does not touch research
-- tables, and does not unseal the 2025+ final holdout.
--
-- Numbering: the concurrent gateway PR used 020; 020-025 belong to the deploy-hardening
-- lineage and 026 to Treasury/equity EOD, so this ships as 027 (never renumber applied SQL).
--
-- Fail-closed principles (defense in depth with ai_gateway.services):
--   * a research run is exposed only when its row exists AND it never accessed the holdout;
--   * an experiment is exposed only when research_is_holdout is explicitly FALSE, its test
--     type is known and not a holdout type, and its test window END is known and before
--     2025-01-01 (NULL = unknown Stage 2 timing = not exportable);
--   * an artifact is exposed only when it is not a model payload AND it is bound to a
--     proven non-holdout experiment, or (run-scoped) every experiment of its run is a
--     proven non-holdout experiment (unknown lineage / zero experiments -> hidden);
--   * artifact payload_json is never exposed (metadata + sha only).

CREATE OR REPLACE VIEW mi_v_strategy_nonholdout_runs AS
SELECT
    r.research_run_id,
    r.strategy_id,
    r.research_lineage_id,
    r.research_kind,
    r.run_status,
    r.holdout_status,
    r.holdout_exposure_status,
    r.holdout_start,
    r.holdout_end,
    r.git_commit,
    r.first_seen_at,
    r.last_seen_at
FROM research_runs r
WHERE COALESCE(r.holdout_accessed, FALSE) = FALSE
  AND COALESCE(r.holdout_access_count, 0) = 0
  AND UPPER(COALESCE(r.holdout_status, '')) NOT IN ('ACCESSED', 'OPEN', 'UNSEALED', 'RUNNING')
  AND UPPER(COALESCE(r.holdout_exposure_status, '')) NOT IN ('ACCESSED_ONCE', 'REPEATEDLY_ACCESSED')
  AND UPPER(COALESCE(r.research_run_id, '')) NOT LIKE '%HOLDOUT%';

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
    b.research_is_holdout,
    b.created_at,
    b.synced_at,
    r.research_kind,
    r.holdout_status AS run_holdout_status,
    r.holdout_exposure_status AS run_holdout_exposure_status
FROM backtests b
JOIN mi_v_strategy_nonholdout_runs r ON r.research_run_id = b.research_run_id
WHERE b.research_is_holdout IS FALSE
  AND b.research_test_type IS NOT NULL
  AND UPPER(b.research_test_type) NOT LIKE '%HOLDOUT%'
  AND UPPER(b.research_test_type) NOT LIKE 'POST_HOLDOUT%'
  AND UPPER(COALESCE(b.research_phase, '')) NOT LIKE '%HOLDOUT%'
  AND UPPER(COALESCE(b.name, '')) NOT LIKE '%HOLDOUT%'
  AND UPPER(COALESCE(b.research_experiment_id, '')) NOT LIKE '%HOLDOUT%'
  AND b.test_end IS NOT NULL
  AND b.test_end < DATE '2025-01-01'
  AND (b.test_start IS NULL OR b.test_start < DATE '2025-01-01')
  AND (b.backtest_end IS NULL OR b.backtest_end < TIMESTAMPTZ '2025-01-01 00:00:00+00');

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
JOIN mi_v_strategy_nonholdout_runs r ON r.research_run_id = w.research_run_id
WHERE w.oos_end IS NOT NULL
  AND w.oos_end < DATE '2025-01-01'
  AND w.oos_start IS NOT NULL
  AND w.oos_start < DATE '2025-01-01'
  AND UPPER(COALESCE(w.outer_window_id, '')) NOT LIKE '%HOLDOUT%';

CREATE OR REPLACE VIEW mi_v_strategy_artifact_status AS
WITH run_counts AS (
    SELECT
        b.research_run_id,
        COUNT(*) AS run_experiment_count,
        COUNT(e.backtest_id) AS run_visible_experiment_count
    FROM backtests b
    LEFT JOIN mi_v_strategy_experiments e
           ON e.research_run_id = b.research_run_id
          AND e.backtest_id = b.backtest_id
    WHERE b.research_run_id IS NOT NULL
    GROUP BY b.research_run_id
)
SELECT
    r.strategy_id,
    a.research_run_id,
    a.research_experiment_id,
    a.artifact_type,
    a.sha256 AS artifact_sha256,
    a.transport,
    a.created_at,
    a.synced_at,
    (a.sha256 IS NOT NULL AND char_length(a.sha256) = 64) AS artifact_valid,
    CASE
        WHEN a.research_experiment_id IS NOT NULL THEN 'NONHOLDOUT_EXPERIMENT_BOUND'
        ELSE 'NONHOLDOUT_RUN_ALL_EXPERIMENTS_PROVEN'
    END AS lineage_status,
    rc.run_experiment_count,
    rc.run_visible_experiment_count
FROM research_artifacts a
JOIN mi_v_strategy_nonholdout_runs r ON r.research_run_id = a.research_run_id
JOIN run_counts rc ON rc.research_run_id = a.research_run_id
WHERE a.artifact_type IS NOT NULL
  AND LOWER(a.artifact_type) NOT LIKE '%model%'
  AND LOWER(a.artifact_type) NOT LIKE '%binary%'
  AND LOWER(a.artifact_type) NOT LIKE '%pickle%'
  AND LOWER(a.artifact_type) NOT LIKE '%object_store%'
  AND UPPER(a.artifact_type) NOT LIKE '%HOLDOUT%'
  AND LOWER(COALESCE(a.logical_path, '')) NOT LIKE '%.pkl%'
  AND LOWER(COALESCE(a.logical_path, '')) NOT LIKE '%.joblib%'
  AND LOWER(COALESCE(a.logical_path, '')) NOT LIKE '%.pt'
  AND LOWER(COALESCE(a.logical_path, '')) NOT LIKE '%.onnx%'
  AND LOWER(COALESCE(a.logical_path, '')) NOT LIKE '%.bin'
  AND LOWER(COALESCE(a.logical_path, '')) NOT LIKE '%model%'
  AND UPPER(COALESCE(a.logical_path, '')) NOT LIKE '%HOLDOUT%'
  AND LOWER(COALESCE(a.artifact_key, '')) NOT LIKE '%.pkl%'
  AND LOWER(COALESCE(a.artifact_key, '')) NOT LIKE '%.joblib%'
  AND LOWER(COALESCE(a.artifact_key, '')) NOT LIKE '%model%'
  AND UPPER(COALESCE(a.artifact_key, '')) NOT LIKE '%HOLDOUT%'
  AND LOWER(COALESCE(a.transport, '')) NOT LIKE '%binary%'
  AND (
        (
            a.research_experiment_id IS NOT NULL
            AND EXISTS (
                SELECT 1
                FROM mi_v_strategy_experiments e
                WHERE e.research_run_id = a.research_run_id
                  AND e.research_experiment_id = a.research_experiment_id
            )
        )
        OR (
            a.research_experiment_id IS NULL
            AND rc.run_experiment_count > 0
            AND rc.run_visible_experiment_count = rc.run_experiment_count
        )
  );
