"""Versioned analytics from canonical observations -> mi_metric_snapshots / mi_credit_index_snapshots.

Every metric declares units, the transform version, and the exact comparison anchors used.
Percentiles/z-scores are descriptive statistics with documented windows; they are not
research acceptance thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import text

from market_intelligence.catalog import CATALOG, CATALOG_BY_ID, CREDIT_SERIES, CURVE_SLOPES, SeriesSpec
from market_intelligence.nulls import strict_dumps
from market_intelligence.store import current_observations
from market_intelligence.transforms import (
    TRANSFORM_VERSION,
    TransformResult,
    ann3m_pct,
    ann6m_pct,
    calendar_change,
    curve_slope,
    common_curve_date,
    latest_date,
    mom_pct,
    pct_to_bps,
    period_difference,
    previous_observation_change,
    qoq_annualized_pct,
    window_statistics,
    yoy_pct,
)

CREDIT_WINDOWS = (
    # label, calendar days, minimum non-missing observations
    ("1Y", 366, 200),
    ("3Y", 3 * 366, 600),
)
CREDIT_MIN_AVAILABLE_OBSERVATIONS = 60

_UNITS_BY_KIND = {
    "percent": "pct",
    "index": "index",
    "count": "count",
    "balance": "level",
    "level": "level",
}


def _round(value: float | None, places: int = 10) -> float | None:
    """Trim binary float noise (e.g. 4.3500000000000005) before persisting."""
    return None if value is None else round(float(value), places)


@dataclass
class MetricRow:
    metric_id: str
    series_id: str | None
    category: str
    as_of: date
    result: TransformResult

    def params(self, run_id: str | None) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "series_id": self.series_id,
            "category": self.category,
            "as_of": self.as_of,
            "value": _round(self.result.value),
            "units": self.result.units,
            "transform_version": TRANSFORM_VERSION,
            "status": self.result.status,
            "detail": strict_dumps(self.result.as_detail()),
            "run_id": run_id,
        }


@dataclass
class AnalyticsReport:
    metrics_written: int = 0
    credit_written: int = 0
    series_without_data: list[str] = field(default_factory=list)
    curve_date: date | None = None
    curve_missing_legs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "transform_version": TRANSFORM_VERSION,
            "metrics_written": self.metrics_written,
            "credit_written": self.credit_written,
            "series_without_data": list(self.series_without_data),
            "curve_date": self.curve_date.isoformat() if self.curve_date else None,
            "curve_missing_legs": list(self.curve_missing_legs),
        }


def _level_units(spec: SeriesSpec) -> str:
    if spec.value_kind == "percent":
        return "pct"
    if spec.value_kind == "index":
        return "index"
    if spec.value_kind == "count":
        return spec.expected_units_contains[0] if spec.expected_units_contains else "count"
    if spec.value_kind == "balance":
        return "{0}_usd".format(spec.expected_units_contains[0]) if spec.expected_units_contains else "usd"
    return spec.expected_units_contains[0] if spec.expected_units_contains else "level"


def series_metrics(spec: SeriesSpec, obs: Mapping[date, Decimal | None]) -> list[MetricRow]:
    at = latest_date(obs)
    if at is None:
        return []
    rows: list[MetricRow] = []
    sid = spec.series_id
    cadence = spec.expected_frequency
    level_units = _level_units(spec)

    def add(name: str, result: TransformResult) -> None:
        rows.append(MetricRow("{0}.{1}".format(sid, name), sid, spec.category, at, result))

    current = float(obs[at])
    add("level", TransformResult(current, level_units, detail={"at": at.isoformat()}))
    for transform in spec.transforms:
        if transform == "level_pct":
            add("level_pct", TransformResult(current, "pct", detail={"at": at.isoformat()}))
        elif transform == "oas_bps":
            add("oas_bps", TransformResult(pct_to_bps(current), "bps", detail={"at": at.isoformat(), "source_units": "pct"}))
        elif transform == "chg_bps":
            add("chg_prev_bps", previous_observation_change(obs, at, units="bps", scale=100.0))
            add("chg_1w_bps", calendar_change(obs, at, days=7, cadence=cadence, units="bps", scale=100.0))
            add("chg_1m_bps", calendar_change(obs, at, months=1, cadence=cadence, units="bps", scale=100.0))
            add("chg_3m_bps", calendar_change(obs, at, months=3, cadence=cadence, units="bps", scale=100.0))
        elif transform == "yoy_pct":
            add("yoy_pct", yoy_pct(obs, at) if cadence != "Q" else _quarterly_yoy(obs, at))
        elif transform == "ann3m_pct":
            add("ann3m_pct", ann3m_pct(obs, at))
        elif transform == "ann6m_pct":
            add("ann6m_pct", ann6m_pct(obs, at))
        elif transform == "mom_pct":
            add("mom_pct", mom_pct(obs, at))
        elif transform == "qoq_annualized_pct":
            add("qoq_saar_pct", qoq_annualized_pct(obs, at))
        elif transform == "mom_change":
            add("mom_change", period_difference(obs, at, 1, units=level_units))
        elif transform == "mom_change_pp":
            add("mom_change_pp", period_difference(obs, at, 1, units="pp"))
        elif transform == "yoy_change_pp":
            add("yoy_change_pp", period_difference(obs, at, 12, units="pp"))
        elif transform == "wow_change":
            add("wow_change", calendar_change(obs, at, days=7, cadence=cadence, units=level_units))
        elif transform == "chg_4w":
            add("chg_4w", calendar_change(obs, at, days=28, cadence=cadence, units=level_units))
        elif transform == "avg_4w":
            add("avg_4w", _trailing_average(obs, at, 4, level_units))
        elif transform == "chg_1d":
            add("chg_prev", previous_observation_change(obs, at, units=level_units))
        elif transform == "chg_1w":
            add("chg_1w", calendar_change(obs, at, days=7, cadence=cadence, units=level_units))
        elif transform == "pctile_available":
            continue  # handled by credit snapshot builder
    return rows


def _quarterly_yoy(obs: Mapping[date, Decimal | None], at: date) -> TransformResult:
    from market_intelligence.transforms import period_ratio_pct

    return period_ratio_pct(obs, at, 12)


def _trailing_average(obs: Mapping[date, Decimal | None], at: date, n: int, units: str) -> TransformResult:
    valid = sorted((d, float(v)) for d, v in obs.items() if v is not None and d <= at)
    if len(valid) < n:
        return TransformResult(None, units, status="INSUFFICIENT_DATA", reason="fewer than {0} observations".format(n), detail={"at": at.isoformat()})
    window = valid[-n:]
    return TransformResult(sum(v for _, v in window) / n, units, detail={"at": at.isoformat(), "first": window[0][0].isoformat(), "count": n})


def credit_snapshot_params(spec: SeriesSpec, obs: Mapping[date, Decimal | None]) -> dict[str, Any] | None:
    at = latest_date(obs)
    if at is None:
        return None
    current = float(obs[at])
    valid_dates = sorted(d for d, v in obs.items() if v is not None)
    n_total = len(valid_dates)
    chosen_label = None
    chosen: TransformResult | None = None
    for label, days, minimum in CREDIT_WINDOWS:
        stats = window_statistics(obs, at, window_days=days, min_observations=minimum, label=label)
        if stats.value is not None:
            chosen_label, chosen = label, stats
    if chosen is None:
        if n_total >= CREDIT_MIN_AVAILABLE_OBSERVATIONS:
            chosen_label = "AVAILABLE"
            chosen = window_statistics(obs, at, window_days=(at - valid_dates[0]).days + 1, min_observations=CREDIT_MIN_AVAILABLE_OBSERVATIONS, label="AVAILABLE")
    if chosen_label == "3Y":
        history_status = "OK"
    elif chosen_label == "1Y":
        history_status = "LIMITED_TO_1Y"  # provider history too short for the 3Y window; never relabeled
    elif chosen is not None and chosen.value is not None:
        history_status = "AVAILABLE_ONLY"
    else:
        history_status = "INSUFFICIENT_HISTORY"
    detail = chosen.as_detail() if chosen is not None else {"status": "INSUFFICIENT_HISTORY", "observations": n_total}
    detail["history_note"] = "Provider limits available history; percentiles cover the labeled window only."
    return {
        "series_id": spec.series_id,
        "bucket": spec.subcategory,
        "as_of": at,
        "oas_bps": _round(pct_to_bps(current)),
        "chg_1d": _round(_bps_or_none(previous_observation_change(obs, at, units="bps", scale=100.0), require_one_session=True)),
        "chg_1w": _round(_bps_or_none(calendar_change(obs, at, days=7, cadence="D", units="bps", scale=100.0))),
        "chg_1m": _round(_bps_or_none(calendar_change(obs, at, months=1, cadence="D", units="bps", scale=100.0))),
        "chg_3m": _round(_bps_or_none(calendar_change(obs, at, months=3, cadence="D", units="bps", scale=100.0))),
        "pwindow": chosen_label,
        "percentile": _round(chosen.value) if chosen is not None else None,
        "zscore": _round(chosen.detail.get("zscore")) if chosen is not None else None,
        "window_obs": (chosen.detail.get("observations") if chosen is not None else n_total),
        "first_date": valid_dates[0] if valid_dates else None,
        "history_status": history_status,
        "transform_version": TRANSFORM_VERSION,
        "export_scope": spec.export_scope,
        "source_refs": strict_dumps({"source_url": spec.source_url, "attribution": spec.attribution}),
        "detail": strict_dumps(detail),
    }


def _bps_or_none(result: TransformResult, *, require_one_session: bool = False) -> float | None:
    if result.value is None:
        return None
    if require_one_session and result.status == "MULTI_SESSION_GAP":
        return None
    return result.value


def curve_metrics(legs: Mapping[str, Mapping[date, Decimal | None]], as_of: date) -> tuple[list[MetricRow], date | None, list[str]]:
    common, missing = common_curve_date({k: v for k, v in legs.items() if v}, as_of)
    rows: list[MetricRow] = []
    if common is None:
        return rows, None, missing
    for name, (long_id, short_id) in CURVE_SLOPES.items():
        result = curve_slope(legs.get(long_id, {}), legs.get(short_id, {}), common)
        result.detail["long"] = long_id
        result.detail["short"] = short_id
        rows.append(MetricRow("curve.slope_{0}_bps".format(name), None, "rates", common, result))
    return rows, common, missing


def build_analytics(conn, *, as_of: date | None = None, run_id: str | None = None) -> AnalyticsReport:
    report = AnalyticsReport()
    observations: dict[str, dict[date, Decimal | None]] = {}
    for spec in CATALOG:
        obs = current_observations(conn, spec.series_id, end=as_of)
        if not obs or latest_date(obs) is None:
            report.series_without_data.append(spec.series_id)
            continue
        observations[spec.series_id] = obs
    rows: list[MetricRow] = []
    for sid, obs in observations.items():
        rows.extend(series_metrics(CATALOG_BY_ID[sid], obs))
    curve_legs = {sid: observations.get(sid, {}) for pair in CURVE_SLOPES.values() for sid in pair}
    curve_as_of = as_of or max((latest_date(o) for o in curve_legs.values() if o), default=None)
    if curve_as_of is not None:
        curve_rows, common, missing = curve_metrics(curve_legs, curve_as_of)
        rows.extend(curve_rows)
        report.curve_date = common
        report.curve_missing_legs = missing
    for row in rows:
        conn.execute(
            text(
                """
                INSERT INTO mi_metric_snapshots (
                    metric_id, series_id, category, as_of, value, units, transform_version, status, detail_json, computed_at, ingestion_run_id
                ) VALUES (
                    :metric_id, :series_id, :category, :as_of, :value, :units, :transform_version, :status, CAST(:detail AS JSONB), NOW(), :run_id
                )
                ON CONFLICT (metric_id, as_of, transform_version) DO UPDATE SET
                    value = EXCLUDED.value,
                    units = EXCLUDED.units,
                    status = EXCLUDED.status,
                    detail_json = EXCLUDED.detail_json,
                    computed_at = NOW(),
                    ingestion_run_id = EXCLUDED.ingestion_run_id
                """
            ),
            row.params(run_id),
        )
        report.metrics_written += 1
    for sid in CREDIT_SERIES:
        obs = observations.get(sid)
        if not obs:
            continue
        params = credit_snapshot_params(CATALOG_BY_ID[sid], obs)
        if params is None:
            continue
        conn.execute(
            text(
                """
                INSERT INTO mi_credit_index_snapshots (
                    series_id, bucket, as_of, oas_bps, change_1d_bps, change_1w_bps, change_1m_bps, change_3m_bps,
                    percentile_window, percentile, zscore, window_observations, history_first_date, history_status,
                    units, transform_version, export_scope, source_refs, detail_json, computed_at
                ) VALUES (
                    :series_id, :bucket, :as_of, :oas_bps, :chg_1d, :chg_1w, :chg_1m, :chg_3m,
                    :pwindow, :percentile, :zscore, :window_obs, :first_date, :history_status,
                    'bps', :transform_version, :export_scope, CAST(:source_refs AS JSONB), CAST(:detail AS JSONB), NOW()
                )
                ON CONFLICT (series_id, as_of, transform_version) DO UPDATE SET
                    oas_bps = EXCLUDED.oas_bps,
                    change_1d_bps = EXCLUDED.change_1d_bps,
                    change_1w_bps = EXCLUDED.change_1w_bps,
                    change_1m_bps = EXCLUDED.change_1m_bps,
                    change_3m_bps = EXCLUDED.change_3m_bps,
                    percentile_window = EXCLUDED.percentile_window,
                    percentile = EXCLUDED.percentile,
                    zscore = EXCLUDED.zscore,
                    window_observations = EXCLUDED.window_observations,
                    history_first_date = EXCLUDED.history_first_date,
                    history_status = EXCLUDED.history_status,
                    source_refs = EXCLUDED.source_refs,
                    detail_json = EXCLUDED.detail_json,
                    computed_at = NOW()
                """
            ),
            params,
        )
        report.credit_written += 1
    return report


__all__ = ["AnalyticsReport", "CREDIT_WINDOWS", "MetricRow", "build_analytics", "credit_snapshot_params", "curve_metrics", "series_metrics"]
