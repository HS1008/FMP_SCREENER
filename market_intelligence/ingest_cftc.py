"""Ingest public CFTC COT watchlist rows into PostgreSQL."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import text

from market_intelligence.cftc_client import CFTC_ATTRIBUTION, CftcClient
from market_intelligence.nulls import canonical_sha256
from market_intelligence.store import RUN_FAILED, RUN_SUCCEEDED, coverage_with_provider_latest, finish_run, record_freshness, start_run

SOURCE_ID = "CFTC_COT"
DATASET = "commitment_of_traders"


@dataclass
class CftcIngestReport:
    status: str
    rows_written: int = 0
    latest_observation: date | None = None
    markets: list[str] = field(default_factory=list)
    failed: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rows_written": self.rows_written,
            "latest_observation": self.latest_observation.isoformat() if self.latest_observation else None,
            "markets": list(self.markets),
            "failed": self.failed,
            "error": self.error,
        }


def ingest_cftc(engine, client: CftcClient | None = None, *, parent_run_id: str | None = None, today: date | None = None) -> CftcIngestReport:
    client = client or CftcClient()
    try:
        rows = client.latest_rows()
    except Exception as exc:  # noqa: BLE001
        with engine.begin() as conn:
            run_id = start_run(conn, source_id=SOURCE_ID, dataset=DATASET, parent_run_id=parent_run_id)
            finish_run(conn, run_id, status=RUN_FAILED, error_redacted=exc.__class__.__name__)
            record_freshness(conn, source_id=SOURCE_ID, dataset=DATASET, cadence="W", transport_status="FAILED", latest_observation=None, success=False, error_redacted=exc.__class__.__name__, run_id=run_id, today=today)
        return CftcIngestReport(status=RUN_FAILED, failed=True, error=exc.__class__.__name__)
    written = 0
    latest = None
    markets: set[str] = set()
    with engine.begin() as conn:
        run_id = start_run(conn, source_id=SOURCE_ID, dataset=DATASET, parent_run_id=parent_run_id)
        for row in rows:
            digest = canonical_sha256(row)
            conn.execute(
                text(
                    """
                    INSERT INTO mi_cftc_cot_observations (
                        source_id, market, report_date, open_interest, noncomm_long, noncomm_short,
                        noncomm_net, comm_long, comm_short, commodity_name, report_type,
                        payload_hash, retrieved_at, ingestion_run_id
                    ) VALUES (
                        :source_id, :market, :report_date, :open_interest, :noncomm_long, :noncomm_short,
                        :noncomm_net, :comm_long, :comm_short, :commodity_name, :report_type,
                        :payload_hash, NOW(), :run_id
                    )
                    ON CONFLICT (source_id, market, report_date) DO UPDATE SET
                        open_interest = EXCLUDED.open_interest,
                        noncomm_long = EXCLUDED.noncomm_long,
                        noncomm_short = EXCLUDED.noncomm_short,
                        noncomm_net = EXCLUDED.noncomm_net,
                        comm_long = EXCLUDED.comm_long,
                        comm_short = EXCLUDED.comm_short,
                        commodity_name = EXCLUDED.commodity_name,
                        report_type = EXCLUDED.report_type,
                        payload_hash = EXCLUDED.payload_hash,
                        last_seen_at = NOW()
                    """
                ),
                {
                    "source_id": SOURCE_ID,
                    "market": row["market"],
                    "report_date": row["report_date"],
                    "open_interest": row["open_interest"],
                    "noncomm_long": row["noncomm_long"],
                    "noncomm_short": row["noncomm_short"],
                    "noncomm_net": row["noncomm_net"],
                    "comm_long": row["comm_long"],
                    "comm_short": row["comm_short"],
                    "commodity_name": row["commodity_name"],
                    "report_type": row["report_type"],
                    "payload_hash": digest,
                    "run_id": run_id,
                },
            )
            written += 1
            markets.add(str(row["market"]))
            obs = date.fromisoformat(row["report_date"])
            if latest is None or obs > latest:
                latest = obs
        finish_run(conn, run_id, status=RUN_SUCCEEDED, counts={"received": len(rows), "inserted": written}, details={"attribution": CFTC_ATTRIBUTION, "markets": sorted(markets)})
        record_freshness(conn, source_id=SOURCE_ID, dataset=DATASET, cadence="W", transport_status="OK", latest_observation=latest, success=True, error_redacted=None, run_id=run_id, today=today, coverage_json=coverage_with_provider_latest(latest, provider="CFTC"))
    return CftcIngestReport(status=RUN_SUCCEEDED, rows_written=written, latest_observation=latest, markets=sorted(markets))
