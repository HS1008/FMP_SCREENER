"""Versioned analytics from canonical observations -> mi_metric_snapshots / mi_credit_index_snapshots.

Every metric declares units, the transform version, and the exact comparison anchors used.
Percentiles/z-scores are descriptive statistics with documented windows; they are not
research acceptance thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping

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
STATUS_WITHDRAWN = "WITHDRAWN_OBSERVATION"

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
    history_dates: dict[str, int] = field(default_factory=dict)
    truncated_series: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "transform_version": TRANSFORM_VERSION,
            "metrics_written": self.metrics_written,
            "credit_written": self.credit_written,
            "series_without_data": list(self.series_without_data),
            "curve_date": self.curve_date.isoformat() if self.curve_date else None,
            "curve_missing_legs": list(self.curve_missing_legs),
            "history_dates_per_series": dict(self.history_dates),
            "truncated_series": list(self.truncated_series),
        }


def _level_units(spec: SeriesSpec) -> str:
    return spec.raw_units


DISPLAY_CONVERSION_VERSION = "display_scale_v1"


def series_metrics(spec: SeriesSpec, obs: Mapping[date, Decimal | None], *, at: date | None = None) -> list[MetricRow]:
    """Metrics for ``spec`` at observation date ``at`` (default: latest valid observation).

    Passing ``at`` computes the metrics as they stand for that date using only observations
    on/before it (history backfill). Values are the current (latest revised) vintage.
    """
    if at is None:
        at = latest_date(obs)
    if at is None:
        return []
    if obs.get(at) is None:
        return withdrawn_series_metrics(spec, at)
    if any(d > at for d in obs):
        obs = {d: v for d, v in obs.items() if d <= at}
    rows: list[MetricRow] = []
    sid = spec.series_id
    cadence = spec.expected_frequency
    level_units = _level_units(spec)

    def add(name: str, result: TransformResult) -> None:
        rows.append(MetricRow("{0}.{1}".format(sid, name), sid, spec.category, at, result))

    current = float(obs[at])
    add("level", TransformResult(current, level_units, detail={"at": at.isoformat(), "aggregation": spec.aggregation, "provider_units": True}))
    if spec.display_divisor and spec.display_units:
        add(
            "level_display",
            TransformResult(
                current / float(spec.display_divisor),
                spec.display_units,
                detail={"at": at.isoformat(), "source_units": level_units, "display_divisor": float(spec.display_divisor), "conversion_version": DISPLAY_CONVERSION_VERSION, "aggregation": spec.aggregation},
            ),
        )
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


def _metric_names_for(spec: SeriesSpec) -> list[tuple[str, str]]:
    """``(metric suffix, units)`` pairs that ``series_metrics`` would emit for ``spec``."""
    names: list[tuple[str, str]] = [("level", _level_units(spec))]
    if spec.display_divisor and spec.display_units:
        names.append(("level_display", spec.display_units))
    for transform in spec.transforms:
        if transform == "level_pct":
            names.append(("level_pct", "pct"))
        elif transform == "oas_bps":
            names.append(("oas_bps", "bps"))
        elif transform == "chg_bps":
            names.extend((n, "bps") for n in ("chg_prev_bps", "chg_1w_bps", "chg_1m_bps", "chg_3m_bps"))
        elif transform == "yoy_pct":
            names.append(("yoy_pct", "pct"))
        elif transform == "ann3m_pct":
            names.append(("ann3m_pct", "pct"))
        elif transform == "ann6m_pct":
            names.append(("ann6m_pct", "pct"))
        elif transform == "mom_pct":
            names.append(("mom_pct", "pct"))
        elif transform == "qoq_annualized_pct":
            names.append(("qoq_saar_pct", "pct"))
        elif transform == "mom_change":
            names.append(("mom_change", _level_units(spec)))
        elif transform == "mom_change_pp":
            names.append(("mom_change_pp", "pp"))
        elif transform == "yoy_change_pp":
            names.append(("yoy_change_pp", "pp"))
        elif transform == "wow_change":
            names.append(("wow_change", _level_units(spec)))
        elif transform == "chg_4w":
            names.append(("chg_4w", _level_units(spec)))
        elif transform == "avg_4w":
            names.append(("avg_4w", _level_units(spec)))
        elif transform == "chg_1d":
            names.append(("chg_prev", _level_units(spec)))
        elif transform == "chg_1w":
            names.append(("chg_1w", _level_units(spec)))
    return names


def withdrawn_series_metrics(spec: SeriesSpec, at: date) -> list[MetricRow]:
    """NULL-publish every derived metric for a date whose current observation is NULL."""
    reason = "current observation is NULL; derived metrics unpublished (observation history retained)"
    rows = []
    for name, units in _metric_names_for(spec):
        result = TransformResult(None, units, status=STATUS_WITHDRAWN, reason=reason, detail={"at": at.isoformat(), "withdrawn": True})
        rows.append(MetricRow("{0}.{1}".format(spec.series_id, name), spec.series_id, spec.category, at, result))
    return rows


def withdrawn_credit_params(spec: SeriesSpec, at: date) -> dict[str, Any]:
    return {
        "series_id": spec.series_id,
        "bucket": spec.subcategory,
        "as_of": at,
        "oas_bps": None,
        "chg_1d": None,
        "chg_1w": None,
        "chg_1m": None,
        "chg_3m": None,
        "pwindow": None,
        "percentile": None,
        "zscore": None,
        "window_obs": None,
        "first_date": None,
        "history_status": STATUS_WITHDRAWN,
        "transform_version": TRANSFORM_VERSION,
        "export_scope": spec.export_scope,
        "source_refs": strict_dumps({"source_url": spec.source_url, "attribution": spec.attribution}),
        "detail": strict_dumps({"status": STATUS_WITHDRAWN, "at": at.isoformat(), "withdrawn": True, "reason": "current observation is NULL; credit snapshot unpublished"}),
    }


def withdrawn_curve_rows(as_of: date) -> list[MetricRow]:
    result = TransformResult(None, "bps", status=STATUS_WITHDRAWN, reason="a curve leg observation is NULL at this date", detail={"at": as_of.isoformat(), "withdrawn": True})
    return [MetricRow("curve.slope_{0}_bps".format(name), None, "rates", as_of, result) for name in CURVE_SLOPES]


def _quarterly_yoy(obs: Mapping[date, Decimal | None], at: date) -> TransformResult:
    from market_intelligence.transforms import period_ratio_pct

    return period_ratio_pct(obs, at, 12)


def _trailing_average(obs: Mapping[date, Decimal | None], at: date, n: int, units: str, *, period_days: int = 7) -> TransformResult:
    """Average of the last ``n`` valid observations, flagged when they span more than ``n`` periods."""
    valid = sorted((d, float(v)) for d, v in obs.items() if v is not None and d <= at)
    if len(valid) < n:
        return TransformResult(None, units, status="INSUFFICIENT_DATA", reason="fewer than {0} observations".format(n), detail={"at": at.isoformat()})
    window = valid[-n:]
    span_days = (window[-1][0] - window[0][0]).days
    expected_span = (n - 1) * period_days
    detail = {"at": at.isoformat(), "first": window[0][0].isoformat(), "count": n, "span_days": span_days, "expected_span_days": expected_span}
    result = TransformResult(sum(v for _, v in window) / n, units, detail=detail)
    if span_days > expected_span + period_days:
        result.status = "GAP_IN_WINDOW"
        result.reason = "the {0} observations span {1} days (expected about {2}); intermediate periods are missing".format(n, span_days, expected_span)
    return result


def credit_snapshot_params(spec: SeriesSpec, obs: Mapping[date, Decimal | None], *, at: date | None = None) -> dict[str, Any] | None:
    if at is None:
        at = latest_date(obs)
    if at is None:
        return None
    if obs.get(at) is None:
        return withdrawn_credit_params(spec, at)
    if any(d > at for d in obs):
        obs = {d: v for d, v in obs.items() if d <= at}
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


_METRIC_UPSERT = text(
    """
    INSERT INTO mi_metric_snapshots (
        metric_id, series_id, category, as_of, value, units, transform_version, status, detail_json, computed_at, ingestion_run_id, inputs_retrieved_max
    ) VALUES (
        :metric_id, :series_id, :category, :as_of, :value, :units, :transform_version, :status, CAST(:detail AS JSONB), NOW(), :run_id, :inputs_retrieved_max
    )
    ON CONFLICT (metric_id, as_of, transform_version) DO UPDATE SET
        value = EXCLUDED.value,
        units = EXCLUDED.units,
        status = EXCLUDED.status,
        detail_json = EXCLUDED.detail_json,
        computed_at = NOW(),
        ingestion_run_id = EXCLUDED.ingestion_run_id,
        inputs_retrieved_max = EXCLUDED.inputs_retrieved_max
    """
)

_CREDIT_UPSERT = text(
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
)

# Bounded default history per cadence for the first backfill (calendar days).
DEFAULT_HISTORY_DAYS = {"D": 3 * 366, "W": 5 * 366, "BW": 5 * 366, "M": 12 * 366, "Q": 12 * 366}
MAX_HISTORY_DATES_PER_SERIES = 4000


def _inputs_retrieved_max(conn, series_id: str, up_to: date) -> Any:
    return conn.execute(
        text("SELECT MAX(retrieved_at) FROM mi_macro_observations WHERE series_id = :s AND is_current AND observation_date <= :d"),
        {"s": series_id, "d": up_to},
    ).scalar()


def revised_since(conn, since) -> dict[str, date]:
    """Earliest observation date per series whose current row was (re)written after ``since``.

    A revised or newly inserted observation changes every rolling metric on or after that
    date, so the incremental recompute restarts from it instead of from the latest date only.
    """
    rows = conn.execute(
        text("SELECT series_id, MIN(observation_date) FROM mi_macro_observations WHERE retrieved_at > :since AND is_current GROUP BY series_id"),
        {"since": since},
    ).all()
    return {str(r[0]): r[1] for r in rows}


def last_analytics_run_at(conn):
    return conn.execute(
        text("SELECT MAX(finished_at) FROM mi_ingestion_runs WHERE source_id = 'ANALYTICS' AND dataset = 'metric_snapshots' AND status = 'SUCCEEDED'"),
    ).scalar()


def _write_rows(conn, rows: list[MetricRow], run_id: str | None, inputs_max: Mapping[str, Any]) -> int:
    if not rows:
        return 0
    params = []
    for row in rows:
        p = row.params(run_id)
        p["inputs_retrieved_max"] = inputs_max.get(row.series_id) if row.series_id else None
        params.append(p)
    conn.execute(_METRIC_UPSERT, params)
    return len(params)


def build_analytics(conn, *, as_of: date | None = None, run_id: str | None = None, history_start: date | None = None, since=None, series_ids: Iterable[str] | None = None) -> AnalyticsReport:
    """Compute metrics/credit snapshots from current observations.

    * default: only the latest observation date per series (plus the common curve date).
    * ``history_start``: every observation date in ``[history_start, as_of]`` per series
      (bounded, idempotent backfill; existing rows are upserted unchanged when inputs are unchanged).
    * ``since``: revision-aware incremental mode; each series is recomputed from the earliest
      observation date whose current row was written after ``since`` (or only the latest date).
    """
    report = AnalyticsReport()
    wanted = set(series_ids) if series_ids else None
    observations: dict[str, dict[date, Decimal | None]] = {}
    inputs_max: dict[str, Any] = {}
    for spec in CATALOG:
        if wanted and spec.series_id not in wanted:
            continue
        obs = current_observations(conn, spec.series_id, end=as_of)
        from market_intelligence.source_resolve import EQUIVALENTS

        for alt in EQUIVALENTS.get(spec.series_id, ()):
            if alt == spec.series_id:
                continue
            extra = current_observations(conn, alt, end=as_of)
            for day, value in extra.items():
                if day not in obs:
                    obs[day] = value
                elif obs[day] is None and value is not None:
                    obs[day] = value
                elif value is not None:
                    # Same-date tie: Treasury overlay wins (alt ids are UST_*).
                    obs[day] = value
        if not obs:
            report.series_without_data.append(spec.series_id)
            continue
        if latest_date(obs) is None and since is None and history_start is None:
            report.series_without_data.append(spec.series_id)
            continue
        observations[spec.series_id] = obs
        inputs_max[spec.series_id] = _inputs_retrieved_max(conn, spec.series_id, as_of or latest_date(obs) or max(obs))
    revised = revised_since(conn, since) if since is not None else {}

    def dates_for(spec: SeriesSpec, obs: Mapping[date, Decimal | None]) -> list[date]:
        """Dates to (re)compute, including current NULL revisions that must invalidate derived rows.

        Valid latest is the newest non-NULL observation. A later (or same) date whose current
        value is NULL is still selected so existing metric/credit rows are unpublished.
        """
        valid_latest = latest_date(obs)
        current_dates = sorted(obs)
        if not current_dates:
            return []
        start: date | None = None
        if history_start is not None:
            start = history_start
        elif since is not None and spec.series_id in revised:
            start = revised[spec.series_id]
        if start is None:
            dates = []
            if valid_latest is not None:
                dates.append(valid_latest)
            dates.extend(d for d in current_dates if valid_latest is None or d > valid_latest)
            return sorted(set(dates))
        end = max(current_dates)
        dates = sorted(d for d in current_dates if start <= d <= end)
        if len(dates) > MAX_HISTORY_DATES_PER_SERIES:
            dates = dates[-MAX_HISTORY_DATES_PER_SERIES:]
            report.truncated_series.append(spec.series_id)
        return dates

    rows: list[MetricRow] = []
    per_series_dates: dict[str, list[date]] = {}
    for sid, obs in observations.items():
        spec = CATALOG_BY_ID[sid]
        dates = dates_for(spec, obs)
        per_series_dates[sid] = dates
        for at in dates:
            rows.extend(series_metrics(spec, obs, at=at))
        if len(rows) >= 5000:
            report.metrics_written += _write_rows(conn, rows, run_id, inputs_max)
            rows = []
    curve_legs = {sid: observations.get(sid, {}) for pair in CURVE_SLOPES.values() for sid in pair}
    curve_dates = sorted({d for sid in curve_legs for d in per_series_dates.get(sid, [])})
    curve_as_of = as_of or max((latest_date(o) for o in curve_legs.values() if o), default=None)
    if curve_as_of is not None and curve_as_of not in curve_dates:
        curve_dates.append(curve_as_of)
        curve_dates.sort()
    for cd in curve_dates:
        withdrawn_leg = any(cd in (curve_legs.get(sid) or {}) and (curve_legs.get(sid) or {}).get(cd) is None for pair in CURVE_SLOPES.values() for sid in pair)
        curve_rows, common, missing = curve_metrics(curve_legs, cd)
        if withdrawn_leg or not curve_rows:
            rows.extend(withdrawn_curve_rows(cd))
        rows.extend(curve_rows)
        if cd == curve_dates[-1]:
            report.curve_date = None if withdrawn_leg else common
            report.curve_missing_legs = missing
    report.metrics_written += _write_rows(conn, rows, run_id, inputs_max)
    for sid in CREDIT_SERIES:
        obs = observations.get(sid)
        if not obs:
            continue
        spec = CATALOG_BY_ID[sid]
        params_list = []
        for at in per_series_dates.get(sid, [latest_date(obs)] if latest_date(obs) else []):
            params = credit_snapshot_params(spec, obs, at=at)
            if params is not None:
                params_list.append(params)
        if params_list:
            conn.execute(_CREDIT_UPSERT, params_list)
            report.credit_written += len(params_list)
    report.history_dates = {sid: len(d) for sid, d in per_series_dates.items() if len(d) > 1}
    return report


__all__ = ["AnalyticsReport", "CREDIT_WINDOWS", "DEFAULT_HISTORY_DAYS", "MetricRow", "STATUS_WITHDRAWN", "build_analytics", "credit_snapshot_params", "curve_metrics", "last_analytics_run_at", "revised_since", "series_metrics", "withdrawn_credit_params", "withdrawn_curve_rows", "withdrawn_series_metrics"]
