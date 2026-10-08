-- Market Overview EOD snapshot prepared outside Streamlit.
-- jobs.publish_overview_snapshot composes the template-shaped snapshot from stored
-- reads (the same overview_snapshot() function the page used to run per request) and
-- publishes it here. Streamlit reads the newest published row through the view and
-- overlays only the cheap stored-quote 1D on top; the page and the Excel export use
-- that same composed snapshot. Nothing in the payload is fabricated; a missing value
-- stays null. Older rows are kept for audit and pruned by the job.

CREATE TABLE IF NOT EXISTS mi_overview_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    snapshot_version  TEXT NOT NULL,
    reference_date    DATE NOT NULL,
    generated_at      TIMESTAMPTZ NOT NULL,
    published_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rows_total        INTEGER NOT NULL,
    rows_missing      INTEGER NOT NULL,
    rows_stale        INTEGER NOT NULL,
    read_errors       JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload           JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_mi_overview_snapshots_published
    ON mi_overview_snapshots (published_at DESC);

COMMENT ON TABLE mi_overview_snapshots IS
    'Market Overview template snapshots published by jobs.publish_overview_snapshot. payload is the full overview_snapshot() dict; Streamlit never writes here.';

CREATE OR REPLACE VIEW mi_v_overview_snapshot_latest AS
SELECT
    s.snapshot_id,
    s.snapshot_version,
    s.reference_date,
    s.generated_at,
    s.published_at,
    s.rows_total,
    s.rows_missing,
    s.rows_stale,
    s.read_errors,
    s.payload
FROM mi_overview_snapshots s
ORDER BY s.published_at DESC, s.generated_at DESC
LIMIT 1;

COMMENT ON VIEW mi_v_overview_snapshot_latest IS
    'Newest published Market Overview snapshot. One row. Read by the Streamlit Market Overview page; the per-request fallback is the live overview_snapshot() read when this is empty.';

-- Cross-asset daily bars now carry bar_quality (PROVISIONAL while the instrument's
-- own session is still open, COMPLETE afterwards) so completed close-to-close
-- horizons can skip the in-progress bar. Column appended; grants preserved.
CREATE OR REPLACE VIEW mi_v_yahoo_cross_asset_history AS
SELECT
    b.instrument_id,
    b.source_id,
    b.bar_date,
    b.close_price,
    b.adj_close_price,
    b.provider_symbol,
    b.provider,
    b.retrieved_at,
    b.last_seen_at,
    b.bar_quality
FROM mi_market_bars b
WHERE b.bar_interval = '1D'
  AND b.provider = 'YAHOO'
  AND b.source_id IN ('YAHOO_FX', 'YAHOO_FUTURES_PROXY', 'YAHOO_CRYPTO');

COMMENT ON VIEW mi_v_yahoo_cross_asset_history IS
    'Yahoo daily history for FX, futures proxies, crypto, and the MOVE index. Provider must be YAHOO. Equity and market-monitor bars are excluded. retrieved_at is the collection time of the close, not a quote time. bar_quality is PROVISIONAL for a bar whose session was still open when collected (NULL on rows written before migration 049).';
