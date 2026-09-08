"""Pure, versioned macro/rates/credit transforms.

All functions take an ordered mapping ``{observation_date: value}`` (``value`` may be
``None``) and return :class:`TransformResult` with the value, units, and the exact
comparison anchors used, or ``None`` with a reason. Nothing is forward-filled and
lags are exact calendar periods, so a missing month yields ``None`` rather than a
row-shifted wrong lag.
"""

from __future__ import annotations

import calendar
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

TRANSFORM_VERSION = "macro_transforms_v1"

# Allowed lag (calendar days) between a calendar anchor and the comparison observation.
ALLOWED_ANCHOR_LAG_DAYS = {"D": 5, "W": 8, "BW": 16, "M": 35, "Q": 100}
# Gap beyond which a "previous observation" change is not a one-session move.
ONE_SESSION_MAX_GAP_DAYS = 4


@dataclass
class TransformResult:
    value: float | None
    units: str
    status: str = "OK"
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_detail(self) -> dict[str, Any]:
        out = dict(self.detail)
        out["status"] = self.status
        if self.reason:
            out["reason"] = self.reason
        out["transform_version"] = TRANSFORM_VERSION
        return out


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def shift_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def latest_date(obs: Mapping[date, Any]) -> date | None:
    valid = [d for d, v in obs.items() if _f(v) is not None]
    return max(valid) if valid else None


def _missing(units: str, reason: str, **detail: Any) -> TransformResult:
    return TransformResult(None, units, status="INSUFFICIENT_DATA", reason=reason, detail=detail)


# ---- exact-period ratios ----------------------------------------------------------

def period_ratio_pct(obs: Mapping[date, Any], at: date, months: int, *, annualize_periods: float = 1.0, units: str = "pct") -> TransformResult:
    """100 * ((x_t / x_{t-months}) ** annualize_periods - 1) with exact calendar alignment."""
    x_t = _f(obs.get(at))
    lag_date = shift_months(at, -months)
    x_lag = _f(obs.get(lag_date))
    detail = {"at": at.isoformat(), "lag_date": lag_date.isoformat(), "months": months, "annualize_periods": annualize_periods}
    if x_t is None:
        return _missing(units, "missing_current", **detail)
    if x_lag is None:
        return _missing(units, "missing_lag_observation", **detail)
    if x_lag <= 0:
        return _missing(units, "non_positive_base", **detail)
    value = 100.0 * ((x_t / x_lag) ** annualize_periods - 1.0)
    return TransformResult(value, units, detail=detail)


def yoy_pct(obs: Mapping[date, Any], at: date) -> TransformResult:
    return period_ratio_pct(obs, at, 12)


def ann3m_pct(obs: Mapping[date, Any], at: date) -> TransformResult:
    return period_ratio_pct(obs, at, 3, annualize_periods=4.0)


def ann6m_pct(obs: Mapping[date, Any], at: date) -> TransformResult:
    return period_ratio_pct(obs, at, 6, annualize_periods=2.0)


def mom_pct(obs: Mapping[date, Any], at: date) -> TransformResult:
    return period_ratio_pct(obs, at, 1)


def qoq_annualized_pct(obs: Mapping[date, Any], at: date) -> TransformResult:
    """Quarterly series: 100 * ((x_q / x_{q-1}) ** 4 - 1). Input must be a level, not an annualized rate."""
    return period_ratio_pct(obs, at, 3, annualize_periods=4.0)


def period_difference(obs: Mapping[date, Any], at: date, months: int, *, units: str) -> TransformResult:
    x_t = _f(obs.get(at))
    lag_date = shift_months(at, -months)
    x_lag = _f(obs.get(lag_date))
    detail = {"at": at.isoformat(), "lag_date": lag_date.isoformat(), "months": months}
    if x_t is None:
        return _missing(units, "missing_current", **detail)
    if x_lag is None:
        return _missing(units, "missing_lag_observation", **detail)
    return TransformResult(x_t - x_lag, units, detail=detail)


# ---- units -------------------------------------------------------------------------

def pct_to_bps(value: float | None) -> float | None:
    return None if value is None else value * 100.0


# ---- comparison changes ------------------------------------------------------------

def _sorted_valid(obs: Mapping[date, Any]) -> list[tuple[date, float]]:
    rows = [(d, _f(v)) for d, v in obs.items()]
    return sorted((d, v) for d, v in rows if v is not None)


def previous_observation_change(obs: Mapping[date, Any], at: date, *, units: str = "level", scale: float = 1.0) -> TransformResult:
    """Change versus the prior valid observation; flags gaps that are not one session."""
    rows = _sorted_valid(obs)
    current = next((v for d, v in rows if d == at), None)
    if current is None:
        return _missing(units, "missing_current", at=at.isoformat())
    prior = [(d, v) for d, v in rows if d < at]
    if not prior:
        return _missing(units, "no_prior_observation", at=at.isoformat())
    prior_date, prior_value = prior[-1]
    gap = (at - prior_date).days
    detail = {"at": at.isoformat(), "comparison_date": prior_date.isoformat(), "gap_days": gap}
    result = TransformResult((current - prior_value) * scale, units, detail=detail)
    if gap > ONE_SESSION_MAX_GAP_DAYS:
        result.status = "MULTI_SESSION_GAP"
        result.reason = "gap of {0} days is not a one-session move".format(gap)
    return result


def calendar_change(
    obs: Mapping[date, Any],
    at: date,
    *,
    days: int | None = None,
    months: int | None = None,
    cadence: str = "D",
    units: str = "level",
    scale: float = 1.0,
) -> TransformResult:
    """Change versus the last observation on/before a calendar anchor within the allowed lag."""
    rows = _sorted_valid(obs)
    current = next((v for d, v in rows if d == at), None)
    if current is None:
        return _missing(units, "missing_current", at=at.isoformat())
    if months is not None:
        anchor = shift_months(at, -months)
    elif days is not None:
        anchor = at - timedelta(days=days)
    else:
        raise ValueError("days or months required")
    allowed = ALLOWED_ANCHOR_LAG_DAYS.get(cadence.upper(), 5)
    candidates = [(d, v) for d, v in rows if d <= anchor]
    detail = {"at": at.isoformat(), "anchor_date": anchor.isoformat(), "allowed_lag_days": allowed}
    if not candidates:
        return _missing(units, "no_observation_on_or_before_anchor", **detail)
    cmp_date, cmp_value = candidates[-1]
    lag = (anchor - cmp_date).days
    detail.update({"comparison_date": cmp_date.isoformat(), "anchor_lag_days": lag})
    if lag > allowed:
        return _missing(units, "comparison_observation_outside_allowed_lag", **detail)
    return TransformResult((current - cmp_value) * scale, units, detail=detail)


# ---- curve -------------------------------------------------------------------------

def curve_slope(
    long_leg: Mapping[date, Any],
    short_leg: Mapping[date, Any],
    at: date,
    *,
    units: str = "bps",
) -> TransformResult:
    """Long minus short aligned to the same observation date; missing legs are reported."""
    long_v = _f(long_leg.get(at))
    short_v = _f(short_leg.get(at))
    missing = [name for name, v in (("long", long_v), ("short", short_v)) if v is None]
    detail = {"at": at.isoformat(), "missing_legs": missing}
    if missing:
        return _missing(units, "missing_leg", **detail)
    return TransformResult(pct_to_bps(long_v - short_v), units, detail=detail)


def common_curve_date(legs: Mapping[str, Mapping[date, Any]], as_of: date) -> tuple[date | None, list[str]]:
    """Latest date <= as_of at which every leg with data has a value; also list legs missing there."""
    candidates: set[date] = set()
    for series in legs.values():
        candidates.update(d for d, v in series.items() if _f(v) is not None and d <= as_of)
    if not candidates:
        return None, sorted(legs)
    for d in sorted(candidates, reverse=True):
        missing = [name for name, series in legs.items() if _f(series.get(d)) is None]
        if not missing:
            return d, []
    best = max(candidates)
    return best, [name for name, series in legs.items() if _f(series.get(best)) is None]


# ---- descriptive statistics ----------------------------------------------------------

def percentile_rank(values: Sequence[float], current: float) -> float:
    """Mid-rank percentile in [0, 100]: (#less + 0.5 * #equal) / n * 100."""
    n = len(values)
    if n == 0:
        raise ValueError("empty window")
    less = sum(1 for v in values if v < current)
    equal = sum(1 for v in values if v == current)
    return 100.0 * (less + 0.5 * equal) / n


def window_statistics(
    obs: Mapping[date, Any],
    at: date,
    *,
    window_days: int,
    min_observations: int,
    label: str,
) -> TransformResult:
    """Percentile and sample-std z-score of the value at ``at`` over the trailing window.

    Descriptive only. NULL for insufficient coverage or zero variance. The window is
    ``(at - window_days, at]`` inclusive of ``at``.
    """
    rows = _sorted_valid(obs)
    current = next((v for d, v in rows if d == at), None)
    start = at - timedelta(days=window_days)
    window = [v for d, v in rows if start < d <= at]
    first_date = next((d for d, v in rows if start < d <= at), None)
    detail = {
        "at": at.isoformat(),
        "window": label,
        "window_days": window_days,
        "window_start_exclusive": start.isoformat(),
        "observations": len(window),
        "min_observations": min_observations,
        "first_observation_in_window": first_date.isoformat() if first_date else None,
        "tie_convention": "midrank",
        "std_convention": "sample_ddof1",
    }
    if current is None:
        return _missing("pctile", "missing_current", **detail)
    if len(window) < min_observations:
        result = _missing("pctile", "insufficient_history", **detail)
        result.status = "INSUFFICIENT_HISTORY"
        return result
    pct = percentile_rank(window, current)
    std = statistics.stdev(window) if len(window) > 1 else 0.0
    mean = statistics.fmean(window)
    z = None if std == 0 else (current - mean) / std
    detail.update({"percentile": pct, "zscore": z, "mean": mean, "std": std})
    if z is None:
        detail["zscore_reason"] = "zero_variance"
    return TransformResult(pct, "pctile", detail=detail)


__all__ = [
    "ALLOWED_ANCHOR_LAG_DAYS",
    "ONE_SESSION_MAX_GAP_DAYS",
    "TRANSFORM_VERSION",
    "TransformResult",
    "ann3m_pct",
    "ann6m_pct",
    "calendar_change",
    "common_curve_date",
    "curve_slope",
    "latest_date",
    "mom_pct",
    "pct_to_bps",
    "percentile_rank",
    "period_difference",
    "period_ratio_pct",
    "previous_observation_change",
    "qoq_annualized_pct",
    "shift_months",
    "window_statistics",
    "yoy_pct",
]
