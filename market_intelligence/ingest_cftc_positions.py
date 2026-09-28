"""Ingest CFTC TFF and Disaggregated futures-only positions.

The legacy futures-only client is unchanged. This writer adds long-form
category rows. Incremental refresh keeps the newest reports. Full mode pages
the official history for the mapped contract codes.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text

from market_intelligence.cftc_positions import CONTRACTS, DISAGG_RESOURCE, TFF_RESOURCE, parse_report_rows
from market_intelligence.nulls import canonical_sha256
from market_intelligence.store import RUN_FAILED, RUN_SUCCEEDED, coverage_with_provider_latest, finish_run, record_freshness, start_run

SOURCE_ID = "CFTC_COT"
DATASET = "tff_disaggregated_positions"
PAGE_SIZE = 5000
INCREMENTAL_LIMIT = 400
USER_AGENT = "FMP_SCREENER MarketIntelligence (public COT ingest)"


@dataclass
class CftcPositionReport:
    status: str
    rows_written: int = 0
    rejected: int = 0
    latest_position_date: date | None = None
    failed: bool = False
    error: str | None = None
    coverage: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rows_written": self.rows_written,
            "rejected": self.rejected,
            "latest_position_date": self.latest_position_date.isoformat() if self.latest_position_date else None,
            "failed": self.failed,
            "error": self.error,
            "coverage": list(self.coverage),
        }


def _codes(resource: str) -> list[str]:
    return [row.contract_code for row in CONTRACTS if row.resource == resource]


def fetch_resource(resource: str, *, mode: str, opener=None) -> list[dict[str, Any]]:
    client = opener or urllib.request.urlopen
    codes = _codes(resource)
    quoted = ",".join("'{0}'".format(code) for code in codes)
    where = "cftc_contract_market_code in ({0})".format(quoted)
    rows: list[dict[str, Any]] = []
    offset = 0
    limit = INCREMENTAL_LIMIT if mode != "full" else PAGE_SIZE
    while True:
        query = urllib.parse.urlencode(
            {
                "$where": where,
                "$order": "report_date_as_yyyy_mm_dd DESC" if mode != "full" else "report_date_as_yyyy_mm_dd",
                "$limit": str(limit),
                "$offset": str(offset),
            }
        )
        req = urllib.request.Request(
            "https://publicreporting.cftc.gov/resource/{0}.json?{1}".format(resource, query),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        with client(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError("CFTC payload is not a list")
        rows.extend(payload)
        if mode != "full" or len(payload) < limit:
            break
        offset += len(payload)
    return rows


def upsert_position_rows(conn, rows: list[dict[str, Any]], *, run_id: str) -> int:
    written = 0
    retrieved = datetime.now(timezone.utc)
    for row in rows:
        digest = canonical_sha256(
            {
                "code": row["cftc_contract_market_code"],
                "date": row["position_date"].isoformat(),
                "category": row["trader_category"],
                "long": row["long_contracts"],
                "short": row["short_contracts"],
                "oi": row["open_interest"],
            }
        )
        conn.execute(
            text(
                """
                INSERT INTO mi_cftc_position_observations (
                    source_id, report_family, cftc_contract_market_code, market_key, market_name,
                    asset_group, position_date, scheduled_publication_date, trader_category,
                    long_contracts, short_contracts, open_interest, payload_hash, retrieved_at, ingestion_run_id
                ) VALUES (
                    :source_id, :report_family, :code, :market_key, :market_name, :asset_group,
                    :position_date, :published, :category, :long_contracts, :short_contracts, :open_interest,
                    :payload_hash, :retrieved_at, :run_id
                )
                ON CONFLICT (source_id, report_family, cftc_contract_market_code, position_date, trader_category)
                DO UPDATE SET
                    long_contracts = EXCLUDED.long_contracts,
                    short_contracts = EXCLUDED.short_contracts,
                    open_interest = EXCLUDED.open_interest,
                    scheduled_publication_date = EXCLUDED.scheduled_publication_date,
                    payload_hash = EXCLUDED.payload_hash,
                    retrieved_at = EXCLUDED.retrieved_at,
                    ingestion_run_id = EXCLUDED.ingestion_run_id
                """
            ),
            {
                "source_id": SOURCE_ID,
                "report_family": row["report_family"],
                "code": row["cftc_contract_market_code"],
                "market_key": row["market_key"],
                "market_name": row["market_name"],
                "asset_group": row["asset_group"],
                "position_date": row["position_date"],
                "published": row["scheduled_publication_date"],
                "category": row["trader_category"],
                "long_contracts": row["long_contracts"],
                "short_contracts": row["short_contracts"],
                "open_interest": row["open_interest"],
                "payload_hash": digest,
                "retrieved_at": retrieved,
                "run_id": run_id,
            },
        )
        written += 1
    return written


def position_coverage(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT market_key, report_family, cftc_contract_market_code,
                   MIN(position_date) AS earliest, MAX(position_date) AS latest,
                   COUNT(DISTINCT position_date) AS reports,
                   COUNT(*) AS rows,
                   STRING_AGG(DISTINCT trader_category, ',' ORDER BY trader_category) AS categories
            FROM mi_cftc_position_observations
            GROUP BY market_key, report_family, cftc_contract_market_code
            ORDER BY market_key
            """
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def ingest_cftc_positions(engine, *, parent_run_id: str | None = None, today: date | None = None, mode: str = "incremental", opener=None) -> CftcPositionReport:
    with engine.begin() as conn:
        run_id = start_run(conn, source_id=SOURCE_ID, dataset=DATASET, parent_run_id=parent_run_id)
        try:
            raw = fetch_resource(TFF_RESOURCE, mode=mode, opener=opener) + fetch_resource(DISAGG_RESOURCE, mode=mode, opener=opener)
            parsed, rejected = parse_report_rows(raw)
            written = upsert_position_rows(conn, parsed, run_id=run_id)
            latest = max((row["position_date"] for row in parsed), default=None)
            coverage = position_coverage(conn) if mode == "full" else []
            finish_run(conn, run_id, status=RUN_SUCCEEDED, counts={"inserted": written, "rejected": rejected}, details={"mode": mode})
            record_freshness(
                conn,
                source_id=SOURCE_ID,
                dataset=DATASET,
                cadence="W",
                transport_status="OK",
                latest_observation=latest,
                success=True,
                error_redacted=None,
                run_id=run_id,
                today=today,
                coverage_json=coverage_with_provider_latest(latest, provider="CFTC"),
            )
        except Exception as exc:  # noqa: BLE001
            finish_run(conn, run_id, status=RUN_FAILED, error_redacted=exc.__class__.__name__)
            record_freshness(
                conn,
                source_id=SOURCE_ID,
                dataset=DATASET,
                cadence="W",
                transport_status="FAILED",
                latest_observation=None,
                success=False,
                error_redacted=exc.__class__.__name__,
                run_id=run_id,
                today=today,
            )
            return CftcPositionReport(status=RUN_FAILED, failed=True, error=exc.__class__.__name__)
    return CftcPositionReport(status=RUN_SUCCEEDED, rows_written=written, rejected=rejected, latest_position_date=latest, coverage=coverage)
