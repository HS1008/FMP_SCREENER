"""FRED catalog ingestion: metadata validation, bounded backfill, incremental refresh, revisions.

Revision-reconciliation policy (``REVISION_LOOKBACK``): an incremental refresh re-requests
a trailing window per cadence so recent revisions are captured; it does *not* claim to
capture arbitrary historical revisions. ``mode="full"`` re-requests the catalog
``backfill_years`` window (periodic revalidation). ``mode="max"`` requests the earliest
date the provider currently exposes (metadata ``observation_start``). It is for an
explicit historical backfill, not the scheduled refresh. Every returned observation is
compared with the current stored value; differences create an auditable new revision row.

Each series is written in its own transaction. A failed series never rolls back another
series and never erases previously stored observations. A shorter provider window does
not delete older rows already stored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

from sqlalchemy import text

from market_intelligence.catalog import (
    CATALOG,
    CATALOG_BY_ID,
    CATALOG_VERSION,
    CREDIT_SERIES,
    FED_FUNDS_TARGET_LOWER,
    FED_FUNDS_TARGET_UPPER,
    FLY_2S5S10S_METRIC,
    FRED_SOURCE_ID,
    MACRO_COVERAGE_METRICS,
    MACRO_MAX_BACKFILL_SERIES,
    RATES_MAX_BACKFILL_SERIES,
    SLOPE_10Y2Y_METRIC,
    SeriesSpec,
    publishable,
    validate_metadata,
)
from market_intelligence.fred_client import FredClient, FredError, parse_fred_date, redact
from market_intelligence.nulls import MalformedValueError, is_missing_token, normalize_numeric
from market_intelligence.store import (
    RUN_FAILED,
    RUN_SUCCEEDED,
    TRANSPORT_FAILED,
    TRANSPORT_OK,
    ObservationInput,
    coverage_with_provider_latest,
    finish_run,
    latest_observation_date,
    ny_today,
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


def latest_usable_observation_date(rows: Iterable[ObservationInput]) -> date | None:
    """Latest date with a published usable value (same missing-token rules as storage).

    FRED often returns ``"."`` for weekends/holidays. Those dates are response coverage, not
    ``provider_latest``. Missing tokens never become zero.
    """
    usable: list[date] = []
    for row in rows:
        if is_missing_token(row.raw_value):
            continue
        try:
            value, _reason = normalize_numeric(row.raw_value)
        except MalformedValueError:
            continue
        if value is None:
            continue
        usable.append(row.observation_date)
    return max(usable) if usable else None


def returned_coverage_last_date(rows: Iterable[ObservationInput]) -> date | None:
    """Max observation date in the provider payload, including missing-token rows."""
    dates = [row.observation_date for row in rows]
    return max(dates) if dates else None


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


# Used only when provider metadata has no observation_start. FRED then returns
# whatever history it still publishes; stored rows outside that reply are kept.
PROVIDER_MAX_START = date(1900, 1, 1)


def request_window(
    spec: SeriesSpec,
    *,
    mode: str,
    today: date,
    latest_stored: date | None,
    provider_start: date | None = None,
) -> tuple[date, date]:
    if mode == "max":
        start = provider_start or PROVIDER_MAX_START
        if start > today:
            start = today
        return start, today
    backfill_start = date(today.year - spec.backfill_years, today.month, 1)
    if mode == "full" or latest_stored is None:
        return backfill_start, today
    lookback = REVISION_LOOKBACK.get(spec.expected_frequency, timedelta(days=45))
    start = max(backfill_start, latest_stored - lookback)
    return start, today


def ingest_series(engine, client: FredClient, spec: SeriesSpec, *, mode: str, today: date, parent_run_id: str | None) -> SeriesIngestResult:
    result = SeriesIngestResult(series_id=spec.series_id, status=RUN_FAILED)
    prefetched_meta: dict[str, Any] | None = None
    provider_start: date | None = None
    if mode == "max":
        try:
            prefetched_meta = client.series_metadata(spec.series_id)
            provider_start = parse_fred_date(prefetched_meta.get("observation_start"))
        except FredError as exc:
            result.request_window = (None, today)
            with engine.begin() as conn:
                result.run_id = start_run(conn, source_id=FRED_SOURCE_ID, dataset="series:{0}".format(spec.series_id), parent_run_id=parent_run_id, request_window=(None, today))
            return _fail(engine, result, spec, redact(str(exc)), retry_count=client.retry_count)
        except Exception as exc:  # noqa: BLE001 - redact everything, never leak query strings
            result.request_window = (None, today)
            with engine.begin() as conn:
                result.run_id = start_run(conn, source_id=FRED_SOURCE_ID, dataset="series:{0}".format(spec.series_id), parent_run_id=parent_run_id, request_window=(None, today))
            return _fail(engine, result, spec, "unexpected {0}".format(redact(exc.__class__.__name__)), retry_count=client.retry_count)
    with engine.begin() as conn:
        latest_stored = latest_observation_date(conn, spec.series_id) if _series_exists(conn, spec.series_id) else None
        window = request_window(spec, mode=mode, today=today, latest_stored=latest_stored, provider_start=provider_start)
        result.request_window = window
        result.run_id = start_run(conn, source_id=FRED_SOURCE_ID, dataset="series:{0}".format(spec.series_id), parent_run_id=parent_run_id, request_window=window)

    spec_fields = spec_registry_fields(spec)
    try:
        meta = prefetched_meta if prefetched_meta is not None else client.series_metadata(spec.series_id)
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
            provider_latest = latest_usable_observation_date(rows)
            returned_last = returned_coverage_last_date(rows)
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
                coverage_json=coverage_with_provider_latest(
                    provider_latest,
                    provider="FRED",
                    extra={"returned_last_date": returned_last.isoformat() if returned_last else None},
                ),
            )
            details = {
                "metadata_status": metadata_status,
                "metadata_mismatches": mismatches,
                "publication_status": "PUBLISHED",
                "provider_observation_start": meta.get("observation_start"),
                "provider_observation_end": meta.get("observation_end"),
                "provider_last_updated": meta.get("last_updated"),
                "returned_first_date": first.isoformat() if first else None,
                "returned_last_date": returned_last.isoformat() if returned_last else None,
                "provider_latest_observation_date": provider_latest.isoformat() if provider_latest else None,
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
    today = today or ny_today()
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
            coverage_json=coverage_with_provider_latest(latest_any, provider="FRED"),
        )
    return report


def credit_history_coverage(engine) -> list[dict[str, Any]]:
    """Stored span for ICE OAS observations and ``*.oas_bps`` metrics.

    Observations are the provider history that was kept. Metric rows are what the
    Credit charts read. Counts only; values are not printed.
    """
    series_ids = list(CREDIT_SERIES)
    in_series = ", ".join("'{0}'".format(sid) for sid in series_ids)
    in_metrics = ", ".join("'{0}.oas_bps'".format(sid) for sid in series_ids)
    with engine.connect() as conn:
        observations = {
            row["series_id"]: row
            for row in conn.execute(
                text(
                    """
                    SELECT series_id,
                           min(observation_date) AS earliest,
                           max(observation_date) AS latest,
                           count(*) AS row_count
                    FROM mi_macro_observations
                    WHERE is_current AND value IS NOT NULL AND series_id IN ({ids})
                    GROUP BY series_id
                    """.format(ids=in_series)
                )
            ).mappings()
        }
        metrics = {
            row["metric_id"]: row
            for row in conn.execute(
                text(
                    """
                    SELECT metric_id,
                           min(as_of) AS earliest,
                           max(as_of) AS latest,
                           count(*) AS row_count
                    FROM mi_metric_snapshots
                    WHERE value IS NOT NULL AND metric_id IN ({ids})
                    GROUP BY metric_id
                    """.format(ids=in_metrics)
                )
            ).mappings()
        }
    coverage: list[dict[str, Any]] = []
    for sid in series_ids:
        spec = CATALOG_BY_ID[sid]
        obs = observations.get(sid)
        metric = metrics.get("{0}.oas_bps".format(sid))
        obs_first = None if obs is None else obs["earliest"]
        obs_last = None if obs is None else obs["latest"]
        metric_first = None if metric is None else metric["earliest"]
        metric_last = None if metric is None else metric["latest"]
        coverage.append(
            {
                "label": spec.label,
                "series_id": sid,
                "earliest": None if obs_first is None else obs_first.isoformat(),
                "latest": None if obs_last is None else obs_last.isoformat(),
                "rows": 0 if obs is None else int(obs["row_count"]),
                "metric_earliest": None if metric_first is None else metric_first.isoformat(),
                "metric_latest": None if metric_last is None else metric_last.isoformat(),
                "metric_rows": 0 if metric is None else int(metric["row_count"]),
            }
        )
    return coverage


def rates_history_coverage(engine) -> list[dict[str, Any]]:
    """Stored span for the rates max-history series and the two curve metrics.

    Counts only. Values are not printed. A shorter later provider response does
    not remove older rows, so these dates are whatever PostgreSQL still holds.
    """
    series_ids = list(RATES_MAX_BACKFILL_SERIES)
    metric_ids = [SLOPE_10Y2Y_METRIC, FLY_2S5S10S_METRIC]
    in_series = ", ".join("'{0}'".format(sid) for sid in series_ids)
    in_metrics = ", ".join("'{0}'".format(mid) for mid in metric_ids)
    with engine.connect() as conn:
        observations = {
            row["series_id"]: row
            for row in conn.execute(
                text(
                    """
                    SELECT series_id,
                           min(observation_date) AS earliest,
                           max(observation_date) AS latest,
                           count(*) AS row_count
                    FROM mi_macro_observations
                    WHERE is_current AND value IS NOT NULL AND series_id IN ({ids})
                    GROUP BY series_id
                    """.format(ids=in_series)
                )
            ).mappings()
        }
        metrics = {
            row["metric_id"]: row
            for row in conn.execute(
                text(
                    """
                    SELECT metric_id,
                           min(as_of) AS earliest,
                           max(as_of) AS latest,
                           count(*) AS row_count
                    FROM mi_metric_snapshots
                    WHERE value IS NOT NULL AND metric_id IN ({ids})
                    GROUP BY metric_id
                    """.format(ids=in_metrics)
                )
            ).mappings()
        }
    coverage: list[dict[str, Any]] = []
    for sid in series_ids:
        obs = observations.get(sid)
        first = None if obs is None else obs["earliest"]
        last = None if obs is None else obs["latest"]
        coverage.append(
            {
                "kind": "series",
                "series_id": sid,
                "earliest": None if first is None else first.isoformat(),
                "latest": None if last is None else last.isoformat(),
                "rows": 0 if obs is None else int(obs["row_count"]),
            }
        )
    for mid in metric_ids:
        metric = metrics.get(mid)
        first = None if metric is None else metric["earliest"]
        last = None if metric is None else metric["latest"]
        coverage.append(
            {
                "kind": "metric",
                "series_id": mid,
                "earliest": None if first is None else first.isoformat(),
                "latest": None if last is None else last.isoformat(),
                "rows": 0 if metric is None else int(metric["row_count"]),
            }
        )
    return coverage


def macro_history_coverage(engine) -> list[dict[str, Any]]:
    """Stored span for Macro dashboard series and the chart metrics.

    Counts only. A later shorter provider window does not delete older rows.
    """
    series_ids = list(MACRO_MAX_BACKFILL_SERIES)
    metric_ids = list(MACRO_COVERAGE_METRICS)
    in_series = ", ".join("'{0}'".format(sid) for sid in series_ids)
    in_metrics = ", ".join("'{0}'".format(mid) for mid in metric_ids)
    with engine.connect() as conn:
        observations = {
            row["series_id"]: row
            for row in conn.execute(
                text(
                    """
                    SELECT series_id,
                           min(observation_date) AS earliest,
                           max(observation_date) AS latest,
                           count(*) AS row_count
                    FROM mi_macro_observations
                    WHERE is_current AND value IS NOT NULL AND series_id IN ({ids})
                    GROUP BY series_id
                    """.format(ids=in_series)
                )
            ).mappings()
        }
        metrics = {
            row["metric_id"]: row
            for row in conn.execute(
                text(
                    """
                    SELECT metric_id,
                           min(as_of) AS earliest,
                           max(as_of) AS latest,
                           count(*) AS row_count
                    FROM mi_metric_snapshots
                    WHERE value IS NOT NULL AND metric_id IN ({ids})
                    GROUP BY metric_id
                    """.format(ids=in_metrics)
                )
            ).mappings()
        }
    coverage: list[dict[str, Any]] = []
    for sid in series_ids:
        obs = observations.get(sid)
        first = None if obs is None else obs["earliest"]
        last = None if obs is None else obs["latest"]
        coverage.append(
            {
                "kind": "series",
                "series_id": sid,
                "earliest": None if first is None else first.isoformat(),
                "latest": None if last is None else last.isoformat(),
                "rows": 0 if obs is None else int(obs["row_count"]),
            }
        )
    for mid in metric_ids:
        metric = metrics.get(mid)
        first = None if metric is None else metric["earliest"]
        last = None if metric is None else metric["latest"]
        coverage.append(
            {
                "kind": "metric",
                "series_id": mid,
                "earliest": None if first is None else first.isoformat(),
                "latest": None if last is None else last.isoformat(),
                "rows": 0 if metric is None else int(metric["row_count"]),
            }
        )
    return coverage


def fed_funds_target_stored(engine) -> list[dict[str, Any]]:
    """Latest current print of each fed funds target-range limit.

    These are public policy levels. The newest stored observation is the range
    in force until a later change is stored. Counts-only coverage stays in
    ``rates_history_coverage``; this helper exists so a backfill log can name
    the current range without printing unrelated series values.
    """
    series_ids = (FED_FUNDS_TARGET_LOWER, FED_FUNDS_TARGET_UPPER)
    in_series = ", ".join("'{0}'".format(sid) for sid in series_ids)
    with engine.connect() as conn:
        found = {
            row["series_id"]: row
            for row in conn.execute(
                text(
                    """
                    SELECT DISTINCT ON (series_id)
                           series_id, observation_date, value
                    FROM mi_macro_observations
                    WHERE is_current AND value IS NOT NULL AND series_id IN ({ids})
                    ORDER BY series_id, observation_date DESC
                    """.format(ids=in_series)
                )
            ).mappings()
        }
    stored: list[dict[str, Any]] = []
    for sid in series_ids:
        row = found.get(sid)
        observed = None if row is None else row["observation_date"]
        value = None if row is None else row["value"]
        stored.append(
            {
                "series_id": sid,
                "observation_date": None if observed is None else observed.isoformat(),
                "value": None if value is None else format(value, "f"),
            }
        )
    return stored


__all__ = [
    "FRED_DATASET",
    "FredIngestReport",
    "REVISION_LOOKBACK",
    "RUN_QUARANTINED",
    "SeriesIngestResult",
    "TRANSPORT_METADATA_REJECTED",
    "TRANSPORT_PARTIAL",
    "credit_history_coverage",
    "fed_funds_target_stored",
    "rates_history_coverage",
    "macro_history_coverage",
    "ingest_fred_catalog",
    "ingest_series",
    "latest_usable_observation_date",
    "request_window",
    "returned_coverage_last_date",
    "spec_registry_fields",
]
