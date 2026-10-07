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


def anchored_yoy_pct(obs: Mapping[date, Any], at: date, *, cadence: str = "W") -> TransformResult:
    """Year-over-year ratio using the last print on or before t−12 months.

    Weekly series rarely land on the same calendar date one year later.
    The comparison must fall within the cadence's allowed anchor lag.
    Monthly and quarterly series keep exact-date :func:`yoy_pct`.
    """
    rows = _sorted_valid(obs)
    current = _value_on(rows, at)
    anchor = shift_months(at, -12)
    allowed = ALLOWED_ANCHOR_LAG_DAYS.get(cadence.upper(), 5)
    detail = {"at": at.isoformat(), "anchor_date": anchor.isoformat(), "months": 12, "allowed_lag_days": allowed, "cadence": cadence.upper()}
    if current is None:
        return _missing("pct", "missing_current", **detail)
    compared = _last_on_or_before(rows, anchor)
    if compared is None:
        return _missing("pct", "no_observation_on_or_before_anchor", **detail)
    cmp_date, cmp_value = compared
    lag = (anchor - cmp_date).days
    detail.update({"comparison_date": cmp_date.isoformat(), "anchor_lag_days": lag})
    if lag > allowed:
        return _missing("pct", "comparison_observation_outside_allowed_lag", **detail)
    if cmp_value <= 0:
        return _missing("pct", "non_positive_base", **detail)
    return TransformResult(100.0 * (current / cmp_value - 1.0), "pct", detail=detail)


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


def mom_change_ma3(obs: Mapping[date, Any], at: date, *, units: str) -> TransformResult:
    """Mean of the three exact monthly differences ending at ``at``.

    Requires levels at t, t−1, t−2, and t−3. A missing lag stays missing.
    The average is of payroll changes, not of the employment level.
    """
    diffs: list[float] = []
    missing: list[str] = []
    anchors: list[str] = []
    for offset in range(3):
        current = shift_months(at, -offset)
        prior = shift_months(at, -(offset + 1))
        anchors.append(current.isoformat())
        left = _f(obs.get(current))
        right = _f(obs.get(prior))
        if left is None or right is None:
            missing.append(current.isoformat() if left is None else prior.isoformat())
        else:
            diffs.append(left - right)
    detail = {"at": at.isoformat(), "anchors": anchors, "months": 3}
    if missing or len(diffs) != 3:
        return _missing(units, "missing_lag_observation", missing=missing, **detail)
    return TransformResult(statistics.fmean(diffs), units, detail=detail)


def sum_12m(obs: Mapping[date, Any], at: date, *, units: str) -> TransformResult:
    """Sum of the observation and the prior 11 exact calendar months.

    A gap stays missing. This is not the sum of the last 12 valid rows.
    """
    dates = [shift_months(at, -offset) for offset in range(12)]
    values: list[float] = []
    missing: list[str] = []
    for day in dates:
        value = _f(obs.get(day))
        if value is None:
            missing.append(day.isoformat())
        else:
            values.append(value)
    detail = {"at": at.isoformat(), "window_start": dates[-1].isoformat(), "months": 12}
    if missing:
        return _missing(units, "missing_window_observation", missing=missing, **detail)
    return TransformResult(sum(values), units, detail=detail)


# ---- units -------------------------------------------------------------------------

def pct_to_bps(value: float | None) -> float | None:
    return None if value is None else value * 100.0


# ---- comparison changes ------------------------------------------------------------

def _sorted_valid_uncached(obs: Mapping[date, Any]) -> list[tuple[date, float]]:
    rows = [(d, _f(v)) for d, v in obs.items()]
    return sorted((d, v) for d, v in rows if v is not None)


class CachedObservations(dict):
    """Observation map that sorts its valid rows once.

    Transforms call ``sorted_rows``. A later write drops the cache. A plain dict
    is sorted on every call, so a test that mutates it between calls stays correct.
    """

    def sorted_rows(self) -> list[tuple[date, float]]:
        rows = getattr(self, "_sorted_rows", None)
        if rows is None:
            rows = _sorted_valid_uncached(self)
            self._sorted_rows = rows
        return rows

    def _drop_cache(self) -> None:
        self._sorted_rows = None

    def __setitem__(self, key: date, value: Any) -> None:
        self._drop_cache()
        super().__setitem__(key, value)

    def __delitem__(self, key: date) -> None:
        self._drop_cache()
        super().__delitem__(key)

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._drop_cache()
        super().update(*args, **kwargs)

    def clear(self) -> None:
        self._drop_cache()
        super().clear()

    def setdefault(self, key: date, default: Any = None) -> Any:
        self._drop_cache()
        return super().setdefault(key, default)


def _sorted_valid(obs: Mapping[date, Any]) -> list[tuple[date, float]]:
    reader = getattr(obs, "sorted_rows", None)
    if callable(reader):
        return reader()
    return _sorted_valid_uncached(obs)


def _first_on_or_after(rows: Sequence[tuple[date, float]], day: date) -> int:
    lo = 0
    hi = len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        if rows[mid][0] < day:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _value_on(rows: Sequence[tuple[date, float]], day: date) -> float | None:
    idx = _first_on_or_after(rows, day)
    if idx < len(rows) and rows[idx][0] == day:
        return rows[idx][1]
    return None


def _last_on_or_before(rows: Sequence[tuple[date, float]], day: date) -> tuple[date, float] | None:
    idx = _first_on_or_after(rows, day)
    if idx < len(rows) and rows[idx][0] == day:
        return rows[idx]
    if idx == 0:
        return None
    return rows[idx - 1]


def valid_rows_through(obs: Mapping[date, Any], at: date) -> list[tuple[date, float]]:
    """Valid observations on or before ``at``, oldest first."""
    rows = _sorted_valid(obs)
    idx = _first_on_or_after(rows, at)
    if idx < len(rows) and rows[idx][0] == at:
        return list(rows[: idx + 1])
    return list(rows[:idx])


def previous_observation_change(obs: Mapping[date, Any], at: date, *, units: str = "level", scale: float = 1.0) -> TransformResult:
    """Change versus the prior valid observation; flags gaps that are not one session."""
    rows = _sorted_valid(obs)
    idx = _first_on_or_after(rows, at)
    if idx >= len(rows) or rows[idx][0] != at:
        return _missing(units, "missing_current", at=at.isoformat())
    if idx == 0:
        return _missing(units, "no_prior_observation", at=at.isoformat())
    current = rows[idx][1]
    prior_date, prior_value = rows[idx - 1]
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
    current = _value_on(rows, at)
    if current is None:
        return _missing(units, "missing_current", at=at.isoformat())
    if months is not None:
        anchor = shift_months(at, -months)
    elif days is not None:
        anchor = at - timedelta(days=days)
    else:
        raise ValueError("days or months required")
    allowed = ALLOWED_ANCHOR_LAG_DAYS.get(cadence.upper(), 5)
    compared = _last_on_or_before(rows, anchor)
    detail = {"at": at.isoformat(), "anchor_date": anchor.isoformat(), "allowed_lag_days": allowed}
    if compared is None:
        return _missing(units, "no_observation_on_or_before_anchor", **detail)
    cmp_date, cmp_value = compared
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


def curve_butterfly(
    two_year: Mapping[date, Any],
    five_year: Mapping[date, Any],
    ten_year: Mapping[date, Any],
    at: date,
    *,
    units: str = "bps",
) -> TransformResult:
    """2×5Y − 2Y − 10Y on one observation date, in basis points.

    Yields are percentage points. ``fly_bps = (2*DGS5 - DGS2 - DGS10) * 100``.
    Positive means the 5Y yield is above the average of the 2Y and 10Y wings.
    A missing leg stays missing. Nearby dates are not consulted, and the result
    is not a backward fill of any Treasury tenor.
    """
    two = _f(two_year.get(at))
    five = _f(five_year.get(at))
    ten = _f(ten_year.get(at))
    missing = [name for name, value in (("DGS2", two), ("DGS5", five), ("DGS10", ten)) if value is None]
    detail = {
        "at": at.isoformat(),
        "legs": ["DGS2", "DGS5", "DGS10"],
        "formula": "2*DGS5 - DGS2 - DGS10",
        "same_date": True,
        "transform": "percent_points_times_100",
        "missing_legs": missing,
        "sign": "positive when 5Y is above the average of the 2Y and 10Y wings",
    }
    if missing:
        return _missing(units, "missing_leg", **detail)
    return TransformResult(pct_to_bps((2.0 * five) - two - ten), units, detail=detail)


def _has_valid_on(rows: Sequence[tuple[date, float]], last: int, day: date) -> bool:
    """True when ``day`` is a valid row at or before index ``last``."""
    if last < 0:
        return False
    idx = _first_on_or_after(rows, day)
    return idx <= last and idx < len(rows) and rows[idx][0] == day


def common_curve_date(legs: Mapping[str, Mapping[date, Any]], as_of: date) -> tuple[date | None, list[str]]:
    """Latest date <= as_of at which every leg has a value; also list legs missing there.

    A date present on every leg returns immediately. A gap walks backward from
    each leg's latest valid row instead of rebuilding the full history.
    """
    prepared: list[tuple[str, list[tuple[date, float]], int]] = []
    any_point = False
    all_on_as_of = True
    for name, series in legs.items():
        rows = _sorted_valid(series)
        end = _first_on_or_after(rows, as_of)
        if end < len(rows) and rows[end][0] == as_of:
            last = end
        else:
            last = end - 1
            all_on_as_of = False
        if last >= 0:
            any_point = True
        else:
            all_on_as_of = False
        prepared.append((name, rows, last))
    if not any_point:
        return None, sorted(legs)
    if all_on_as_of:
        return as_of, []
    idxs = [last for _name, _rows, last in prepared]
    while True:
        dated = [prepared[i][1][idxs[i]][0] for i in range(len(prepared)) if idxs[i] >= 0]
        if not dated:
            break
        day = max(dated)
        missing = [prepared[i][0] for i in range(len(prepared)) if idxs[i] < 0 or prepared[i][1][idxs[i]][0] != day]
        if not missing:
            return day, []
        for i in range(len(prepared)):
            if idxs[i] >= 0 and prepared[i][1][idxs[i]][0] == day:
                idxs[i] -= 1
    best = max(rows[last][0] for _name, rows, last in prepared if last >= 0)
    missing = [name for name, rows, last in prepared if not _has_valid_on(rows, last, best)]
    return best, missing


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
    current = _value_on(rows, at)
    start = at - timedelta(days=window_days)
    left = _first_on_or_after(rows, start)
    if left < len(rows) and rows[left][0] == start:
        left += 1
    right = _first_on_or_after(rows, at)
    if right < len(rows) and rows[right][0] == at:
        right += 1
    window_rows = rows[left:right] if left < right else []
    window = [v for _d, v in window_rows]
    first_date = window_rows[0][0] if window_rows else None
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
    "CachedObservations",
    "TransformResult",
    "valid_rows_through",
    "anchored_yoy_pct",
    "ann3m_pct",
    "ann6m_pct",
    "calendar_change",
    "common_curve_date",
    "curve_butterfly",
    "curve_slope",
    "latest_date",
    "mom_change_ma3",
    "mom_pct",
    "pct_to_bps",
    "percentile_rank",
    "period_difference",
    "sum_12m",
    "period_ratio_pct",
    "previous_observation_change",
    "qoq_annualized_pct",
    "shift_months",
    "window_statistics",
    "yoy_pct",
]
