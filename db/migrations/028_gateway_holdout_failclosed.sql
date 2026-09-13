-- Fail-closed holdout proof for AI gateway research views.
-- Additive. Does not edit applied 027 SQL. Does not touch Stage 1 / Stage 2 methodology.
--
-- NULL / unknown run-level holdout access is hidden. COALESCE-to-safe is refused:
--   holdout_accessed NULL -> hidden
--   holdout_status NULL / unknown -> hidden
--   holdout_exposure_status NULL / unknown (non-stage1) -> hidden
-- Stage 1 may use EXPOSED_PRIOR_TO_STAGE1; Stage 2 / unknown kinds require LOCKED + PRISTINE.

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
WHERE r.holdout_accessed IS FALSE
  AND r.holdout_access_count IS NOT NULL
  AND r.holdout_access_count = 0
  AND r.holdout_status IS NOT NULL
  AND UPPER(COALESCE(r.research_run_id, '')) NOT LIKE '%HOLDOUT%'
  AND (
        (
            LOWER(COALESCE(r.research_kind, '')) = 'stage1'
            AND UPPER(r.holdout_status) IN ('LOCKED', 'EXPOSED_PRIOR_TO_STAGE1')
        )
        OR (
            LOWER(COALESCE(r.research_kind, '')) IS DISTINCT FROM 'stage1'
            AND UPPER(r.holdout_status) = 'LOCKED'
            AND r.holdout_exposure_status IS NOT NULL
            AND UPPER(r.holdout_exposure_status) IN ('PRISTINE', 'NEVER_ACCESSED')
        )
      );
