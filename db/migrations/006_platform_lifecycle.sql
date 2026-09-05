-- Additive research vs promotion vs holdout fields. Does not change economic numbers.

ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS promotion_gate VARCHAR(64);
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS holdout_status VARCHAR(32);
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS economic_gate VARCHAR(32);

CREATE INDEX IF NOT EXISTS idx_research_runs_promotion_gate
    ON research_runs (promotion_gate);
