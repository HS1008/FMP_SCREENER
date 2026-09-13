"""Write official Treasury XML observations into canonical MI tables."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text

from market_intelligence import CODE_VERSION
from market_intelligence.catalog import EXPORT_ATTRIBUTION_REQUIRED
from market_intelligence.store import (
    ObservationInput,
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_SUCCEEDED,
    TRANSPORT_FAILED,
    TRANSPORT_OK,
    finish_run,
    record_freshness,
    start_run,
    upsert_macro_series,
    upsert_observations,
    upsert_source_registry,
    utcnow,
)
from market_intelligence.treasury_xml import (
    NOMINAL_FIELDS,
    REAL_FIELDS,
    TreasuryPoint,
    TreasuryXmlClient,
)

TREASURY_SOURCE_ID = "TREASURY"
TREASURY_ATTRIBUTION = (
    "U.S. Department of the Treasury Daily Treasury Par Yield Curve Rates "
    "(XML feed). Indicative quotations near 3:30 p.m. ET; not a live quote."
)
TREASURY_CATALOG_VERSION = "treasury_xml_v1"


@dataclass
class TreasuryIngestReport:
    status: str = RUN_SUCCEEDED
    points_received: int = 0
    series_written: int = 0
    inserted: int = 0
    revised: int = 0
    unchanged: int = 0
    rejected: int = 0
    latest_observation: date | None = None
    failed: bool = False
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "points_received": self.points_received,
            "series_written": self.series_written,
            "inserted": self.inserted,
            "revised": self.revised,
            "unchanged": self.unchanged,
            "rejected": self.rejected,
            "latest_observation": self.latest_observation.isoformat() if self.latest_observation else None,
            "failed": self.failed,
            "error": self.error,
            "details": self.details,
        }


def _ensure_source(conn) -> None:
    upsert_source_registry(
        conn,
        [
            {
                "source_id": TREASURY_SOURCE_ID,
                "provider": "U.S. Department of the Treasury",
                "dataset": "daily_treasury_yield_curve_xml",
                "source_url": "https://home.treasury.gov/treasury-daily-interest-rate-xml-feed",
                "expected_cadence": "D",
                "usage_scope": EXPORT_ATTRIBUTION_REQUIRED,
                "attribution": TREASURY_ATTRIBUTION,
                "terms_notes": "Official daily XML par yields. Distinct from Fiscal Data JSON and FRED DGS*.",
                "units_metadata": {"yield": "percent"},
            }
        ],
        enabled={TREASURY_SOURCE_ID: True},
        access={TREASURY_SOURCE_ID: "CONFIGURED"},
    )


def _ensure_series(conn, point: TreasuryPoint) -> None:
    upsert_macro_series(
        conn,
        series_id=point.series_id,
        source_id=TREASURY_SOURCE_ID,
        provider_series_id=point.field_name,
        spec_fields={
            "category": "rates",
            "subcategory": "nominal_curve" if point.curve == "nominal" else "real_yield",
            "export_scope": EXPORT_ATTRIBUTION_REQUIRED,
            "catalog_version": TREASURY_CATALOG_VERSION,
            "source_url": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve",
            "catalog_units": "percent",
            "catalog_label": "{0} Treasury {1}".format(point.tenor, "par yield" if point.curve == "nominal" else "real yield"),
            "aggregation": "point_observation",
            "display_divisor": None,
            "display_units": None,
        },
        meta={
            "id": point.field_name,
            "units": "percent",
            "frequency_short": "D",
            "seasonal_adjustment_short": "NSA",
            "title": point.series_id,
        },
        metadata_status="VALIDATED",
        mismatches=[],
    )


def _record_provider_row(conn, point: TreasuryPoint, *, retrieved_at: datetime, run_id: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mi_provider_observations (
                source_id, provider_series_id, canonical_series_id, fred_equivalent,
                observation_date, value, raw_value, units, curve, tenor,
                retrieved_at, first_seen_at, release_time, revision_seq, is_current,
                selection_reason, ingestion_run_id, details_json
            ) VALUES (
                :source_id, :provider_series_id, :canonical_series_id, :fred_equivalent,
                :observation_date, :value, :raw_value, 'percent', :curve, :tenor,
                :retrieved_at, :retrieved_at, NULL, 1, TRUE,
                'treasury_xml', :run_id, CAST(:details AS JSONB)
            )
            ON CONFLICT (source_id, provider_series_id, observation_date, revision_seq)
            DO UPDATE SET
                last_seen_at = EXCLUDED.retrieved_at,
                value = CASE
                    WHEN mi_provider_observations.value IS NOT DISTINCT FROM EXCLUDED.value
                    THEN mi_provider_observations.value
                    ELSE EXCLUDED.value
                END,
                raw_value = EXCLUDED.raw_value,
                is_current = TRUE
            """
        ),
        {
            "source_id": TREASURY_SOURCE_ID,
            "provider_series_id": point.field_name,
            "canonical_series_id": point.series_id,
            "fred_equivalent": point.fred_equivalent,
            "observation_date": point.observation_date,
            "value": point.value,
            "raw_value": point.raw_value,
            "curve": point.curve,
            "tenor": point.tenor,
            "retrieved_at": retrieved_at,
            "run_id": run_id,
            "details": '{"feed":"treasury_daily_xml"}',
        },
    )


def ingest_treasury(
    engine,
    client: TreasuryXmlClient | None = None,
    *,
    today: date | None = None,
    parent_run_id: str | None = None,
    lookback_months: int = 2,
) -> TreasuryIngestReport:
    today = today or utcnow().date()
    client = client or TreasuryXmlClient()
    report = TreasuryIngestReport()
    retrieved_at = utcnow()
    try:
        points = client.fetch_recent(today=today, lookback_months=lookback_months)
    except Exception as exc:  # noqa: BLE001
        report.failed = True
        report.status = RUN_FAILED
        report.error = exc.__class__.__name__
        with engine.begin() as conn:
            _ensure_source(conn)
            rid = start_run(conn, source_id=TREASURY_SOURCE_ID, dataset="daily_treasury_xml", parent_run_id=parent_run_id)
            record_freshness(
                conn,
                source_id=TREASURY_SOURCE_ID,
                dataset="daily_treasury_xml",
                cadence="D",
                transport_status=TRANSPORT_FAILED,
                latest_observation=None,
                success=False,
                error_redacted=exc.__class__.__name__,
                run_id=rid,
                today=today,
                series_id="UST_NOM_10Y",
            )
            finish_run(conn, rid, status=RUN_FAILED, error_redacted=exc.__class__.__name__)
        return report

    report.points_received = len(points)
    by_series: dict[str, list[TreasuryPoint]] = defaultdict(list)
    for point in points:
        by_series[point.series_id].append(point)
        if report.latest_observation is None or point.observation_date > report.latest_observation:
            if point.value is not None:
                report.latest_observation = point.observation_date

    with engine.begin() as conn:
        _ensure_source(conn)
        rid = start_run(
            conn,
            source_id=TREASURY_SOURCE_ID,
            dataset="daily_treasury_xml",
            parent_run_id=parent_run_id,
            request_window=(date(today.year, today.month, 1), today),
        )
        for series_id, series_points in by_series.items():
            # Last point per date wins; value change becomes a revision via upsert.
            latest_by_date: dict[date, TreasuryPoint] = {}
            for point in series_points:
                latest_by_date[point.observation_date] = point
            sample = next(iter(latest_by_date.values()))
            _ensure_series(conn, sample)
            rows = [
                ObservationInput(observation_date=p.observation_date, raw_value=p.raw_value)
                for p in latest_by_date.values()
            ]
            counts = upsert_observations(
                conn,
                series_id=series_id,
                rows=rows,
                retrieved_at=retrieved_at,
                run_id=rid,
                today=today,
            )
            report.inserted += counts.inserted
            report.revised += counts.revised
            report.unchanged += counts.unchanged
            report.rejected += counts.rejected
            report.series_written += 1
            for point in latest_by_date.values():
                # SAVEPOINT so a missing mi_provider_observations table (pre-026 schema or
                # SQLite unit tests) cannot poison the enclosing PostgreSQL transaction.
                nested = conn.begin_nested()
                try:
                    _record_provider_row(conn, point, retrieved_at=retrieved_at, run_id=rid)
                    nested.commit()
                except Exception:  # noqa: BLE001
                    nested.rollback()
                    report.details["provider_rows_skipped"] = int(report.details.get("provider_rows_skipped") or 0) + 1
        record_freshness(
            conn,
            source_id=TREASURY_SOURCE_ID,
            dataset="daily_treasury_xml",
            cadence="D",
            transport_status=TRANSPORT_OK,
            latest_observation=report.latest_observation,
            success=True,
            error_redacted=None,
            run_id=rid,
            today=today,
            series_id="UST_NOM_10Y",
        )
        report.status = RUN_PARTIAL if report.rejected else RUN_SUCCEEDED
        finish_run(
            conn,
            rid,
            status=report.status,
            counts={"inserted": report.inserted, "revised": report.revised, "unchanged": report.unchanged, "rejected": report.rejected},
            details={"code_version": CODE_VERSION, "catalog_version": TREASURY_CATALOG_VERSION, "series": report.series_written},
        )
    report.details["code_version"] = CODE_VERSION
    return report


__all__ = ["TREASURY_ATTRIBUTION", "TREASURY_SOURCE_ID", "TreasuryIngestReport", "ingest_treasury"]
