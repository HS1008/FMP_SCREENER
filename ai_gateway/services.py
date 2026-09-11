"""Semantic read-only queries over curated ``mi_v_*`` views. No arbitrary SQL.

Every tool reads the SAME canonical PostgreSQL store as the Streamlit dashboard through the
``mi_readonly`` role, inside one READ ONLY transaction, and returns an export-filtered
envelope (``ai_gateway.envelope.respond``). The service layer understands the post-FMP
architecture:

* rates: official Treasury Daily Par Yield Curve XML preferred by observation date, FRED
  DGS*/DFII* as fallback, same-date complete curve only, newer partial tenors separate;
* sectors: independent equity EOD adapter (``EQUITY_EOD``) -> ``ret_1d`` and ``rs_1d`` on
  aligned sessions; legacy FMP bundles are labelled, never called; unavailable is explicit;
* industry / subgroup: the current-context hierarchy (sector -> industry ETF comparison ->
  curated basket), never described as an official GICS taxonomy;
* research: holdout fail-closed views (migration 027) plus a second Python filter; the
  Stage 2 final holdout, 2025+ metrics and model payloads have no read path here.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from sqlalchemy import text

from market_intelligence.catalog import CATALOG_BY_ID, CURVE_SLOPES, CURVE_TENORS, FRED_ATTRIBUTION
from market_intelligence.freshness import FRESHNESS_POLICY_VERSION
from market_intelligence.overview import build_session_changes, build_what_changed
from market_intelligence.quote_status import derive_quote_status, morning_overnight_section
from market_intelligence.read_models import (
    credit_context,
    data_health_context,
    ibkr_collector_status,
    ibkr_quotes_latest,
    industries_context,
    macro_context,
    morning_index,
    morning_latest,
    observation_history,
    order_flow_overview,
    rates_context,
    sectors_context,
    strategies_context,
)
from market_intelligence.readonly_db import ReadOnlyUnavailable, readonly_connection
from market_intelligence.sector_mapping import CANONICAL_SECTORS, CLASSIFICATION_VERSION
from market_intelligence.taxonomy import (
    ALL_BASKETS,
    INDUSTRY_PROXIES,
    KIND_CUSTOM_BASKET,
    KIND_ETF_COMPARISON,
    KIND_THEME,
    SECTOR_PROXIES,
    TAXONOMY_VERSION,
)

from ai_gateway.cache import cached
from ai_gateway.envelope import frozen_provenance, live_provenance, respond
from ai_gateway.errors import (
    DATA_NOT_AVAILABLE,
    DATABASE_UNAVAILABLE,
    HOLDOUT_FORBIDDEN,
    TOOL_FORBIDDEN,
    UNKNOWN_STRATEGY,
    GatewayError,
)
from ai_gateway.validation import (
    bounded_range,
    history_limit,
    optional_strategy,
    parse_date,
    parse_since,
    require_series_id,
    resolve_sector,
)

SCHEMA_NOTES = {
    "observed": "Values stored by ingestion or the provider.",
    "derived": "Deterministic calculations from stored observations (curve slopes, deltas, ratio-change RS).",
    "interpretation": "NONE — the gateway does not emit market opinions.",
}

# Capabilities that must have NO implementation path in this process. The dispatcher refuses
# them before any handler lookup, and tests assert none of them is ever registered.
FORBIDDEN_TOOLS = frozenset(
    {
        "execute_sql",
        "query_database",
        "run_sql",
        "place_order",
        "execute_trade",
        "rebalance_portfolio",
        "submit_order",
        "connect_ibkr",
        "launch_backtest",
        "create_backtest",
        "compile_quantconnect",
        "promote_model",
        "promote_strategy",
        "run_holdout",
        "open_holdout",
        "ml_final_holdout",
        "download_model",
        "get_model_binary",
    }
)

STAGE2_HOLDOUT_TYPES = frozenset(
    {
        "ML_FINAL_HOLDOUT",
        "FINAL_HOLDOUT",
        "HOLDOUT",
        "POST_HOLDOUT_ML_TRAIN",
        "POST_HOLDOUT_ML_OOS",
        "POST_HOLDOUT_WFO_TRAIN",
        "POST_HOLDOUT_WFO_TEST",
    }
)

HOLDOUT_START = date(2025, 1, 1)
HOLDOUT_TOKENS = ("HOLDOUT",)
MODEL_TOKENS = ("model", ".pkl", ".joblib", ".onnx", "pickle", "binary", "object_store")
ARTIFACT_LINEAGE_ALLOWED = frozenset({"NONHOLDOUT_EXPERIMENT_BOUND", "NONHOLDOUT_RUN_ALL_EXPERIMENTS_PROVEN"})

RATES_SOURCE_NOTE = (
    "Official U.S. Treasury Daily Par Yield Curve XML is preferred when its observation date is "
    "equal or newer; FRED DGS*/DFII* remain the fallback and the source for series the XML does not "
    "publish. Selection is by observation date then explicit tie preference (TREASURY before FRED); "
    "retrieval time never decides. Slopes use same-date legs only. Newer partial Treasury tenors "
    "are listed separately and never mixed into the complete curve."
)

SECTOR_SOURCE_NOTE = (
    "ret_1d = P[t] / P[previous aligned NYSE session] - 1 on adjusted close; rs_1d = "
    "(P_asset[t]/P_bench[t]) / (P_asset[t-1]/P_bench[t-1]) - 1 with asset and benchmark on the same "
    "two sessions. Missing sessions are null, never forward-filled; zero is a valid return. "
    "Multi-session gaps are not called 1D. EQUITY_EOD marks the independent equity adapter; "
    "FMP_LEGACY marks precomputed bundles (never a live FMP call). export_scope follows the source "
    "registry: an entitlement-unverified provider stays INTERNAL_ONLY for remote clients."
)

HIERARCHY_NOTE = (
    "Current-context hierarchy sector -> industry ETF comparison -> curated basket "
    "(equal-dollar, daily-rebalanced). Baskets are display groups, not official GICS "
    "subindustries; SMH/XSD/KRE/XBI/XOP/XRT are ETF comparisons, not mutually exclusive "
    "classifications. Sectors without a defensible mapping are explicitly UNAVAILABLE. "
    "Never point-in-time; research_eligible=false everywhere."
)

TOOL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "get_morning_context",
        "description": "Compact morning market snapshot from the published frozen morning_context artifact, plus live freshness (policy v2).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_market_pulse",
        "description": "Current Market Pulse: sector 1D leadership (aligned sessions), Treasury-preferred 10Y, credit identity, bond activity, overnight quotes.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_rates_curve",
        "description": "Same-date Treasury par-yield curve: official Treasury XML preferred by observation date, FRED fallback per leg, complete-curve date, newer partial tenors separately, derived nominal-minus-real labelled as derived.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_macro_overview",
        "description": "Canonical macro catalog (FRED) latest values grouped by category with publication-aware freshness.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_macro_series",
        "description": "Bounded history for one catalog series_id (FRED macro/rates or Treasury UST_* series).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["series_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_credit_overview",
        "description": "ICE BofA OAS buckets via FRED (IG, HY, rating buckets) with freshness. Values are RESTRICTED_REDISTRIBUTION: remote sessions receive identity/dates/status only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_sector_rotation",
        "description": "Sector ETF rotation vs SPY: ret_1d and rs_1d on aligned sessions plus 1W/1M/3M/6M/12M windows, with equity-EOD source provenance and explicit unavailable state when no entitled provider is configured.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_sector_detail",
        "description": "One canonical sector: its ETF row, industry ETF comparisons and curated subgroups from the current-context hierarchy.",
        "inputSchema": {
            "type": "object",
            "properties": {"sector": {"type": "string"}},
            "required": ["sector"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_industry_rotation",
        "description": "Industry-level relative strength vs the parent sector ETF: independent EOD ETF comparisons (e.g. SMH, XSD, KRE, XBI, XOP, XRT) and, where still stored, legacy FMP bundles labelled as such.",
        "inputSchema": {
            "type": "object",
            "properties": {"sector": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_subindustry_rotation",
        "description": "Curated current-context subgroups (e.g. semiconductor internals: AI Compute/GPUs, Equipment, Memory, Networking, Analog, Foundry, Mobile) vs their sector ETF. Explicit UNAVAILABLE for sectors without a defensible mapping; never an official GICS subindustry.",
        "inputSchema": {
            "type": "object",
            "properties": {"sector": {"type": "string"}, "industry": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_order_flow",
        "description": "FINRA TRACE aggregate activity (not an order book; no inferred aggressor direction).",
        "inputSchema": {
            "type": "object",
            "properties": {"include_history": {"type": "boolean"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_strategy_summary",
        "description": "Ingested Stage 1/Stage 2 research status. COMPLETE is not an economic pass. Final holdout and 2025+ metrics have no read path.",
        "inputSchema": {
            "type": "object",
            "properties": {"strategy": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_strategy_oos_windows",
        "description": "Non-holdout OOS windows for a strategy from proven non-holdout runs. Windows with unknown or 2025+ boundaries are excluded.",
        "inputSchema": {
            "type": "object",
            "properties": {"strategy": {"type": "string"}},
            "required": ["strategy"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_strategy_experiments",
        "description": "Non-holdout ingested experiments and artifact status bound to proven non-holdout lineage. No model binaries, no artifact payloads, no holdout metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {"strategy": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_data_health",
        "description": "Per-dataset freshness (policy v2: LATEST_AVAILABLE / AWAITING_RELEASE / INGESTION_OVERDUE / STALE / MISSING / INVALID_FUTURE / TRANSPORT_FAILURE), observation vs ingestion timestamps, transport status, last error.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_market_changes",
        "description": "Factual session-to-session and longer changes from stored analytics. No causal language.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": "previous_session, yesterday, or YYYY-MM-DD",
                }
            },
            "additionalProperties": False,
        },
    },
)

HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {}


def register(name: str):
    if name in FORBIDDEN_TOOLS:
        raise RuntimeError("refusing to register forbidden capability {0}".format(name))

    def decorator(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        HANDLERS[name] = fn
        return fn

    return decorator


def invoke(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    if name in FORBIDDEN_TOOLS:
        raise GatewayError(TOOL_FORBIDDEN, "this tool is not available")
    handler = HANDLERS.get(name)
    if handler is None:
        raise GatewayError(DATA_NOT_AVAILABLE, "unknown tool", details={"tool": name})
    return handler(**(arguments or {}))


def _readonly():
    try:
        return readonly_connection()
    except ReadOnlyUnavailable as exc:
        raise GatewayError(DATABASE_UNAVAILABLE, "read-only database unavailable", http_status=503, details={"status": exc.reason}) from None


def _view_exists(conn, name: str) -> bool:
    return bool(conn.execute(text("SELECT 1 FROM information_schema.views WHERE table_name = :n"), {"n": name}).first())


def _capture(conn) -> Any:
    return conn.execute(text("SELECT transaction_timestamp()")).scalar()


# ---- sectors / hierarchy -----------------------------------------------------------------------


def _rank_sectors(rows: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    decorated: list[tuple[int, float | None, dict[str, Any]]] = []
    for row in rows:
        metrics = row.get("metrics") or {}
        value = metrics.get(metric)
        number = None if value is None else float(value)
        decorated.append((0 if number is None else 1, number, row))
    ranked = sorted(decorated, key=lambda item: (item[0], item[1] if item[1] is not None else 0.0), reverse=True)
    out = []
    rank = 1
    for has_value, _number, row in ranked:
        item = dict(row)
        item["rank_metric"] = metric
        item["rank"] = rank if has_value else None
        if has_value:
            rank += 1
        out.append(item)
    return out


def _preferred_sector_rows(sectors: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """Independent EOD rows win over legacy bundles for the same sector; source is reported."""
    rows = list((sectors.get("datasets") or {}).get("ETF_RS_VS_SPY") or [])
    by_sector: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("canonical_sector") or row.get("sector_key") or "")
        current = by_sector.get(key)
        if current is None:
            by_sector[key] = row
            continue
        cur_legacy = str(current.get("source_id") or "") == "FMP_LEGACY"
        new_legacy = str(row.get("source_id") or "") == "FMP_LEGACY"
        if cur_legacy and not new_legacy:
            by_sector[key] = row
        elif cur_legacy == new_legacy and str(row.get("as_of") or "") > str(current.get("as_of") or ""):
            by_sector[key] = row
    chosen = list(by_sector.values())
    sources = sorted({str(r.get("source_id")) for r in chosen if r.get("source_id")})
    primary = None
    if sources:
        primary = "EQUITY_EOD" if "EQUITY_EOD" in sources else sources[0]
    return chosen, primary


def _sector_row(row: dict[str, Any]) -> dict[str, Any]:
    metrics = row.get("metrics") or {}
    coverage = row.get("coverage") or {}
    source_id = row.get("source_id")
    return {
        "sector": row.get("canonical_sector") or row.get("sector_key"),
        "sector_key": row.get("sector_key"),
        "entity_kind": row.get("entity_kind"),
        "instrument_id": row.get("instrument_id"),
        "benchmark": row.get("benchmark"),
        "as_of": row.get("as_of"),
        "prev_session": coverage.get("prev_session") if isinstance(coverage, dict) else None,
        "aligned_with_benchmark": coverage.get("aligned_with_benchmark") if isinstance(coverage, dict) else None,
        "adjustment_basis": coverage.get("adjustment_basis") if isinstance(coverage, dict) else None,
        "return_kind": coverage.get("return_kind") if isinstance(coverage, dict) else row.get("return_basis"),
        "source_id": source_id,
        "source_kind": "independent_equity_eod" if source_id == "EQUITY_EOD" else ("legacy_fmp_bundle" if source_id == "FMP_LEGACY" else "other"),
        "legacy_fmp": source_id == "FMP_LEGACY",
        "research_eligible": row.get("research_eligible"),
        "export_scope": row.get("export_scope"),
        "classification_version": CLASSIFICATION_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "artifact_sha256": row.get("artifact_sha256"),
        "ret_1d": metrics.get("ret_1d"),
        "rs_1d": metrics.get("rs_chg_1d"),
        "ret_1w": metrics.get("ret_1w"),
        "ret_1m": metrics.get("ret_1m"),
        "ret_3m": metrics.get("ret_3m"),
        "ret_6m": metrics.get("ret_6m"),
        "ret_12m": metrics.get("ret_12m"),
        "rs_1w": metrics.get("rs_chg_1w"),
        "rs_1m": metrics.get("rs_chg_1m"),
        "rs_3m": metrics.get("rs_chg_3m"),
        "rs_6m": metrics.get("rs_chg_6m"),
        "rs_12m": metrics.get("rs_chg_12m"),
        "pct_vs_50dma": metrics.get("pct_vs_50dma"),
        "pct_vs_200dma": metrics.get("pct_vs_200dma"),
        "rank_1d": row.get("rank"),
        "value_kind": {
            "ret_1d_kind": "observed" if metrics.get("ret_1d") is not None else "unavailable",
            "rs_1d_kind": "derived" if metrics.get("rs_chg_1d") is not None else "unavailable",
            "rs_windows_kind": "derived",
        },
    }


def _sector_payload(sectors: dict[str, Any], health_sources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    chosen, primary = _preferred_sector_rows(sectors)
    ranked_1d = _rank_sectors(chosen, "ret_1d")
    items = [_sector_row(row) for row in ranked_1d]
    equity = next((h for h in (health_sources or []) if h.get("source_id") == "EQUITY_EOD"), None)
    provider_status = {
        "source_id": "EQUITY_EOD",
        "provider": (equity or {}).get("provider"),
        "access_status": (equity or {}).get("access_status") or ("UNKNOWN" if equity is None else None),
        "enabled": (equity or {}).get("enabled"),
        "freshness_status": (equity or {}).get("freshness_status"),
        "latest_observation": (equity or {}).get("latest_observation_date"),
        "export_scope": (sectors.get("source_scopes") or {}).get("EQUITY_EOD") or (equity or {}).get("usage_scope"),
    }
    available = bool(items)
    reason = None
    if not items:
        reason = "no sector snapshots stored (independent equity EOD provider not configured or not yet ingested; legacy FMP disabled)"
    elif primary == "FMP_LEGACY":
        reason = None
    return {
        "dataset": "ETF_RS_VS_SPY",
        "primary_source": primary,
        "as_of_by_dataset": (sectors.get("as_of_by_dataset") or {}).get("ETF_RS_VS_SPY"),
        "equity_provider": provider_status,
        "unavailable_reason": reason,
        "note": SECTOR_SOURCE_NOTE,
        "rows": items,
        "source": "mi_v_sector_latest",
        "available": available,
    }


def _basket_catalog() -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for basket in ALL_BASKETS:
        out.setdefault(basket.parent_sector, []).append(
            {
                "key": basket.key,
                "label": basket.label,
                "kind": basket.kind,
                "parent_industry": basket.parent_industry,
                "members": list(basket.members),
                "notes": basket.notes,
            }
        )
    return out


def _industry_rows(industries: dict[str, Any], *, sector: str | None = None, datasets: tuple[str, ...], limit: int = 500) -> list[dict[str, Any]]:
    stored = industries.get("datasets") or {}
    items: list[dict[str, Any]] = []
    for dataset in datasets:
        for parent, rows in (stored.get(dataset) or {}).items():
            if sector and str(parent).lower() != str(sector).lower() and parent.replace(" ", "_") != sector:
                continue
            for row in rows:
                metrics = row.get("metrics") or {}
                coverage = row.get("coverage") or {}
                source_id = row.get("source_id")
                kind = coverage.get("kind") if isinstance(coverage, dict) else None
                status = coverage.get("status") if isinstance(coverage, dict) else None
                items.append(
                    {
                        "sector": parent,
                        "dataset": dataset,
                        "industry": row.get("industry_key"),
                        "group_kind": kind or ("INDUSTRY_LEGACY_FMP" if source_id == "FMP_LEGACY" else ("SUBGROUP_UNAVAILABLE" if dataset == "SUBGROUP_UNAVAILABLE" else None)),
                        "status": status or ("AVAILABLE" if metrics else "UNAVAILABLE"),
                        "instrument_id": row.get("instrument_id"),
                        "benchmark": row.get("benchmark"),
                        "as_of": row.get("as_of"),
                        "members": coverage.get("membership") if isinstance(coverage, dict) else None,
                        "members_used": coverage.get("members_used") if isinstance(coverage, dict) else None,
                        "members_missing": coverage.get("members_missing") if isinstance(coverage, dict) else None,
                        "weighting": coverage.get("weighting") if isinstance(coverage, dict) else None,
                        "ret_1d": metrics.get("ret_1d"),
                        "rs_1d": metrics.get("rs_chg_1d"),
                        "rs_1w": metrics.get("rs_chg_1w"),
                        "rs_1m": metrics.get("rs_chg_1m"),
                        "rs_3m": metrics.get("rs_chg_3m"),
                        "rs_6m": metrics.get("rs_chg_6m"),
                        "rs_12m": metrics.get("rs_chg_12m"),
                        "source_id": source_id,
                        "legacy_fmp": source_id == "FMP_LEGACY",
                        "export_scope": row.get("export_scope"),
                        "research_eligible": False,
                        "taxonomy_version": TAXONOMY_VERSION,
                        "note": coverage.get("notes") or coverage.get("note") if isinstance(coverage, dict) else None,
                    }
                )
    items.sort(key=lambda r: (str(r.get("sector") or ""), str(r.get("dataset") or ""), str(r.get("industry") or "")))
    return items[:limit]


def _hierarchy_for(sector: str | None) -> dict[str, Any]:
    sectors = [sector] if sector else list(SECTOR_PROXIES)
    out = {}
    for name in sectors:
        proxies = INDUSTRY_PROXIES.get(name) or {}
        baskets = [b for b in ALL_BASKETS if b.parent_sector == name]
        out[name] = {
            "sector_etf": SECTOR_PROXIES.get(name),
            "industry_etf_comparisons": [{"label": label, "instrument_id": etf} for label, etf in proxies.items()],
            "curated_subgroups": [
                {"key": b.key, "label": b.label, "kind": b.kind, "parent_industry": b.parent_industry, "members": list(b.members), "notes": b.notes}
                for b in baskets
                if b.kind in {KIND_CUSTOM_BASKET, KIND_THEME}
            ],
            "etf_comparisons": [
                {"key": b.key, "label": b.label, "members": list(b.members), "notes": b.notes}
                for b in baskets
                if b.kind == KIND_ETF_COMPARISON
            ],
            "subgroup_status": "AVAILABLE" if any(b.kind in {KIND_CUSTOM_BASKET, KIND_THEME} for b in baskets) else "UNAVAILABLE",
            "industry_status": "AVAILABLE" if proxies or any(b.kind == KIND_ETF_COMPARISON for b in baskets) else "UNAVAILABLE",
        }
    return out


# ---- morning / pulse ----------------------------------------------------------------------------


def _compact_morning(snapshot: dict[str, Any], health: dict[str, Any], pulse: dict[str, Any]) -> dict[str, Any]:
    body = snapshot.get("snapshot_json") or {}
    sections = body.get("sections") or {}
    not_current = [
        s
        for s in (health.get("sources") or [])
        if str(s.get("freshness_status") or "") in {"STALE", "INGESTION_OVERDUE", "TRANSPORT_FAILURE", "INVALID_FUTURE"} and not s.get("retired_optional")
    ]
    return {
        "as_of": snapshot.get("as_of_date") or body.get("as_of_date"),
        "cutoff_at": snapshot.get("cutoff_at") or body.get("cutoff_at"),
        "completeness": snapshot.get("completeness") or body.get("completeness"),
        "schema_version": snapshot.get("schema_version") or body.get("schema_version"),
        "markets": pulse.get("headline"),
        "rates": _section_data(sections, "rates"),
        "macro": _section_data(sections, "macro"),
        "credit": _section_data(sections, "credit"),
        "rotation": pulse.get("sectors"),
        "order_flow": _section_data(sections, "order_flow"),
        "strategies": _section_data(sections, "strategy_monitor_summary"),
        "freshness": {
            "not_current_count": len(not_current),
            "not_current": [
                {
                    "dataset": row.get("freshness_dataset") or row.get("dataset"),
                    "provider": row.get("provider") or row.get("source_id"),
                    "latest_observation": row.get("latest_observation_date"),
                    "status": row.get("freshness_status"),
                    "cadence": row.get("dataset_cadence"),
                }
                for row in not_current[:40]
            ],
            "policy": FRESHNESS_POLICY_VERSION,
        },
        "sections_status": body.get("sections_status"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "notes": SCHEMA_NOTES,
    }


def _section_data(sections: dict[str, Any], name: str) -> Any:
    sec = sections.get(name) or {}
    data = sec.get("data")
    if name == "macro" and isinstance(data, dict):
        categories = data.get("categories") or {}
        compact = {}
        for key, rows in categories.items():
            compact[key] = [
                {
                    "series_id": row.get("series_id"),
                    "label": row.get("label"),
                    "latest": row.get("latest"),
                    "freshness": row.get("freshness"),
                    "source": row.get("source"),
                }
                for row in (rows or [])[:12]
            ]
        return {"categories": compact, "attribution": data.get("attribution")}
    if name == "rates" and isinstance(data, dict):
        return {
            "curve": data.get("curve"),
            "complete_curve_date": data.get("complete_curve_date"),
            "partial_newer": data.get("partial_newer"),
            "slopes": data.get("slopes"),
            "source_ids": data.get("source_ids"),
            "attribution": data.get("attribution"),
            "units_note": data.get("units_note"),
        }
    if name == "credit" and isinstance(data, dict):
        return {"buckets": data.get("buckets"), "attribution": data.get("attribution")}
    if name == "order_flow" and isinstance(data, dict):
        return {
            "not_an_order_book": data.get("not_an_order_book"),
            "breadth": data.get("breadth"),
            "sentiment": {"customer_net": (data.get("sentiment") or {}).get("customer_net"), "perspective": ((data.get("sentiment") or {}).get("customer_net") or {}).get("perspective")},
            "attribution": data.get("attribution"),
        }
    if name == "strategy_monitor_summary" and isinstance(data, dict):
        return {"strategies": data.get("strategies"), "note": data.get("note")}
    return data


def _rates_body(rates: dict[str, Any]) -> dict[str, Any]:
    curve = rates.get("curve") or []
    sources = rates.get("source_ids") or []
    by_source: dict[str, list[str]] = {}
    for leg in curve:
        sid = leg.get("source_id")
        if sid:
            by_source.setdefault(str(sid), []).append(str(leg.get("tenor")))
    return {
        "curve": curve,
        "complete_curve_date": rates.get("complete_curve_date"),
        "curve_observation_dates": rates.get("curve_observation_dates"),
        "curve_dates_mixed": rates.get("curve_dates_mixed"),
        "partial_newer": rates.get("partial_newer"),
        "legs_by_source": by_source,
        "slopes": rates.get("slopes"),
        "slope_aliases": {
            "2s10s": "10Y2Y",
            "2s30s": "30Y2Y",
            "5s30s": "30Y5Y",
            "3m10y": "10Y3M",
        },
        "real_yields": rates.get("real_yields"),
        "inflation_compensation": rates.get("inflation_compensation"),
        "derived_nominal_minus_real": rates.get("derived_nominal_minus_real"),
        "derived_note": "derived_nominal_minus_real is computed from same-date nominal and real legs; it is NOT the published T5YIE/T10YIE breakeven series.",
        "policy": rates.get("policy"),
        "units_note": rates.get("units_note"),
        "attribution": rates.get("attribution"),
        "source_ids": sources,
        "fallback_used": bool(rates.get("fallback")),
        "source_priority": {
            "preferred": "TREASURY (official Daily Treasury Par Yield Curve XML)",
            "fallback": "FRED DGS*/DFII* (also the source for series Treasury XML does not publish)",
            "selection": "observation date, then explicit tie preference TREASURY > FRED; retrieval time never decides",
            "note": RATES_SOURCE_NOTE,
        },
        "tenors": list(CURVE_TENORS),
        "derived_slopes": list(CURVE_SLOPES),
        "notes": SCHEMA_NOTES,
    }


def _pulse_body(conn) -> dict[str, Any]:
    rates = rates_context(conn)
    credit = credit_context(conn)
    sectors = sectors_context(conn)
    order_flow = order_flow_overview(conn)
    collectors = ibkr_collector_status(conn)
    quotes = ibkr_quotes_latest(conn)
    health = data_health_context(conn)
    quote_state = derive_quote_status(collectors=collectors, quotes=quotes)
    sector_payload = _sector_payload(sectors, health.get("sources"))
    rows = sector_payload["rows"]
    lead = next((row for row in rows if row.get("ret_1d") is not None), None)
    lag = next((row for row in reversed(rows) if row.get("ret_1d") is not None), None)
    ten = next((row for row in rates.get("curve") or [] if row.get("tenor") == "10Y"), None)
    ig = next((row for row in credit.get("buckets") or [] if row.get("bucket") == "ig_broad"), None)
    hy = next((row for row in credit.get("buckets") or [] if row.get("bucket") == "hy_broad"), None)
    quotes_by_id = {str(row.get("instrument_id") or "").upper(): row for row in quotes}
    index_ids = ("SPY", "QQQ", "IWM", "DIA", "VIX")
    indexes = []
    for symbol in index_ids:
        row = quotes_by_id.get(symbol)
        if not row:
            continue
        indexes.append(
            {
                "symbol": symbol,
                "last": row.get("last") or row.get("close") or row.get("price"),
                "bid": row.get("bid"),
                "ask": row.get("ask"),
                "quote_ts": row.get("quote_ts") or row.get("retrieved_at"),
                "source": "IBKR",
                "export_scope": "INTERNAL_ONLY",
                "delay_status": row.get("market_data_type") or row.get("delay_status"),
                "kind": "observed",
            }
        )
    return {
        "headline": {
            "us_10y": ten,
            "us_10y_complete_curve_date": rates.get("complete_curve_date"),
            "ig_oas": ig,
            "hy_oas": hy,
            "sector_lead_1d": lead,
            "sector_lag_1d": lag,
        },
        "indexes": indexes,
        "overnight_quotes": morning_overnight_section(quote_state),
        "sectors": sector_payload,
        "session_changes": build_session_changes(rates=rates, credit=credit, sectors=sectors, order_flow=order_flow),
        "attribution": {"rates": rates.get("attribution"), "macro": FRED_ATTRIBUTION},
        "notes": SCHEMA_NOTES,
        "index_note": "Index prints come from stored IBKR quotes when the collector has delivered them. Missing symbols are omitted, not zeroed.",
    }


@register("get_morning_context")
def get_morning_context() -> dict[str, Any]:
    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            health = data_health_context(conn)
            pulse = _pulse_body(conn)
        if not snapshot:
            return respond(
                None,
                provenance=live_provenance(views=["mi_v_morning_context_latest"], captured_at=captured_at),
                tool="get_morning_context",
                available=False,
                unavailable_reason="no published morning_context snapshot",
                degraded=True,
            )
        body = _compact_morning(snapshot, health, pulse)
        return respond(
            body,
            provenance=frozen_provenance(snapshot),
            tool="get_morning_context",
            available=True,
            degraded=(snapshot.get("completeness") != "COMPLETE"),
            snapshot=snapshot,
            extra={"live_health_captured_at": captured_at},
        )

    return cached("morning_context", "latest", _build)


@register("get_market_pulse")
def get_market_pulse() -> dict[str, Any]:
    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            body = _pulse_body(conn)
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_sector_latest", "mi_v_metric_latest", "mi_v_macro_latest", "mi_v_source_health"], captured_at=captured_at),
            tool="get_market_pulse",
            available=True,
            snapshot=snapshot,
        )

    return cached("market_pulse", "latest", _build)


@register("get_rates_curve")
def get_rates_curve() -> dict[str, Any]:
    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            rates = rates_context(conn)
        body = _rates_body(rates)
        available = any((row or {}).get("yield_pct") is not None for row in (rates.get("curve") or []))
        return respond(
            body if available else None,
            provenance=live_provenance(views=["mi_v_macro_latest", "mi_v_macro_observations_current", "mi_v_metric_latest"], captured_at=captured_at),
            tool="get_rates_curve",
            available=available,
            unavailable_reason=None if available else "no Treasury par yields stored (Treasury XML and FRED both absent)",
            snapshot=snapshot,
        )

    return cached("rates_curve", "latest", _build)


@register("get_macro_overview")
def get_macro_overview() -> dict[str, Any]:
    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            macro = macro_context(conn)
        return respond(
            {**macro, "freshness_policy": FRESHNESS_POLICY_VERSION, "notes": SCHEMA_NOTES},
            provenance=live_provenance(views=["mi_v_macro_latest", "mi_v_metric_latest"], captured_at=captured_at),
            tool="get_macro_overview",
            available=bool(macro.get("categories")),
            unavailable_reason=None if macro.get("categories") else "no macro observations",
            snapshot=snapshot,
        )

    return cached("macro_overview", "latest", _build)


def _series_source(series_id: str, latest_row: dict[str, Any] | None) -> dict[str, Any]:
    spec = CATALOG_BY_ID.get(series_id)
    source_id = (latest_row or {}).get("source_id") or ("TREASURY" if series_id.startswith("UST_") else "FRED")
    if source_id == "TREASURY":
        from market_intelligence.ingest_treasury import TREASURY_ATTRIBUTION

        return {"provider": "TREASURY", "source_id": "TREASURY", "attribution": TREASURY_ATTRIBUTION}
    return {"provider": "FRED", "source_id": source_id, "attribution": spec.attribution if spec else FRED_ATTRIBUTION}


@register("get_macro_series")
def get_macro_series(series_id: str, start_date: str | None = None, end_date: str | None = None, limit: int | None = None) -> dict[str, Any]:
    sid = require_series_id(series_id)
    start = parse_date(start_date, field="start_date")
    end = parse_date(end_date, field="end_date")
    bounded_range(start, end)
    row_limit = history_limit(limit)
    spec = CATALOG_BY_ID.get(sid)
    cache_key = "{0}:{1}:{2}:{3}".format(sid, start, end, row_limit)

    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            history = observation_history(conn, sid, start=start, limit=row_limit)
            latest_meta = next(
                (dict(r) for r in conn.execute(text("SELECT source_id, export_scope FROM mi_v_macro_latest WHERE series_id = :s"), {"s": sid}).mappings().all()),
                None,
            )
            if end is not None:
                history = [row for row in history if parse_date(row.get("observation_date"), field="observation_date") is not None and parse_date(row.get("observation_date"), field="observation_date") <= end]
            latest = history[-1] if history else None
            previous = history[-2] if len(history) > 1 else None
            latest_value = None if latest is None else latest.get("value")
            previous_value = None if previous is None else previous.get("value")
            change = None if latest_value is None or previous_value is None else latest_value - previous_value
            scope = (latest_meta or {}).get("export_scope") or (spec.export_scope if spec else None)
            body = {
                "series_id": sid,
                "description": spec.label if spec else sid,
                "category": spec.category if spec else None,
                "frequency": spec.expected_frequency if spec else None,
                "units": ", ".join(spec.expected_units_contains) if spec and spec.expected_units_contains else None,
                "source": _series_source(sid, latest_meta),
                "export_scope": scope,
                "latest": latest,
                "previous": previous,
                "change": change,
                "history": history,
                "row_count": len(history),
                "truncated": len(history) >= row_limit,
                "notes": SCHEMA_NOTES,
            }
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_macro_observations_current", "mi_v_macro_latest"], captured_at=captured_at),
            tool="get_macro_series",
            available=bool(history),
            unavailable_reason=None if history else "no observations for series",
            snapshot=snapshot,
        )

    return cached("macro_series", cache_key, _build)


@register("get_credit_overview")
def get_credit_overview() -> dict[str, Any]:
    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            credit = credit_context(conn)
        body = {
            **credit,
            "notes": SCHEMA_NOTES,
            "value_kind": "observed (OAS); changes are derived",
            "rights_note": "ICE BofA indices via FRED are RESTRICTED_REDISTRIBUTION: values appear only in a local owner session or with an explicit recorded remote right.",
        }
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_credit_latest"], captured_at=captured_at),
            tool="get_credit_overview",
            available=bool(credit.get("buckets")),
            unavailable_reason=None if credit.get("buckets") else "no credit snapshots",
            snapshot=snapshot,
        )

    return cached("credit_overview", "latest", _build)


@register("get_sector_rotation")
def get_sector_rotation() -> dict[str, Any]:
    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            sectors = sectors_context(conn)
            health = data_health_context(conn)
        body = _sector_payload(sectors, health.get("sources"))
        available = bool(body.pop("available"))
        reason = body.pop("unavailable_reason")
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_sector_latest", "mi_v_source_health"], captured_at=captured_at),
            tool="get_sector_rotation",
            available=available,
            unavailable_reason=reason,
            snapshot=snapshot,
        )

    return cached("sector_rotation", "latest", _build)


@register("get_sector_detail")
def get_sector_detail(sector: str) -> dict[str, Any]:
    canonical = resolve_sector(sector)
    cache_key = canonical or ""

    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            sectors = sectors_context(conn)
            industries = industries_context(conn)
            health = data_health_context(conn)
        rotation = _sector_payload(sectors, health.get("sources"))
        match = next((row for row in rotation["rows"] if row.get("sector") == canonical or row.get("sector_key") == canonical), None)
        industry_rows = _industry_rows(industries, sector=canonical, datasets=("INDUSTRY_RS_VS_SECTOR_ETF",))
        subgroup_rows = _industry_rows(industries, sector=canonical, datasets=("THEME_RS", "SUBGROUP_UNAVAILABLE"))
        hierarchy = _hierarchy_for(canonical).get(canonical) or {}
        body = {
            "sector": canonical,
            "classification_version": CLASSIFICATION_VERSION,
            "taxonomy_version": TAXONOMY_VERSION,
            "canonical_sectors": list(CANONICAL_SECTORS),
            "sector_row": match,
            "industries": industry_rows,
            "subgroups": subgroup_rows,
            "hierarchy": hierarchy,
            "subgroup_status": hierarchy.get("subgroup_status") or "UNAVAILABLE",
            "hierarchy_note": HIERARCHY_NOTE,
            "notes": SCHEMA_NOTES,
        }
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_sector_latest", "mi_v_industry_latest", "mi_v_source_health"], captured_at=captured_at),
            tool="get_sector_detail",
            available=match is not None or bool(industry_rows) or bool(subgroup_rows),
            unavailable_reason=None if match or industry_rows or subgroup_rows else "sector not present in stored snapshots",
            snapshot=snapshot,
        )

    return cached("sector_detail", cache_key, _build)


@register("get_industry_rotation")
def get_industry_rotation(sector: str | None = None) -> dict[str, Any]:
    canonical = resolve_sector(sector) if sector else None
    cache_key = canonical or "all"

    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            industries = industries_context(conn)
        rows = _industry_rows(industries, sector=canonical, datasets=("INDUSTRY_RS_VS_SECTOR_ETF",))
        sources = sorted({str(r.get("source_id")) for r in rows if r.get("source_id")})
        hierarchy = _hierarchy_for(canonical)
        body = {
            "sector": canonical,
            "hierarchy": "sector > industry ETF comparison",
            "taxonomy_version": TAXONOMY_VERSION,
            "rows": rows,
            "source_ids": sources,
            "legacy_fmp_rows": sum(1 for r in rows if r.get("legacy_fmp")),
            "independent_rows": sum(1 for r in rows if not r.get("legacy_fmp")),
            "industry_status_by_sector": {name: spec["industry_status"] for name, spec in hierarchy.items()},
            "note": HIERARCHY_NOTE,
        }
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_industry_latest"], captured_at=captured_at),
            tool="get_industry_rotation",
            available=bool(rows),
            unavailable_reason=None if rows else "no industry snapshots stored for the requested scope",
            snapshot=snapshot,
        )

    return cached("industry_rotation", cache_key, _build)


@register("get_subindustry_rotation")
def get_subindustry_rotation(sector: str | None = None, industry: str | None = None) -> dict[str, Any]:
    canonical = resolve_sector(sector) if sector else None

    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            industries = industries_context(conn)
        rows = _industry_rows(industries, sector=canonical, datasets=("THEME_RS", "SUBGROUP_UNAVAILABLE"))
        if industry:
            wanted = str(industry).strip().lower()
            catalog = {b.label.lower(): b for b in ALL_BASKETS}
            rows = [
                row
                for row in rows
                if str(row.get("industry") or "").lower() == wanted
                or (catalog.get(str(row.get("industry") or "").lower()) is not None and str(catalog[str(row.get("industry") or "").lower()].parent_industry or "").lower() == wanted)
            ]
        hierarchy = _hierarchy_for(canonical)
        available_rows = [row for row in rows if row.get("status") == "AVAILABLE" or (row.get("dataset") == "THEME_RS" and any(row.get(k) is not None for k in ("ret_1d", "rs_1d", "rs_1m")))]
        unavailable_sectors = sorted({row.get("sector") for row in rows if row.get("dataset") == "SUBGROUP_UNAVAILABLE"})
        body = {
            "sector": canonical,
            "industry": industry,
            "hierarchy": "sector > industry > curated subgroup (current-context basket)",
            "taxonomy_version": TAXONOMY_VERSION,
            "subgroup_kind": "CURATED_EQUAL_DOLLAR_BASKET_OR_CROSS_SECTOR_THEME",
            "official_gics_subindustry": False,
            "rows": rows,
            "available_rows": len(available_rows),
            "unavailable_sectors": unavailable_sectors,
            "subgroup_status_by_sector": {name: spec["subgroup_status"] for name, spec in hierarchy.items()},
            "catalog": _basket_catalog() if canonical is None else {canonical: _basket_catalog().get(canonical, [])},
            "note": HIERARCHY_NOTE,
        }
        available = bool(available_rows)
        reason = None
        if not available:
            if canonical and hierarchy.get(canonical, {}).get("subgroup_status") == "UNAVAILABLE":
                reason = "no curated subgroup is defined for {0}; membership is not guessed".format(canonical)
            else:
                reason = "no subgroup snapshots stored (independent equity EOD provider not configured or not yet ingested)"
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_industry_latest"], captured_at=captured_at),
            tool="get_subindustry_rotation",
            available=available,
            unavailable_reason=reason,
            snapshot=snapshot,
        )

    return cached("subindustry_rotation", "{0}:{1}".format(canonical, industry), _build)


@register("get_order_flow")
def get_order_flow(include_history: bool = False) -> dict[str, Any]:
    cache_key = "history" if include_history else "summary"

    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            if include_history:
                from market_intelligence.read_models import order_flow_context

                flow = order_flow_context(conn, history_limit=120, include_history=True)
            else:
                flow = order_flow_overview(conn)
        body = {
            **flow,
            "observed_vs_derived": {
                "observed": "FINRA Query API aggregate volume, trades, advances/declines",
                "derived": "session-to-session volume/trade differences; customer_net volume difference",
                "not_inferred": "aggressor side, hidden liquidity, institutional identity, fund flows",
            },
            "notes": SCHEMA_NOTES,
        }
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_finra_aggregate_current", "mi_v_order_flow_coverage"], captured_at=captured_at),
            tool="get_order_flow",
            available=True,
            snapshot=snapshot,
        )

    return cached("order_flow", cache_key, _build)


# ---- research (holdout fail-closed) -------------------------------------------------------------


def _text_has_holdout(*values: Any) -> bool:
    for value in values:
        if value in (None, ""):
            continue
        if any(token in str(value).upper() for token in HOLDOUT_TOKENS):
            return True
    return False


def _date_or_none(raw: Any) -> date | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _holdout_blocked(row: dict[str, Any], *, require_known_end: bool = True) -> bool:
    """Second, independent filter over what the SQL view already excluded.

    Any holdout marker, any 2025+ boundary, an unknown window END, or an explicit-true
    holdout flag blocks the row. ``research_is_holdout`` must be explicitly false when the
    column is present (NULL = unknown = blocked).
    """
    test_type = str(row.get("research_test_type") or row.get("test_type") or "")
    phase = str(row.get("research_phase") or row.get("phase") or "")
    if test_type.upper() in STAGE2_HOLDOUT_TYPES or phase.upper() == "HOLDOUT":
        return True
    if _text_has_holdout(test_type, phase, row.get("name"), row.get("research_experiment_id"), row.get("research_run_id"), row.get("outer_window_id"), row.get("research_window_id")):
        return True
    if "research_is_holdout" in row and row.get("research_is_holdout") is not False:
        return True
    if row.get("research_is_holdout") is True:
        return True
    for key in ("run_holdout_status", "holdout_status"):
        if str(row.get(key) or "").upper() in {"ACCESSED", "OPEN", "UNSEALED"}:
            return True
    if str(row.get("run_holdout_exposure_status") or "").upper() in {"ACCESSED_ONCE", "REPEATEDLY_ACCESSED"}:
        return True
    ends = []
    for key in ("oos_end", "test_end"):
        if key in row:
            ended = _date_or_none(row.get(key))
            if ended is None:
                if require_known_end:
                    return True
                continue
            ends.append(ended)
            if ended >= HOLDOUT_START:
                return True
    for key in ("oos_start", "test_start"):
        started = _date_or_none(row.get(key))
        if started is not None and started >= HOLDOUT_START:
            return True
    if require_known_end and not ends and any(key in row for key in ("oos_end", "test_end")):
        return True
    return False


def _artifact_blocked(row: dict[str, Any]) -> bool:
    """Artifacts must be non-model and bound to proven non-holdout lineage."""
    kind = str(row.get("artifact_type") or "").lower()
    if not kind:
        return True
    haystack = " ".join(str(row.get(k) or "") for k in ("artifact_type", "logical_path", "artifact_key", "transport")).lower()
    if any(token in haystack for token in MODEL_TOKENS):
        return True
    if _text_has_holdout(row.get("artifact_type"), row.get("logical_path"), row.get("artifact_key"), row.get("research_run_id"), row.get("research_experiment_id")):
        return True
    if str(row.get("lineage_status") or "") not in ARTIFACT_LINEAGE_ALLOWED:
        return True
    total = row.get("run_experiment_count")
    visible = row.get("run_visible_experiment_count")
    if total is None or visible is None:
        return True
    try:
        if int(total) <= 0 or int(visible) <= 0:
            return True
        if row.get("research_experiment_id") in (None, "") and int(visible) != int(total):
            return True
    except (TypeError, ValueError):
        return True
    if "payload_json" in row or "payload" in row:
        return True
    return False


def _public_artifact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "strategy_id": row.get("strategy_id"),
        "research_run_id": row.get("research_run_id"),
        "research_experiment_id": row.get("research_experiment_id"),
        "artifact_type": row.get("artifact_type"),
        "artifact_sha256": row.get("artifact_sha256"),
        "artifact_valid": row.get("artifact_valid"),
        "transport": row.get("transport"),
        "created_at": row.get("created_at"),
        "synced_at": row.get("synced_at"),
        "lineage_status": row.get("lineage_status"),
        "export_scope": "INTERNAL_SUMMARY",
    }


@register("get_strategy_summary")
def get_strategy_summary(strategy: str | None = None) -> dict[str, Any]:
    wanted = optional_strategy(strategy)

    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            ctx = strategies_context(conn)
        rows = list(ctx.get("strategies") or [])
        if wanted:
            rows = [row for row in rows if row.get("strategy_id") == wanted]
            if not rows:
                raise GatewayError(UNKNOWN_STRATEGY, "strategy not found")
        safe_rows = []
        for row in rows:
            item = {k: v for k, v in row.items() if not _text_has_holdout(k) or k in {"holdout_status", "holdout_exposure_status"}}
            item["holdout_metrics"] = "NOT_EXPOSED"
            safe_rows.append(item)
        body = {
            "strategies": safe_rows,
            "note": ctx.get("note"),
            "assessment_note": "economic_gate remains NOT_DEFINED unless a human supplied thresholds. COMPLETE is not PASS.",
            "holdout_note": "Stage 2 final holdout (2025+) status is reported as LOCKED/EXCLUDED metadata only; no holdout metrics exist in this gateway.",
        }
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_strategy_research_summary"], captured_at=captured_at),
            tool="get_strategy_summary",
            available=bool(rows),
            unavailable_reason=None if rows else "no strategy runs",
            snapshot=snapshot,
        )

    return cached("strategy_summary", wanted or "all", _build)


@register("get_strategy_oos_windows")
def get_strategy_oos_windows(strategy: str) -> dict[str, Any]:
    wanted = optional_strategy(strategy)
    if not wanted:
        raise GatewayError(UNKNOWN_STRATEGY, "strategy is required")

    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            if not _view_exists(conn, "mi_v_strategy_oos_windows"):
                return respond(
                    {"strategy": wanted, "windows": [], "holdout_excluded": False, "holdout_filter": {"sql_view": False, "gateway": False}},
                    provenance=live_provenance(views=["mi_v_strategy_oos_windows"], captured_at=captured_at),
                    tool="get_strategy_oos_windows",
                    available=False,
                    unavailable_reason="strategy OOS view (migration 027) is not installed",
                    snapshot=snapshot,
                )
            rows = [
                dict(r)
                for r in conn.execute(
                    text("SELECT * FROM mi_v_strategy_oos_windows WHERE strategy_id = :sid ORDER BY oos_start LIMIT 400"),
                    {"sid": wanted},
                ).mappings().all()
            ]
        kept = [row for row in rows if not _holdout_blocked(row)]
        body = {
            "strategy": wanted,
            "windows": kept,
            "holdout_excluded": True,
            "holdout_filter": {"sql_view": True, "gateway": True, "rows_from_view": len(rows), "rows_removed_by_gateway": len(rows) - len(kept)},
            "holdout_start": HOLDOUT_START.isoformat(),
            "note": "Windows come from proven non-holdout runs; unknown or 2025+ boundaries are excluded in SQL and again in the gateway.",
        }
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_strategy_oos_windows"], captured_at=captured_at),
            tool="get_strategy_oos_windows",
            available=True,
            snapshot=snapshot,
        )

    return cached("strategy_oos_windows", wanted, _build)


@register("get_strategy_experiments")
def get_strategy_experiments(strategy: str | None = None) -> dict[str, Any]:
    wanted = optional_strategy(strategy)

    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            if not _view_exists(conn, "mi_v_strategy_experiments"):
                return respond(
                    {"strategy": wanted, "experiments": [], "artifacts": [], "holdout_excluded": False, "holdout_filter": {"sql_view": False, "gateway": False}},
                    provenance=live_provenance(views=["mi_v_strategy_experiments"], captured_at=captured_at),
                    tool="get_strategy_experiments",
                    available=False,
                    unavailable_reason="strategy experiment view (migration 027) is not installed",
                    snapshot=snapshot,
                )
            sql = "SELECT * FROM mi_v_strategy_experiments"
            params: dict[str, Any] = {}
            if wanted:
                sql += " WHERE strategy_id = :sid"
                params["sid"] = wanted
            sql += " ORDER BY strategy_id, research_run_id, research_window_id LIMIT :lim"
            params["lim"] = 400
            rows = [dict(r) for r in conn.execute(text(sql), params).mappings().all()]
            artifacts_raw: list[dict[str, Any]] = []
            artifact_view = _view_exists(conn, "mi_v_strategy_artifact_status")
            if artifact_view:
                art_sql = "SELECT * FROM mi_v_strategy_artifact_status"
                art_params: dict[str, Any] = {}
                if wanted:
                    art_sql += " WHERE strategy_id = :sid"
                    art_params["sid"] = wanted
                art_sql += " ORDER BY synced_at DESC NULLS LAST LIMIT 80"
                artifacts_raw = [dict(r) for r in conn.execute(text(art_sql), art_params).mappings().all()]
        kept = [row for row in rows if not _holdout_blocked(row)]
        visible_runs = {str(row.get("research_run_id")) for row in kept}
        artifacts = [
            _public_artifact(row)
            for row in artifacts_raw
            if not _artifact_blocked(row) and str(row.get("research_run_id")) in visible_runs
        ]
        body = {
            "strategy": wanted,
            "experiments": kept,
            "artifacts": artifacts,
            "holdout_excluded": True,
            "holdout_filter": {
                "sql_view": True,
                "gateway": True,
                "experiments_from_view": len(rows),
                "experiments_removed_by_gateway": len(rows) - len(kept),
                "artifacts_from_view": len(artifacts_raw),
                "artifacts_removed_by_gateway": len(artifacts_raw) - len(artifacts),
                "artifact_view_installed": artifact_view,
            },
            "model_binaries": "never exported",
            "artifact_payloads": "never exported (metadata and sha256 only)",
            "note": "Metrics may be reconstructed from monthly returns. Null means missing, not zero. Artifacts are bound to proven non-holdout experiments/runs.",
        }
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_strategy_experiments", "mi_v_strategy_artifact_status"], captured_at=captured_at),
            tool="get_strategy_experiments",
            available=True,
            snapshot=snapshot,
        )

    return cached("strategy_experiments", wanted or "all", _build)


@register("get_data_health")
def get_data_health() -> dict[str, Any]:
    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            health = data_health_context(conn)
        sources = []
        for row in health.get("sources") or []:
            sources.append(
                {
                    "dataset": row.get("freshness_dataset") or row.get("dataset"),
                    "provider": row.get("provider") or row.get("source_id"),
                    "source_id": row.get("source_id"),
                    "access_status": row.get("access_status"),
                    "enabled": row.get("enabled"),
                    "usage_scope": row.get("usage_scope"),
                    "latest_observation": row.get("latest_observation_date"),
                    "latest_observation_retrieved_at": row.get("latest_observation_retrieved_at"),
                    "latest_successful_ingestion": row.get("last_success_at") or row.get("latest_success_at"),
                    "last_attempt_at": row.get("last_attempt_at"),
                    "expected_cadence": row.get("dataset_cadence") or row.get("expected_cadence"),
                    "age_days": row.get("age_days"),
                    "stale_after_estimate": row.get("stale_after_estimate"),
                    "freshness_status": row.get("freshness_status"),
                    "stored_freshness_status": row.get("stored_freshness_status"),
                    "transport_status": row.get("transport_status"),
                    "last_error": row.get("last_error_redacted") or row.get("last_error") or row.get("transport_error"),
                    "retired_optional": bool(row.get("retired_optional")),
                    "evaluated_on": row.get("evaluated_on"),
                    "freshness_policy_version": row.get("freshness_policy_version"),
                    "export_scope": "INTERNAL_SUMMARY",
                }
            )
        body = {
            "sources": sources,
            "not_current": [row for row in sources if row.get("freshness_status") in {"STALE", "INGESTION_OVERDUE", "TRANSPORT_FAILURE", "INVALID_FUTURE", "MISSING"} and not row.get("retired_optional")],
            "stale": [row for row in sources if row.get("freshness_status") == "STALE" and not row.get("retired_optional")],
            "failed_transport": health.get("failed_transport"),
            "quarantine": health.get("quarantine"),
            "finra_quarantine": health.get("finra_quarantine"),
            "freshness_policy_version": FRESHNESS_POLICY_VERSION,
            "status_vocabulary": ["LATEST_AVAILABLE", "AWAITING_RELEASE", "INGESTION_OVERDUE", "STALE", "MISSING", "INVALID_FUTURE", "TRANSPORT_FAILURE"],
            "calendar_note": "Daily series are judged against their publication calendar (NYSE for equities, Treasury/SIFMA-style for rates, federal for macro releases) with observed holidays; a successful fetch never makes an old observation current.",
            "notes": SCHEMA_NOTES,
        }
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_source_health"], captured_at=captured_at),
            tool="get_data_health",
            available=bool(sources),
            unavailable_reason=None if sources else "no sources registered",
            snapshot=snapshot,
        )

    return cached("data_health", "latest", _build)


@register("get_market_changes")
def get_market_changes(since: str = "previous_session") -> dict[str, Any]:
    parsed = parse_since(since)

    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            rates = rates_context(conn)
            credit = credit_context(conn)
            sectors = sectors_context(conn)
            macro = macro_context(conn)
            order_flow = order_flow_overview(conn)
            prior = None
            if _view_exists(conn, "mi_v_morning_context_index"):
                index = morning_index(conn, limit=8)
                if len(index) > 1:
                    prior = index[1]
        session = build_session_changes(rates=rates, credit=credit, sectors=sectors, order_flow=order_flow)
        changed = build_what_changed(rates=rates, credit=credit, sectors=sectors, macro=macro, order_flow=order_flow)
        body = {
            "since": parsed.isoformat() if isinstance(parsed, date) else parsed,
            "session_changes": session,
            "what_changed": changed,
            "prior_snapshot": None
            if prior is None
            else {
                "snapshot_id": prior.get("snapshot_id"),
                "generated_at": prior.get("generated_at"),
                "cutoff_at": prior.get("cutoff_at"),
                "as_of_date": prior.get("as_of_date"),
                "completeness": prior.get("completeness"),
            },
            "interpretation": "NONE",
            "notes": SCHEMA_NOTES,
        }
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_metric_latest", "mi_v_sector_latest"], captured_at=captured_at),
            tool="get_market_changes",
            available=bool(session or changed),
            unavailable_reason=None if session or changed else "no stored changes",
            snapshot=snapshot,
        )

    return cached("market_changes", str(parsed), _build)


def refuse_holdout() -> None:
    raise GatewayError(HOLDOUT_FORBIDDEN, "Stage 2 final holdout is not available through this gateway")
