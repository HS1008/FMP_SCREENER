"""FRED catalog ingestion: metadata validation, bounded backfill, incremental refresh, revisions.

Revision-reconciliation policy (``REVISION_LOOKBACK``): an incremental refresh re-requests
a trailing window per cadence so recent revisions are captured; it does *not* claim to
capture arbitrary historical revisions. ``mode="full"`` re-requests the full bounded
backfill window (periodic revalidation). Every returned observation is compared with the
current stored value; differences create an auditable new revision row.

Each series is written in its own transaction. A failed series never rolls back another
series and never erases previously stored observations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

from market_intelligence.catalog import CATALOG, CATALOG_BY_ID, CATALOG_VERSION, FRED_SOURCE_ID, SeriesSpec, publishable, validate_metadata
from market_intelligence.fred_client import FredClient, FredError, redact
from market_intelligence.store import (
    RUN_FAILED,
    RUN_SUCCEEDED,
    TRANSPORT_FAILED,
    TRANSPORT_OK,
    ObservationInput,
    finish_run,
    latest_observation_date,
    quarantine_observations,
    record_freshness,
    start_run,
    upsert_macro_series,
    upsert_observations,
    utcnow,
)

logger = logging.getLogger(__name__)

FRED_DATASET = "fred_series_observations"
RUN_QUARANTINED = "QUARANTINED"
TRANSPORT_METADATA_REJECTED = "METADATA_REJECTED"
TRANSPORT_PARTIAL = "PARTIAL"

REVISION_LOOKBACK = {
    "D": timedelta(days=45),
    "W": timedelta(days=120),
    "BW": timedelta(days=120),
    "M": timedelta(days=15 * 31),
    "Q": timedelta(days=3 * 366),
}


@dataclass
class SeriesIngestResult:
    series_id: str
    status: str
    counts: dict[str, int] = field(default_factory=dict)
    latest_observation: date | None = None
    first_observation: date | None = None
    metadata_status: str = "UNVALIDATED"
    freshness_status: str = "UNKNOWN"
    error: str | None = None
    run_id: str | None = None
    request_window: tuple[date | None, date | None] = (None, None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "series_id": self.series_id,
            "status": self.status,
            "counts": dict(self.counts),
            "latest_observation": self.latest_observation.isoformat() if self.latest_observation else None,
            "first_observation": self.first_observation.isoformat() if self.first_observation else None,
            "metadata_status": self.metadata_status,
            "freshness_status": self.freshness_status,
            "error": self.error,
            "run_id": self.run_id,
            "request_window": [d.isoformat() if d else None for d in self.request_window],
        }


@dataclass
class FredIngestReport:
    parent_run_id: str | None
    mode: str
    results: list[SeriesIngestResult] = field(default_factory=list)
    transport_status: str | None = None

    @property
    def failed(self) -> list[SeriesIngestResult]:
        return [r for r in self.results if r.status in (RUN_FAILED, RUN_QUARANTINED)]

    @property
    def quarantined(self) -> list[SeriesIngestResult]:
        return [r for r in self.results if r.status == RUN_QUARANTINED]

    @property
    def succeeded(self) -> list[SeriesIngestResult]:
        return [r for r in self.results if r.status == RUN_SUCCEEDED]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": FRED_SOURCE_ID,
            "parent_run_id": self.parent_run_id,
            "mode": self.mode,
            "catalog_version": CATALOG_VERSION,
            "series_total": len(self.results),
            "series_succeeded": len(self.succeeded),
            "series_failed": len(self.failed),
            "series_quarantined_metadata": [r.series_id for r in self.quarantined],
            "transport_status": self.transport_status,
            "results": [r.as_dict() for r in self.results],
        }


def request_window(spec: SeriesSpec, *, mode: str, today: date, latest_stored: date | None) -> tuple[date, date]:
    backfill_start = date(today.year - spec.backfill_years, today.month, 1)
    if mode == "full" or latest_stored is None:
        return backfill_start, today
    lookback = REVISION_LOOKBACK.get(spec.expected_frequency, timedelta(days=45))
    start = max(backfill_start, latest_stored - lookback)
    return start, today


def ingest_series(engine, client: FredClient, spec: SeriesSpec, *, mode: str, today: date, parent_run_id: str | None) -> SeriesIngestResult:
    result = SeriesIngestResult(series_id=spec.series_id, status=RUN_FAILED)
    with engine.begin() as conn:
        latest_stored = latest_observation_date(conn, spec.series_id) if _series_exists(conn, spec.series_id) else None
        window = request_window(spec, mode=mode, today=today, latest_stored=latest_stored)
        result.request_window = window
        result.run_id = start_run(conn, source_id=FRED_SOURCE_ID, dataset="series:{0}".format(spec.series_id), parent_run_id=parent_run_id, request_window=window)

    spec_fields = spec_registry_fields(spec)
    try:
        meta = client.series_metadata(spec.series_id)
        metadata_status, mismatches = validate_metadata(spec, meta)
        observations = client.observations(spec.series_id, observation_start=window[0], observation_end=window[1])
    except FredError as exc:
        return _fail(engine, result, spec, redact(str(exc)), retry_count=client.retry_count)
    except Exception as exc:  # noqa: BLE001 - redact everything, never leak query strings
        return _fail(engine, result, spec, "unexpected {0}".format(redact(exc.__class__.__name__)), retry_count=client.retry_count)

    retrieved_at = utcnow()
    rows = [
        ObservationInput(o.observation_date, o.raw_value, o.realtime_start, o.realtime_end)
        for o in observations
    ]
    if not publishable(metadata_status):
        return _quarantine(engine, result, spec, spec_fields, meta, metadata_status, mismatches, rows, retrieved_at=retrieved_at, today=today, retry_count=client.retry_count, mode=mode)
    try:
        with engine.begin() as conn:
            upsert_macro_series(
                conn,
                series_id=spec.series_id,
                source_id=FRED_SOURCE_ID,
                provider_series_id=spec.series_id,
                spec_fields=spec_fields,
                meta=meta,
                metadata_status=metadata_status,
                mismatches=mismatches,
            )
            counts = upsert_observations(conn, series_id=spec.series_id, rows=rows, retrieved_at=retrieved_at, run_id=result.run_id, today=today)
            latest = latest_observation_date(conn, spec.series_id)
            first = min((r.observation_date for r in rows), default=None)
            freshness = record_freshness(
                conn,
                source_id=FRED_SOURCE_ID,
                dataset="series:{0}".format(spec.series_id),
                cadence=spec.expected_frequency,
                transport_status=TRANSPORT_OK,
                latest_observation=latest,
                success=True,
                error_redacted=None,
                run_id=result.run_id,
                today=today,
                metadata_status=metadata_status,
                latest_observation_retrieved_at=retrieved_at,
            )
            details = {
                "metadata_status": metadata_status,
                "metadata_mismatches": mismatches,
                "publication_status": "PUBLISHED",
                "provider_observation_start": meta.get("observation_start"),
                "provider_observation_end": meta.get("observation_end"),
                "provider_last_updated": meta.get("last_updated"),
                "returned_first_date": first.isoformat() if first else None,
                "returned_last_date": max((r.observation_date for r in rows), default=None).isoformat() if rows else None,
                "rejected_samples": counts.rejected_samples,
                "mode": mode,
            }
            finish_run(conn, result.run_id, status=RUN_SUCCEEDED, counts=counts.as_dict(), retry_count=client.retry_count, details=details)
        result.status = RUN_SUCCEEDED
        result.counts = counts.as_dict()
        result.latest_observation = latest
        result.first_observation = first
        result.metadata_status = metadata_status
        result.freshness_status = freshness
        return result
    except Exception as exc:  # noqa: BLE001
        return _fail(engine, result, spec, "db write failed: {0}".format(redact(exc.__class__.__name__)), retry_count=client.retry_count)


def spec_registry_fields(spec: SeriesSpec) -> dict[str, Any]:
    return {
        "category": spec.category,
        "subcategory": spec.subcategory,
        "catalog_version": CATALOG_VERSION,
        "source_url": spec.source_url,
        "notes": spec.notes,
        "export_scope": spec.export_scope,
        "expected_frequency": spec.expected_frequency,
        "catalog_units": spec.raw_units,
        "catalog_label": spec.label,
        "aggregation": spec.aggregation,
        "display_divisor": spec.display_divisor,
        "display_units": spec.display_units,
    }


def _quarantine(engine, result: SeriesIngestResult, spec: SeriesSpec, spec_fields: dict[str, Any], meta: dict[str, Any], metadata_status: str, mismatches: list[dict], rows: list[ObservationInput], *, retrieved_at, today: date, retry_count: int, mode: str) -> SeriesIngestResult:
    """Publication gate: keep the payload for diagnosis, keep the last valid data, report loudly."""
    logger.warning("FRED series %s metadata %s; %d observations quarantined", spec.series_id, metadata_status, len(rows))
    result.status = RUN_QUARANTINED
    result.metadata_status = metadata_status
    result.error = "metadata {0}; observations quarantined, last valid data retained".format(metadata_status)
    try:
        with engine.begin() as conn:
            upsert_macro_series(conn, series_id=spec.series_id, source_id=FRED_SOURCE_ID, provider_series_id=spec.series_id, spec_fields=spec_fields, meta=meta, metadata_status=metadata_status, mismatches=mismatches)
            quarantined = quarantine_observations(conn, series_id=spec.series_id, rows=rows, retrieved_at=retrieved_at, run_id=result.run_id, reason="METADATA_{0}".format(metadata_status), metadata_status=metadata_status, detail={"mismatches": mismatches})
            latest_valid = latest_observation_date(conn, spec.series_id)
            result.freshness_status = record_freshness(
                conn,
                source_id=FRED_SOURCE_ID,
                dataset="series:{0}".format(spec.series_id),
                cadence=spec.expected_frequency,
                transport_status=TRANSPORT_METADATA_REJECTED,
                latest_observation=latest_valid,
                success=False,
                error_redacted=result.error[:500],
                run_id=result.run_id,
                today=today,
                metadata_status=metadata_status,
            )
            result.counts = {"received": len(rows), "inserted": 0, "revised": 0, "unchanged": 0, "rejected": quarantined}
            result.latest_observation = latest_valid
            finish_run(conn, result.run_id, status=RUN_QUARANTINED, counts=result.counts, error_redacted=result.error[:500], retry_count=retry_count, details={"metadata_status": metadata_status, "metadata_mismatches": mismatches, "publication_status": "QUARANTINED_METADATA", "quarantined_rows": quarantined, "mode": mode})
    except Exception:  # noqa: BLE001 - bookkeeping must not mask the gate outcome
        logger.exception("failed to record quarantine for %s", spec.series_id)
    return result


def _series_exists(conn, series_id: str) -> bool:
    from sqlalchemy import text

    return bool(conn.execute(text("SELECT 1 FROM mi_macro_series WHERE series_id = :s"), {"s": series_id}).first())


def _fail(engine, result: SeriesIngestResult, spec: SeriesSpec, error: str, *, retry_count: int) -> SeriesIngestResult:
    logger.warning("FRED series %s failed: %s", spec.series_id, error)
    result.status = RUN_FAILED
    result.error = error
    try:
        with engine.begin() as conn:
            finish_run(conn, result.run_id, status=RUN_FAILED, error_redacted=error[:500], retry_count=retry_count)
            result.freshness_status = record_freshness(
                conn,
                source_id=FRED_SOURCE_ID,
                dataset="series:{0}".format(spec.series_id),
                cadence=spec.expected_frequency,
                transport_status=TRANSPORT_FAILED,
                latest_observation=None,
                success=False,
                error_redacted=error[:500],
                run_id=result.run_id,
            )
    except Exception:  # noqa: BLE001 - failure bookkeeping must not mask the original failure
        logger.exception("failed to record FRED failure for %s", spec.series_id)
    return result


def ingest_fred_catalog(engine, client: FredClient, *, series_ids: Iterable[str] | None = None, mode: str = "incremental", today: date | None = None, parent_run_id: str | None = None) -> FredIngestReport:
    today = today or utcnow().date()
    specs = [CATALOG_BY_ID[s] for s in series_ids] if series_ids else list(CATALOG)
    report = FredIngestReport(parent_run_id=parent_run_id, mode=mode)
    for spec in specs:
        report.results.append(ingest_series(engine, client, spec, mode=mode, today=today, parent_run_id=parent_run_id))
    with engine.begin() as conn:
        latest_any = max((r.latest_observation for r in report.succeeded if r.latest_observation), default=None)
        if report.failed and report.succeeded:
            transport = TRANSPORT_PARTIAL
        elif report.failed:
            transport = TRANSPORT_FAILED
        else:
            transport = TRANSPORT_OK
        report.transport_status = transport
        error = None
        if report.failed:
            error = "{0} series failed ({1} metadata-quarantined)".format(len(report.failed), len(report.quarantined))
        record_freshness(
            conn,
            source_id=FRED_SOURCE_ID,
            dataset=FRED_DATASET,
            cadence="MIXED",
            transport_status=transport,
            latest_observation=latest_any,
            success=bool(report.succeeded),
            error_redacted=error,
            run_id=parent_run_id,
            today=today,
        )
    return report


__all__ = ["FRED_DATASET", "FredIngestReport", "REVISION_LOOKBACK", "RUN_QUARANTINED", "SeriesIngestResult", "TRANSPORT_METADATA_REJECTED", "TRANSPORT_PARTIAL", "ingest_fred_catalog", "ingest_series", "request_window", "spec_registry_fields"]
