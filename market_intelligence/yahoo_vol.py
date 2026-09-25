"""Yahoo-backed volatility analytics. No network I/O.

Constructions:
- VIX index term structure uses Yahoo closes for ^VIX9D / ^VIX / ^VIX3M / ^VIX6M / ^VIX1Y.
  It is not a VIX futures curve and is never labeled contango or backwardation.
  A displayed curve uses one common observation date only; front=9D, back=1Y.
- SKEW is the Cboe SKEW Index from Yahoo ticker ^SKEW (not a 25-delta SPX skew).
- RV20 is sample stdev of 20 daily log returns times sqrt(252) times 100 (vol points).
- Implied vs realized uses VIX minus that RV20 on the same observation date.
"""

from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Any, Mapping, Sequence

METHOD_VIX = "yahoo_vix_v1"
METHOD_SKEW = "yahoo_skew_index_v1"
METHOD_TERM = "yahoo_vix_index_term_v1"
METHOD_RV = "yahoo_gspc_rv20_v1"
METHOD_SPREAD = "vix_minus_gspc_rv20_v1"

CATEGORY = "YAHOO_VOL"
SOURCE_ID = "YAHOO_VOL"

TICKER_VIX = "^VIX"
TICKER_SKEW = "^SKEW"
TICKER_RV = "^GSPC"
RV_UNDERLYING_LABEL = "GSPC"

# (yahoo_ticker, tenor_label, metric_id)
TERM_TENORS: tuple[tuple[str, str, str], ...] = (
    ("^VIX9D", "9D", "VIX_9D"),
    ("^VIX", "1M", "VIX_1M"),
    ("^VIX3M", "3M", "VIX_3M"),
    ("^VIX6M", "6M", "VIX_6M"),
    ("^VIX1Y", "1Y", "VIX_1Y"),
)

FRONT_TENOR = "9D"
BACK_TENOR = "1Y"
FRONT_TICKER = "^VIX9D"
BACK_TICKER = "^VIX1Y"

RV_WINDOW = 20
FLAT_EPS = 0.05

SeriesRows = Sequence[tuple[date, float]]


def realized_vol_20(closes: Sequence[float | None]) -> dict[str, Any]:
    """Sample stdev of last 20 close-to-close log returns, annualized to vol points."""
    detail = {
        "method": METHOD_RV,
        "window": RV_WINDOW,
        "annualization": 252,
        "std": "sample",
        "underlying": RV_UNDERLYING_LABEL,
        "yahoo_ticker": TICKER_RV,
        "return": "close_to_close_log",
    }
    ordered = [float(c) for c in closes if c is not None and c == c and float(c) > 0]
    if len(ordered) < RV_WINDOW + 1:
        return {
            "value": None,
            "status": "INCOMPLETE",
            "reason": "fewer_than_21_closes",
            "observation_date": None,
            **detail,
            "observations": len(ordered),
        }
    window = ordered[-(RV_WINDOW + 1) :]
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
    if len(returns) != RV_WINDOW:
        return {
            "value": None,
            "status": "INCOMPLETE",
            "reason": "incomplete_return_window",
            "observation_date": None,
            **detail,
        }
    stdev = statistics.stdev(returns)  # sample (n-1)
    value = stdev * math.sqrt(252) * 100.0
    return {
        "value": value,
        "status": "OK",
        "reason": None,
        "observation_date": None,
        **detail,
        "observations": len(ordered),
    }


def realized_vol_20_from_series(rows: SeriesRows) -> dict[str, Any]:
    """RV20 from dated closes. Observation date is the last GSPC close in the 21-close window."""
    ordered = [(day, float(close)) for day, close in rows if close is not None and close == close and float(close) > 0]
    ordered.sort(key=lambda item: item[0])
    if len(ordered) < RV_WINDOW + 1:
        return realized_vol_20([c for _, c in ordered])
    window = ordered[-(RV_WINDOW + 1) :]
    result = realized_vol_20([close for _, close in window])
    result["observation_date"] = window[-1][0]
    result["window_start_date"] = window[0][0]
    result["observations"] = len(ordered)
    return result


def vix_minus_rv(
    vix: float | None,
    rv: Mapping[str, Any],
    *,
    vix_observation_date: date | None = None,
    rv_observation_date: date | None = None,
) -> dict[str, Any]:
    """VIX − RV20 only when both sides share the same observation date."""
    base = {
        "metric_id": "VIX_MINUS_GSPC_RV20",
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
    """Latest common session date with VIX close and an RV20 window ending that day."""
    vix_by = {day: float(level) for day, level in vix_rows if level is not None and float(level) > 0}
    common_dates = sorted(vix_by.keys(), reverse=True)
    gspc_ordered = sorted(
        ((day, float(close)) for day, close in gspc_rows if close is not None and float(close) > 0),
        key=lambda item: item[0],
    )
    for day in common_dates:
        gspc_upto = [(d, c) for d, c in gspc_ordered if d <= day]
        rv = realized_vol_20_from_series(gspc_upto)
        if rv.get("status") != "OK" or rv.get("observation_date") != day:
            continue
        return vix_minus_rv(vix_by[day], rv, vix_observation_date=day, rv_observation_date=day)
    rv_latest = realized_vol_20_from_series(gspc_ordered)
    vix_latest_date = common_dates[0] if common_dates else None
    return vix_minus_rv(
        vix_by.get(vix_latest_date) if vix_latest_date else None,
        rv_latest,
        vix_observation_date=vix_latest_date,
        rv_observation_date=rv_latest.get("observation_date"),
    )


def term_structure(series: Mapping[str, SeriesRows]) -> dict[str, Any]:
    """Build VIX index term structure from one common observation date.

    Front endpoint is 9D (^VIX9D); back endpoint is 1Y (^VIX1Y). Slope is published
    only when both endpoints exist on the chosen date. Intermediate gaps stay unavailable
    for that date — never filled from another session.
    """
    by_date: dict[date, dict[str, float]] = {}
    for ticker, _tenor, _metric_id in TERM_TENORS:
        for day, level in series.get(ticker) or ():
            if level is None or float(level) <= 0:
                continue
            by_date.setdefault(day, {})[ticker] = float(level)

    candidates = sorted(by_date.keys(), reverse=True)
    curve_date: date | None = None
    for day in candidates:
        levels = by_date[day]
        if FRONT_TICKER in levels and BACK_TICKER in levels:
            curve_date = day
            break
    if curve_date is None and candidates:
        # Partial curve: latest session with any tenor; slope stays incomplete without endpoints.
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
    slope = None if front is None or back is None else float(back) - float(front)
    if slope is None:
        state = None
        slope_reason = "missing_canonical_endpoints" if curve_date is not None else "no_tenor_observations"
    elif abs(slope) < FLAT_EPS:
        state = "flat"
        slope_reason = None
    elif slope > 0:
        state = "upward_sloping"
        slope_reason = None
    else:
        state = "downward_sloping"
        slope_reason = None

    return {
        "label": "VIX index term structure",
        "construction": METHOD_TERM,
        "observation_date": curve_date,
        "front_tenor": FRONT_TENOR,
        "back_tenor": BACK_TENOR,
        "points": points,
        "front_to_back_slope": slope,
        "curve_state": state,
        "slope_status": "OK" if slope is not None else "INCOMPLETE",
        "slope_reason": slope_reason,
        "unavailable_tenors": unavailable,
        "note": (
            "Index tenors from Yahoo closes on one common observation date. "
            "Front=9D, back=1Y. Not a VIX futures curve. Never labeled contango/backwardation."
        ),
    }


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
