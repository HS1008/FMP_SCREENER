-- Latest metrics must not sort the whole snapshot table.
-- DISTINCT ON (metric_id) planned a parallel sequential scan of mi_metric_snapshots
-- (about 1.2 million rows) and exceeded the 15s read-only statement timeout, so
-- Rates & Curve stopped before its title. One indexed lookup per metric_id stays
-- under that limit. Withdrawn rows are still skipped. History views are unchanged.
-- Column order matches 015_metric_latest_skips_withdrawn.sql. Grants stay in place.

CREATE OR REPLACE VIEW mi_v_metric_latest AS
WITH RECURSIVE metric_ids AS (
    (
        SELECT metric_id
        FROM mi_metric_snapshots
        WHERE status IS DISTINCT FROM 'WITHDRAWN_OBSERVATION'
        ORDER BY metric_id
        LIMIT 1
    )
    UNION ALL
    SELECT next_id.metric_id
    FROM metric_ids
    JOIN LATERAL (
        SELECT m.metric_id
        FROM mi_metric_snapshots m
        WHERE m.metric_id > metric_ids.metric_id
          AND m.status IS DISTINCT FROM 'WITHDRAWN_OBSERVATION'
        ORDER BY m.metric_id
        LIMIT 1
    ) next_id ON TRUE
)
SELECT
    m.metric_id,
    m.series_id,
    m.category,
    m.as_of,
    m.value,
    m.units,
    m.transform_version,
    m.status,
    m.detail_json,
    m.computed_at,
    s.export_scope,
    m.inputs_retrieved_max,
    s.publication_status
FROM metric_ids ids
JOIN LATERAL (
    SELECT
        snap.metric_id,
        snap.series_id,
        snap.category,
        snap.as_of,
        snap.value,
        snap.units,
        snap.transform_version,
        snap.status,
        snap.detail_json,
        snap.computed_at,
        snap.inputs_retrieved_max
    FROM mi_metric_snapshots snap
    WHERE snap.metric_id = ids.metric_id
      AND snap.status IS DISTINCT FROM 'WITHDRAWN_OBSERVATION'
    ORDER BY snap.as_of DESC, snap.computed_at DESC
    LIMIT 1
) m ON TRUE
LEFT JOIN mi_macro_series s ON s.series_id = m.series_id;

COMMENT ON VIEW mi_v_metric_latest IS
    'Newest non-withdrawn metric snapshot per metric_id. Uses one index lookup per metric so a full history does not exceed the read-only statement timeout.';
