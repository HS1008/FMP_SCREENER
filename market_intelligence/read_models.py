"""Read models over curated ``mi_v_*`` views. Used by Streamlit pages, the AI context API,
and the morning-context builder. Every function takes an open connection; callers decide
whether it is the read-only role (pages/API) or the writer inside a snapshot transaction.

All returned structures are strict-JSON friendly (dates as ISO strings, no NaN) and every
metric carries ``units``, ``as_of``, ``source``/``export_scope`` so export filtering can be
applied uniformly.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import text

from market_intelligence.catalog import CATALOG, CATALOG_BY_ID, CURVE_SLOPES, CURVE_TENORS, FRED_ATTRIBUTION, CREDIT_SERIES
from market_intelligence.nulls import normalize_payload

MAX_HISTORY_ROWS = 4000


def _rows(conn, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [normalize_payload(dict(r)) for r in conn.execute(text(sql), params or {}).mappings().all()]


# ---- primitives ------------------------------------------------------------------------

def source_health(conn) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT * FROM mi_v_source_health ORDER BY source_id, freshness_dataset")


def recent_runs(conn, limit: int = 200) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT * FROM mi_v_ingestion_runs_recent ORDER BY started_at DESC LIMIT :limit", {"limit": int(limit)})


def macro_latest(conn) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT * FROM mi_v_macro_latest ORDER BY category, subcategory, series_id")


def metric_latest(conn) -> dict[str, dict[str, Any]]:
    rows = _rows(conn, "SELECT * FROM mi_v_metric_latest")
    return {r["metric_id"]: r for r in rows}


def metric_history(conn, metric_id: str, *, start: date | None = None, limit: int = MAX_HISTORY_ROWS) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        SELECT as_of, value, units, status FROM mi_v_metric_history
        WHERE metric_id = :metric_id AND (:start IS NULL OR as_of >= :start)
        ORDER BY as_of DESC LIMIT :limit
        """,
        {"metric_id": metric_id, "start": start, "limit": int(limit)},
    )[::-1]


def observation_history(conn, series_id: str, *, start: date | None = None, limit: int = MAX_HISTORY_ROWS) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        SELECT observation_date, value FROM mi_v_macro_observations_current
        WHERE series_id = :series_id AND value IS NOT NULL AND (:start IS NULL OR observation_date >= :start)
        ORDER BY observation_date DESC LIMIT :limit
        """,
        {"series_id": series_id, "start": start, "limit": int(limit)},
    )[::-1]


def credit_latest(conn) -> list[dict[str, Any]]:
    rows = _rows(conn, "SELECT * FROM mi_v_credit_latest")
    order = {sid: i for i, sid in enumerate(CREDIT_SERIES)}
    return sorted(rows, key=lambda r: order.get(r["series_id"], 999))  # catalog order, never by value


def sector_latest(conn, dataset: str | None = None) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT * FROM mi_v_sector_latest WHERE (:dataset IS NULL OR dataset = :dataset) ORDER BY dataset, entity_kind, sector_key",
        {"dataset": dataset},
    )


def industry_latest(conn, dataset: str | None = None, parent_sector_key: str | None = None) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        SELECT * FROM mi_v_industry_latest
        WHERE (:dataset IS NULL OR dataset = :dataset) AND (:parent IS NULL OR parent_sector_key = :parent)
        ORDER BY dataset, parent_sector_key, industry_key
        """,
        {"dataset": dataset, "parent": parent_sector_key},
    )


def morning_latest(conn) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM mi_v_morning_context_latest")
    return rows[0] if rows else None


def morning_index(conn, limit: int = 50) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT * FROM mi_v_morning_context_index ORDER BY generated_at DESC LIMIT :limit", {"limit": int(limit)})


def strategy_summary(conn) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT * FROM mi_v_strategy_research_summary ORDER BY strategy_id")


def research_ideas(conn) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT * FROM mi_v_research_ideas ORDER BY updated_at DESC")


# ---- composite contexts ---------------------------------------------------------------------

def _metric_entry(metrics: dict[str, dict[str, Any]], metric_id: str) -> dict[str, Any] | None:
    row = metrics.get(metric_id)
    if row is None:
        return None
    detail = row.get("detail_json") or {}
    return {
        "metric_id": metric_id,
        "value": row.get("value"),
        "units": row.get("units"),
        "as_of": row.get("as_of"),
        "status": row.get("status"),
        "transform_version": row.get("transform_version"),
        "comparison": {k: detail.get(k) for k in ("comparison_date", "anchor_date", "lag_date", "gap_days", "anchor_lag_days", "missing_legs") if detail.get(k) is not None},
        "reason": detail.get("reason"),
    }


def _series_block(series_row: dict[str, Any], metrics: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sid = series_row["series_id"]
    spec = CATALOG_BY_ID.get(sid)
    transforms: dict[str, Any] = {}
    prefix = sid + "."
    for metric_id in sorted(m for m in metrics if m.startswith(prefix)):
        entry = _metric_entry(metrics, metric_id)
        if entry is not None:
            transforms[metric_id[len(prefix):]] = entry
    return {
        "series_id": sid,
        "label": spec.label if spec else series_row.get("title"),
        "title": series_row.get("title"),
        "category": series_row.get("category"),
        "subcategory": series_row.get("subcategory"),
        "latest": {"value": series_row.get("value"), "observation_date": series_row.get("observation_date"), "units": series_row.get("units"), "retrieved_at": series_row.get("retrieved_at")},
        "frequency": series_row.get("frequency_short"),
        "seasonal_adjustment": series_row.get("seasonal_adjustment_short"),
        "vintage_kind": series_row.get("vintage_kind"),
        "pit_safe": series_row.get("pit_safe"),
        "metadata_status": series_row.get("metadata_status"),
        "export_scope": series_row.get("export_scope"),
        "source": {"provider": "FRED", "url": series_row.get("source_url"), "attribution": spec.attribution if spec else FRED_ATTRIBUTION},
        "notes": spec.notes if spec else None,
        "transforms": transforms,
    }


def macro_context(conn) -> dict[str, Any]:
    latest = macro_latest(conn)
    metrics = metric_latest(conn)
    by_category: dict[str, list[dict[str, Any]]] = {}
    for row in latest:
        if row.get("observation_date") is None:
            continue
        by_category.setdefault(row["category"] or "uncategorized", []).append(_series_block(row, metrics))
    missing = [s.series_id for s in CATALOG if s.series_id not in {r["series_id"] for r in latest if r.get("observation_date")}]
    return {
        "categories": by_category,
        "series_without_data": missing,
        "attribution": FRED_ATTRIBUTION,
    }


def rates_context(conn) -> dict[str, Any]:
    latest = {r["series_id"]: r for r in macro_latest(conn)}
    metrics = metric_latest(conn)
    curve = []
    for tenor, sid in CURVE_TENORS.items():
        row = latest.get(sid)
        curve.append(
            {
                "tenor": tenor,
                "series_id": sid,
                "yield_pct": row.get("value") if row else None,
                "observation_date": row.get("observation_date") if row else None,
                "chg_prev_bps": (_metric_entry(metrics, sid + ".chg_prev_bps") or {}).get("value"),
                "chg_1w_bps": (_metric_entry(metrics, sid + ".chg_1w_bps") or {}).get("value"),
                "chg_1m_bps": (_metric_entry(metrics, sid + ".chg_1m_bps") or {}).get("value"),
                "chg_3m_bps": (_metric_entry(metrics, sid + ".chg_3m_bps") or {}).get("value"),
                "export_scope": row.get("export_scope") if row else None,
            }
        )
    slopes = {name: _metric_entry(metrics, "curve.slope_{0}_bps".format(name)) for name in CURVE_SLOPES}
    dates = {c["observation_date"] for c in curve if c["observation_date"]}
    real = [_series_block(latest[s], metrics) for s in ("DFII5", "DFII10", "DFII20", "DFII30") if s in latest and latest[s].get("observation_date")]
    comp = [_series_block(latest[s], metrics) for s in ("T5YIE", "T10YIE", "T5YIFR") if s in latest and latest[s].get("observation_date")]
    policy = [_series_block(latest[s], metrics) for s in ("DFF", "SOFR") if s in latest and latest[s].get("observation_date")]
    return {
        "curve": curve,
        "curve_dates_mixed": len(dates) > 1,
        "curve_observation_dates": sorted(d for d in dates),
        "slopes": slopes,
        "real_yields": real,
        "inflation_compensation": comp,
        "policy": policy,
        "units_note": "Yields in percent; changes in basis points (percent x 100).",
        "attribution": FRED_ATTRIBUTION,
    }


def credit_context(conn) -> dict[str, Any]:
    rows = credit_latest(conn)
    buckets = []
    for row in rows:
        spec = CATALOG_BY_ID.get(row["series_id"])
        buckets.append(
            {
                "series_id": row["series_id"],
                "bucket": row["bucket"],
                "label": spec.label if spec else row["series_id"],
                "as_of": row["as_of"],
                "oas_bps": row["oas_bps"],
                "change_1d_bps": row["change_1d_bps"],
                "change_1w_bps": row["change_1w_bps"],
                "change_1m_bps": row["change_1m_bps"],
                "change_3m_bps": row["change_3m_bps"],
                "percentile_window": row["percentile_window"],
                "percentile": row["percentile"],
                "zscore": row["zscore"],
                "window_observations": row["window_observations"],
                "history_first_date": row["history_first_date"],
                "history_status": row["history_status"],
                "units": row["units"],
                "export_scope": row["export_scope"],
                "source": row.get("source_refs"),
            }
        )
    return {
        "buckets": buckets,
        "coverage_note": "Provider history is limited; percentiles cover only the labeled window (1Y/3Y/AVAILABLE) or are NULL.",
        "attribution": (CATALOG_BY_ID[CREDIT_SERIES[0]].attribution if CREDIT_SERIES else None),
    }


def sectors_context(conn) -> dict[str, Any]:
    rows = sector_latest(conn)
    datasets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        datasets.setdefault(row["dataset"], []).append(
            {
                "sector_key": row["sector_key"],
                "entity_kind": row["entity_kind"],
                "canonical_sector": row["canonical_sector"],
                "provider_label": row["provider_label"],
                "instrument_id": row["instrument_id"],
                "as_of": row["as_of"],
                "benchmark": row["benchmark"],
                "return_basis": row["return_basis"],
                "value_basis": row["value_basis"],
                "universe_method": row["universe_method"],
                "research_eligible": row["research_eligible"],
                "metrics": row["metrics_json"],
                "coverage": row["coverage_json"],
                "source_id": row["source_id"],
                "export_scope": "INTERNAL_ONLY" if row["source_id"] == "FMP_LEGACY" else "ATTRIBUTION_REQUIRED",
                "artifact_sha256": row["artifact_sha256"],
            }
        )
    as_of_by_dataset = {ds: sorted({r["as_of"] for r in items if r["as_of"]}) for ds, items in datasets.items()}
    return {"datasets": datasets, "as_of_by_dataset": as_of_by_dataset, "note": "Per-dataset as_of; rotation and dispersion bundles may differ."}


def industries_context(conn) -> dict[str, Any]:
    rows = industry_latest(conn)
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in rows:
        out.setdefault(row["dataset"], {}).setdefault(row["parent_sector_key"], []).append(
            {
                "industry_key": row["industry_key"],
                "instrument_id": row["instrument_id"],
                "as_of": row["as_of"],
                "benchmark": row["benchmark"],
                "return_basis": row["return_basis"],
                "metrics": row["metrics_json"],
                "export_scope": "INTERNAL_ONLY" if row["source_id"] == "FMP_LEGACY" else "ATTRIBUTION_REQUIRED",
            }
        )
    return {"datasets": out}


def data_health_context(conn) -> dict[str, Any]:
    health = source_health(conn)
    return {"sources": health, "stale": [h for h in health if h.get("freshness_status") == "STALE"], "failed_transport": [h for h in health if h.get("transport_status") == "FAILED"]}


def strategies_context(conn) -> dict[str, Any]:
    rows = strategy_summary(conn)
    strategies = []
    for row in rows:
        strategies.append(
            {
                "strategy_id": row["strategy_id"],
                "research_kind": row["research_kind"],
                "research_mode": row["research_mode"],
                "asset_class": row["asset_class"],
                "research_status": row["run_status"],
                "economic_gate": row["economic_gate"] or "NOT_DEFINED",
                "promotion_gate": row["promotion_gate"] or "LOCKED",
                "holdout_status": row["holdout_status"] or row["holdout_exposure_status"],
                "delivery_status": row["delivery_status"],
                "expected_experiments": row["expected_experiment_count"],
                "completed_experiments": row["completed_count"],
                "failed_experiments": row["failed_count"],
                "last_seen_at": row["last_seen_at"],
                "metric_provenance_note": "Aggregate risk statistics may be reconstructed from monthly returns; they are not native QC runtime statistics.",
                "export_scope": "INTERNAL_SUMMARY",
            }
        )
    return {"strategies": strategies, "note": "COMPLETE research is not an economic pass; promotion stays locked until a human decision."}


__all__ = [
    "credit_context",
    "credit_latest",
    "data_health_context",
    "industries_context",
    "industry_latest",
    "macro_context",
    "macro_latest",
    "metric_history",
    "metric_latest",
    "morning_index",
    "morning_latest",
    "observation_history",
    "rates_context",
    "recent_runs",
    "research_ideas",
    "sector_latest",
    "sectors_context",
    "source_health",
    "strategies_context",
    "strategy_summary",
]
