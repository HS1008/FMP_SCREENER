"""Yahoo-backed volatility analytics. No network I/O.

Constructions:
- VIX index term structure uses Yahoo closes for ^VIX9D / ^VIX / ^VIX3M / ^VIX6M / ^VIX1Y.
  It is not a VIX futures curve and is never labeled contango or backwardation.
- SKEW is the Cboe SKEW Index from Yahoo ticker ^SKEW (not a 25-delta SPX skew).
- RV20 is sample stdev of 20 daily log returns times sqrt(252) times 100 (vol points).
- Implied vs realized uses VIX minus that RV20 for the chosen equity index/ETF.
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

RV_WINDOW = 20
FLAT_EPS = 0.05


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
        return {"value": None, "status": "INCOMPLETE", "reason": "fewer_than_21_closes", **detail, "observations": len(ordered)}
    window = ordered[-(RV_WINDOW + 1) :]
    returns: list[float] = []
    for i in range(1, len(window)):
        prev, cur = window[i - 1], window[i]
        if prev <= 0 or cur <= 0:
            return {"value": None, "status": "INCOMPLETE", "reason": "non_positive_close", **detail}
        returns.append(math.log(cur / prev))
    if len(returns) != RV_WINDOW:
        return {"value": None, "status": "INCOMPLETE", "reason": "incomplete_return_window", **detail}
    stdev = statistics.stdev(returns)  # sample (n-1)
    value = stdev * math.sqrt(252) * 100.0
    return {"value": value, "status": "OK", "reason": None, **detail, "observations": len(ordered)}


def vix_minus_rv(vix: float | None, rv: Mapping[str, Any]) -> dict[str, Any]:
    if rv.get("status") != "OK" or rv.get("value") is None:
        return {
            "metric_id": "VIX_MINUS_GSPC_RV20",
            "value": None,
            "vix": vix,
            "rv": rv.get("value"),
            "status": "INCOMPLETE",
            "reason": rv.get("reason") or "missing_rv",
            "method": METHOD_SPREAD,
            "underlying": RV_UNDERLYING_LABEL,
        }
    if vix is None:
        return {
            "metric_id": "VIX_MINUS_GSPC_RV20",
            "value": None,
            "vix": None,
            "rv": rv.get("value"),
            "status": "INCOMPLETE",
            "reason": "missing_vix",
            "method": METHOD_SPREAD,
            "underlying": RV_UNDERLYING_LABEL,
        }
    return {
        "metric_id": "VIX_MINUS_GSPC_RV20",
        "value": float(vix) - float(rv["value"]),
        "vix": float(vix),
        "rv": float(rv["value"]),
        "status": "OK",
        "reason": None,
        "method": METHOD_SPREAD,
        "underlying": RV_UNDERLYING_LABEL,
        "window": RV_WINDOW,
    }


def term_structure(levels: Mapping[str, float | None]) -> dict[str, Any]:
    points = []
    for ticker, tenor, metric_id in TERM_TENORS:
        level = levels.get(ticker)
        points.append({"ticker": ticker, "tenor": tenor, "metric_id": metric_id, "level": level})
    available = [p for p in points if p["level"] is not None]
    front = available[0]["level"] if available else None
    back = available[-1]["level"] if available else None
    slope = None if front is None or back is None else float(back) - float(front)
    if slope is None:
        state = None
    elif abs(slope) < FLAT_EPS:
        state = "flat"
    elif slope > 0:
        state = "upward_sloping"
    else:
        state = "downward_sloping"
    return {
        "label": "VIX index term structure",
        "construction": METHOD_TERM,
        "points": points,
        "front_to_back_slope": slope,
        "curve_state": state,
        "note": "Index tenors from Yahoo closes. Not a VIX futures curve. Never labeled contango/backwardation.",
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
