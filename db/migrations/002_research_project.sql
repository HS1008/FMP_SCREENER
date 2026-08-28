-- Dedicated QuantConnect research project fields.
-- Idempotent. Does not change execution/paper qc_project_id or qc_deployment_id.
-- Does not delete backtests or holdout_exposures.

ALTER TABLE strategies
    ADD COLUMN IF NOT EXISTS qc_research_project_id VARCHAR(100);

ALTER TABLE strategies
    ADD COLUMN IF NOT EXISTS qc_research_project_name VARCHAR(255);

UPDATE strategies
SET qc_research_project_name = 'SPYTrendResearch'
WHERE strategy_id = 'SPYTrend'
  AND (qc_research_project_name IS NULL OR qc_research_project_name = '');
