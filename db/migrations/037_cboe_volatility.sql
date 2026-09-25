-- Cboe volatility read models. Metrics live in mi_metric_snapshots (category CBOE_VOL).
-- Additive. Does not edit applied SQL. Does not touch research or holdout tables.

CREATE OR REPLACE VIEW mi_v_cboe_vol_latest AS
SELECT DISTINCT ON (metric_id)
    metric_id,
    as_of,
    value,
    units,
    transform_version,
    status,
    detail_json,
    computed_at AS ingested_at,
    inputs_retrieved_max AS provider_observation_ts,
    ingestion_run_id
FROM mi_metric_snapshots
WHERE category = 'CBOE_VOL'
  AND status IS DISTINCT FROM 'WITHDRAWN_OBSERVATION'
ORDER BY metric_id, as_of DESC, computed_at DESC;

CREATE OR REPLACE VIEW mi_v_cboe_vol_history AS
SELECT
    metric_id,
    as_of,
    value,
    units,
    transform_version,
    status,
    detail_json,
    computed_at AS ingested_at,
    inputs_retrieved_max AS provider_observation_ts
FROM mi_metric_snapshots
WHERE category = 'CBOE_VOL'
  AND status = 'OK'
  AND value IS NOT NULL
ORDER BY metric_id, as_of;
