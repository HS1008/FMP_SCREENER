"""EIA v2 ingest. Requires EIA_API_KEY. Prices stay on FRED; this stores inventory/production."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

from sqlalchemy import text

from market_intelligence.nulls import canonical_sha256
from market_intelligence.store import RUN_FAILED, RUN_SKIPPED, RUN_SUCCEEDED, coverage_with_provider_latest, finish_run, record_freshness, start_run

SOURCE_ID = "EIA_ENERGY"
DATASET = "petroleum_and_gas_statistics"
EIA_BASE = "https://api.eia.gov/v2/"
# Weekly petroleum stocks, Cushing stocks, weekly crude production, and working gas.
# Facet ids match the EIA v2 routes already used for WCESTUS1 and weekly gas storage.
EIA_SERIES = (
    ("petroleum/stoc/wstk/data", "WCESTUS1", "crude_stocks"),
    ("petroleum/stoc/wstk/data", "WCESTCUS1", "cushing_crude_stocks"),
    ("petroleum/sum/sndw/data", "WCRFPUS2", "crude_production"),
    ("natural-gas/stor/wkly/data", "NW2_EPG0_SWO_R48_BCF", "working_gas_storage"),
)
INCREMENTAL_LENGTH = 52
MAX_PAGE_LENGTH = 5000


@dataclass
class EiaIngestReport:
    status: str
    rows_written: int = 0
    latest_observation: date | None = None
    series: list[str] = field(default_factory=list)
    failed: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rows_written": self.rows_written,
            "latest_observation": self.latest_observation.isoformat() if self.latest_observation else None,
            "series": list(self.series),
            "failed": self.failed,
            "error": self.error,
        }


def api_key_from_env(env: Mapping[str, str] | None = None) -> str | None:
    import os

    raw = (env or os.environ).get("EIA_API_KEY")
    key = str(raw or "").strip()
    return key or None


def _candidate_queries(route: str, series_id: str, alias: str) -> list[tuple[str, dict[str, str]]]:
    """Primary series facet, then documented alternates when that facet is empty.

    Cushing weekly stocks are published as WCESTCUS1 and also as the
    duoarea/product/process combination on the same stocks route. Weekly
    crude production is WCRFPUS2 on the supply summary route.
    """
    series_facet = {"facets[series][]": series_id}
    queries = [(route, series_facet)]
    if alias == "cushing_crude_stocks":
        queries.append(
            (
                "petroleum/stoc/wstk/data",
                {"facets[duoarea][]": "YCUOK", "facets[product][]": "EPC0", "facets[process][]": "SAX"},
            )
        )
        queries.append(("seriesid/PET.WCESTCUS1.W", {}))
    elif alias == "crude_production":
        queries.append(("petroleum/crd/crpdn/data", series_facet))
        queries.append(("seriesid/PET.WCRFPUS2.W", {}))
    return queries


def _page(route: str, facets: Mapping[str, str], key: str, *, length: int, offset: int) -> list[dict[str, Any]]:
    params = {
        "api_key": key,
        "frequency": "weekly",
        "data[0]": "value",
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": str(length),
        "offset": str(offset),
    }
    params.update(facets)
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        "{0}{1}?{2}".format(EIA_BASE, route, query),
        headers={"User-Agent": "FMP_SCREENER MarketIntelligence", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return list(((payload.get("response") or {}).get("data")) or [])


def _fetch_series(route: str, series_id: str, key: str, *, history: str, alias: str) -> list[dict[str, Any]]:
    length = MAX_PAGE_LENGTH if history == "max" else INCREMENTAL_LENGTH
    last_error = "EIA empty_or_error_payload for {0}".format(series_id)
    for attempt_route, facets in _candidate_queries(route, series_id, alias):
        offset = 0
        rows: list[dict[str, Any]] = []
        try:
            while True:
                points = _page(attempt_route, facets, key, length=length, offset=offset)
                if not points and offset == 0:
                    break
                rows.extend(points)
                if history != "max" or len(points) < length:
                    break
                offset += len(points)
        except Exception as exc:  # noqa: BLE001
            last_error = "{0} {1}".format(series_id, exc.__class__.__name__)
            continue
        if rows:
            return rows
    raise RuntimeError(last_error)


def ingest_eia(engine, *, env: Mapping[str, str] | None = None, parent_run_id: str | None = None, today: date | None = None, history: str = "incremental") -> EiaIngestReport:
    key = api_key_from_env(env)
    if not key:
        with engine.begin() as conn:
            run_id = start_run(conn, source_id=SOURCE_ID, dataset=DATASET, parent_run_id=parent_run_id)
            finish_run(conn, run_id, status=RUN_SKIPPED, details={"reason": "EIA_API_KEY missing"})
            record_freshness(conn, source_id=SOURCE_ID, dataset=DATASET, cadence="W", transport_status="SKIPPED", latest_observation=None, success=False, error_redacted="EIA_API_KEY missing; register at https://www.eia.gov/opendata/", run_id=run_id, today=today)
        return EiaIngestReport(status=RUN_SKIPPED, error="EIA_API_KEY missing")
    written = 0
    latest = None
    seen: list[str] = []
    errors: list[str] = []
    with engine.begin() as conn:
        run_id = start_run(conn, source_id=SOURCE_ID, dataset=DATASET, parent_run_id=parent_run_id)
        try:
            for route, series_id, alias in EIA_SERIES:
                try:
                    points = _fetch_series(route, series_id, key, history=history, alias=alias)
                except Exception as exc:  # noqa: BLE001
                    errors.append("{0}:{1}".format(alias, str(exc)[:160]))
                    continue
                seen.append(alias)
                for point in points:
                    period = str(point.get("period") or "")[:10]
                    if not period:
                        continue
                    value = point.get("value")
                    digest = canonical_sha256({"series": series_id, "period": period, "value": value})
                    conn.execute(
                        text(
                            """
                            INSERT INTO mi_eia_observations (source_id, series_id, observation_date, value, units, payload_hash, ingestion_run_id)
                            VALUES (:source_id, :series_id, :observation_date, :value, :units, :payload_hash, :run_id)
                            ON CONFLICT (source_id, series_id, observation_date) DO UPDATE SET
                                value = EXCLUDED.value,
                                units = EXCLUDED.units,
                                payload_hash = EXCLUDED.payload_hash,
                                last_seen_at = NOW()
                            """
                        ),
                        {
                            "source_id": SOURCE_ID,
                            "series_id": alias,
                            "observation_date": period,
                            "value": value,
                            "units": point.get("units"),
                            "payload_hash": digest,
                            "run_id": run_id,
                        },
                    )
                    written += 1
                    obs = date.fromisoformat(period)
                    if latest is None or obs > latest:
                        latest = obs
            if errors:
                detail = "; ".join(errors)[:240]
                finish_run(conn, run_id, status=RUN_FAILED, error_redacted=detail, counts={"inserted": written}, details={"series": seen, "errors": errors})
                record_freshness(conn, source_id=SOURCE_ID, dataset=DATASET, cadence="W", transport_status="FAILED", latest_observation=latest, success=False, error_redacted=detail, run_id=run_id, today=today)
                return EiaIngestReport(status=RUN_FAILED, rows_written=written, latest_observation=latest, series=seen, failed=True, error=detail)
            finish_run(conn, run_id, status=RUN_SUCCEEDED, counts={"inserted": written}, details={"series": seen})
            record_freshness(conn, source_id=SOURCE_ID, dataset=DATASET, cadence="W", transport_status="OK", latest_observation=latest, success=True, error_redacted=None, run_id=run_id, today=today, coverage_json=coverage_with_provider_latest(latest, provider="EIA"))
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)[:160]
            finish_run(conn, run_id, status=RUN_FAILED, error_redacted=detail)
            record_freshness(conn, source_id=SOURCE_ID, dataset=DATASET, cadence="W", transport_status="FAILED", latest_observation=latest, success=False, error_redacted=detail, run_id=run_id, today=today)
            return EiaIngestReport(status=RUN_FAILED, rows_written=written, latest_observation=latest, series=seen, failed=True, error=detail)
    return EiaIngestReport(status=RUN_SUCCEEDED, rows_written=written, latest_observation=latest, series=seen)
