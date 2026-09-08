"""Deterministic ``morning_context_v1`` snapshots built from canonical PostgreSQL.

The builder reads every input inside one REPEATABLE READ transaction (a consistent cutoff)
so concurrent ingestion cannot mix half-published batches, assembles a strict-JSON body,
hashes it (SHA-256 of the body excluding ``artifact_sha256``) and publishes an immutable
row keyed by that hash. Re-running with identical inputs and the same explicit
``generated_at`` reproduces the same hash and publishes nothing new. Old snapshots are
never modified; historical snapshots are never recalculated with revised data.

``generated_at`` is when the builder ran; ``cutoff_at`` is the DB read point; every
metric keeps its own observation date. A single as_of never implies all inputs are equally
current. No LLM is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text

from market_intelligence import CODE_VERSION
from market_intelligence.catalog import CATALOG_VERSION
from market_intelligence.freshness import FRESHNESS_POLICY_VERSION
from market_intelligence.nulls import canonical_sha256, normalize_payload, strict_dumps
from market_intelligence.read_models import (
    credit_context,
    data_health_context,
    industries_context,
    macro_context,
    rates_context,
    recent_runs,
    sectors_context,
    strategies_context,
)
from market_intelligence.transforms import TRANSFORM_VERSION

SCHEMA_VERSION = "morning_context_v1"
SECTION_ORDER = (
    "data_health",
    "market",
    "macro",
    "rates",
    "liquidity",
    "credit",
    "sectors",
    "industries",
    "strategy_monitor_summary",
)


@dataclass
class BuildResult:
    snapshot_id: str
    snapshot_sha256: str
    completeness: str
    sections_status: dict[str, dict[str, Any]]
    published_new: bool
    generated_at: str
    cutoff_at: str
    as_of_date: str
    body: dict[str, Any] = field(default_factory=dict, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "PUBLISHED" if self.published_new else "ALREADY_PUBLISHED",
            "snapshot_id": self.snapshot_id,
            "snapshot_sha256": self.snapshot_sha256,
            "completeness": self.completeness,
            "sections_status": self.sections_status,
            "generated_at": self.generated_at,
            "cutoff_at": self.cutoff_at,
            "as_of_date": self.as_of_date,
        }


def _section(data: Any, *, empty_reason: str) -> dict[str, Any]:
    if data is None or data == {} or data == [] or (isinstance(data, dict) and all(v in (None, [], {}) for v in data.values())):
        return {"status": "UNAVAILABLE", "reason": empty_reason, "data": None}
    return {"status": "OK", "reason": None, "data": data}


def _market_section(sectors: dict[str, Any], rates: dict[str, Any]) -> dict[str, Any]:
    rs_rows = (sectors.get("datasets") or {}).get("ETF_RS_VS_SPY") or []
    leadership = []
    for row in rs_rows:
        metrics = row.get("metrics") or {}
        leadership.append(
            {
                "sector_key": row["sector_key"],
                "entity_kind": row["entity_kind"],
                "instrument_id": row["instrument_id"],
                "as_of": row["as_of"],
                "rs_chg_1w": metrics.get("rs_chg_1w"),
                "rs_chg_1m": metrics.get("rs_chg_1m"),
                "rs_chg_3m": metrics.get("rs_chg_3m"),
                "ret_1m": metrics.get("ret_1m"),
                "benchmark": row.get("benchmark"),
                "value_basis": row.get("value_basis"),
                "export_scope": row.get("export_scope"),
            }
        )
    leadership.sort(key=lambda r: (r["rs_chg_1m"] is None, -(r["rs_chg_1m"] or 0.0)))
    ten_year = next((c for c in rates.get("curve", []) if c["tenor"] == "10Y"), None)
    return {
        "overnight_quotes": {"status": "UNAVAILABLE", "reason": "No live quote source is configured (IBKR market data disabled). Yesterday's close is not labeled overnight."},
        "sector_leadership_1m_rs_vs_spy": leadership,
        "us_10y": ten_year,
        "slopes_bps": rates.get("slopes"),
    }


def _liquidity_section(macro: dict[str, Any]) -> dict[str, Any] | None:
    liquidity = (macro.get("categories") or {}).get("liquidity")
    if not liquidity:
        return None
    return {
        "series": liquidity,
        "disclosure": "Balances differ in dating (Wednesday levels vs weekly averages vs daily) and scale (millions vs billions); no composite liquidity score is computed.",
    }


def build_snapshot_body(conn, *, generated_at: datetime, cutoff_at: datetime, generation_params: dict[str, Any]) -> dict[str, Any]:
    macro = macro_context(conn)
    rates = rates_context(conn)
    credit = credit_context(conn)
    sectors = sectors_context(conn)
    industries = industries_context(conn)
    health = data_health_context(conn)
    strategies = strategies_context(conn)
    runs = recent_runs(conn, limit=50)

    macro_categories = {k: v for k, v in (macro.get("categories") or {}).items() if k in {"growth", "labor", "inflation", "policy"}}
    sections = {
        "data_health": _section(health if health.get("sources") else None, empty_reason="No sources registered yet; run jobs.market_intelligence_refresh."),
        "market": _section(_market_section(sectors, rates) if (sectors.get("datasets") or rates.get("curve_observation_dates")) else None, empty_reason="No sector or rates data available."),
        "macro": _section({"categories": macro_categories, "series_without_data": macro.get("series_without_data"), "attribution": macro.get("attribution")} if macro_categories else None, empty_reason="No FRED macro observations stored (FRED_API_KEY not configured or refresh not run)."),
        "rates": _section(rates if rates.get("curve_observation_dates") else None, empty_reason="No Treasury curve observations stored."),
        "liquidity": _section(_liquidity_section(macro), empty_reason="No liquidity series stored."),
        "credit": _section(credit if credit.get("buckets") else None, empty_reason="No credit index snapshots stored."),
        "sectors": _section(sectors if sectors.get("datasets") else None, empty_reason="No sector snapshots stored (legacy bridge not run)."),
        "industries": _section(industries if industries.get("datasets") else None, empty_reason="No industry snapshots stored."),
        "strategy_monitor_summary": _section(strategies if strategies.get("strategies") else None, empty_reason="No research runs in PostgreSQL."),
    }
    sections_status = {name: {"status": sec["status"], "reason": sec["reason"]} for name, sec in sections.items()}
    completeness = "COMPLETE" if all(s["status"] == "OK" for s in sections.values()) else "PARTIAL"
    input_refs = {
        "source_freshness": [
            {k: h.get(k) for k in ("source_id", "freshness_dataset", "last_success_at", "latest_observation_date", "transport_status", "freshness_status")}
            for h in (health.get("sources") or [])
        ],
        "recent_ingestion_run_ids": [r["run_id"] for r in runs],
        "sector_artifact_hashes": sorted(r["artifact_sha256"] for items in (sectors.get("datasets") or {}).values() for r in items),
        "versions": {
            "code_version": CODE_VERSION,
            "catalog_version": CATALOG_VERSION,
            "transform_version": TRANSFORM_VERSION,
            "freshness_policy_version": FRESHNESS_POLICY_VERSION,
        },
    }
    body = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "cutoff_at": cutoff_at.isoformat(),
        "as_of_date": cutoff_at.date().isoformat(),
        "generation_params": generation_params,
        "semantics": {
            "generated_at": "wall-clock time the builder ran",
            "cutoff_at": "database read point (REPEATABLE READ); no input newer than this is included",
            "per_metric_dates": "every metric carries its own observation_date/as_of; a single as_of does not imply equal currency",
            "vintage": "FRED values are LATEST_REVISED, not point-in-time",
        },
        "completeness": completeness,
        "sections_status": sections_status,
        "sections": {name: sections[name] for name in SECTION_ORDER},
        "input_refs": input_refs,
        "excluded_by_policy": ["credentials", "account ids", "client holdings", "model binaries", "raw licensed datasets", "embargoed/holdout data"],
    }
    body = normalize_payload(body)
    body["artifact_sha256"] = canonical_sha256(body)
    return body


def build_and_publish(engine, *, parent_run_id: str | None = None, as_of: date | None = None, generated_at: datetime | None = None, created_by: str = "backend", extra_params: dict[str, Any] | None = None) -> BuildResult:
    generated_at = generated_at or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    cutoff_at = generated_at
    params = {"as_of_override": as_of.isoformat() if as_of else None, "parent_run_id": parent_run_id, **(extra_params or {})}
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with conn.begin():
            body = build_snapshot_body(conn, generated_at=generated_at, cutoff_at=cutoff_at, generation_params=params)
            sha = body["artifact_sha256"]
            snapshot_id = "mc_{0}".format(sha[:20])
            inserted = conn.execute(
                text(
                    """
                    INSERT INTO mi_morning_context_snapshots (
                        snapshot_id, schema_version, generated_at, cutoff_at, as_of_date, generation_params, input_refs,
                        sections_status, snapshot_json, snapshot_sha256, completeness, publication_state, created_by, created_at
                    ) VALUES (
                        :snapshot_id, :schema_version, :generated_at, :cutoff_at, :as_of_date, CAST(:params AS JSONB), CAST(:input_refs AS JSONB),
                        CAST(:sections_status AS JSONB), CAST(:body AS JSONB), :sha, :completeness, 'PUBLISHED', :created_by, NOW()
                    )
                    ON CONFLICT (snapshot_id) DO NOTHING
                    """
                ),
                {
                    "snapshot_id": snapshot_id,
                    "schema_version": SCHEMA_VERSION,
                    "generated_at": generated_at,
                    "cutoff_at": cutoff_at,
                    "as_of_date": cutoff_at.date(),
                    "params": strict_dumps(params),
                    "input_refs": strict_dumps(body["input_refs"]),
                    "sections_status": strict_dumps(body["sections_status"]),
                    "body": strict_dumps(body),
                    "sha": sha,
                    "completeness": body["completeness"],
                    "created_by": created_by,
                },
            ).rowcount
    return BuildResult(
        snapshot_id=snapshot_id,
        snapshot_sha256=sha,
        completeness=body["completeness"],
        sections_status=body["sections_status"],
        published_new=bool(inserted),
        generated_at=body["generated_at"],
        cutoff_at=body["cutoff_at"],
        as_of_date=body["as_of_date"],
        body=body,
    )


__all__ = ["SCHEMA_VERSION", "SECTION_ORDER", "BuildResult", "build_and_publish", "build_snapshot_body"]
