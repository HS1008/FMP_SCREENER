"""Deterministic ``morning_context_v2`` snapshots built from canonical PostgreSQL.

Current-only builder. Every input is read inside one REPEATABLE READ transaction; the
capture point (``cutoff_at``) is the database transaction timestamp of that read, not a
caller-supplied value. Historical reconstruction ("what would the snapshot have said on
date X") is refused: revised FRED history is not point-in-time knowledge, and selecting
observations by observation date alone would fabricate a vintage that never existed.
Retrieving an already *stored* old snapshot is a different operation (``morning_index``).

Hashing: ``artifact_sha256`` is SHA-256 of the strict-JSON body excluding only that key
(shared artifact convention). ``content_sha256`` additionally excludes the runtime
timestamps (``generated_at``/``cutoff_at``/``captured_health.evaluated_at``), so two runs
over the same frozen inputs are recognised as the same content and nothing new is published.
Identical inputs with different runtime timestamps therefore share ``content_sha256`` but
not ``artifact_sha256``; that is the honest definition of reproducibility here.

Snapshots are immutable once published. Corrections are new snapshots; supersession and
quality are recorded in separate metadata columns, never by editing the stored body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from market_intelligence import CODE_VERSION
from market_intelligence.catalog import CATALOG, CATALOG_BY_ID, CATALOG_VERSION, CREDIT_SERIES, CURVE_TENORS
from market_intelligence.freshness import FRESHNESS_POLICY_VERSION, assess_freshness
from market_intelligence.nulls import canonical_sha256, normalize_payload, strict_dumps
from market_intelligence.read_models import (
    credit_context,
    data_health_context,
    industries_context,
    macro_context,
    rates_context,
    sectors_context,
    strategies_context,
)
from market_intelligence.transforms import TRANSFORM_VERSION

SCHEMA_VERSION = "morning_context_v2"
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

SECTION_OK = "OK"
SECTION_PARTIAL = "PARTIAL"
SECTION_UNAVAILABLE = "UNAVAILABLE"
SECTION_STALE = "STALE"

COMPLETENESS_COMPLETE = "COMPLETE"
COMPLETENESS_PARTIAL = "PARTIAL"
COMPLETENESS_EMPTY = "EMPTY"

# Keys that never make a section "available" on their own (static text / policy notes / identity).
_STATIC_KEYS = {
    "attribution", "note", "notes", "units_note", "disclosure", "coverage_note", "metric_provenance_note",
    "semantics", "leadership_order", "units",
}
_IDENTITY_KEYS = {
    "metric_id", "series_id", "status", "units", "as_of", "observation_date", "label", "title", "category",
    "subcategory", "tenor", "export_scope", "source", "source_id", "frequency", "seasonal_adjustment",
    "vintage_kind", "pit_safe", "metadata_status", "publication_status", "publication_reason",
    "transform_version", "computed_at", "retrieved_at", "revision_seq", "ingestion_run_id",
    "sector_key", "entity_kind", "instrument_id", "canonical_sector", "provider_label", "benchmark",
    "return_basis", "value_basis", "universe_method", "research_eligible", "dataset", "industry_key",
    "aggregation", "catalog_units", "artifact_sha256", "schema_version", "methodology_version",
    "bucket", "window_observations", "percentile_window", "history_first_date", "history_status",
    "reason", "restriction_reason", "restricted", "display", "freshness", "coverage",
    "strategy_id", "research_kind", "research_mode", "asset_class", "research_status",
    "economic_gate", "promotion_gate", "holdout_status", "delivery_status",
}
_NUMERIC_VALUE_KEYS = {
    "value", "oas_bps", "yield_pct", "chg_prev_bps", "chg_1w_bps", "chg_1m_bps", "chg_3m_bps",
    "change_1d_bps", "change_1w_bps", "change_1m_bps", "change_3m_bps", "percentile", "zscore",
    "rs_chg_1w", "rs_chg_1m", "rs_chg_3m", "rs_chg_6m", "rs_chg_12m", "ret_1m", "ret_1w", "ret_3m",
}

# Per-section cadence used to judge captured staleness against the capture time (summary only).
_SECTION_CADENCE = {"market": "D", "rates": "D", "credit": "D", "sectors": "D", "industries": "D", "liquidity": "W", "macro": "M"}

# Catalog series that must be present and individually fresh for a section to be OK.
_MACRO_REQUIRED = tuple(s.series_id for s in CATALOG if s.category in {"growth", "labor", "inflation", "policy"})
_RATES_REQUIRED = tuple(CURVE_TENORS.values())
_LIQUIDITY_REQUIRED = tuple(s.series_id for s in CATALOG if s.category == "liquidity")
_CREDIT_REQUIRED = tuple(CREDIT_SERIES)

# Statuses that mean "this object is not numerical data" even when other strings are present.
_EMPTY_STATUSES = frozenset({"INSUFFICIENT_DATA", "UNAVAILABLE", "WITHDRAWN_OBSERVATION", "NULL_NO_MEMBERS", "NULL_INSUFFICIENT_HISTORY"})


class HistoricalReconstructionUnsupported(ValueError):
    """A snapshot for a past cutoff was requested; only current snapshots can be built."""


@dataclass
class BuildResult:
    snapshot_id: str
    snapshot_sha256: str
    content_sha256: str
    completeness: str
    sections_status: dict[str, dict[str, Any]]
    published_new: bool
    generated_at: str
    cutoff_at: str
    as_of_date: str
    superseded_snapshot_id: str | None = None
    body: dict[str, Any] = field(default_factory=dict, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "PUBLISHED" if self.published_new else "ALREADY_PUBLISHED",
            "snapshot_id": self.snapshot_id,
            "snapshot_sha256": self.snapshot_sha256,
            "content_sha256": self.content_sha256,
            "completeness": self.completeness,
            "sections_status": self.sections_status,
            "generated_at": self.generated_at,
            "cutoff_at": self.cutoff_at,
            "as_of_date": self.as_of_date,
            "superseded_snapshot_id": self.superseded_snapshot_id,
        }


# ---- section semantics --------------------------------------------------------------------

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _has_value(obj: Any) -> bool:
    """True when ``obj`` carries at least one numerical value (zero counts; labels/status/units do not)."""
    if obj is None or obj == "" or obj == [] or obj == {}:
        return False
    if isinstance(obj, bool):
        return False
    if isinstance(obj, (int, float)):
        return True
    if isinstance(obj, str):
        return False
    if isinstance(obj, dict):
        if obj.get("status") in _EMPTY_STATUSES and not any(_is_number(obj.get(k)) for k in _NUMERIC_VALUE_KEYS):
            return False
        if any(k in obj for k in _NUMERIC_VALUE_KEYS):
            if any(_is_number(obj.get(k)) for k in _NUMERIC_VALUE_KEYS):
                return True
            nested = {k: v for k, v in obj.items() if k not in _NUMERIC_VALUE_KEYS}
            return any(_has_value(v) for k, v in nested.items() if k not in _STATIC_KEYS and k not in _IDENTITY_KEYS)
        return any(_has_value(v) for k, v in obj.items() if k not in _STATIC_KEYS and k not in _IDENTITY_KEYS)
    if isinstance(obj, list):
        return any(_has_value(v) for v in obj)
    return False


def _dates_in(obj: Any, keys=("as_of", "observation_date")) -> list[date]:
    out: list[date] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str):
                try:
                    out.append(date.fromisoformat(v[:10]))
                except ValueError:
                    pass
            else:
                out.extend(_dates_in(v, keys))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_dates_in(v, keys))
    return out


def _presence(data: Any, *, required: list[str]) -> bool:
    """Non-numerical sections (health, strategy summary) are present when required keys exist."""
    if not isinstance(data, dict):
        return bool(data)
    return all(data.get(k) not in (None, [], {}) for k in required)


def _collect_series_blocks(data: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if isinstance(data, dict):
        if data.get("series_id") or data.get("metric_id") or data.get("tenor"):
            blocks.append(data)
        for key, value in data.items():
            if key in {"latest", "transforms", "metrics", "source", "freshness", "comparison", "display"}:
                continue
            blocks.extend(_collect_series_blocks(value))
    elif isinstance(data, list):
        for item in data:
            blocks.extend(_collect_series_blocks(item))
    return blocks


def _required_input_row(series_id: str, data: Any, capture_date: date) -> dict[str, Any]:
    spec = CATALOG_BY_ID.get(series_id)
    cadence = spec.expected_frequency if spec else None
    block = next((b for b in _collect_series_blocks(data) if b.get("series_id") == series_id), None)
    latest = (block or {}).get("latest") if isinstance((block or {}).get("latest"), dict) else {}
    obs = latest.get("observation_date") or (block or {}).get("observation_date") or (block or {}).get("as_of")
    obs_d = None
    if isinstance(obs, str):
        try:
            obs_d = date.fromisoformat(obs[:10])
        except ValueError:
            obs_d = None
    has_value = _has_value(block) if block is not None else False
    if block is None:
        state = "MISSING"
        freshness = None
    elif not has_value:
        state = "MISSING_OBS"
        freshness = assess_freshness(obs_d, cadence, capture_date) if obs_d is not None else None
    else:
        freshness = assess_freshness(obs_d, cadence, capture_date)
        state = "STALE" if freshness.status == "STALE" else "AVAILABLE"
    pub = (block or {}).get("publication_status")
    if pub == "QUARANTINED_METADATA":
        state = "METADATA_QUARANTINE"
    return {
        "series_id": series_id,
        "cadence": cadence,
        "observation_date": obs_d.isoformat() if obs_d else None,
        "has_value": has_value,
        "state": state,
        "freshness": None if freshness is None else {"status": freshness.status, "age_days": freshness.age_days, "tolerance_days": freshness.tolerance_days, "policy_version": FRESHNESS_POLICY_VERSION},
        "publication_status": pub,
    }


def _section_required_series(name: str) -> tuple[str, ...]:
    if name == "macro":
        return _MACRO_REQUIRED
    if name == "rates":
        return _RATES_REQUIRED
    if name == "liquidity":
        return _LIQUIDITY_REQUIRED
    if name == "credit":
        return _CREDIT_REQUIRED
    if name == "market":
        return ("DGS10",)
    return ()


def section_status(name: str, data: Any, *, required: list[str], capture_date: date, empty_reason: str, presence: str = "numeric") -> dict[str, Any]:
    """Availability = explicit numerical/presence contract + per-required-series freshness.

    ``presence`` is ``numeric`` (market values; zero counts, labels/status/units do not) or
    ``registry`` (data-health / strategy rows that carry statuses, not quotes).
    Required series are judged individually by their own cadence; one fresh series cannot
    conceal another required series that is missing or stale.
    """
    available = _presence(data, required=required) if presence == "registry" else _has_value(data)
    required_series = _section_required_series(name)
    inputs = [_required_input_row(sid, data, capture_date) for sid in required_series] if data is not None else [{"series_id": sid, "cadence": (CATALOG_BY_ID[sid].expected_frequency if sid in CATALOG_BY_ID else None), "observation_date": None, "has_value": False, "state": "MISSING", "freshness": None, "publication_status": None} for sid in required_series]
    missing_fields = [k for k in required if not (_presence(data, required=[k]) if presence == "registry" else _has_value(data.get(k) if isinstance(data, dict) else None))]
    missing_series = [i["series_id"] for i in inputs if i["state"] in {"MISSING", "MISSING_OBS", "METADATA_QUARANTINE"}]
    stale_series = [i["series_id"] for i in inputs if i["state"] == "STALE"]
    if not available:
        return {
            "status": SECTION_UNAVAILABLE,
            "reason": empty_reason,
            "data": None,
            "required_missing": list(required),
            "latest_observation_date": None,
            "captured_freshness": None,
            "required_inputs": inputs,
            "stale_required": stale_series,
            "missing_required": missing_series,
        }
    dates = _dates_in(data)
    latest = max(dates) if dates else None
    cadence = _SECTION_CADENCE.get(name)
    # Summary freshness is the worst required input (stale wins). Section cadence stays the declared default.
    if stale_series:
        worst = next(i for i in inputs if i["series_id"] == stale_series[0])
        freshness = assess_freshness(date.fromisoformat(worst["observation_date"]) if worst.get("observation_date") else None, worst.get("cadence") or cadence, capture_date)
    elif latest is not None:
        freshness = assess_freshness(latest, cadence, capture_date)
    else:
        freshness = None
    status = SECTION_OK if not missing_fields and not missing_series else SECTION_PARTIAL
    reasons = []
    if missing_fields:
        reasons.append("required fields without data: {0}".format(", ".join(missing_fields)))
    if missing_series:
        reasons.append("required series missing or without a numerical observation: {0}".format(", ".join(missing_series)))
    if stale_series:
        status = SECTION_STALE if status == SECTION_OK else SECTION_PARTIAL
        reasons.append("required series stale at capture: {0}".format(", ".join(stale_series)))
    return {
        "status": status,
        "reason": "; ".join(reasons) if reasons else None,
        "data": data,
        "required_missing": missing_fields + [s for s in missing_series if s not in missing_fields],
        "latest_observation_date": latest.isoformat() if latest else None,
        "captured_freshness": None if freshness is None else {"status": freshness.status, "age_days": freshness.age_days, "tolerance_days": freshness.tolerance_days, "cadence": cadence, "policy_version": FRESHNESS_POLICY_VERSION},
        "required_inputs": inputs,
        "stale_required": stale_series,
        "missing_required": missing_series,
    }


def _market_section(sectors: dict[str, Any], rates: dict[str, Any]) -> dict[str, Any] | None:
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
                "source_id": row.get("source_id"),
            }
        )
    # Catalog (identity) order; consumers rank locally. A value-ranked list would leak
    # restricted values through position after export redaction.
    leadership.sort(key=lambda r: str(r["sector_key"]))
    curve = [c for c in rates.get("curve", []) if c.get("yield_pct") is not None]
    ten_year = next((c for c in rates.get("curve", []) if c["tenor"] == "10Y" and c.get("yield_pct") is not None), None)
    if not leadership and not curve:
        return None
    return {
        "overnight_quotes": {"status": SECTION_UNAVAILABLE, "reason": "No live quote source is configured (IBKR market data disabled). Yesterday's close is not labeled overnight."},
        "sector_leadership_rs_vs_spy": leadership,
        "leadership_order": "sector_key (identity order; not ranked)",
        "us_10y": ten_year,
        "slopes_bps": rates.get("slopes") if curve else None,
    }


def _liquidity_section(macro: dict[str, Any]) -> dict[str, Any] | None:
    liquidity = (macro.get("categories") or {}).get("liquidity")
    if not liquidity:
        return None
    return {
        "series": liquidity,
        "disclosure": "Balances differ in dating (Wednesday levels vs week averages ending Wednesday vs daily) and scale (provider units are millions or billions; level_display is an explicit conversion); no composite liquidity score is computed.",
    }


def _lineage(macro: dict[str, Any], rates: dict[str, Any], credit: dict[str, Any], sectors: dict[str, Any], industries: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    """Exact contributing references: series revisions, metric rows, credit rows, artifacts, runs."""
    series_refs: list[dict[str, Any]] = []
    metric_refs: list[dict[str, Any]] = []
    run_ids: set[str] = set()
    blocks = [b for cat in (macro.get("categories") or {}).values() for b in cat]
    blocks += rates.get("real_yields", []) + rates.get("inflation_compensation", []) + rates.get("policy", [])
    seen = set()
    for block in blocks:
        sid = block.get("series_id")
        if sid in seen:
            continue
        seen.add(sid)
        latest = block.get("latest") or {}
        series_refs.append({"series_id": sid, "observation_date": latest.get("observation_date"), "revision_seq": latest.get("revision_seq"), "retrieved_at": latest.get("retrieved_at"), "ingestion_run_id": latest.get("ingestion_run_id"), "publication_status": block.get("publication_status")})
        if latest.get("ingestion_run_id"):
            run_ids.add(str(latest["ingestion_run_id"]))
        for name, entry in (block.get("transforms") or {}).items():
            metric_refs.append({"metric_id": entry.get("metric_id"), "as_of": entry.get("as_of"), "computed_at": entry.get("computed_at"), "transform_version": entry.get("transform_version")})
    for leg in rates.get("curve", []):
        if leg.get("observation_date"):
            series_refs.append({"series_id": leg["series_id"], "observation_date": leg.get("observation_date"), "revision_seq": leg.get("revision_seq"), "retrieved_at": leg.get("retrieved_at"), "ingestion_run_id": leg.get("ingestion_run_id"), "publication_status": leg.get("publication_status")})
            if leg.get("ingestion_run_id"):
                run_ids.add(str(leg["ingestion_run_id"]))
    for name, entry in (rates.get("slopes") or {}).items():
        if entry:
            metric_refs.append({"metric_id": entry.get("metric_id"), "as_of": entry.get("as_of"), "computed_at": entry.get("computed_at"), "transform_version": entry.get("transform_version")})
    credit_refs = [{"series_id": b["series_id"], "as_of": b["as_of"], "transform_version": b.get("transform_version"), "computed_at": b.get("computed_at")} for b in credit.get("buckets", [])]
    sector_refs = sorted({r["artifact_sha256"] for items in (sectors.get("datasets") or {}).values() for r in items})
    industry_refs = sorted({r.get("artifact_sha256") for ds in (industries.get("datasets") or {}).values() for items in ds.values() for r in items if r.get("artifact_sha256")})
    return {
        "series_revisions": sorted(series_refs, key=lambda r: str(r["series_id"])),
        "metric_rows": sorted(metric_refs, key=lambda r: str(r["metric_id"])),
        "credit_rows": credit_refs,
        "sector_artifact_hashes": sector_refs,
        "industry_artifact_hashes": industry_refs,
        "contributing_ingestion_run_ids": sorted(run_ids),
        "source_freshness": [
            {k: h.get(k) for k in ("source_id", "freshness_dataset", "last_success_at", "latest_observation_date", "latest_observation_retrieved_at", "transport_status", "freshness_status", "metadata_status")}
            for h in (health.get("sources") or [])
        ],
        "versions": {
            "code_version": CODE_VERSION,
            "catalog_version": CATALOG_VERSION,
            "transform_version": TRANSFORM_VERSION,
            "freshness_policy_version": FRESHNESS_POLICY_VERSION,
            "schema_version": SCHEMA_VERSION,
        },
    }


def _quarantined_series(macro: dict[str, Any]) -> list[str]:
    out = []
    for cat in (macro.get("categories") or {}).values():
        for block in cat:
            if block.get("publication_status") == "QUARANTINED_METADATA":
                out.append(block["series_id"])
    return sorted(out)


def build_snapshot_body(conn, *, generated_at: datetime, cutoff_at: datetime, generation_params: dict[str, Any]) -> dict[str, Any]:
    capture_date = cutoff_at.date()
    macro = macro_context(conn, today=capture_date)
    rates = rates_context(conn)
    credit = credit_context(conn)
    sectors = sectors_context(conn)
    industries = industries_context(conn)
    health = data_health_context(conn, today=capture_date)
    strategies = strategies_context(conn)

    macro_categories = {k: v for k, v in (macro.get("categories") or {}).items() if k in {"growth", "labor", "inflation", "policy"}}
    macro_data = {"categories": macro_categories, "series_without_data": macro.get("series_without_data"), "quarantined_series": _quarantined_series(macro), "attribution": macro.get("attribution")} if macro_categories else None
    rates_data = rates if any(c.get("yield_pct") is not None for c in rates.get("curve", [])) else None
    sections = {
        "data_health": section_status("data_health", {"sources": health.get("sources")} if health.get("sources") else None, required=["sources"], capture_date=capture_date, empty_reason="No sources registered yet; run jobs.market_intelligence_refresh.", presence="registry"),
        "market": section_status("market", _market_section(sectors, rates), required=["sector_leadership_rs_vs_spy", "us_10y"], capture_date=capture_date, empty_reason="No sector or rates data available."),
        "macro": section_status("macro", macro_data, required=["categories"], capture_date=capture_date, empty_reason="No FRED macro observations stored (FRED_API_KEY not configured or refresh not run)."),
        "rates": section_status("rates", rates_data, required=["curve", "slopes"], capture_date=capture_date, empty_reason="No Treasury curve observations stored."),
        "liquidity": section_status("liquidity", _liquidity_section(macro), required=["series"], capture_date=capture_date, empty_reason="No liquidity series stored."),
        "credit": section_status("credit", credit if credit.get("buckets") else None, required=["buckets"], capture_date=capture_date, empty_reason="No credit index snapshots stored."),
        "sectors": section_status("sectors", sectors if sectors.get("datasets") else None, required=["datasets"], capture_date=capture_date, empty_reason="No sector snapshots stored (legacy bridge not run)."),
        "industries": section_status("industries", industries if industries.get("datasets") else None, required=["datasets"], capture_date=capture_date, empty_reason="No industry snapshots stored."),
        "strategy_monitor_summary": section_status("strategy_monitor_summary", strategies if strategies.get("strategies") else None, required=["strategies"], capture_date=capture_date, empty_reason="No research runs in PostgreSQL.", presence="registry"),
    }
    _status_keys = ("status", "reason", "required_missing", "latest_observation_date", "captured_freshness", "required_inputs", "stale_required", "missing_required")
    sections_status = {name: {k: sec[k] for k in _status_keys} for name, sec in sections.items()}
    statuses = [s["status"] for s in sections.values()]
    if all(s == SECTION_OK for s in statuses):
        completeness = COMPLETENESS_COMPLETE
    elif all(s == SECTION_UNAVAILABLE for s in statuses):
        completeness = COMPLETENESS_EMPTY
    else:
        completeness = COMPLETENESS_PARTIAL
    stale_sources = [h for h in (health.get("sources") or []) if h.get("freshness_status") == "STALE"]
    body = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "cutoff_at": cutoff_at.isoformat(),
        "as_of_date": capture_date.isoformat(),
        "generation_params": generation_params,
        "semantics": {
            "generated_at": "wall-clock time the builder ran",
            "cutoff_at": "database transaction timestamp of the REPEATABLE READ capture; no input newer than this is included",
            "as_of_date": "calendar date of cutoff_at (UTC); not an observation date",
            "per_metric_dates": "every metric carries its own observation_date/as_of; a single as_of does not imply equal currency",
            "vintage": "FRED values are LATEST_REVISED (current vintage), not point-in-time; not research-eligible as PIT",
            "reconstruction": "historical cutoffs are refused; stored snapshots are the only historical record",
            "captured_health": "health as evaluated at capture; delivery-time health is added by the API outside this body",
        },
        "completeness": completeness,
        "sections_status": sections_status,
        "captured_health": {
            "evaluated_at": cutoff_at.isoformat(),
            "stale_sources": [{k: h.get(k) for k in ("source_id", "freshness_dataset", "latest_observation_date", "dataset_cadence")} for h in stale_sources],
            "failed_transport": [{k: h.get(k) for k in ("source_id", "freshness_dataset", "transport_status")} for h in (health.get("sources") or []) if h.get("transport_status") in ("FAILED", "METADATA_REJECTED", "PARTIAL")],
            "quarantined_series": _quarantined_series(macro),
        },
        "sections": {name: {k: sections[name][k] for k in ("status", "reason", "data")} for name in SECTION_ORDER},
        "input_refs": _lineage(macro, rates, credit, sectors, industries, health),
        "excluded_by_policy": ["credentials", "account ids", "client holdings", "model binaries", "raw licensed datasets", "embargoed/holdout data"],
    }
    body = normalize_payload(body)
    body["content_sha256"] = content_hash(body)
    body["artifact_sha256"] = canonical_sha256(body)
    return body


def content_hash(body: dict[str, Any]) -> str:
    """Hash of the body without runtime timestamps and without the digests themselves."""
    stripped = {k: v for k, v in body.items() if k not in {"artifact_sha256", "content_sha256", "generated_at", "cutoff_at", "as_of_date"}}
    captured = dict(stripped.get("captured_health") or {})
    captured.pop("evaluated_at", None)
    stripped["captured_health"] = captured
    # Runtime-only generation parameters (which job ran, what cutoff it asked for) are not content.
    stripped["generation_params"] = {k: v for k, v in (stripped.get("generation_params") or {}).items() if k not in {"parent_run_id", "requested_cutoff"}}
    return canonical_sha256(stripped)


def _db_now(conn) -> datetime:
    now = conn.execute(text("SELECT transaction_timestamp()")).scalar()
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def build_and_publish(engine, *, parent_run_id: str | None = None, requested_cutoff: datetime | None = None, generated_at: datetime | None = None, created_by: str = "backend", extra_params: dict[str, Any] | None = None, supersede_reason: str | None = None) -> BuildResult:
    """Build the current snapshot and publish it unless identical content is already published.

    ``requested_cutoff`` is only accepted when it is not in the past relative to the database
    capture time (tolerance 5 minutes); anything earlier is a historical reconstruction request
    and raises :class:`HistoricalReconstructionUnsupported`.
    """
    generated_at = generated_at or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    if requested_cutoff is not None and requested_cutoff.tzinfo is None:
        requested_cutoff = requested_cutoff.replace(tzinfo=timezone.utc)
    params = {"parent_run_id": parent_run_id, "requested_cutoff": requested_cutoff.isoformat() if requested_cutoff else None, "builder": "current_only", **(extra_params or {})}
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with conn.begin():
            cutoff_at = _db_now(conn)
            if requested_cutoff is not None and requested_cutoff < cutoff_at - timedelta(minutes=5):
                raise HistoricalReconstructionUnsupported(
                    "requested cutoff {0} is before the database capture time {1}; historical snapshots cannot be rebuilt from revised data. Use the stored snapshot index instead.".format(requested_cutoff.isoformat(), cutoff_at.isoformat())
                )
            body = build_snapshot_body(conn, generated_at=generated_at, cutoff_at=cutoff_at, generation_params=params)
            sha = body["artifact_sha256"]
            content = body["content_sha256"]
            latest = conn.execute(
                text("SELECT snapshot_id, content_sha256, snapshot_sha256 FROM mi_morning_context_snapshots WHERE publication_state = 'PUBLISHED' ORDER BY cutoff_at DESC, generated_at DESC, created_at DESC LIMIT 1")
            ).mappings().first()
            if latest is not None and latest["content_sha256"] == content and not supersede_reason:
                stored = conn.execute(
                    text(
                        """
                        SELECT snapshot_id, snapshot_sha256, content_sha256, snapshot_json, generated_at, cutoff_at, as_of_date, sections_status, completeness
                        FROM mi_morning_context_snapshots WHERE snapshot_id = :id
                        """
                    ),
                    {"id": latest["snapshot_id"]},
                ).mappings().one()
                stored_body = stored["snapshot_json"] if isinstance(stored["snapshot_json"], dict) else body

                def _iso(value: Any) -> str:
                    if hasattr(value, "isoformat"):
                        return value.isoformat()
                    return str(value)

                return BuildResult(
                    snapshot_id=stored["snapshot_id"],
                    snapshot_sha256=stored["snapshot_sha256"],
                    content_sha256=stored["content_sha256"],
                    completeness=stored["completeness"],
                    sections_status=stored["sections_status"] or stored_body.get("sections_status") or {},
                    published_new=False,
                    generated_at=_iso(stored["generated_at"]),
                    cutoff_at=_iso(stored["cutoff_at"]),
                    as_of_date=_iso(stored["as_of_date"])[:10],
                    body=stored_body,
                )
            snapshot_id = "mc_{0}".format(sha[:20])
            inserted = conn.execute(
                text(
                    """
                    INSERT INTO mi_morning_context_snapshots (
                        snapshot_id, schema_version, generated_at, cutoff_at, as_of_date, generation_params, input_refs,
                        sections_status, snapshot_json, snapshot_sha256, completeness, publication_state, created_by, created_at, content_sha256
                    ) VALUES (
                        :snapshot_id, :schema_version, :generated_at, :cutoff_at, :as_of_date, CAST(:params AS JSONB), CAST(:input_refs AS JSONB),
                        CAST(:sections_status AS JSONB), CAST(:body AS JSONB), :sha, :completeness, 'PUBLISHED', :created_by, NOW(), :content
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
                    "content": content,
                },
            ).rowcount
            superseded = None
            if inserted and supersede_reason and latest is not None:
                conn.execute(
                    text("UPDATE mi_morning_context_snapshots SET publication_state = 'SUPERSEDED', superseded_by = :new, superseded_at = NOW(), quality_status = 'SUPERSEDED', quality_note = :note WHERE snapshot_id = :old AND publication_state = 'PUBLISHED'"),
                    {"new": snapshot_id, "old": latest["snapshot_id"], "note": supersede_reason[:500]},
                )
                superseded = latest["snapshot_id"]
    return BuildResult(
        snapshot_id=snapshot_id,
        snapshot_sha256=sha,
        content_sha256=content,
        completeness=body["completeness"],
        sections_status=body["sections_status"],
        published_new=bool(inserted),
        generated_at=body["generated_at"],
        cutoff_at=body["cutoff_at"],
        as_of_date=body["as_of_date"],
        superseded_snapshot_id=superseded,
        body=body,
    )


def mark_quality(conn, snapshot_id: str, *, quality_status: str, note: str) -> int:
    """Flag a published snapshot (e.g. after a metadata correction) without touching its body."""
    return conn.execute(
        text("UPDATE mi_morning_context_snapshots SET quality_status = :q, quality_note = :n WHERE snapshot_id = :s"),
        {"q": quality_status, "n": note[:500], "s": snapshot_id},
    ).rowcount


__all__ = [
    "COMPLETENESS_COMPLETE",
    "COMPLETENESS_EMPTY",
    "COMPLETENESS_PARTIAL",
    "SCHEMA_VERSION",
    "SECTION_ORDER",
    "SECTION_OK",
    "SECTION_PARTIAL",
    "SECTION_STALE",
    "SECTION_UNAVAILABLE",
    "BuildResult",
    "HistoricalReconstructionUnsupported",
    "build_and_publish",
    "build_snapshot_body",
    "content_hash",
    "mark_quality",
    "section_status",
    "_has_value",
]
