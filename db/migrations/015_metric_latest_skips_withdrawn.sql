-- Additive: latest metric/credit views skip withdrawn (NULL-revised) derived rows so
-- readers see the last valid publication. History views keep the withdrawn rows.
-- Raw observation revisions are unchanged. Morning snapshot bodies are not rewritten.

CREATE OR REPLACE VIEW mi_v_metric_latest AS
SELECT DISTINCT ON (m.metric_id)
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
FROM mi_metric_snapshots m
LEFT JOIN mi_macro_series s ON s.series_id = m.series_id
WHERE m.status IS DISTINCT FROM 'WITHDRAWN_OBSERVATION'
ORDER BY m.metric_id, m.as_of DESC, m.computed_at DESC;

CREATE OR REPLACE VIEW mi_v_credit_latest AS
SELECT DISTINCT ON (series_id)
    series_id,
    bucket,
    as_of,
    oas_bps,
    change_1d_bps,
    change_1w_bps,
    change_1m_bps,
    change_3m_bps,
    percentile_window,
    percentile,
    zscore,
    window_observations,
    history_first_date,
    history_status,
    units,
    transform_version,
    export_scope,
    source_refs,
    detail_json,
    computed_at
FROM mi_credit_index_snapshots
WHERE history_status IS DISTINCT FROM 'WITHDRAWN_OBSERVATION'
ORDER BY series_id, as_of DESC, computed_at DESC;
