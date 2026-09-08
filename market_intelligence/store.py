"""Canonical PostgreSQL writers: registry, ingestion runs, observations with revisions, freshness.

All functions take an open SQLAlchemy connection; callers own transaction boundaries
(one transaction per source so a failed source never rolls back another).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from sqlalchemy import text

from market_intelligence import CODE_VERSION
from market_intelligence.catalog import CATALOG_VERSION, SOURCE_REGISTRY_DEFAULTS, publishable
from market_intelligence.freshness import FRESHNESS_POLICY_VERSION, assess_freshness
from market_intelligence.nulls import MalformedValueError, normalize_numeric, strict_dumps

RUN_ATTEMPTED = "ATTEMPTED"
RUN_SUCCEEDED = "SUCCEEDED"
RUN_FAILED = "FAILED"
RUN_SKIPPED = "SKIPPED"
RUN_PARTIAL = "PARTIAL"

TRANSPORT_OK = "OK"
TRANSPORT_FAILED = "FAILED"
TRANSPORT_SKIPPED = "SKIPPED"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id(prefix: str = "run") -> str:
    return "{0}_{1}".format(prefix, uuid.uuid4().hex[:20])


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return strict_dumps(value)


# ---- registry ----------------------------------------------------------------------

def upsert_source_registry(conn, entries: Iterable[Mapping[str, Any]] | None = None, *, enabled: Mapping[str, bool] | None = None, access: Mapping[str, str] | None = None) -> int:
    entries = list(entries if entries is not None else SOURCE_REGISTRY_DEFAULTS)
    enabled = enabled or {}
    access = access or {}
    count = 0
    for entry in entries:
        sid = entry["source_id"]
        conn.execute(
            text(
                """
                INSERT INTO mi_source_registry (
                    source_id, provider, dataset, enabled, access_status, source_url, expected_cadence,
                    units_metadata, usage_scope, terms_notes, attribution, catalog_version, updated_at
                ) VALUES (
                    :source_id, :provider, :dataset, :enabled, :access_status, :source_url, :expected_cadence,
                    CAST(:units_metadata AS JSONB), :usage_scope, :terms_notes, :attribution, :catalog_version, NOW()
                )
                ON CONFLICT (source_id) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    dataset = EXCLUDED.dataset,
                    enabled = EXCLUDED.enabled,
                    access_status = EXCLUDED.access_status,
                    source_url = EXCLUDED.source_url,
                    expected_cadence = EXCLUDED.expected_cadence,
                    units_metadata = EXCLUDED.units_metadata,
                    usage_scope = EXCLUDED.usage_scope,
                    terms_notes = EXCLUDED.terms_notes,
                    attribution = EXCLUDED.attribution,
                    catalog_version = EXCLUDED.catalog_version,
                    updated_at = NOW()
                """
            ),
            {
                "source_id": sid,
                "provider": entry.get("provider"),
                "dataset": entry.get("dataset"),
                "enabled": bool(enabled.get(sid, entry.get("enabled", False))),
                "access_status": access.get(sid, entry.get("access_status", "CONFIGURATION_REQUIRED")),
                "source_url": entry.get("source_url"),
                "expected_cadence": entry.get("expected_cadence"),
                "units_metadata": _json(entry.get("units_metadata") or {}),
                "usage_scope": entry.get("usage_scope", "INTERNAL_ONLY"),
                "terms_notes": entry.get("terms_notes"),
                "attribution": entry.get("attribution"),
                "catalog_version": entry.get("catalog_version", CATALOG_VERSION),
            },
        )
        count += 1
    return count


# ---- ingestion runs ------------------------------------------------------------------

def start_run(conn, *, source_id: str, dataset: str | None, parent_run_id: str | None = None, request_window: tuple[date | None, date | None] = (None, None), dry_run: bool = False, catalog_version: str = CATALOG_VERSION) -> str:
    run_id = new_run_id()
    conn.execute(
        text(
            """
            INSERT INTO mi_ingestion_runs (
                run_id, parent_run_id, source_id, dataset, started_at, status, code_version, catalog_version,
                request_window_start, request_window_end, dry_run
            ) VALUES (
                :run_id, :parent_run_id, :source_id, :dataset, NOW(), :status, :code_version, :catalog_version,
                :ws, :we, :dry_run
            )
            """
        ),
        {
            "run_id": run_id,
            "parent_run_id": parent_run_id,
            "source_id": source_id,
            "dataset": dataset,
            "status": RUN_ATTEMPTED,
            "code_version": CODE_VERSION,
            "catalog_version": catalog_version,
            "ws": request_window[0],
            "we": request_window[1],
            "dry_run": dry_run,
        },
    )
    return run_id


def finish_run(conn, run_id: str, *, status: str, counts: Mapping[str, int] | None = None, error_redacted: str | None = None, retry_count: int = 0, details: Mapping[str, Any] | None = None) -> None:
    counts = counts or {}
    conn.execute(
        text(
            """
            UPDATE mi_ingestion_runs SET
                finished_at = NOW(),
                status = :status,
                rows_received = :received,
                rows_inserted = :inserted,
                rows_revised = :revised,
                rows_unchanged = :unchanged,
                rows_rejected = :rejected,
                error_redacted = :error_redacted,
                retry_count = :retry_count,
                details_json = CAST(:details AS JSONB)
            WHERE run_id = :run_id
            """
        ),
        {
            "run_id": run_id,
            "status": status,
            "received": counts.get("received"),
            "inserted": counts.get("inserted"),
            "revised": counts.get("revised"),
            "unchanged": counts.get("unchanged"),
            "rejected": counts.get("rejected"),
            "error_redacted": error_redacted,
            "retry_count": retry_count,
            "details": _json(details) if details is not None else None,
        },
    )


# ---- series metadata -------------------------------------------------------------------

PUBLICATION_PUBLISHED = "PUBLISHED"
PUBLICATION_QUARANTINED = "QUARANTINED_METADATA"
PUBLICATION_UNVALIDATED = "UNVALIDATED"


def upsert_macro_series(conn, *, series_id: str, source_id: str, provider_series_id: str, spec_fields: Mapping[str, Any], meta: Mapping[str, Any] | None, metadata_status: str, mismatches: list[dict] | None) -> str:
    """Upsert series metadata and return the publication status decided by the metadata gate.

    Provider metadata columns are only overwritten when the metadata validated; a failed
    validation records the status/mismatches and leaves the last valid description intact so
    downstream consumers keep seeing the units that match the published observations.
    """
    meta = meta or {}
    publish = publishable(metadata_status)
    publication_status = PUBLICATION_PUBLISHED if publish else PUBLICATION_QUARANTINED
    reason = None if publish else "metadata {0}: {1}".format(metadata_status, strict_dumps(mismatches or [])[:400])
    conn.execute(
        text(
            """
            INSERT INTO mi_macro_series (
                series_id, source_id, provider_series_id, title, units, units_short, frequency, frequency_short,
                seasonal_adjustment, seasonal_adjustment_short, category, subcategory, catalog_version, source_url,
                source_notes, provider_last_updated, provider_observation_start, provider_observation_end,
                metadata_status, metadata_mismatch_json, vintage_kind, pit_safe, export_scope, updated_at,
                publication_status, publication_reason, catalog_units, catalog_label, aggregation, display_divisor, display_units,
                last_validated_at, last_quarantined_at
            ) VALUES (
                :series_id, :source_id, :provider_series_id,
                CASE WHEN :publish THEN :title END, CASE WHEN :publish THEN :units END, CASE WHEN :publish THEN :units_short END,
                CASE WHEN :publish THEN :frequency END, CASE WHEN :publish THEN :frequency_short END,
                CASE WHEN :publish THEN :sa END, CASE WHEN :publish THEN :sa_short END, :category, :subcategory, :catalog_version, :source_url,
                :source_notes, CASE WHEN :publish THEN :provider_last_updated END, CASE WHEN :publish THEN CAST(:obs_start AS DATE) END, CASE WHEN :publish THEN CAST(:obs_end AS DATE) END,
                :metadata_status, CAST(:mismatches AS JSONB), 'LATEST_REVISED', FALSE, :export_scope, NOW(),
                :publication_status, :publication_reason, :catalog_units, :catalog_label, :aggregation, :display_divisor, :display_units,
                CASE WHEN :publish THEN NOW() ELSE NULL END, CASE WHEN :publish THEN NULL ELSE NOW() END
            )
            ON CONFLICT (series_id) DO UPDATE SET
                title = CASE WHEN :publish THEN COALESCE(EXCLUDED.title, mi_macro_series.title) ELSE mi_macro_series.title END,
                units = CASE WHEN :publish THEN COALESCE(EXCLUDED.units, mi_macro_series.units) ELSE mi_macro_series.units END,
                units_short = CASE WHEN :publish THEN COALESCE(EXCLUDED.units_short, mi_macro_series.units_short) ELSE mi_macro_series.units_short END,
                frequency = CASE WHEN :publish THEN COALESCE(EXCLUDED.frequency, mi_macro_series.frequency) ELSE mi_macro_series.frequency END,
                frequency_short = CASE WHEN :publish THEN COALESCE(EXCLUDED.frequency_short, mi_macro_series.frequency_short) ELSE mi_macro_series.frequency_short END,
                seasonal_adjustment = CASE WHEN :publish THEN COALESCE(EXCLUDED.seasonal_adjustment, mi_macro_series.seasonal_adjustment) ELSE mi_macro_series.seasonal_adjustment END,
                seasonal_adjustment_short = CASE WHEN :publish THEN COALESCE(EXCLUDED.seasonal_adjustment_short, mi_macro_series.seasonal_adjustment_short) ELSE mi_macro_series.seasonal_adjustment_short END,
                category = EXCLUDED.category,
                subcategory = EXCLUDED.subcategory,
                catalog_version = EXCLUDED.catalog_version,
                source_url = EXCLUDED.source_url,
                source_notes = EXCLUDED.source_notes,
                provider_last_updated = CASE WHEN :publish THEN COALESCE(EXCLUDED.provider_last_updated, mi_macro_series.provider_last_updated) ELSE mi_macro_series.provider_last_updated END,
                provider_observation_start = CASE WHEN :publish THEN COALESCE(EXCLUDED.provider_observation_start, mi_macro_series.provider_observation_start) ELSE mi_macro_series.provider_observation_start END,
                provider_observation_end = CASE WHEN :publish THEN COALESCE(EXCLUDED.provider_observation_end, mi_macro_series.provider_observation_end) ELSE mi_macro_series.provider_observation_end END,
                metadata_status = EXCLUDED.metadata_status,
                metadata_mismatch_json = EXCLUDED.metadata_mismatch_json,
                export_scope = EXCLUDED.export_scope,
                updated_at = NOW(),
                publication_status = EXCLUDED.publication_status,
                publication_reason = EXCLUDED.publication_reason,
                catalog_units = EXCLUDED.catalog_units,
                catalog_label = EXCLUDED.catalog_label,
                aggregation = EXCLUDED.aggregation,
                display_divisor = EXCLUDED.display_divisor,
                display_units = EXCLUDED.display_units,
                last_validated_at = CASE WHEN :publish THEN NOW() ELSE mi_macro_series.last_validated_at END,
                last_quarantined_at = CASE WHEN :publish THEN mi_macro_series.last_quarantined_at ELSE NOW() END
            """
        ),
        {
            "series_id": series_id,
            "source_id": source_id,
            "provider_series_id": provider_series_id,
            "title": meta.get("title"),
            "units": meta.get("units"),
            "units_short": meta.get("units_short"),
            "frequency": meta.get("frequency"),
            "frequency_short": meta.get("frequency_short") or spec_fields.get("expected_frequency"),
            "sa": meta.get("seasonal_adjustment"),
            "sa_short": meta.get("seasonal_adjustment_short"),
            "category": spec_fields.get("category"),
            "subcategory": spec_fields.get("subcategory"),
            "catalog_version": spec_fields.get("catalog_version", CATALOG_VERSION),
            "source_url": spec_fields.get("source_url"),
            "source_notes": spec_fields.get("notes"),
            "provider_last_updated": meta.get("last_updated"),
            "obs_start": _date_or_none(meta.get("observation_start")),
            "obs_end": _date_or_none(meta.get("observation_end")),
            "metadata_status": metadata_status,
            "mismatches": _json(mismatches or []),
            "export_scope": spec_fields.get("export_scope", "INTERNAL_ONLY"),
            "publish": publish,
            "publication_status": publication_status,
            "publication_reason": reason,
            "catalog_units": spec_fields.get("catalog_units"),
            "catalog_label": spec_fields.get("catalog_label"),
            "aggregation": spec_fields.get("aggregation"),
            "display_divisor": spec_fields.get("display_divisor"),
            "display_units": spec_fields.get("display_units"),
        },
    )
    return publication_status


def quarantine_observations(conn, *, series_id: str, rows: Iterable["ObservationInput"], retrieved_at: datetime, run_id: str | None, reason: str, metadata_status: str | None, detail: Mapping[str, Any] | None = None) -> int:
    """Retain rejected payloads for diagnosis without promoting them to current observations."""
    rows = list(rows)
    if not rows:
        return 0
    conn.execute(
        text(
            """
            INSERT INTO mi_macro_observation_quarantine (
                series_id, observation_date, raw_value, realtime_start, realtime_end, retrieved_at, ingestion_run_id, reason, metadata_status, detail_json
            ) VALUES (
                :series_id, :d, :raw_value, :rs, :re, :retrieved_at, :run_id, :reason, :metadata_status, CAST(:detail AS JSONB)
            )
            """
        ),
        [
            {
                "series_id": series_id,
                "d": r.observation_date,
                "raw_value": None if r.raw_value is None else str(r.raw_value)[:64],
                "rs": r.realtime_start,
                "re": r.realtime_end,
                "retrieved_at": retrieved_at,
                "run_id": run_id,
                "reason": reason,
                "metadata_status": metadata_status,
                "detail": _json(detail) if detail is not None else None,
            }
            for r in rows
        ],
    )
    return len(rows)


def _date_or_none(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


# ---- observations with revision semantics -----------------------------------------------

@dataclass
class ObservationInput:
    observation_date: date
    raw_value: Any
    realtime_start: date | None = None
    realtime_end: date | None = None


@dataclass
class UpsertCounts:
    received: int = 0
    inserted: int = 0
    revised: int = 0
    unchanged: int = 0
    rejected: int = 0
    rejected_samples: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rejected_samples is None:
            self.rejected_samples = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "inserted": self.inserted,
            "revised": self.revised,
            "unchanged": self.unchanged,
            "rejected": self.rejected,
        }


def _values_equal(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return a == b


def upsert_observations(conn, *, series_id: str, rows: Iterable[ObservationInput], retrieved_at: datetime, run_id: str | None, today: date | None = None) -> UpsertCounts:
    """Idempotent upsert with auditable revisions.

    - identical value -> untouched economic row, ``last_seen_at`` refreshed
    - different value -> previous row ``is_current=false`` + ``superseded_at``; new row revision_seq+1
    - missing tokens -> NULL value with raw token retained; malformed text -> rejected (never stored as 0)
    - observation dates after ``today`` (retrieval date) -> rejected and quarantined (never current)
    """
    counts = UpsertCounts()
    rows = list(rows)
    counts.received = len(rows)
    if not rows:
        return counts
    today = today or retrieved_at.date()
    future = [r for r in rows if r.observation_date > today]
    if future:
        quarantine_observations(conn, series_id=series_id, rows=future, retrieved_at=retrieved_at, run_id=run_id, reason="FUTURE_OBSERVATION_DATE", metadata_status=None, detail={"today": today.isoformat()})
        counts.rejected += len(future)
        for r in future[:5]:
            if len(counts.rejected_samples) < 5:
                counts.rejected_samples.append({"observation_date": r.observation_date.isoformat(), "raw_value": str(r.raw_value)[:32], "error": "observation date is after retrieval date"})
        rows = [r for r in rows if r.observation_date <= today]
        if not rows:
            return counts
    dates = [r.observation_date for r in rows]
    existing = conn.execute(
        text(
            """
            SELECT observation_date, value, revision_seq
            FROM mi_macro_observations
            WHERE series_id = :series_id AND is_current AND observation_date = ANY(:dates)
            """
        ),
        {"series_id": series_id, "dates": dates},
    ).mappings().all()
    current: dict[date, tuple[Decimal | None, int]] = {
        r["observation_date"]: (Decimal(str(r["value"])) if r["value"] is not None else None, int(r["revision_seq"]))
        for r in existing
    }
    for row in rows:
        try:
            value, _reason = normalize_numeric(row.raw_value)
        except MalformedValueError as exc:
            counts.rejected += 1
            if len(counts.rejected_samples) < 5:
                counts.rejected_samples.append({"observation_date": row.observation_date.isoformat(), "raw_value": str(row.raw_value)[:32], "error": str(exc)[:120]})
            continue
        prior = current.get(row.observation_date)
        if prior is not None and _values_equal(prior[0], value):
            conn.execute(
                text(
                    """
                    UPDATE mi_macro_observations SET last_seen_at = :seen
                    WHERE series_id = :series_id AND observation_date = :d AND is_current
                    """
                ),
                {"seen": retrieved_at, "series_id": series_id, "d": row.observation_date},
            )
            counts.unchanged += 1
            continue
        next_seq = 1
        if prior is not None:
            next_seq = prior[1] + 1
            conn.execute(
                text(
                    """
                    UPDATE mi_macro_observations SET is_current = FALSE, superseded_at = :seen
                    WHERE series_id = :series_id AND observation_date = :d AND is_current
                    """
                ),
                {"seen": retrieved_at, "series_id": series_id, "d": row.observation_date},
            )
            counts.revised += 1
        else:
            counts.inserted += 1
        conn.execute(
            text(
                """
                INSERT INTO mi_macro_observations (
                    series_id, observation_date, value, raw_value, realtime_start, realtime_end,
                    retrieved_at, ingestion_run_id, revision_seq, is_current, last_seen_at
                ) VALUES (
                    :series_id, :d, :value, :raw_value, :rs, :re, :retrieved_at, :run_id, :seq, TRUE, :retrieved_at
                )
                """
            ),
            {
                "series_id": series_id,
                "d": row.observation_date,
                "value": value,
                "raw_value": None if row.raw_value is None else str(row.raw_value),
                "rs": row.realtime_start,
                "re": row.realtime_end,
                "retrieved_at": retrieved_at,
                "run_id": run_id,
                "seq": next_seq,
            },
        )
    return counts


def current_observations(conn, series_id: str, *, start: date | None = None, end: date | None = None) -> dict[date, Decimal | None]:
    rows = conn.execute(
        text(
            """
            SELECT observation_date, value FROM mi_macro_observations
            WHERE series_id = :series_id AND is_current
              AND (:start IS NULL OR observation_date >= :start)
              AND (:end IS NULL OR observation_date <= :end)
            ORDER BY observation_date
            """
        ),
        {"series_id": series_id, "start": start, "end": end},
    ).all()
    return {r[0]: (Decimal(str(r[1])) if r[1] is not None else None) for r in rows}


def latest_observation_date(conn, series_id: str) -> date | None:
    row = conn.execute(
        text(
            """
            SELECT MAX(observation_date) FROM mi_macro_observations
            WHERE series_id = :series_id AND is_current AND value IS NOT NULL
            """
        ),
        {"series_id": series_id},
    ).scalar()
    return row


# ---- freshness ----------------------------------------------------------------------

def record_freshness(conn, *, source_id: str, dataset: str, cadence: str | None, transport_status: str, latest_observation: date | None, success: bool, error_redacted: str | None, run_id: str | None, today: date | None = None, metadata_status: str | None = None, latest_observation_retrieved_at: datetime | None = None) -> str:
    """Update transport + observation freshness separately. Failed retrievals keep last valid data.

    The stored ``freshness_status`` is the assessment *at write time*; readers must recompute
    against their own clock (``read_models.source_health``) because health decays even when
    no ingestion job runs. The ``expected_next_release`` column holds the stale-after bound implied by the
    tolerance, not an official release calendar date.
    """
    today = today or utcnow().date()
    prior = conn.execute(
        text("SELECT latest_observation_date, last_success_at, latest_observation_retrieved_at FROM mi_data_freshness WHERE source_id = :s AND dataset = :d"),
        {"s": source_id, "d": dataset},
    ).mappings().first()
    effective_latest = latest_observation
    if effective_latest is None and prior is not None:
        effective_latest = prior["latest_observation_date"]
    elif prior is not None and prior["latest_observation_date"] is not None and latest_observation is not None:
        effective_latest = max(latest_observation, prior["latest_observation_date"])
    retrieved = latest_observation_retrieved_at
    if retrieved is None and prior is not None and (latest_observation is None or effective_latest == prior["latest_observation_date"]):
        retrieved = prior["latest_observation_retrieved_at"]
    if retrieved is None and success and latest_observation is not None:
        retrieved = utcnow()
    assessment = assess_freshness(effective_latest, cadence, today)
    conn.execute(
        text(
            """
            INSERT INTO mi_data_freshness (
                source_id, dataset, last_attempt_at, last_success_at, latest_observation_date, expected_cadence,
                tolerance_days, expected_next_release, transport_status, freshness_status, last_error_redacted,
                last_run_id, updated_at, freshness_policy_version, latest_observation_retrieved_at, metadata_status
            ) VALUES (
                :s, :d, NOW(), CASE WHEN :success THEN NOW() ELSE NULL END, :latest, :cadence,
                :tol, :next_release, :transport, :fresh, :err, :run_id, NOW(), :policy, :retrieved, :meta
            )
            ON CONFLICT (source_id, dataset) DO UPDATE SET
                last_attempt_at = NOW(),
                last_success_at = CASE WHEN :success THEN NOW() ELSE mi_data_freshness.last_success_at END,
                latest_observation_date = :latest,
                expected_cadence = :cadence,
                tolerance_days = :tol,
                expected_next_release = :next_release,
                transport_status = :transport,
                freshness_status = :fresh,
                last_error_redacted = :err,
                last_run_id = :run_id,
                updated_at = NOW(),
                freshness_policy_version = :policy,
                latest_observation_retrieved_at = COALESCE(:retrieved, mi_data_freshness.latest_observation_retrieved_at),
                metadata_status = COALESCE(:meta, mi_data_freshness.metadata_status)
            """
        ),
        {
            "s": source_id,
            "d": dataset,
            "success": bool(success),
            "latest": effective_latest,
            "cadence": cadence,
            "tol": assessment.tolerance_days,
            "next_release": assessment.stale_after,
            "transport": transport_status,
            "fresh": assessment.status,
            "err": error_redacted,
            "run_id": run_id,
            "policy": FRESHNESS_POLICY_VERSION,
            "retrieved": retrieved,
            "meta": metadata_status,
        },
    )
    return assessment.status


__all__ = [
    "ObservationInput",
    "PUBLICATION_PUBLISHED",
    "PUBLICATION_QUARANTINED",
    "PUBLICATION_UNVALIDATED",
    "quarantine_observations",
    "RUN_ATTEMPTED",
    "RUN_FAILED",
    "RUN_PARTIAL",
    "RUN_SKIPPED",
    "RUN_SUCCEEDED",
    "TRANSPORT_FAILED",
    "TRANSPORT_OK",
    "TRANSPORT_SKIPPED",
    "UpsertCounts",
    "current_observations",
    "finish_run",
    "latest_observation_date",
    "new_run_id",
    "record_freshness",
    "start_run",
    "upsert_macro_series",
    "upsert_observations",
    "upsert_source_registry",
    "utcnow",
]
