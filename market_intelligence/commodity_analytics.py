"""Commodity futures-proxy returns and same-date metal ratios.

Windows count valid provider trading-day observations on that Yahoo series
(1, 5, 21, 63, 126, 252). They are not crypto calendar days.

Gold/copper uses the two closes on the same date. Neither leg is carried
forward. The ratio is a market-price ratio of Yahoo futures proxies. It is
not a physical-unit valuation and it is not a recession, growth, or risk-off
signal. These series are not an official futures roll.
"""

from __future__ import annotations

from datetime import date
from typing import Sequence

from market_intelligence.fx_analytics import observation_return
from market_intelligence.markets_analytics import _finite, as_day


COMMODITY_WINDOWS = (
    ("1D", 1),
    ("1W", 5),
    ("1M", 21),
    ("3M", 63),
    ("6M", 126),
    ("1Y", 252),
)

COMMODITY_METHODOLOGY = (
    "Commodity prices are Yahoo futures proxies (CL=F, BZ=F, NG=F, GC=F, SI=F, HG=F, ZC=F, ZW=F, ZS=F). "
    "They are provider-maintained front-month style series for market monitoring. "
    "They are not an official continuous settlement history and not a backtest-grade roll. "
    "Returns count provider trading-day observations (1, 5, 21, 63, 126, 252). "
    "FRED DCOILWTICO and DHHNGSP are official spot references and are not spliced into the futures proxies. "
    "FRED PCOPPUSDM is a monthly global copper price and is not spliced into HG=F. "
    "Gold/copper is gold proxy close divided by copper proxy close on the same date. "
    "Missing dates stay missing."
)


def close_points(rows: Sequence[object]) -> list[tuple[date, float]]:
    out: list[tuple[date, float]] = []
    for row in rows:
        if not isinstance(row, dict) and not hasattr(row, "get"):
            continue
        day = as_day(row.get("bar_date") if "bar_date" in row else row.get("as_of"))
        close = _finite(row.get("close") if "close" in row else row.get("value"))
        if day is None or close is None or close <= 0:
            continue
        out.append((day, close))
    out.sort(key=lambda item: item[0])
    deduped: list[tuple[date, float]] = []
    for day, close in out:
        if deduped and deduped[-1][0] == day:
            deduped[-1] = (day, close)
        else:
            deduped.append((day, close))
    return deduped


def window_returns(points: Sequence[tuple[date, float]]) -> dict[str, float | None]:
    return {label: observation_return(points, lag) for label, lag in COMMODITY_WINDOWS}


def same_date_ratio(
    left: Sequence[tuple[date, float]],
    right: Sequence[tuple[date, float]],
) -> list[tuple[date, float]]:
    """``left / right`` on dates present in both. No nearest-date join."""
    right_map = {day: value for day, value in right if value}
    ratios: list[tuple[date, float]] = []
    for day, value in left:
        other = right_map.get(day)
        if other is None or other == 0 or value is None:
            continue
        ratios.append((day, value / other))
    return ratios
