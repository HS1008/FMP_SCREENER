"""Persist official EIA v2 extracts into canonical tables."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text

from market_intelligence.eia_client import EIA_CATALOG, EIA_CATALOG_VERSION, EIA_SOURCE_ID, EIASeriesSpec
from market_intelligence.store import RUN_FAILED, RUN_SUCCEEDED, finish_run, new_run_id, record_freshness, start_run, utcnow


def _period_date(period: str) -> date | None:
    raw = str(period or "")
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    return None


def _decimal(value: Any) -> tuple[Decimal | None, str | None]:
    if value is None or value in {"", ".", "NA", "null", "None"}:
        return None, "missing_marker"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None, "non_numeric"
    if not number.is_finite():
        return None, "non_finite"
    return number, None


def ensure_series(conn, spec: EIASeriesSpec) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mi_eia_series (series_id, route, facets_json, frequency, units, geographic_scope, catalog_version, source_url, status)
            VALUES (:series_id, :route, CAST(:facets AS JSONB), :frequency, :units, :geo, :catalog, :url, :status)
            ON CONFLICT (series_id) DO UPDATE SET route = EXCLUDED.route, facets_json = EXCLUDED.facets_json, catalog_version = EXCLUDED.catalog_version, status = EXCLUDED.status
            """
        ),
        {
            "series_id": spec.series_id,
            "route": spec.route,
            "facets": __import__("json").dumps(spec.facets),
            "frequency": spec.frequency,
            "units": spec.units,
            "geo": spec.geographic_scope,
            "catalog": EIA_CATALOG_VERSION,
            "url": "https://api.eia.gov/v2/{0}/data/".format(spec.route),
            "status": spec.status,
        },
    )


def persist_extract(conn, spec: EIASeriesSpec, extract: dict[str, Any], *, run_id: str) -> dict[str, int]:
    ensure_series(conn, spec)
    inserted = 0
    missing = 0
    retrieved = utcnow()
    if not extract.get("complete"):
        conn.execute(
            text("UPDATE mi_eia_series SET status = 'PARTIAL' WHERE series_id = :sid"),
            {"sid": spec.series_id},
        )
        return {"inserted": 0, "missing": 0, "incomplete": 1}
    for row in extract.get("rows") or []:
        period = str(row.get("period") or "")
        if not period:
            continue
        value, reason = _decimal(row.get(spec.data_column, row.get("value")))
        if value is None:
            missing += 1
        payload_hash = str(extract.get("content_sha256") or "")
        conn.execute(
            text("UPDATE mi_eia_observations SET is_current = FALSE WHERE series_id = :sid AND period = :period AND is_current"),
            {"sid": spec.series_id, "period": period},
        )
        conn.execute(
            text(
                """
                INSERT INTO mi_eia_observations (
                    series_id, period, observation_date, value, units, revision_seq, is_current,
                    available_at, available_at_basis, retrieved_at, ingestion_run_id, payload_hash, missing_reason
                )
                VALUES (
                    :sid, :period, :obs_date, :value, :units,
                    COALESCE((SELECT MAX(revision_seq) FROM mi_eia_observations WHERE series_id = :sid AND period = :period), 0) + 1,
                    TRUE, :available_at, 'FIRST_SEEN', :retrieved_at, :run_id, :payload_hash, :missing_reason
                )
                """
            ),
            {
                "sid": spec.series_id,
                "period": period,
                "obs_date": _period_date(period),
                "value": value,
                "units": spec.units,
                "available_at": retrieved,
                "retrieved_at": retrieved,
                "run_id": run_id,
                "payload_hash": payload_hash,
                "missing_reason": reason,
            },
        )
        inserted += 1
    return {"inserted": inserted, "missing": missing, "incomplete": 0}


def ingest_eia_catalog(engine, client, *, parent_run_id: str | None = None, specs: tuple[EIASeriesSpec, ...] | None = None) -> dict[str, Any]:
    specs = specs or EIA_CATALOG
    report: dict[str, Any] = {"source_id": EIA_SOURCE_ID, "failed": False, "series": []}
    with engine.begin() as conn:
        run_id = start_run(conn, source_id=EIA_SOURCE_ID, dataset="eia_v2_catalog", parent_run_id=parent_run_id)
        try:
            for spec in specs:
                extract = client.fetch_pages(spec)
                counts = persist_extract(conn, spec, extract, run_id=run_id)
                latest = None
                if extract.get("rows"):
                    latest = _period_date(str(extract["rows"][-1].get("period") or ""))
                record_freshness(
                    conn,
                    source_id=EIA_SOURCE_ID,
                    dataset=spec.series_id,
                    cadence="W" if spec.frequency == "weekly" else "H",
                    transport_status="OK" if extract.get("complete") else "PARTIAL",
                    latest_observation=latest,
                    success=bool(extract.get("complete")),
                    error_redacted=None if extract.get("complete") else "incomplete_extract",
                    run_id=run_id,
                )
                report["series"].append({"series_id": spec.series_id, **counts, "complete": extract.get("complete")})
                if not extract.get("complete"):
                    report["failed"] = True
            finish_run(conn, run_id, status=RUN_FAILED if report["failed"] else RUN_SUCCEEDED, details={"catalog_version": EIA_CATALOG_VERSION})
        except Exception as exc:  # noqa: BLE001
            finish_run(conn, run_id, status=RUN_FAILED, error_redacted=exc.__class__.__name__)
            report["failed"] = True
            report["error"] = exc.__class__.__name__
    return report
