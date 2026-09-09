"""Ingest FINRA Query API aggregate datasets into canonical PostgreSQL.

Each dataset is its own transaction. Checkpoints advance only after a successful commit.
Aggregate rows are never exploded into fake TRACE prints. Revisions are hash-compared.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.finra_catalog import (
    CAP_AVAILABLE,
    CAP_ENTITLEMENT_REQUIRED,
    CAP_TEMPORARILY_UNAVAILABLE,
    FINRA_ATTRIBUTION,
    FINRA_CATALOG_VERSION,
    FINRA_QUERY_SOURCE_ID,
    FINRA_TERMS,
    FINRA_TRACE_SOURCE_ID,
    QUERY_DATASETS,
    QUERY_DATASETS_BY_NAME,
    TRACE_INDIVIDUAL,
    FinraDatasetSpec,
    category_key,
)
from market_intelligence.finra_client import FinraClient, FinraError, _parse_date
from market_intelligence.nulls import MalformedValueError, canonical_sha256, normalize_numeric, strict_dumps
from market_intelligence.store import (
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_SUCCEEDED,
    finish_run,
    record_freshness,
    start_run,
    utcnow,
)

logger = logging.getLogger(__name__)


@dataclass
class DatasetIngestResult:
    dataset: str
    status: str
    capability_status: str
    http_status: int | None = None
    counts: dict[str, int] = field(default_factory=dict)
    latest_observation: date | None = None
    first_observation: date | None = None
    request_window: tuple[date | None, date | None] = (None, None)
    error: str | None = None
    run_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "status": self.status,
            "capability_status": self.capability_status,
            "http_status": self.http_status,
            "counts": dict(self.counts),
            "latest_observation": self.latest_observation.isoformat() if self.latest_observation else None,
            "first_observation": self.first_observation.isoformat() if self.first_observation else None,
            "request_window": [d.isoformat() if d else None for d in self.request_window],
            "error": self.error,
            "run_id": self.run_id,
        }


@dataclass
class FinraIngestReport:
    status: str
    results: list[DatasetIngestResult] = field(default_factory=list)
    failed: bool = False
    authenticated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "authenticated": self.authenticated,
            "failed": self.failed,
            "results": [row.as_dict() for row in self.results],
        }


def ensure_finra_sources(conn, *, query_enabled: bool, query_access: str, trace_access: str = CAP_ENTITLEMENT_REQUIRED) -> None:
    rows = (
        {
            "source_id": FINRA_QUERY_SOURCE_ID,
            "provider": "FINRA",
            "dataset": "fixedIncomeMarket_aggregates",
            "enabled": query_enabled,
            "access_status": query_access,
            "source_url": "https://developer.finra.org/docs",
            "expected_cadence": "D",
            "usage_scope": "INTERNAL_ONLY",
            "attribution": FINRA_ATTRIBUTION,
            "terms_notes": FINRA_TERMS,
            "catalog_version": FINRA_CATALOG_VERSION,
        },
        {
            "source_id": FINRA_TRACE_SOURCE_ID,
            "provider": "FINRA",
            "dataset": "trace_corporate_trades",
            "enabled": False,
            "access_status": trace_access,
            "source_url": "https://www.finra.org/filing-reporting/trace/documentation",
            "expected_cadence": "INTRADAY",
            "usage_scope": "INTERNAL_ONLY",
            "attribution": "FINRA TRACE individual transactions (not on Query API).",
            "terms_notes": TRACE_INDIVIDUAL.coverage_note,
            "catalog_version": FINRA_CATALOG_VERSION,
        },
    )
    for row in rows:
        conn.execute(
            text(
                """
                INSERT INTO mi_source_registry (
                    source_id, provider, dataset, enabled, access_status, source_url, expected_cadence,
                    units_metadata, usage_scope, terms_notes, attribution, catalog_version, updated_at
                ) VALUES (
                    :source_id, :provider, :dataset, :enabled, :access_status, :source_url, :expected_cadence,
                    '{}'::jsonb, :usage_scope, :terms_notes, :attribution, :catalog_version, NOW()
                )
                ON CONFLICT (source_id) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    dataset = EXCLUDED.dataset,
                    enabled = EXCLUDED.enabled,
                    access_status = EXCLUDED.access_status,
                    source_url = EXCLUDED.source_url,
                    expected_cadence = EXCLUDED.expected_cadence,
                    terms_notes = EXCLUDED.terms_notes,
                    attribution = EXCLUDED.attribution,
                    catalog_version = EXCLUDED.catalog_version,
                    updated_at = NOW()
                """
            ),
            row,
        )


def upsert_capability(conn, *, spec: FinraDatasetSpec, status: str, http_status: int | None, record_count: int | None, schema_fields: list[str] | None, latest: date | None, coverage_note: str, error_redacted: str | None = None, success: bool = False, details: Mapping[str, Any] | None = None) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mi_finra_dataset_capability (
                group_name, dataset, source_id, environment, capability_status, http_status, record_count,
                schema_fields, coverage_note, latest_observation_date, last_probe_at, last_success_at,
                error_redacted, details_json
            ) VALUES (
                :group_name, :dataset, :source_id, 'production', :status, :http_status, :record_count,
                CAST(:schema_fields AS JSONB), :coverage_note, :latest, NOW(),
                CASE WHEN :success THEN NOW() ELSE NULL END, :error_redacted, CAST(:details AS JSONB)
            )
            ON CONFLICT (group_name, dataset, environment) DO UPDATE SET
                source_id = EXCLUDED.source_id,
                capability_status = EXCLUDED.capability_status,
                http_status = EXCLUDED.http_status,
                record_count = EXCLUDED.record_count,
                schema_fields = COALESCE(EXCLUDED.schema_fields, mi_finra_dataset_capability.schema_fields),
                coverage_note = EXCLUDED.coverage_note,
                latest_observation_date = COALESCE(EXCLUDED.latest_observation_date, mi_finra_dataset_capability.latest_observation_date),
                last_probe_at = NOW(),
                last_success_at = CASE WHEN :success THEN NOW() ELSE mi_finra_dataset_capability.last_success_at END,
                error_redacted = EXCLUDED.error_redacted,
                details_json = COALESCE(EXCLUDED.details_json, mi_finra_dataset_capability.details_json)
            """
        ),
        {
            "group_name": spec.group,
            "dataset": spec.dataset,
            "source_id": spec.source_id,
            "status": status,
            "http_status": http_status,
            "record_count": record_count,
            "schema_fields": strict_dumps(schema_fields or []),
            "coverage_note": coverage_note,
            "latest": latest,
            "success": success,
            "error_redacted": error_redacted,
            "details": strict_dumps(details or {}),
        },
    )


def _checkpoint_date(conn, dataset: str) -> date | None:
    return conn.execute(text("SELECT last_committed_observation_date FROM mi_finra_ingest_checkpoint WHERE dataset = :d"), {"d": dataset}).scalar()


def _write_checkpoint(conn, dataset: str, latest: date | None, overlap_days: int, details: Mapping[str, Any] | None = None) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mi_finra_ingest_checkpoint (dataset, last_committed_observation_date, last_success_at, overlap_days, details_json)
            VALUES (:dataset, :latest, NOW(), :overlap, CAST(:details AS JSONB))
            ON CONFLICT (dataset) DO UPDATE SET
                last_committed_observation_date = COALESCE(EXCLUDED.last_committed_observation_date, mi_finra_ingest_checkpoint.last_committed_observation_date),
                last_success_at = NOW(),
                overlap_days = EXCLUDED.overlap_days,
                details_json = EXCLUDED.details_json
            """
        ),
        {"dataset": dataset, "latest": latest, "overlap": overlap_days, "details": strict_dumps(details or {})},
    )


def _metric_payload(spec: FinraDatasetSpec, row: Mapping[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for name in spec.numeric_fields:
        if name not in row:
            metrics[name] = None
            continue
        raw = row.get(name)
        try:
            value, _reason = normalize_numeric(raw) if raw is not None and str(raw).strip() != "" else (None, "missing")
        except MalformedValueError:
            value = None
        metrics[name] = float(value) if isinstance(value, Decimal) else value
    return metrics


def _grain(spec: FinraDatasetSpec, row: Mapping[str, Any]) -> dict[str, Any]:
    return {name: (None if row.get(name) is None else str(row.get(name))) for name in spec.grain_fields}


def upsert_aggregate_rows(conn, spec: FinraDatasetSpec, records: list[Mapping[str, Any]], *, run_id: str, retrieved_at: datetime) -> dict[str, int]:
    counts = {"received": 0, "inserted": 0, "revised": 0, "unchanged": 0, "rejected": 0}
    for raw in records:
        counts["received"] += 1
        nested = conn.begin_nested()
        try:
            obs_date = _parse_date(raw.get(spec.date_field))
            if obs_date is None:
                counts["rejected"] += 1
                nested.rollback()
                continue
            grain = _grain(spec, raw)
            key = category_key(spec, dict(raw))
            metrics = _metric_payload(spec, raw)
            payload_hash = canonical_sha256({"grain": grain, "metrics": metrics, "date": obs_date.isoformat(), "dataset": spec.dataset})
            current = conn.execute(
                text(
                    """
                    SELECT id, payload_hash, revision_seq FROM mi_finra_aggregate_observations
                    WHERE source_id = :src AND dataset = :ds AND observation_date = :d AND category_key = :k AND is_current
                    """
                ),
                {"src": spec.source_id, "ds": spec.dataset, "d": obs_date, "k": key},
            ).mappings().first()
            if current and current["payload_hash"] == payload_hash:
                conn.execute(text("UPDATE mi_finra_aggregate_observations SET last_seen_at = :seen WHERE id = :id"), {"seen": retrieved_at, "id": current["id"]})
                counts["unchanged"] += 1
                nested.commit()
                continue
            if current:
                conn.execute(text("UPDATE mi_finra_aggregate_observations SET is_current = FALSE, superseded_at = :seen WHERE id = :id"), {"seen": retrieved_at, "id": current["id"]})
                revision = int(current["revision_seq"]) + 1
                counts["revised"] += 1
            else:
                revision = 1
                counts["inserted"] += 1
            conn.execute(
                text(
                    """
                    INSERT INTO mi_finra_aggregate_observations (
                        source_id, dataset, observation_date, category_key, grain_json, metrics_json, units_note,
                        volume_is_capped, payload_hash, retrieved_at, ingestion_run_id, revision_seq, is_current, last_seen_at
                    ) VALUES (
                        :src, :ds, :d, :k, CAST(:grain AS JSONB), CAST(:metrics AS JSONB), :units,
                        :capped, :hash, :retrieved, :run_id, :rev, TRUE, :retrieved
                    )
                    """
                ),
                {
                    "src": spec.source_id,
                    "ds": spec.dataset,
                    "d": obs_date,
                    "k": key,
                    "grain": strict_dumps(grain),
                    "metrics": strict_dumps(metrics),
                    "units": spec.units_note,
                    "capped": spec.volume_is_capped,
                    "hash": payload_hash,
                    "retrieved": retrieved_at,
                    "run_id": run_id,
                    "rev": revision,
                },
            )
            nested.commit()
        except SQLAlchemyError:
            nested.rollback()
            counts["rejected"] += 1
            logger.exception("FINRA aggregate row rejected")
    return counts


def _window_for(conn, spec: FinraDatasetSpec, *, today: date, mode: str) -> tuple[date, date]:
    end = today
    if mode == "full":
        return today - timedelta(days=spec.backfill_calendar_days), end
    prior = _checkpoint_date(conn, spec.dataset)
    if prior is None:
        start = today - timedelta(days=spec.backfill_calendar_days)
    else:
        start = prior - timedelta(days=spec.overlap_days)
    return start, end


def ingest_dataset(engine, client: FinraClient, spec: FinraDatasetSpec, *, parent_run_id: str | None, today: date, mode: str = "incremental") -> DatasetIngestResult:
    retrieved_at = utcnow()
    with engine.begin() as conn:
        window = _window_for(conn, spec, today=today, mode=mode)
        run_id = start_run(conn, source_id=spec.source_id, dataset=spec.dataset, parent_run_id=parent_run_id, request_window=window, catalog_version=FINRA_CATALOG_VERSION)
    try:
        records = client.query_all(spec, start=window[0], end=window[1])
    except FinraError as exc:
        with engine.begin() as conn:
            upsert_capability(conn, spec=spec, status=exc.capability or CAP_TEMPORARILY_UNAVAILABLE, http_status=exc.status, record_count=None, schema_fields=None, latest=None, coverage_note=spec.coverage_note, error_redacted=str(exc), success=False)
            record_freshness(conn, source_id=spec.source_id, dataset=spec.dataset, cadence=spec.cadence, transport_status="FAILED", latest_observation=None, success=False, error_redacted=str(exc), run_id=run_id)
            finish_run(conn, run_id, status=RUN_FAILED, error_redacted=str(exc), details={"http_status": exc.status, "capability": exc.capability})
        return DatasetIngestResult(spec.dataset, RUN_FAILED, exc.capability or CAP_TEMPORARILY_UNAVAILABLE, http_status=exc.status, error=str(exc), run_id=run_id, request_window=window)

    schema_fields: list[str] = []
    dates: list[date] = []
    for row in records:
        for key in row:
            if key not in schema_fields:
                schema_fields.append(key)
        parsed = _parse_date(row.get(spec.date_field))
        if parsed is not None:
            dates.append(parsed)
    latest = max(dates) if dates else None
    first = min(dates) if dates else None
    with engine.begin() as conn:
        counts = upsert_aggregate_rows(conn, spec, records, run_id=run_id, retrieved_at=retrieved_at)
        upsert_capability(
            conn,
            spec=spec,
            status=CAP_AVAILABLE,
            http_status=200,
            record_count=len(records),
            schema_fields=schema_fields,
            latest=latest,
            coverage_note=spec.coverage_note,
            success=True,
            details={"window_start": window[0].isoformat(), "window_end": window[1].isoformat()},
        )
        rejected = counts["rejected"]
        received = counts["received"]
        record_freshness(
            conn,
            source_id=spec.source_id,
            dataset=spec.dataset,
            cadence=spec.cadence,
            transport_status="OK" if rejected == 0 else "PARTIAL",
            latest_observation=latest,
            success=rejected < received or received == 0,
            error_redacted=None if rejected == 0 else "{0} aggregate row(s) rejected".format(rejected),
            run_id=run_id,
            latest_observation_retrieved_at=retrieved_at,
            metadata_status="VALIDATED",
        )
        if latest is not None:
            _write_checkpoint(conn, spec.dataset, latest, spec.overlap_days, {"received": received})
        status = RUN_SUCCEEDED if rejected == 0 else RUN_PARTIAL
        finish_run(conn, run_id, status=status, counts=counts, details={"window_start": window[0].isoformat(), "window_end": window[1].isoformat(), "schema_fields": schema_fields[:40]})
    return DatasetIngestResult(spec.dataset, status, CAP_AVAILABLE, http_status=200, counts=counts, latest_observation=latest, first_observation=first, request_window=window, run_id=run_id)


def record_individual_trace_limitation(conn) -> None:
    upsert_capability(
        conn,
        spec=TRACE_INDIVIDUAL,
        status=CAP_ENTITLEMENT_REQUIRED,
        http_status=None,
        record_count=0,
        schema_fields=[],
        latest=None,
        coverage_note=TRACE_INDIVIDUAL.coverage_note,
        success=False,
        details={"query_api": False, "traqs_probed": False},
    )
    record_freshness(
        conn,
        source_id=FINRA_TRACE_SOURCE_ID,
        dataset=TRACE_INDIVIDUAL.dataset,
        cadence="INTRADAY",
        transport_status="SKIPPED",
        latest_observation=None,
        success=False,
        error_redacted="individual TRACE tape is not a Query API dataset",
        run_id=None,
    )


def ingest_finra(engine, client: FinraClient, *, parent_run_id: str | None = None, today: date | None = None, mode: str = "incremental", datasets: list[str] | None = None) -> FinraIngestReport:
    today = today or utcnow().date()
    if datasets:
        wanted = []
        for name in datasets:
            spec = QUERY_DATASETS_BY_NAME.get(name)
            if spec is None:
                raise ValueError("unknown FINRA Query dataset {0!r}".format(name))
            wanted.append(spec)
    else:
        wanted = list(QUERY_DATASETS)
    report = FinraIngestReport(status=RUN_SUCCEEDED, authenticated=True)
    with engine.begin() as conn:
        ensure_finra_sources(conn, query_enabled=True, query_access="CONFIGURED")
        record_individual_trace_limitation(conn)
    failures = 0
    for spec in wanted:
        result = ingest_dataset(engine, client, spec, parent_run_id=parent_run_id, today=today, mode=mode)
        report.results.append(result)
        if result.status == RUN_FAILED:
            failures += 1
    report.failed = failures > 0
    report.status = RUN_SUCCEEDED if failures == 0 else RUN_PARTIAL
    return report
