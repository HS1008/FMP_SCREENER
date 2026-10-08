"""Publish the Market Overview EOD snapshot from a server job.

The snapshot is the same ``overview_snapshot()`` dict the page used to compose on
every request. A job builds it with the writer connection and stores it in
``mi_overview_snapshots``; Streamlit reads the newest row through
``mi_v_overview_snapshot_latest`` and only overlays the cheap stored-quote 1D.
Streamlit never imports this module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from market_intelligence.nulls import strict_dumps
from market_intelligence.overview_snapshot import overview_snapshot

logger = logging.getLogger("market_intelligence.overview_publish")
KEEP_SNAPSHOTS = 48


def publish_overview_snapshot(conn, *, now: datetime | None = None, keep: int = KEEP_SNAPSHOTS) -> dict[str, Any]:
    """Compose and store one snapshot. An identical snapshot refreshes ``published_at`` only."""
    moment = now or datetime.now(timezone.utc)
    snapshot = overview_snapshot(conn, today=moment.astimezone(timezone.utc).date())
    payload = strict_dumps(snapshot)
    conn.execute(
        text(
            """
            INSERT INTO mi_overview_snapshots (
                snapshot_id, snapshot_version, reference_date, generated_at, published_at,
                rows_total, rows_missing, rows_stale, read_errors, payload
            ) VALUES (
                :snapshot_id, :snapshot_version, :reference_date, :generated_at, :published_at,
                :rows_total, :rows_missing, :rows_stale, CAST(:read_errors AS jsonb), CAST(:payload AS jsonb)
            )
            ON CONFLICT (snapshot_id) DO UPDATE SET
                published_at = EXCLUDED.published_at,
                generated_at = EXCLUDED.generated_at,
                read_errors = EXCLUDED.read_errors
            """
        ),
        {
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_version": str(snapshot.get("snapshot_version") or ""),
            "reference_date": snapshot["reference_date"],
            "generated_at": snapshot["generated_at"],
            "published_at": moment,
            "rows_total": int(snapshot.get("rows_total") or 0),
            "rows_missing": int(snapshot.get("rows_missing") or 0),
            "rows_stale": int(snapshot.get("rows_stale") or 0),
            "read_errors": strict_dumps(snapshot.get("read_errors") or {}),
            "payload": payload,
        },
    )
    if keep > 0:
        conn.execute(
            text(
                """
                DELETE FROM mi_overview_snapshots
                WHERE snapshot_id IN (
                    SELECT snapshot_id FROM mi_overview_snapshots
                    ORDER BY published_at DESC, generated_at DESC
                    OFFSET :keep
                )
                """
            ),
            {"keep": int(keep)},
        )
    logger.info(
        "overview snapshot published id=%s rows=%s missing=%s stale=%s errors=%s",
        snapshot["snapshot_id"],
        snapshot.get("rows_total"),
        snapshot.get("rows_missing"),
        snapshot.get("rows_stale"),
        ",".join(sorted((snapshot.get("read_errors") or {}).keys())) or "-",
    )
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "rows_total": snapshot.get("rows_total"),
        "rows_missing": snapshot.get("rows_missing"),
        "rows_stale": snapshot.get("rows_stale"),
        "read_errors": snapshot.get("read_errors") or {},
        "payload_bytes": len(payload),
    }


__all__ = ["KEEP_SNAPSHOTS", "publish_overview_snapshot"]
