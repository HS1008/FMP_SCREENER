"""Versioned equal-dollar daily-rebalanced return indexes.

One index series is used for plots, returns, and RS. Multiplying one
constituent's entire price history by a constant must not change the index
return. This is a display-method decision and must not change research portfolios.

Do not port ``semi_rotation_engine._subgroup_equal_weight_raw`` (mean of raw
prices is price-weighted in return terms).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

BASKET_METHOD_VERSION = "equal_dollar_daily_rebalance_v1"


@dataclass(frozen=True)
class BasketPoint:
    as_of: date
    level: float
    ret_1d: float | None
    members_used: tuple[str, ...]
    members_missing: tuple[str, ...]
    coverage: float


def daily_rebalanced_equal_weight(
    prices: Mapping[str, Mapping[date, float]],
    *,
    start: date | None = None,
    end: date | None = None,
) -> list[BasketPoint]:
    """Equal-dollar (equal-weight return) index. Coverage is members with both t and t-1."""
    all_dates = sorted({d for series in prices.values() for d in series})
    if start is not None:
        all_dates = [d for d in all_dates if d >= start]
    if end is not None:
        all_dates = [d for d in all_dates if d <= end]
    if not all_dates:
        return []
    members = tuple(sorted(prices))
    out: list[BasketPoint] = []
    level = 1.0
    prev: date | None = None
    for idx, day in enumerate(all_dates):
        if idx == 0:
            out.append(BasketPoint(day, level, None, members, (), 0.0))
            prev = day
            continue
        used: list[str] = []
        missing: list[str] = []
        rets: list[float] = []
        for symbol in members:
            series = prices[symbol]
            px_t = series.get(day)
            px_p = series.get(prev) if prev is not None else None
            if px_t is None or px_p is None or px_p == 0:
                missing.append(symbol)
                continue
            rets.append(px_t / px_p - 1.0)
            used.append(symbol)
        if not rets:
            out.append(BasketPoint(day, level, None, (), tuple(missing), 0.0))
            prev = day
            continue
        ret = sum(rets) / len(rets)
        level *= 1.0 + ret
        coverage = len(used) / len(members) if members else 0.0
        out.append(BasketPoint(day, level, ret, tuple(used), tuple(missing), coverage))
        prev = day
    return out


def aligned_session_return(px_t: float | None, px_prev: float | None) -> float | None:
    """1D price return. Missing either session is null (never forward-filled). Zero is valid."""
    if px_t is None or px_prev is None or px_prev == 0:
        return None
    return px_t / px_prev - 1.0


def ratio_change_rs(
    asset_t: float | None,
    asset_prev: float | None,
    bench_t: float | None,
    bench_prev: float | None,
) -> float | None:
    """rs = (P[t]/B[t]) / (P[prev]/B[prev]) - 1. Aligned sessions only."""
    if None in (asset_t, asset_prev, bench_t, bench_prev):
        return None
    if asset_prev == 0 or bench_t == 0 or bench_prev == 0:
        return None
    return (asset_t / bench_t) / (asset_prev / bench_prev) - 1.0


__all__ = [
    "BASKET_METHOD_VERSION",
    "BasketPoint",
    "aligned_session_return",
    "daily_rebalanced_equal_weight",
    "ratio_change_rs",
]
