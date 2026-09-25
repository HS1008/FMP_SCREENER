"""Yahoo-backed volatility analytics. No network I/O.

Constructions:
- VIX index term structure uses Yahoo closes for ^VIX1D / ^VIX9D / ^VIX / ^VIX3M / ^VIX6M / ^VIX1Y.
  Tenor order is explicit (1D, 9D, 1M, 3M, 6M, 1Y), never alphabetical.
  It is not a VIX futures curve and is never labeled contango or backwardation.
  A displayed curve uses one common observation date only. Front=9D, back=1Y.
- SKEW is the Cboe SKEW Index from Yahoo ticker ^SKEW (not a 25-delta SPX skew).
- RV21 is the sample standard deviation (ddof=1) of 21 daily log returns
  times sqrt(252) times 100. Twenty-one returns require 22 closing prices.
- Implied vs realized uses VIX minus that RV21 on the same observation date.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime
from typing import Any, Mapping, Sequence

METHOD_VIX = "yahoo_vix_v1"
METHOD_SKEW = "yahoo_skew_index_v1"
METHOD_TERM = "yahoo_vix_index_term_v1"
METHOD_RV = "yahoo_gspc_rv21_v1"
METHOD_SPREAD = "vix_minus_gspc_rv21_v1"

CATEGORY = "YAHOO_VOL"
SOURCE_ID = "YAHOO_VOL"

TICKER_VIX = "^VIX"
TICKER_SKEW = "^SKEW"
TICKER_RV = "^GSPC"
RV_UNDERLYING_LABEL = "GSPC"

METRIC_RV = "GSPC_REALIZED_VOL_21D"
METRIC_SPREAD = "VIX_MINUS_GSPC_RV21"

# (yahoo_ticker, tenor_label, metric_id) in display order. Do not sort these labels.
TERM_TENORS: tuple[tuple[str, str, str], ...] = (
    ("^VIX1D", "1D", "VIX_1D"),
    ("^VIX9D", "9D", "VIX_9D"),
    ("^VIX", "1M", "VIX_1M"),
    ("^VIX3M", "3M", "VIX_3M"),
    ("^VIX6M", "6M", "VIX_6M"),
    ("^VIX1Y", "1Y", "VIX_1Y"),
)
TENOR_AXIS: tuple[str, ...] = tuple(tenor for _ticker, tenor, _metric_id in TERM_TENORS)
TENOR_METRIC_IDS: tuple[str, ...] = tuple(metric_id for _ticker, _tenor, metric_id in TERM_TENORS)
TENOR_RANK: dict[str, int] = {tenor: index for index, tenor in enumerate(TENOR_AXIS)}

FRONT_TENOR = "9D"
BACK_TENOR = "1Y"
FRONT_TICKER = "^VIX9D"
BACK_TICKER = "^VIX1Y"

RV_RETURNS = 21
RV_CLOSES = RV_RETURNS + 1  # 22 closes produce 21 daily log returns
RV_WINDOW = RV_RETURNS
FLAT_EPS = 0.05

SeriesRows = Sequence[tuple[date, float]]


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _positive_closes(closes: Sequence[float | None]) -> list[float]:
    ordered: list[float] = []
    for close in closes:
        number = positive_number(close)
        if number is not None:
            ordered.append(number)
    return ordered


def _positive_series(rows: SeriesRows) -> list[tuple[date, float]]:
    by_day: dict[date, float] = {}
    for day, close in rows:
        session = _as_date(day)
        number = positive_number(close)
        if session is None or number is None:
            continue
        by_day[session] = number
    return sorted(by_day.items())


def _rv_detail() -> dict[str, Any]:
    return {
        "method": METHOD_RV,
        "window": RV_WINDOW,
        "returns": RV_RETURNS,
        "closes_required": RV_CLOSES,
        "annualization": 252,
        "std": "sample",
        "ddof": 1,
        "underlying": RV_UNDERLYING_LABEL,
        "yahoo_ticker": TICKER_RV,
        "return": "close_to_close_log",
    }


def realized_vol_21(closes: Sequence[float | None]) -> dict[str, Any]:
    """Sample stdev of the last 21 close-to-close log returns, annualized to vol points.

    Twenty-one returns require 22 positive closes. Fewer closes stay missing (never zero).
    """
    detail = _rv_detail()
    ordered = _positive_closes(closes)
    if len(ordered) < RV_CLOSES:
        return {
            "value": None,
            "status": "INCOMPLETE",
            "reason": "fewer_than_22_closes",
            "observation_date": None,
            **detail,
            "observations": len(ordered),
        }
    window = ordered[-RV_CLOSES:]
    returns: list[float] = []
    for i in range(1, len(window)):
        prev, cur = window[i - 1], window[i]
        if prev <= 0 or cur <= 0:
            return {
                "value": None,
                "status": "INCOMPLETE",
                "reason": "non_positive_close",
                "observation_date": None,
                **detail,
            }
        returns.append(math.log(cur / prev))
    if len(returns) != RV_RETURNS:
        return {
            "value": None,
            "status": "INCOMPLETE",
            "reason": "incomplete_return_window",
            "observation_date": None,
            **detail,
        }
    stdev = statistics.stdev(returns)  # sample, ddof=1
    value = stdev * math.sqrt(252) * 100.0
    return {
        "value": value,
        "status": "OK",
        "reason": None,
        "observation_date": None,
        **detail,
        "observations": len(ordered),
    }


def realized_vol_21_from_series(rows: SeriesRows) -> dict[str, Any]:
    """Latest RV21. Observation date is the last close in the 22-close window."""
    ordered = _positive_series(rows)
    if len(ordered) < RV_CLOSES:
        result = realized_vol_21([close for _day, close in ordered])
        result["observations"] = len(ordered)
        return result
    window = ordered[-RV_CLOSES:]
    result = realized_vol_21([close for _day, close in window])
    result["observation_date"] = window[-1][0]
    result["window_start_date"] = window[0][0]
    result["observations"] = len(ordered)
    return result


def historical_realized_vol(rows: SeriesRows) -> list[dict[str, Any]]:
    """Rolling RV21 for every date with 21 trailing valid log returns.

    Dates without a full 22-close window are omitted. They are not stored as zero.
    Each point uses only closes on or before its observation date.
    """
    ordered = _positive_series(rows)
    points: list[dict[str, Any]] = []
    if len(ordered) < RV_CLOSES:
        return points
    for end_idx in range(RV_RETURNS, len(ordered)):
        window = ordered[end_idx - RV_RETURNS : end_idx + 1]
        if len(window) != RV_CLOSES:
            continue
        result = realized_vol_21([close for _day, close in window])
        if result.get("status") != "OK" or result.get("value") is None:
            continue
        result["observation_date"] = window[-1][0]
        result["window_start_date"] = window[0][0]
        result["observations"] = end_idx + 1
        points.append(result)
    return points


def vix_minus_rv(
    vix: float | None,
    rv: Mapping[str, Any],
    *,
    vix_observation_date: date | None = None,
    rv_observation_date: date | None = None,
) -> dict[str, Any]:
    """VIX − RV21 only when both sides share the same observation date."""
    base = {
        "metric_id": METRIC_SPREAD,
        "method": METHOD_SPREAD,
        "underlying": RV_UNDERLYING_LABEL,
        "window": RV_WINDOW,
        "observation_date": None,
    }
    if rv.get("status") != "OK" or rv.get("value") is None:
        return {
            **base,
            "value": None,
            "vix": vix,
            "rv": rv.get("value"),
            "status": "INCOMPLETE",
            "reason": rv.get("reason") or "missing_rv",
            "vix_observation_date": vix_observation_date,
            "rv_observation_date": rv_observation_date or rv.get("observation_date"),
        }
    rv_date = rv_observation_date or rv.get("observation_date")
    if vix is None:
        return {
            **base,
            "value": None,
            "vix": None,
            "rv": rv.get("value"),
            "status": "INCOMPLETE",
            "reason": "missing_vix",
            "vix_observation_date": vix_observation_date,
            "rv_observation_date": rv_date,
        }
    if vix_observation_date is None or rv_date is None or vix_observation_date != rv_date:
        return {
            **base,
            "value": None,
            "vix": float(vix),
            "rv": float(rv["value"]),
            "status": "INCOMPLETE",
            "reason": "date_mismatch",
            "vix_observation_date": vix_observation_date,
            "rv_observation_date": rv_date,
        }
    return {
        **base,
        "value": float(vix) - float(rv["value"]),
        "vix": float(vix),
        "rv": float(rv["value"]),
        "status": "OK",
        "reason": None,
        "observation_date": vix_observation_date,
        "vix_observation_date": vix_observation_date,
        "rv_observation_date": rv_date,
    }


def align_vix_minus_rv(vix_rows: SeriesRows, gspc_rows: SeriesRows) -> dict[str, Any]:
    """Latest common session date with a VIX close and an RV21 window ending that day."""
    vix_by = {day: close for day, close in _positive_series(vix_rows)}
    common_dates = sorted(vix_by.keys(), reverse=True)
    gspc_ordered = _positive_series(gspc_rows)
    for day in common_dates:
        gspc_upto = [(session, close) for session, close in gspc_ordered if session <= day]
        rv = realized_vol_21_from_series(gspc_upto)
        if rv.get("status") != "OK" or rv.get("observation_date") != day:
            continue
        return vix_minus_rv(vix_by[day], rv, vix_observation_date=day, rv_observation_date=day)
    rv_latest = realized_vol_21_from_series(gspc_ordered)
    vix_latest_date = common_dates[0] if common_dates else None
    return vix_minus_rv(
        vix_by.get(vix_latest_date) if vix_latest_date else None,
        rv_latest,
        vix_observation_date=vix_latest_date,
        rv_observation_date=rv_latest.get("observation_date"),
    )


def historical_vix_minus_rv(vix_rows: SeriesRows, rv_points: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Same-date VIX − RV21 points. A missing side is omitted, never zero-filled."""
    vix_by = {day: close for day, close in _positive_series(vix_rows)}
    spreads: list[dict[str, Any]] = []
    for rv in rv_points:
        day = _as_date(rv.get("observation_date"))
        if day is None or day not in vix_by:
            continue
        spread = vix_minus_rv(vix_by[day], rv, vix_observation_date=day, rv_observation_date=day)
        if spread.get("status") == "OK" and spread.get("value") is not None:
            spreads.append(spread)
    spreads.sort(key=lambda item: item["observation_date"])
    return spreads


def classify_slope(front: float | None, back: float | None) -> dict[str, Any]:
    """Index-curve shape from back(1Y) − front(9D). Never contango or backwardation."""
    if front is None or back is None:
        return {
            "front_to_back_slope": None,
            "curve_state": None,
            "slope_status": "INCOMPLETE",
            "slope_reason": "missing_canonical_endpoints",
        }
    slope = float(back) - float(front)
    if abs(slope) < FLAT_EPS:
        state = "flat"
    elif slope > 0:
        state = "upward_sloping"
    else:
        state = "downward_sloping"
    return {
        "front_to_back_slope": slope,
        "curve_state": state,
        "slope_status": "OK",
        "slope_reason": None,
    }


def term_structure(series: Mapping[str, SeriesRows]) -> dict[str, Any]:
    """Build the latest VIX index term structure from one common observation date.

    Front endpoint is 9D (^VIX9D); back endpoint is 1Y (^VIX1Y). Slope is published
    only when both endpoints exist on the chosen date. 1D is part of the displayed
    curve and does not change the slope endpoints. Intermediate gaps stay unavailable
    for that date — never filled from another session.
    """
    by_date: dict[date, dict[str, float]] = {}
    for ticker, _tenor, _metric_id in TERM_TENORS:
        for day, level in series.get(ticker) or ():
            number = positive_number(level)
            session = _as_date(day)
            if number is None or session is None:
                continue
            by_date.setdefault(session, {})[ticker] = number

    candidates = sorted(by_date.keys(), reverse=True)
    curve_date: date | None = None
    for day in candidates:
        levels = by_date[day]
        if FRONT_TICKER in levels and BACK_TICKER in levels:
            curve_date = day
            break
    if curve_date is None and candidates:
        curve_date = candidates[0]

    points = []
    unavailable: list[str] = []
    for ticker, tenor, metric_id in TERM_TENORS:
        level = by_date.get(curve_date, {}).get(ticker) if curve_date is not None else None
        if level is None:
            unavailable.append(tenor)
        points.append(
            {
                "ticker": ticker,
                "tenor": tenor,
                "metric_id": metric_id,
                "level": level,
                "observation_date": curve_date if level is not None else None,
            }
        )

    front = by_date.get(curve_date, {}).get(FRONT_TICKER) if curve_date is not None else None
    back = by_date.get(curve_date, {}).get(BACK_TICKER) if curve_date is not None else None
    slope_info = classify_slope(front, back)
    if curve_date is None:
        slope_info = {
            "front_to_back_slope": None,
            "curve_state": None,
            "slope_status": "INCOMPLETE",
            "slope_reason": "no_tenor_observations",
        }

    return {
        "label": "VIX index term structure",
        "construction": METHOD_TERM,
        "observation_date": curve_date,
        "front_tenor": FRONT_TENOR,
        "back_tenor": BACK_TENOR,
        "points": points,
        "front_to_back_slope": slope_info["front_to_back_slope"],
        "curve_state": slope_info["curve_state"],
        "slope_status": slope_info["slope_status"],
        "slope_reason": slope_info["slope_reason"],
        "unavailable_tenors": unavailable,
        "tenor_axis": list(TENOR_AXIS),
        "note": (
            "Index tenors from Yahoo closes on one common observation date. "
            "Order is 1D, 9D, 1M, 3M, 6M, 1Y. Front=9D, back=1Y. "
            "Not a VIX futures curve. Never labeled contango/backwardation."
        ),
    }


def common_curve_dates_from_history(history: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[date]:
    """Trading dates where every required tenor has a non-null level.

    The intersection is the selectable six-tenor curve. A tenor that does not
    yet exist is absent, so earlier dates stay out of this set.
    """
    sets: list[set[date]] = []
    for metric_id in TENOR_METRIC_IDS:
        days: set[date] = set()
        for row in history.get(metric_id) or ():
            if row.get("value") is None:
                continue
            session = _as_date(row.get("as_of"))
            if session is not None:
                days.add(session)
        sets.append(days)
    if not sets or any(not day_set for day_set in sets):
        return []
    return sorted(set.intersection(*sets))


def resolve_curve_date(requested: date | None, common_dates: Sequence[date]) -> date | None:
    """Most recent common curve date on or before ``requested``.

    None means no eligible date exists. The resolver never returns a later session.
    """
    ordered = sorted({day for day in (_as_date(item) for item in common_dates) if day is not None})
    if not ordered:
        return None
    if requested is None:
        return ordered[-1]
    target = _as_date(requested)
    if target is None:
        return ordered[-1]
    eligible = [day for day in ordered if day <= target]
    if not eligible:
        return None
    return eligible[-1]


def curve_levels_on_date(history: Mapping[str, Sequence[Mapping[str, Any]]], curve_date: date | None) -> list[dict[str, Any]]:
    """Tenor levels for one date, in TENOR_AXIS order. Missing tenors stay None."""
    target = _as_date(curve_date)
    levels: list[dict[str, Any]] = []
    for ticker, tenor, metric_id in TERM_TENORS:
        value = None
        if target is not None:
            for row in history.get(metric_id) or ():
                if _as_date(row.get("as_of")) != target or row.get("value") is None:
                    continue
                number = positive_number(row.get("value"))
                if number is None:
                    continue
                value = number
                break
        levels.append(
            {
                "tenor": tenor,
                "metric_id": metric_id,
                "yahoo_ticker": ticker,
                "value": value,
                "as_of": None if value is None or target is None else target.isoformat(),
            }
        )
    return levels


def positive_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number) or number <= 0:
        return None
    return number
