"""Semantic read-only queries over curated ``mi_v_*`` views. No arbitrary SQL."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

from sqlalchemy import text

from market_intelligence.catalog import CATALOG_BY_ID, CURVE_SLOPES, CURVE_TENORS, FRED_ATTRIBUTION
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
    clamp_limit,
    history_limit,
    optional_strategy,
    parse_date,
    parse_since,
    require_series_id,
    resolve_sector,
)

SCHEMA_NOTES = {
    "observed": "Values stored by ingestion or the provider.",
    "derived": "Deterministic calculations from stored observations (curve slopes, deltas).",
    "interpretation": "NONE — the gateway does not emit market opinions.",
}

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
        "POST_HOLDOUT_ML_TRAIN",
        "POST_HOLDOUT_ML_OOS",
        "POST_HOLDOUT_WFO_TRAIN",
        "POST_HOLDOUT_WFO_TEST",
    }
)

HOLDOUT_START = date(2025, 1, 1)

TOOL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "get_morning_context",
        "description": "Compact morning market snapshot from the published frozen morning_context_v2 artifact, plus live freshness.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_market_pulse",
        "description": "Current Market Pulse: sector 1D leadership, rates, credit identity, bond activity, overnight quotes.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_rates_curve",
        "description": "Treasury curve tenors and slopes from the freshest published FRED observations, with provenance.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_macro_overview",
        "description": "Canonical macro catalog latest values grouped by category.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_macro_series",
        "description": "Bounded history for one catalog series_id.",
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
        "description": "ICE BofA OAS buckets via FRED (IG, HY, rating buckets) with freshness. Owner sessions include values.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_sector_rotation",
        "description": "Sector ETF rotation vs SPY including 1D absolute return and available RS windows.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_sector_detail",
        "description": "One canonical sector plus its industries.",
        "inputSchema": {
            "type": "object",
            "properties": {"sector": {"type": "string"}},
            "required": ["sector"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_industry_rotation",
        "description": "Industry-level relative strength vs parent sector ETF from stored snapshots.",
        "inputSchema": {
            "type": "object",
            "properties": {"sector": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_subindustry_rotation",
        "description": "Subindustry rotation if present in the canonical store. Currently unavailable; returns the industry leaf.",
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
        "description": "Ingested Stage 1/Stage 2 research status. COMPLETE is not an economic pass. Holdout metrics are excluded.",
        "inputSchema": {
            "type": "object",
            "properties": {"strategy": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_strategy_oos_windows",
        "description": "Non-holdout OOS windows for a strategy. Windows ending 2025-01-01 or later are excluded.",
        "inputSchema": {
            "type": "object",
            "properties": {"strategy": {"type": "string"}},
            "required": ["strategy"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_strategy_experiments",
        "description": "Non-holdout ingested experiments/backtests for a strategy. No model binaries.",
        "inputSchema": {
            "type": "object",
            "properties": {"strategy": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_data_health",
        "description": "Per-dataset freshness, lag, last error, and stale status using cadence-aware rules.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_market_changes",
        "description": "Factual day-to-day and longer changes from stored analytics. No causal language.",
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


def _sector_payload(sectors: dict[str, Any]) -> dict[str, Any]:
    rows = list((sectors.get("datasets") or {}).get("ETF_RS_VS_SPY") or [])
    ranked_1d = _rank_sectors(rows, "ret_1d")
    items = []
    for row in ranked_1d:
        metrics = row.get("metrics") or {}
        items.append(
            {
                "sector": row.get("canonical_sector") or row.get("sector_key"),
                "sector_key": row.get("sector_key"),
                "entity_kind": row.get("entity_kind"),
                "instrument_id": row.get("instrument_id"),
                "benchmark": row.get("benchmark"),
                "as_of": row.get("as_of"),
                "source_id": row.get("source_id"),
                "export_scope": row.get("export_scope"),
                "classification_version": CLASSIFICATION_VERSION,
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
                "legacy_fmp": row.get("source_id") == "FMP_LEGACY",
                "value_kind": {
                    "ret_1d": "observed",
                    "rs_1d": "observed" if metrics.get("rs_chg_1d") is not None else "unavailable",
                    "rs_1m": "observed",
                },
            }
        )
    return {
        "dataset": "ETF_RS_VS_SPY",
        "as_of_by_dataset": (sectors.get("as_of_by_dataset") or {}).get("ETF_RS_VS_SPY"),
        "note": (
            "1D return is the last stored ETF session on adjusted close. Relative strength is the "
            "change in the ETF/SPY adjusted-close ratio. rs_chg_1d is exposed only when stored; "
            "it is not manufactured. FMP_LEGACY marks precomputed bundles, not a live FMP call."
        ),
        "rows": items,
        "source": "mi_v_sector_latest",
    }


def _industry_rows(industries: dict[str, Any], *, sector: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    datasets = industries.get("datasets") or {}
    rs = datasets.get("INDUSTRY_RS_VS_SECTOR_ETF") or {}
    items: list[dict[str, Any]] = []
    for parent, rows in rs.items():
        if sector and parent != sector and parent not in {sector, sector.replace(" ", "_")}:
            if parent.lower() != sector.lower():
                continue
        for row in rows:
            metrics = row.get("metrics") or {}
            items.append(
                {
                    "sector": parent,
                    "industry": row.get("industry_key"),
                    "subindustry": None,
                    "instrument_id": row.get("instrument_id"),
                    "benchmark": row.get("benchmark"),
                    "as_of": row.get("as_of"),
                    "ret_1d": metrics.get("ret_1d"),
                    "rs_1d": metrics.get("rs_chg_1d"),
                    "rs_1w": metrics.get("rs_chg_1w"),
                    "rs_1m": metrics.get("rs_chg_1m"),
                    "rs_3m": metrics.get("rs_chg_3m"),
                    "rs_12m": metrics.get("rs_chg_12m"),
                    "export_scope": row.get("export_scope"),
                    "source": "mi_v_industry_latest",
                    "legacy_fmp": True,
                    "hierarchy": "sector > industry",
                }
            )
    items.sort(key=lambda r: (str(r.get("sector") or ""), str(r.get("industry") or "")))
    return items[:limit]


def _compact_morning(snapshot: dict[str, Any], health: dict[str, Any], pulse: dict[str, Any]) -> dict[str, Any]:
    body = snapshot.get("snapshot_json") or {}
    sections = body.get("sections") or {}
    stale = [s for s in (health.get("sources") or []) if s.get("freshness_status") == "STALE"]
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
            "stale_count": len(stale),
            "stale": [
                {
                    "dataset": row.get("freshness_dataset") or row.get("dataset"),
                    "provider": row.get("provider") or row.get("source_id"),
                    "latest_observation": row.get("latest_observation_date"),
                    "status": row.get("freshness_status"),
                    "cadence": row.get("dataset_cadence"),
                }
                for row in stale[:40]
            ],
            "policy": "freshness_policy_v1 (business-day aware for daily series)",
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
            "slopes": data.get("slopes"),
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


def _pulse_body(conn) -> dict[str, Any]:
    rates = rates_context(conn)
    credit = credit_context(conn)
    sectors = sectors_context(conn)
    order_flow = order_flow_overview(conn)
    collectors = ibkr_collector_status(conn)
    quotes = ibkr_quotes_latest(conn)
    quote_state = derive_quote_status(collectors=collectors, quotes=quotes)
    sector_payload = _sector_payload(sectors)
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
                "delay_status": row.get("market_data_type") or row.get("delay_status"),
                "kind": "observed",
            }
        )
    return {
        "headline": {
            "us_10y": ten,
            "ig_oas": ig,
            "hy_oas": hy,
            "sector_lead_1d": lead,
            "sector_lag_1d": lag,
        },
        "indexes": indexes,
        "overnight_quotes": morning_overnight_section(quote_state),
        "sectors": sector_payload,
        "session_changes": build_session_changes(rates=rates, credit=credit, sectors=sectors, order_flow=order_flow),
        "attribution": FRED_ATTRIBUTION,
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
            provenance=live_provenance(views=["mi_v_sector_latest", "mi_v_metric_latest", "mi_v_macro_latest"], captured_at=captured_at),
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
        body = {
            "curve": rates.get("curve"),
            "slopes": rates.get("slopes"),
            "slope_aliases": {
                "2s10s": "10Y2Y",
                "2s30s": "30Y2Y",
                "5s30s": "30Y5Y",
                "3m10y": "10Y3M",
            },
            "curve_dates_mixed": rates.get("curve_dates_mixed"),
            "real_yields": rates.get("real_yields"),
            "inflation_compensation": rates.get("inflation_compensation"),
            "policy": rates.get("policy"),
            "units_note": rates.get("units_note"),
            "attribution": rates.get("attribution"),
            "source_priority": {
                "current": "FRED constant-maturity (DGS*)",
                "fallback": None,
                "fiscal_data": "NOT_IMPLEMENTED",
                "note": "The canonical store currently ingests Treasury yields from FRED. FiscalData/Treasury is not a live producer in this repository.",
            },
            "tenors": list(CURVE_TENORS),
            "derived_slopes": list(CURVE_SLOPES),
            "notes": SCHEMA_NOTES,
        }
        available = any((row or {}).get("yield_pct") is not None for row in (rates.get("curve") or []))
        return respond(
            body if available else None,
            provenance=live_provenance(views=["mi_v_macro_latest", "mi_v_metric_latest"], captured_at=captured_at),
            tool="get_rates_curve",
            available=available,
            unavailable_reason=None if available else "no Treasury yields published",
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
            {**macro, "notes": SCHEMA_NOTES},
            provenance=live_provenance(views=["mi_v_macro_latest", "mi_v_metric_latest"], captured_at=captured_at),
            tool="get_macro_overview",
            available=bool(macro.get("categories")),
            unavailable_reason=None if macro.get("categories") else "no macro observations",
            snapshot=snapshot,
        )

    return cached("macro_overview", "latest", _build)


@register("get_macro_series")
def get_macro_series(series_id: str, start_date: str | None = None, end_date: str | None = None, limit: int | None = None) -> dict[str, Any]:
    sid = require_series_id(series_id)
    start = parse_date(start_date, field="start_date")
    end = parse_date(end_date, field="end_date")
    bounded_range(start, end)
    row_limit = history_limit(limit)
    spec = CATALOG_BY_ID[sid]
    cache_key = "{0}:{1}:{2}:{3}".format(sid, start, end, row_limit)

    def _build() -> dict[str, Any]:
        with _readonly() as conn:
            captured_at = _capture(conn)
            snapshot = morning_latest(conn)
            history = observation_history(conn, sid, start=start, limit=row_limit)
            if end is not None:
                history = [row for row in history if parse_date(row.get("observation_date"), field="observation_date") is not None and parse_date(row.get("observation_date"), field="observation_date") <= end]
            latest = history[-1] if history else None
            previous = history[-2] if len(history) > 1 else None
            latest_value = None if latest is None else latest.get("value")
            previous_value = None if previous is None else previous.get("value")
            change = None if latest_value is None or previous_value is None else latest_value - previous_value
            body = {
                "series_id": sid,
                "description": spec.label,
                "category": spec.category,
                "frequency": spec.expected_frequency,
                "units": ", ".join(spec.expected_units_contains) if spec.expected_units_contains else None,
                "source": {"provider": "FRED", "attribution": spec.attribution},
                "export_scope": spec.export_scope,
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
            provenance=live_provenance(views=["mi_v_macro_observations_current"], captured_at=captured_at),
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
        body = {**credit, "notes": SCHEMA_NOTES, "value_kind": "observed (OAS); changes are derived"}
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
        body = _sector_payload(sectors)
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_sector_latest"], captured_at=captured_at),
            tool="get_sector_rotation",
            available=bool(body.get("rows")),
            unavailable_reason=None if body.get("rows") else "no sector snapshots",
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
        rotation = _sector_payload(sectors)
        match = next((row for row in rotation["rows"] if row.get("sector") == canonical or row.get("sector_key") == canonical), None)
        industry_rows = _industry_rows(industries, sector=canonical)
        body = {
            "sector": canonical,
            "classification_version": CLASSIFICATION_VERSION,
            "canonical_sectors": list(CANONICAL_SECTORS),
            "sector_row": match,
            "industries": industry_rows,
            "subindustries": [],
            "subindustry_status": "DATA_NOT_AVAILABLE",
            "notes": SCHEMA_NOTES,
        }
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_sector_latest", "mi_v_industry_latest"], captured_at=captured_at),
            tool="get_sector_detail",
            available=match is not None or bool(industry_rows),
            unavailable_reason=None if match or industry_rows else "sector not present in stored snapshots",
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
        rows = _industry_rows(industries, sector=canonical)
        body = {
            "sector": canonical,
            "hierarchy": "sector > industry",
            "rows": rows,
            "legacy_fmp": True,
            "note": (
                "Industry RS is stored from legacy precomputed bundles (FMP_LEGACY). "
                "The gateway does not call the FMP API. Subindustry is not in the canonical store."
            ),
        }
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_industry_latest"], captured_at=captured_at),
            tool="get_industry_rotation",
            available=bool(rows),
            unavailable_reason=None if rows else "no industry snapshots",
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
        rows = _industry_rows(industries, sector=canonical)
        if industry:
            rows = [row for row in rows if str(row.get("industry") or "").lower() == str(industry).lower()]
        body = {
            "available": False,
            "error": DATA_NOT_AVAILABLE,
            "sector": canonical,
            "industry": industry,
            "hierarchy": "sector > industry",
            "subindustry_status": "NOT_IN_CANONICAL_STORE",
            "note": (
                "The non-FMP architecture stores Sector and Industry. Subindustry is not modeled "
                "in mi_industry_snapshots. This tool does not call FMP. Industry rows are returned "
                "as the current leaf of the hierarchy."
            ),
            "industries": rows,
        }
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_industry_latest"], captured_at=captured_at),
            tool="get_subindustry_rotation",
            available=False,
            unavailable_reason="subindustry is not in the canonical store",
            snapshot=snapshot,
            extra={"industry_rows_available": bool(rows)},
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


def _holdout_blocked(row: dict[str, Any]) -> bool:
    test_type = str(row.get("research_test_type") or row.get("test_type") or "")
    phase = str(row.get("research_phase") or row.get("phase") or "")
    if test_type in STAGE2_HOLDOUT_TYPES or phase == "HOLDOUT":
        return True
    if row.get("research_is_holdout") is True:
        return True
    for key in ("oos_end", "test_end"):
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            ended = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        if ended >= HOLDOUT_START:
            return True
    return False


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
        body = {
            "strategies": rows,
            "note": ctx.get("note"),
            "assessment_note": "economic_gate remains NOT_DEFINED unless a human supplied thresholds. COMPLETE is not PASS.",
            "holdout_note": "Stage 2 windows ending 2025-01-01 or later are not exposed.",
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
                    {"strategy": wanted, "windows": []},
                    provenance=live_provenance(views=["mi_v_strategy_oos_windows"], captured_at=captured_at),
                    tool="get_strategy_oos_windows",
                    available=False,
                    unavailable_reason="strategy OOS view is not installed",
                    snapshot=snapshot,
                )
            rows = [
                dict(r)
                for r in conn.execute(
                    text("SELECT * FROM mi_v_strategy_oos_windows WHERE strategy_id = :sid ORDER BY oos_start"),
                    {"sid": wanted},
                ).mappings().all()
            ]
        rows = [row for row in rows if not _holdout_blocked(row)]
        body = {
            "strategy": wanted,
            "windows": rows,
            "holdout_excluded": True,
            "holdout_start": HOLDOUT_START.isoformat(),
            "note": "Holdout / 2025+ Stage 2 windows are filtered in SQL and again in the gateway.",
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
                    {"strategy": wanted, "experiments": []},
                    provenance=live_provenance(views=["mi_v_strategy_experiments"], captured_at=captured_at),
                    tool="get_strategy_experiments",
                    available=False,
                    unavailable_reason="strategy experiment view is not installed",
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
            artifacts = []
            if _view_exists(conn, "mi_v_strategy_artifact_status"):
                art_sql = "SELECT * FROM mi_v_strategy_artifact_status"
                if wanted:
                    art_sql += " WHERE strategy_id = :sid"
                art_sql += " ORDER BY synced_at DESC NULLS LAST LIMIT 80"
                artifacts = [dict(r) for r in conn.execute(text(art_sql), params).mappings().all()]
        rows = [row for row in rows if not _holdout_blocked(row)]
        body = {
            "strategy": wanted,
            "experiments": rows,
            "artifacts": artifacts,
            "holdout_excluded": True,
            "model_binaries": "never exported",
            "note": "Metrics may be reconstructed from monthly returns. Null means missing, not zero.",
        }
        return respond(
            body,
            provenance=live_provenance(views=["mi_v_strategy_experiments"], captured_at=captured_at),
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
                    "latest_observation": row.get("latest_observation_date"),
                    "latest_successful_ingestion": row.get("last_success_at") or row.get("latest_success_at"),
                    "expected_cadence": row.get("dataset_cadence") or row.get("expected_cadence"),
                    "lag_days": row.get("age_days"),
                    "stale_threshold_days": row.get("tolerance_days"),
                    "stale": row.get("freshness_status") == "STALE",
                    "freshness_status": row.get("freshness_status"),
                    "stored_freshness_status": row.get("stored_freshness_status"),
                    "last_error": row.get("last_error") or row.get("transport_error"),
                    "latest_run_status": row.get("transport_status") or row.get("last_run_status"),
                    "fallback_provider": row.get("fallback_source_id"),
                    "evaluated_on": row.get("evaluated_on"),
                    "freshness_policy_version": row.get("freshness_policy_version"),
                    "export_scope": "INTERNAL_SUMMARY",
                }
            )
        body = {
            "sources": sources,
            "stale": [row for row in sources if row.get("stale")],
            "failed_transport": health.get("failed_transport"),
            "quarantine": health.get("quarantine"),
            "finra_quarantine": health.get("finra_quarantine"),
            "calendar_note": "Daily series use US business days (weekends and observed federal holidays are not treated as missing observations).",
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
