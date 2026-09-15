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
# Weekly petroleum stocks and working gas in storage.
EIA_SERIES = (
    ("petroleum/stoc/wstk/data", "WCESTUS1", "crude_stocks"),
    ("natural-gas/stor/wkly/data", "NW2_EPG0_SWO_R48_BCF", "working_gas_storage"),
)


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


def ingest_eia(engine, *, env: Mapping[str, str] | None = None, parent_run_id: str | None = None, today: date | None = None) -> EiaIngestReport:
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
    with engine.begin() as conn:
        run_id = start_run(conn, source_id=SOURCE_ID, dataset=DATASET, parent_run_id=parent_run_id)
        try:
            for route, series_id, alias in EIA_SERIES:
                query = urllib.parse.urlencode({"api_key": key, "frequency": "weekly", "data[0]": "value", "facets[series][]": series_id, "sort[0][column]": "period", "sort[0][direction]": "desc", "length": "52"})
                req = urllib.request.Request("{0}{1}?{2}".format(EIA_BASE, route, query), headers={"User-Agent": "FMP_SCREENER MarketIntelligence", "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                points = ((payload.get("response") or {}).get("data")) or []
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
            finish_run(conn, run_id, status=RUN_SUCCEEDED, counts={"inserted": written}, details={"series": seen})
            record_freshness(conn, source_id=SOURCE_ID, dataset=DATASET, cadence="W", transport_status="OK", latest_observation=latest, success=True, error_redacted=None, run_id=run_id, today=today, coverage_json=coverage_with_provider_latest(latest, provider="EIA"))
        except Exception as exc:  # noqa: BLE001
            finish_run(conn, run_id, status=RUN_FAILED, error_redacted=exc.__class__.__name__)
            record_freshness(conn, source_id=SOURCE_ID, dataset=DATASET, cadence="W", transport_status="FAILED", latest_observation=latest, success=False, error_redacted=exc.__class__.__name__, run_id=run_id, today=today)
            return EiaIngestReport(status=RUN_FAILED, rows_written=written, latest_observation=latest, series=seen, failed=True, error=exc.__class__.__name__)
    return EiaIngestReport(status=RUN_SUCCEEDED, rows_written=written, latest_observation=latest, series=seen)
