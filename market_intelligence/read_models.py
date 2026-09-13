"""Read models over curated ``mi_v_*`` views. Used by Streamlit pages, the AI context API,
and the morning-context builder. Every function takes an open connection; callers decide
whether it is the read-only role (pages/API) or the writer inside a snapshot transaction.

All returned structures are strict-JSON friendly (dates as ISO strings, no NaN) and every
metric carries ``units``, ``as_of``, ``source``/``export_scope`` so export filtering can be
applied uniformly.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text

from market_intelligence.catalog import CATALOG, CATALOG_BY_ID, CURVE_SLOPES, CURVE_TENORS, EXPORT_ATTRIBUTION_REQUIRED, EXPORT_INTERNAL_ONLY, FRED_ATTRIBUTION, CREDIT_SERIES
from market_intelligence.freshness import FRESHNESS_POLICY_VERSION, assess_freshness
from market_intelligence.nulls import normalize_payload

MAX_HISTORY_ROWS = 4000

# Delivery-age policy for published morning snapshots (hours since capture). Versioned so
# the API can state which rule produced ``snapshot_age_status``.
SNAPSHOT_AGE_POLICY_VERSION = "snapshot_age_policy_v1"
SNAPSHOT_AGE_AGING_HOURS = 30
SNAPSHOT_AGE_STALE_HOURS = 78  # spans a weekend + one missed weekday refresh


def _rows(conn, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [normalize_payload(dict(r)) for r in conn.execute(text(sql), params or {}).mappings().all()]


def _today(conn, today: date | None) -> date:
    if today is not None:
        return today
    # Database clock keeps every reader (pages, API, snapshot builder) on one clock source.
    return conn.execute(text("SELECT CURRENT_DATE")).scalar()


# ---- primitives ------------------------------------------------------------------------

def ops_status(conn) -> dict[str, Any]:
    """Compact platform ops row for Data Health / System only."""
    if not _view_exists(conn, "mi_v_ops_status"):
        return {}
    rows = _rows(conn, "SELECT * FROM mi_v_ops_status LIMIT 1")
    return rows[0] if rows else {}


def source_health(conn, *, today: date | None = None) -> list[dict[str, Any]]:
    """Registry x freshness rows with health recomputed against an explicit clock.

    ``stored_freshness_status`` is what the last writer recorded; ``freshness_status`` is
    re-evaluated now from ``latest_observation_date`` and the dataset cadence, so health
    decays even when no ingestion job has run. ``stale_after_estimate`` is the tolerance
    bound, not an official release date.
    """
    today = _today(conn, today)
    rows = _rows(conn, "SELECT * FROM mi_v_source_health ORDER BY source_id, freshness_dataset")
    out = []
    for row in rows:
        cadence = row.get("dataset_cadence") or row.get("expected_cadence")
        latest = row.get("latest_observation_date")
        latest_d = date.fromisoformat(latest) if isinstance(latest, str) else latest
        dataset = str(row.get("freshness_dataset") or row.get("dataset") or "")
        series_id = dataset.split("series:", 1)[1] if dataset.startswith("series:") else None
        assessment = assess_freshness(
            latest_d,
            cadence,
            today,
            series_id=series_id,
            source_id=row.get("source_id"),
            transport_status=row.get("transport_status"),
        )
        row["stored_freshness_status"] = row.get("freshness_status")
        if str(row.get("access_status") or "") in {"RETIRED_OPTIONAL", "RETIRED"}:
            row["freshness_status"] = row.get("freshness_status") or "UNKNOWN"
            row["retired_optional"] = True
        else:
            row["freshness_status"] = assessment.status if latest_d is not None else (row.get("freshness_status") or "MISSING")
        row["age_days"] = assessment.age_days
        row["tolerance_days"] = assessment.tolerance_days if assessment.tolerance_days is not None else row.get("tolerance_days")
        row.pop("expected_next_release", None)  # pre-012 view column name; never an official release date
        if assessment.stale_after is not None:
            row["stale_after_estimate"] = assessment.stale_after.isoformat()
        row["dataset_cadence"] = cadence
        row["evaluated_on"] = today.isoformat()
        row["freshness_policy_version"] = row.get("freshness_policy_version") or FRESHNESS_POLICY_VERSION
        out.append(row)
    return out


def snapshot_age(snapshot: dict[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any]:
    """Delivery-time age of a published snapshot (never stored inside the snapshot body)."""
    now = now or datetime.now(timezone.utc)
    if not snapshot or not snapshot.get("cutoff_at"):
        return {"snapshot_age_status": "NONE", "age_hours": None, "policy_version": SNAPSHOT_AGE_POLICY_VERSION, "evaluated_at": now.isoformat()}
    cutoff = snapshot["cutoff_at"]
    if isinstance(cutoff, str):
        cutoff = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    hours = (now - cutoff).total_seconds() / 3600.0
    if hours <= SNAPSHOT_AGE_AGING_HOURS:
        status = "CURRENT"
    elif hours <= SNAPSHOT_AGE_STALE_HOURS:
        status = "AGING"
    else:
        status = "STALE"
    return {"snapshot_age_status": status, "age_hours": round(hours, 2), "policy_version": SNAPSHOT_AGE_POLICY_VERSION, "evaluated_at": now.isoformat(), "aging_after_hours": SNAPSHOT_AGE_AGING_HOURS, "stale_after_hours": SNAPSHOT_AGE_STALE_HOURS}


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

def _metric_entry(metrics: dict[str, dict[str, Any]], metric_id: str, *, export_scope: str | None = None) -> dict[str, Any] | None:
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
        "computed_at": row.get("computed_at"),
        "comparison": {k: detail.get(k) for k in ("comparison_date", "anchor_date", "lag_date", "gap_days", "anchor_lag_days", "missing_legs", "span_days") if detail.get(k) is not None},
        "reason": detail.get("reason"),
        "export_scope": export_scope or row.get("export_scope"),
    }


def _series_block(series_row: dict[str, Any], metrics: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sid = series_row["series_id"]
    spec = CATALOG_BY_ID.get(sid)
    transforms: dict[str, Any] = {}
    prefix = sid + "."
    scope = series_row.get("export_scope")
    for metric_id in sorted(m for m in metrics if m.startswith(prefix)):
        entry = _metric_entry(metrics, metric_id, export_scope=scope)
        if entry is not None:
            transforms[metric_id[len(prefix):]] = entry
    return {
        "series_id": sid,
        "label": spec.label if spec else series_row.get("title"),
        "title": series_row.get("title"),
        "category": series_row.get("category"),
        "subcategory": series_row.get("subcategory"),
        "latest": {
            "value": series_row.get("value"),
            "observation_date": series_row.get("observation_date"),
            "units": series_row.get("units"),
            "retrieved_at": series_row.get("retrieved_at"),
            "revision_seq": series_row.get("revision_seq"),
            "ingestion_run_id": series_row.get("ingestion_run_id"),
            "export_scope": scope,
        },
        "frequency": series_row.get("frequency_short"),
        "seasonal_adjustment": series_row.get("seasonal_adjustment_short"),
        "aggregation": series_row.get("aggregation") or (spec.aggregation if spec else None),
        "catalog_units": series_row.get("catalog_units") or (spec.raw_units if spec else None),
        "display": {"divisor": series_row.get("display_divisor"), "units": series_row.get("display_units")} if series_row.get("display_divisor") else None,
        "vintage_kind": series_row.get("vintage_kind"),
        "pit_safe": series_row.get("pit_safe"),
        "metadata_status": series_row.get("metadata_status"),
        "publication_status": series_row.get("publication_status"),
        "publication_reason": series_row.get("publication_reason"),
        "export_scope": scope,
        "source": {"provider": "FRED", "url": series_row.get("source_url"), "attribution": spec.attribution if spec else FRED_ATTRIBUTION},
        "notes": spec.notes if spec else None,
        "transforms": transforms,
    }


def macro_context(conn, *, today: date | None = None) -> dict[str, Any]:
    latest = macro_latest(conn)
    metrics = metric_latest(conn)
    today = _today(conn, today)
    by_category: dict[str, list[dict[str, Any]]] = {}
    for row in latest:
        if row.get("observation_date") is None:
            continue
        block = _series_block(row, metrics)
        spec = CATALOG_BY_ID.get(row["series_id"])
        obs_date = row.get("observation_date")
        obs_d = date.fromisoformat(obs_date) if isinstance(obs_date, str) else obs_date
        fresh = assess_freshness(obs_d, spec.expected_frequency if spec else row.get("frequency_short"), today)
        block["freshness"] = {"status": fresh.status, "age_days": fresh.age_days, "tolerance_days": fresh.tolerance_days, "cadence": spec.expected_frequency if spec else row.get("frequency_short"), "evaluated_on": today.isoformat()}
        by_category.setdefault(row["category"] or "uncategorized", []).append(block)
    missing = [s.series_id for s in CATALOG if s.series_id not in {r["series_id"] for r in latest if r.get("observation_date")}]
    return {
        "categories": by_category,
        "series_without_data": missing,
        "attribution": FRED_ATTRIBUTION,
    }


def _normalize_obs_date(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw)[:10])


def rates_context(conn) -> dict[str, Any]:
    from market_intelligence.ingest_treasury import TREASURY_ATTRIBUTION
    from market_intelligence.source_resolve import EQUIVALENTS, latest_common_observation_date, resolve_observation
    from market_intelligence.treasury_xml import COMPLETE_NOMINAL_TENORS

    latest_rows = list(macro_latest(conn))
    latest = {r["series_id"]: r for r in latest_rows}
    metrics = metric_latest(conn)
    per_tenor_resolved: dict[str, dict[date, Any]] = {}
    per_tenor_row: dict[str, dict[date, dict[str, Any]]] = {}
    for tenor, sid in CURVE_TENORS.items():
        alts = EQUIVALENTS.get(sid, (sid,))
        by_date: dict[date, list[dict[str, Any]]] = {}
        for alt in alts:
            meta = latest.get(alt) or {}
            source = meta.get("source_id") or ("TREASURY" if str(alt).startswith("UST_") else "FRED")
            for hist in observation_history(conn, alt, limit=40):
                day = _normalize_obs_date(hist.get("observation_date"))
                if day is None or hist.get("value") is None:
                    continue
                by_date.setdefault(day, []).append(
                    {
                        "series_id": alt,
                        "source_id": source,
                        "observation_date": day,
                        "value": hist.get("value"),
                        "retrieved_at": meta.get("retrieved_at"),
                        "revision_seq": meta.get("revision_seq"),
                        "ingestion_run_id": meta.get("ingestion_run_id"),
                        "publication_status": meta.get("publication_status"),
                        "export_scope": meta.get("export_scope"),
                    }
                )
        resolved_rows: dict[date, dict[str, Any]] = {}
        resolved_values: dict[date, Any] = {}
        for day, candidates in by_date.items():
            picked = resolve_observation(sid, candidates)
            if picked is None:
                continue
            base = next(c for c in candidates if c.get("series_id") == picked.series_id and _normalize_obs_date(c.get("observation_date")) == picked.observation_date)
            resolved_rows[day] = {
                **base,
                "source_id": picked.source_id,
                "selection_reason": picked.selection_reason,
                "fallback": picked.fallback,
                "value": picked.value,
            }
            resolved_values[day] = picked.value
        per_tenor_resolved[tenor] = resolved_values
        per_tenor_row[tenor] = resolved_rows

    complete_day = latest_common_observation_date(per_tenor_resolved, COMPLETE_NOMINAL_TENORS)
    complete_date = complete_day.isoformat() if complete_day else None

    def _leg(tenor: str, sid: str, row: dict[str, Any] | None, obs_day: date | None) -> dict[str, Any]:
        scope = (row.get("export_scope") if row else None) or (CATALOG_BY_ID[sid].export_scope if sid in CATALOG_BY_ID else None)
        return {
            "tenor": tenor,
            "series_id": sid,
            "provider_series_id": (row or {}).get("series_id") or sid,
            "yield_pct": (row or {}).get("value"),
            "observation_date": obs_day.isoformat() if obs_day else None,
            "retrieved_at": (row or {}).get("retrieved_at"),
            "revision_seq": (row or {}).get("revision_seq"),
            "ingestion_run_id": (row or {}).get("ingestion_run_id"),
            "publication_status": (row or {}).get("publication_status"),
            "source_id": (row or {}).get("source_id"),
            "selection_reason": (row or {}).get("selection_reason"),
            "fallback": bool((row or {}).get("fallback")),
            "chg_prev_bps": (_metric_entry(metrics, sid + ".chg_prev_bps") or {}).get("value"),
            "chg_1w_bps": (_metric_entry(metrics, sid + ".chg_1w_bps") or {}).get("value"),
            "chg_1m_bps": (_metric_entry(metrics, sid + ".chg_1m_bps") or {}).get("value"),
            "chg_3m_bps": (_metric_entry(metrics, sid + ".chg_3m_bps") or {}).get("value"),
            "export_scope": scope,
        }

    complete_curve = []
    if complete_day is not None:
        for tenor, sid in CURVE_TENORS.items():
            row = (per_tenor_row.get(tenor) or {}).get(complete_day)
            complete_curve.append(_leg(tenor, sid, row, complete_day if row else None))
    latest_curve = []
    for tenor, sid in CURVE_TENORS.items():
        rows = per_tenor_row.get(tenor) or {}
        latest_day = max(rows) if rows else None
        latest_curve.append(_leg(tenor, sid, rows.get(latest_day) if latest_day else None, latest_day))
    partial = [
        leg
        for leg in latest_curve
        if complete_day is not None and leg.get("observation_date") and date.fromisoformat(str(leg["observation_date"])[:10]) > complete_day
    ]
    curve = complete_curve or latest_curve
    slopes = {name: _metric_entry(metrics, "curve.slope_{0}_bps".format(name), export_scope=EXPORT_ATTRIBUTION_REQUIRED) for name in CURVE_SLOPES}
    dates = {c["observation_date"] for c in curve if c["observation_date"]}
    real = [_series_block(latest[s], metrics) for s in ("DFII5", "DFII10", "DFII20", "DFII30", "UST_REAL_5Y", "UST_REAL_10Y", "UST_REAL_20Y", "UST_REAL_30Y") if s in latest and latest[s].get("observation_date")]
    comp = [_series_block(latest[s], metrics) for s in ("T5YIE", "T10YIE", "T5YIFR") if s in latest and latest[s].get("observation_date")]
    derived_be = []
    for tenor, nom_id, real_id in (("5Y", "DGS5", "DFII5"), ("10Y", "DGS10", "DFII10")):
        nom = next((c for c in complete_curve if c["tenor"] == tenor), None)
        real_row = latest.get("UST_REAL_{0}".format(tenor)) or latest.get(real_id)
        if nom and nom.get("yield_pct") is not None and real_row and real_row.get("value") is not None and str(nom.get("observation_date") or "")[:10] == str(real_row.get("observation_date") or "")[:10]:
            derived_be.append(
                {
                    "tenor": tenor,
                    "value": float(nom["yield_pct"]) - float(real_row["value"]),
                    "units": "percentage_points",
                    "observation_date": nom["observation_date"],
                    "label": "derived_nominal_minus_real",
                    "source": {"nominal": nom.get("source_id"), "real": real_row.get("source_id") or ("TREASURY" if str(real_row.get("series_id") or "").startswith("UST_") else "FRED")},
                    "notes": "Derived same-date nominal minus real par yield. Not the published T5YIE/T10YIE breakeven series.",
                    # Both legs are public attribution-required government series.
                    "export_scope": EXPORT_ATTRIBUTION_REQUIRED,
                }
            )
    policy = [_series_block(latest[s], metrics) for s in ("DFF", "SOFR") if s in latest and latest[s].get("observation_date")]
    sources = sorted({c.get("source_id") for c in curve if c.get("source_id")})
    return {
        "curve": complete_curve or curve,
        "partial_newer": partial,
        "complete_curve_date": complete_date,
        "curve_dates_mixed": complete_date is None and len(dates) > 1,
        "curve_observation_dates": sorted(d for d in dates),
        "slopes": slopes,
        "real_yields": real,
        "inflation_compensation": comp,
        "derived_nominal_minus_real": derived_be,
        "policy": policy,
        "source_ids": sources,
        "fallback": any(c.get("fallback") for c in curve),
        "units_note": "Yields in percent; changes in basis points (percent x 100). Same-date legs only.",
        "attribution": TREASURY_ATTRIBUTION if "TREASURY" in sources else FRED_ATTRIBUTION,
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
                "transform_version": row.get("transform_version"),
                "computed_at": row.get("computed_at"),
                "export_scope": row["export_scope"],
                "source": row.get("source_refs"),
            }
        )
    return {
        "buckets": buckets,
        "coverage_note": "Provider history is limited; percentiles cover only the labeled window (1Y/3Y/AVAILABLE) or are NULL.",
        "attribution": (CATALOG_BY_ID[CREDIT_SERIES[0]].attribution if CREDIT_SERIES else None),
    }


def source_export_scopes(conn) -> dict[str, str]:
    """``source_id -> usage_scope`` from the source registry (curated view).

    The registry is the single place where a provider's redistribution posture is recorded.
    Callers must treat a missing source as ``INTERNAL_ONLY`` (fail closed); a derived
    sector return from an entitlement-unverified equity adapter is not automatically
    exportable to a remote AI client.
    """
    scopes: dict[str, str] = {}
    try:
        for row in _rows(conn, "SELECT source_id, usage_scope FROM mi_v_source_health"):
            sid = str(row.get("source_id") or "").strip()
            scope = str(row.get("usage_scope") or "").strip().upper()
            if sid and scope:
                scopes[sid] = scope
    except Exception:  # noqa: BLE001 - a missing view means no rights are known
        return {}
    return scopes


def _scope_for_source(scopes: dict[str, str], source_id: Any) -> str:
    return scopes.get(str(source_id or "").strip()) or EXPORT_INTERNAL_ONLY


def sectors_context(conn) -> dict[str, Any]:
    rows = sector_latest(conn)
    scopes = source_export_scopes(conn)
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
                "export_scope": _scope_for_source(scopes, row["source_id"]),
                "artifact_sha256": row["artifact_sha256"],
            }
        )
    as_of_by_dataset = {ds: sorted({r["as_of"] for r in items if r["as_of"]}) for ds, items in datasets.items()}
    return {
        "datasets": datasets,
        "as_of_by_dataset": as_of_by_dataset,
        "source_scopes": {sid: scopes.get(sid) for sid in sorted({str(r["source_id"]) for r in rows if r.get("source_id")})},
        "note": "Per-dataset as_of; rotation and dispersion bundles may differ. export_scope follows the source registry usage_scope (unknown source -> INTERNAL_ONLY).",
    }


def pit_sector_context(conn, *, history_limit: int = MAX_HISTORY_ROWS) -> dict[str, Any]:
    """PIT sector internals (isolated QS producer -> hash-verified artifact -> canonical rows).

    Returns the latest current row per sector plus bounded history for charts, with the artifact
    provenance/boundary so the page can label synthetic or research-ineligible data honestly.
    Empty when the consumer has never ingested (the views may not exist before migration 014).
    """
    if not _view_exists(conn, "mi_v_pit_sector_internals_latest"):
        return {"available": False, "reason": "MIGRATION_PENDING", "latest": [], "history": {}, "artifacts": []}
    latest = _rows(conn, "SELECT * FROM mi_v_pit_sector_internals_latest ORDER BY sector, method_version")
    artifacts = _rows(conn, "SELECT * FROM mi_v_pit_sector_artifacts ORDER BY ingested_at DESC LIMIT 20")
    history: dict[str, list[dict[str, Any]]] = {}
    if latest:
        rows = _rows(
            conn,
            """
            SELECT decision_date, sector, method_version, revision_seq, provenance, research_eligible, constituent_count, priced_count,
                   pct_above_20d, pct_above_50d, pct_above_100d, pct_above_200d, median_return, ew_return, cw_return, cw_status,
                   ew_minus_cw, dispersion, return_denominator, held_ew_return, held_status, hhi_cap, top5_cap_share, concentration_status
            FROM mi_v_pit_sector_internals_current
            ORDER BY decision_date DESC, sector
            LIMIT :limit
            """,
            {"limit": history_limit},
        )
        for row in reversed(rows):
            history.setdefault(row["sector"], []).append(row)
    return {
        "available": bool(latest),
        "reason": None if latest else "NO_ARTIFACT_INGESTED",
        "latest": latest,
        "history": history,
        "artifacts": artifacts,
        "export_scope": "INTERNAL_ONLY",
        "note": "Aggregates from decision-time membership; trailing statistics and the held equal-weight portfolio are distinct quantities. SYNTHETIC_TEST_ONLY provenance is never research evidence.",
    }


def industries_context(conn) -> dict[str, Any]:
    rows = industry_latest(conn)
    scopes = source_export_scopes(conn)
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
                "coverage": row.get("provenance_json") or {},
                "source_id": row["source_id"],
                "export_scope": _scope_for_source(scopes, row["source_id"]),
            }
        )
    return {"datasets": out}


def ibkr_collector_status(conn) -> list[dict[str, Any]]:
    if not _view_exists(conn, "mi_v_ibkr_collector_status"):
        return []
    return _rows(conn, "SELECT * FROM mi_v_ibkr_collector_status ORDER BY collector_id")


def ibkr_quotes_latest(conn) -> list[dict[str, Any]]:
    if not _view_exists(conn, "mi_v_ibkr_quotes_latest"):
        return []
    return _rows(conn, "SELECT * FROM mi_v_ibkr_quotes_latest ORDER BY instrument_id")


def order_flow_context(conn, *, today: date | None = None, history_limit: int = 120, include_history: bool = True) -> dict[str, Any]:
    """Corporate bond trading activity from FINRA Query API aggregates stored in PostgreSQL."""
    from market_intelligence.finra_catalog import (
        BREADTH_DISPLAY_CATEGORIES,
        CORPORATE_BREADTH,
        CORPORATE_CAPPED_VOLUME,
        CORPORATE_SENTIMENT,
        FINRA_ATTRIBUTION,
        SENTIMENT_CUSTOMER_BUY,
        SENTIMENT_CUSTOMER_SELL,
        TRACE_INDIVIDUAL,
    )

    today = _today(conn, today)
    coverage = _rows(conn, "SELECT * FROM mi_v_order_flow_coverage ORDER BY group_name, dataset") if _view_exists(conn, "mi_v_order_flow_coverage") else []
    current = _rows(conn, "SELECT * FROM mi_v_finra_aggregate_current ORDER BY dataset, observation_date DESC, category_key") if _view_exists(conn, "mi_v_finra_aggregate_current") else []
    trades = _rows(conn, "SELECT source_trade_id FROM mi_v_trace_individual_trades LIMIT 1") if _view_exists(conn, "mi_v_trace_individual_trades") else []
    by_dataset: dict[str, list[dict[str, Any]]] = {}
    for row in current:
        by_dataset.setdefault(row["dataset"], []).append(row)

    def _latest_rows(dataset: str) -> list[dict[str, Any]]:
        rows = by_dataset.get(dataset) or []
        if not rows:
            return []
        latest = max(r["observation_date"] for r in rows if r.get("observation_date"))
        return [r for r in rows if r.get("observation_date") == latest]

    def _prior_rows(dataset: str, latest_date) -> list[dict[str, Any]]:
        rows = by_dataset.get(dataset) or []
        earlier = [r["observation_date"] for r in rows if r.get("observation_date") and r["observation_date"] < latest_date]
        if not earlier:
            return []
        prior = max(earlier)
        return [r for r in rows if r.get("observation_date") == prior]

    def _metric(row: dict[str, Any], name: str):
        metrics = row.get("metrics_json") or {}
        if isinstance(metrics, str):
            import json as _json

            metrics = _json.loads(metrics)
        return metrics.get(name)

    def _grain_val(row: dict[str, Any], name: str):
        grain = row.get("grain_json") or {}
        if isinstance(grain, str):
            import json as _json

            grain = _json.loads(grain)
        return grain.get(name)

    breadth_latest = _latest_rows(CORPORATE_BREADTH.dataset)
    breadth_date = breadth_latest[0]["observation_date"] if breadth_latest else None
    breadth_prior = _prior_rows(CORPORATE_BREADTH.dataset, breadth_date) if breadth_date else []
    prior_by_cat = {_grain_val(r, "productCategory"): r for r in breadth_prior}
    breadth_cards = []
    for row in breadth_latest:
        cat = _grain_val(row, "productCategory")
        prior = prior_by_cat.get(cat)
        volume = _metric(row, "totalVolume")
        trades_n = _metric(row, "totalTrades")
        prior_volume = _metric(prior, "totalVolume") if prior else None
        prior_trades = _metric(prior, "totalTrades") if prior else None
        breadth_cards.append(
            {
                "product_category": cat,
                "observation_date": row["observation_date"],
                "prior_observation_date": prior["observation_date"] if prior else None,
                "total_volume": volume,
                "total_trades": trades_n,
                "advances": _metric(row, "advances"),
                "declines": _metric(row, "declines"),
                "unchanged": _metric(row, "unchanged"),
                "fifty_two_week_high": _metric(row, "fiftyTwoWeekHigh"),
                "fifty_two_week_low": _metric(row, "fiftyTwoWeekLow"),
                "volume_change": None if volume is None or prior_volume is None else volume - prior_volume,
                "trade_count_change": None if trades_n is None or prior_trades is None else trades_n - prior_trades,
                "volume_is_capped": bool(row.get("volume_is_capped")),
                "retrieved_at": row.get("retrieved_at"),
                "revision_seq": row.get("revision_seq"),
                "units_note": row.get("units_note"),
            }
        )
    breadth_cards.sort(key=lambda r: (BREADTH_DISPLAY_CATEGORIES.index(r["product_category"]) if r["product_category"] in BREADTH_DISPLAY_CATEGORIES else 99, r["product_category"] or ""))

    sentiment_latest = _latest_rows(CORPORATE_SENTIMENT.dataset)
    sentiment_date = sentiment_latest[0]["observation_date"] if sentiment_latest else None
    sentiment_rows = []
    customer_buy = None
    customer_sell = None
    for row in sentiment_latest:
        product = (_grain_val(row, "productCategory") or "").lower()
        trade_type = _grain_val(row, "tradeType")
        item = {
            "trade_type": trade_type,
            "product_category": _grain_val(row, "productCategory"),
            "observation_date": row["observation_date"],
            "total_volume": _metric(row, "totalVolume"),
            "total_trades": _metric(row, "totalTrades"),
            "total_transactions": _metric(row, "totalTransactions"),
            "retrieved_at": row.get("retrieved_at"),
        }
        sentiment_rows.append(item)
        if (trade_type or "").lower() == "all securities" and product == SENTIMENT_CUSTOMER_BUY:
            customer_buy = item
        if (trade_type or "").lower() == "all securities" and product == SENTIMENT_CUSTOMER_SELL:
            customer_sell = item
    customer_net = None
    if customer_buy and customer_sell and customer_buy.get("total_volume") is not None and customer_sell.get("total_volume") is not None:
        customer_net = {
            "observation_date": sentiment_date,
            "customer_buy_volume": customer_buy["total_volume"],
            "customer_sell_volume": customer_sell["total_volume"],
            "customer_net_volume": customer_buy["total_volume"] - customer_sell["total_volume"],
            "perspective": (
                "Dealer-reported customer side from FINRA corporateMarketSentiment "
                "(productCategory customer buy vs customer sell, tradeType all securities). "
                "Not buyer initiation, not institutional identity, and not a fund-flow estimate. "
                "Every trade has a buyer and a seller."
            ),
        }

    capped_latest = _latest_rows(CORPORATE_CAPPED_VOLUME.dataset)
    capped_date = capped_latest[0]["observation_date"] if capped_latest else None
    capped_rows = []
    identity_complete = True if capped_latest else False
    for row in capped_latest:
        trade_year = _grain_val(row, "tradeYear")
        trade_month = _grain_val(row, "tradeMonth")
        if trade_year in (None, "") or trade_month in (None, ""):
            identity_complete = False
        capped_rows.append(
            {
                "grade_code": _grain_val(row, "gradeCode"),
                "rule_144a_flag": _grain_val(row, "144AFlag"),
                "trade_year": trade_year,
                "trade_month": trade_month,
                "observation_date": row["observation_date"],
                "reporting_period": (
                    None
                    if trade_year in (None, "") or trade_month in (None, "")
                    else "{0}-{1:02d}".format(int(trade_year), int(trade_month))
                    if str(trade_year).isdigit() and str(trade_month).isdigit()
                    else "{0}-{1}".format(trade_year, trade_month)
                ),
                "total_trade_count": _metric(row, "totalTradeCount"),
                "total_volume_quantity": _metric(row, "totalVolumeQuantity"),
                "customer_buy_par_lt_5y": _metric(row, "customerBuyParLessThan5YearsQuantity"),
                "customer_sell_par_lt_5y": _metric(row, "customerSellParLessThan5YearsQuantity"),
                "volume_is_capped": True,
                "identity_complete": trade_year not in (None, "") and trade_month not in (None, ""),
                "retrieved_at": row.get("retrieved_at"),
                "units_note": row.get("units_note"),
            }
        )

    history = []
    if include_history and _view_exists(conn, "mi_v_finra_aggregate_history"):
        history = _rows(
            conn,
            """
            SELECT observation_date, category_key, metrics_json, volume_is_capped
            FROM mi_v_finra_aggregate_history
            WHERE dataset = :ds AND is_current
            ORDER BY observation_date
            """,
            {"ds": CORPORATE_BREADTH.dataset},
        )
        if history_limit and len(history) > history_limit * 8:
            cutoff_dates = sorted({r["observation_date"] for r in history})[-history_limit:]
            keep = set(cutoff_dates)
            history = [r for r in history if r["observation_date"] in keep]

    individual_available = bool(trades)
    loaded_dates = [r["observation_date"] for r in current if r.get("observation_date")]
    return {
        "title": "Corporate Bond Trading Activity",
        "not_an_order_book": True,
        "coverage_explanation": (
            "Reported TRACE activity aggregates from the FINRA Query API, delayed/end-of-day "
            "as published by FINRA. This is not a live order book, Level 2 depth, aggressor "
            "direction, hidden liquidity, or unexecuted orders."
        ),
        "attribution": FINRA_ATTRIBUTION,
        "coverage": coverage,
        "individual_trades": {
            "available": individual_available,
            "capability_status": "AVAILABLE" if individual_available else "ENTITLEMENT_REQUIRED",
            "note": TRACE_INDIVIDUAL.coverage_note,
        },
        "breadth": {
            "dataset": CORPORATE_BREADTH.dataset,
            "latest_observation_date": breadth_date,
            "rows": breadth_cards,
            "units_note": CORPORATE_BREADTH.units_note,
            "overlap_note": "productCategory rows overlap; IG/HY/convertibles are not additive to all securities.",
        },
        "sentiment": {
            "dataset": CORPORATE_SENTIMENT.dataset,
            "latest_observation_date": sentiment_date,
            "rows": sentiment_rows,
            "customer_net": customer_net,
            "units_note": CORPORATE_SENTIMENT.units_note,
        },
        "capped_volume": {
            "dataset": CORPORATE_CAPPED_VOLUME.dataset,
            "latest_observation_date": capped_date,
            "rows": capped_rows,
            "units_note": CORPORATE_CAPPED_VOLUME.units_note,
            "capped_note": "Capped/reported source quantities are lower bounds where FINRA caps size. Not exact VWAP.",
            "identity_validated": identity_complete,
            "headline_eligible": identity_complete,
            "identity_note": (
                None
                if identity_complete
                else (
                    "Capped-volume identity is not validated: current rows are missing "
                    "tradeYear/tradeMonth. Figures are withheld from headlines until replay "
                    "stores those reporting-period dimensions."
                )
            ),
        },
        "history": history,
        "loaded_interval": {
            "min_observation_date": min(loaded_dates) if loaded_dates else None,
            "max_observation_date": max(loaded_dates) if loaded_dates else None,
            "row_count": len(current),
        },
        "evaluated_on": today.isoformat(),
        "export_scope": "INTERNAL_ONLY",
    }


def order_flow_overview(conn, *, today: date | None = None) -> dict[str, Any]:
    """Order-flow read model without history series (Overview / morning snapshot)."""
    return order_flow_context(conn, today=today, include_history=False)


def data_health_context(conn, *, today: date | None = None) -> dict[str, Any]:
    health = source_health(conn, today=today)
    quarantine = _rows(conn, "SELECT * FROM mi_v_macro_quarantine_summary ORDER BY series_id, reason") if _view_exists(conn, "mi_v_macro_quarantine_summary") else []
    finra_quarantine = _rows(conn, "SELECT * FROM mi_v_finra_aggregate_quarantine ORDER BY created_at DESC LIMIT 200") if _view_exists(conn, "mi_v_finra_aggregate_quarantine") else []
    return {
        "sources": health,
        "stale": [h for h in health if h.get("freshness_status") == "STALE" and not h.get("retired_optional")],
        "failed_transport": [
            h
            for h in health
            if h.get("transport_status") in ("FAILED", "METADATA_REJECTED", "PARTIAL") and not h.get("retired_optional")
        ],
        "incomplete_coverage": [
            h
            for h in health
            if h.get("coverage_status") in ("PARTIAL", "EMPTY") and not h.get("retired_optional")
        ],
        "quarantine": quarantine,
        "finra_quarantine": finra_quarantine,
        "export_scope": "INTERNAL_SUMMARY",
    }


def _view_exists(conn, name: str) -> bool:
    return bool(conn.execute(text("SELECT 1 FROM information_schema.views WHERE table_name = :n"), {"n": name}).first())


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
    "SNAPSHOT_AGE_POLICY_VERSION",
    "credit_context",
    "credit_latest",
    "data_health_context",
    "order_flow_context",
    "snapshot_age",
    "ibkr_collector_status",
    "ibkr_quotes_latest",
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
    "ops_status",
    "source_health",
    "strategies_context",
    "strategy_summary",
]
