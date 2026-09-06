-- Additive delivery status for platform research. Does not change Stage 1/2 rows.
ALTER TABLE research_runs
    ADD COLUMN IF NOT EXISTS delivery_status VARCHAR(32);

CREATE INDEX IF NOT EXISTS research_runs_delivery_status_idx
    ON research_runs (delivery_status);
