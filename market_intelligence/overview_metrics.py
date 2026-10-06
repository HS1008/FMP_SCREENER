"""Shared window statistics for the Market Overview and the full pages that feed it.

Every number here is derived from stored closes. Missing observations stay
missing: nothing is filled, carried forward, or set to zero.

* ``observation_returns`` counts valid provider observations (the FX/commodity
  convention: 1, 5, 21, 63, 126, 252) and matches ``fx_analytics.observation_return``.
* ``risk_window_stats`` is the template's "Volatility Annualized 3M / Sharpe 3M /
  Max DD 3M" block: ``window`` simple daily returns ending at the last
  observation, annualized with ``periods_per_year``. Sharpe uses a zero risk-free
  rate. Max drawdown is the worst close-to-close decline from a running peak
  inside the window. Fewer than ``window + 1`` closes is ``None`` for all three.
* ``percent_change_of_level`` turns a basis-point move into the fractional
  change of the underlying level (the template's "%Change" for yields).
"""

from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Any, Mapping, Sequence

from market_intelligence.fx_analytics import observation_return
from market_intelligence.markets_analytics import _finite

RISK_WINDOW_SESSIONS = 63
SESSIONS_PER_YEAR = 252
CRYPTO_DAYS_PER_YEAR = 365
RISK_METHODOLOGY = (
    "Volatility Annualized 3M is the sample standard deviation of the last 63 daily simple returns "
    "times the square root of 252 (365 for crypto calendar days). Sharpe 3M is the mean of those "
    "daily returns divided by that standard deviation, annualized the same way, with a zero risk-free "
    "rate. Max DD 3M is the worst close-to-close decline from the running peak inside those 63 "
    "observations. Fewer than 64 stored closes leaves all three blank."
)


def observation_returns(points: Sequence[tuple[date, float]], windows: Sequence[tuple[str, int]]) -> dict[str, float | None]:
    """Fractional return for each ``(label, lag)`` window over valid observations."""
    return {label: observation_return(points, lag) for label, lag in windows}


def risk_window_stats(
    points: Sequence[tuple[date, float]],
    *,
    window: int = RISK_WINDOW_SESSIONS,
    periods_per_year: int = SESSIONS_PER_YEAR,
) -> dict[str, float | None]:
    """Annualized volatility, annualized Sharpe (rf = 0), and max drawdown over ``window`` returns."""
    empty = {"vol_ann": None, "sharpe": None, "max_dd": None}
    clean = [(day, value) for day, value in points if _finite(value) is not None and float(value) > 0]
    if window < 2 or len(clean) < window + 1:
        return empty
    closes = [float(value) for _day, value in clean[-(window + 1) :]]
    returns = [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes))]
    if len(returns) < 2:
        return empty
    stdev = statistics.stdev(returns)
    mean = statistics.fmean(returns)
    scale = math.sqrt(float(periods_per_year))
    vol_ann = stdev * scale
    sharpe = None if stdev == 0 else mean / stdev * scale
    peak = closes[0]
    max_dd = 0.0
    for close in closes:
        if close > peak:
            peak = close
        if peak > 0:
            max_dd = min(max_dd, close / peak - 1.0)
    return {"vol_ann": vol_ann, "sharpe": sharpe, "max_dd": max_dd}


def percent_change_of_level(level: Any, change_bps: Any) -> float | None:
    """Fractional change of a percent-denominated level given its basis-point move.

    ``level`` is the current value in percent (4.10), ``change_bps`` the stored
    move in basis points (+12). Prior level is ``level - change_bps / 100``; a
    zero or missing prior stays ``None``.
    """
    current = _finite(level)
    move = _finite(change_bps)
    if current is None or move is None:
        return None
    prior = current - move / 100.0
    if prior == 0:
        return None
    return (current - prior) / abs(prior)


def level_difference(points: Sequence[tuple[date, float]], lag: int, *, scale: float = 1.0) -> float | None:
    """Difference (not ratio) between the last observation and ``lag`` observations earlier."""
    if lag < 1 or len(points) <= lag:
        return None
    past = _finite(points[-1 - lag][1])
    current = _finite(points[-1][1])
    if past is None or current is None:
        return None
    return (current - past) * scale


def points_from_rows(rows: Sequence[Mapping[str, Any]], *, date_key: str, value_key: str) -> list[tuple[date, float]]:
    """Ascending ``(date, value)`` pairs with missing or non-finite values dropped."""
    out: list[tuple[date, float]] = []
    for row in rows:
        raw_day = row.get(date_key)
        value = _finite(row.get(value_key))
        if raw_day is None or value is None:
            continue
        if isinstance(raw_day, date):
            day = raw_day
        else:
            try:
                day = date.fromisoformat(str(raw_day)[:10])
            except ValueError:
                continue
        out.append((day, value))
    out.sort(key=lambda item: item[0])
    return out


__all__ = [
    "CRYPTO_DAYS_PER_YEAR",
    "RISK_METHODOLOGY",
    "RISK_WINDOW_SESSIONS",
    "SESSIONS_PER_YEAR",
    "level_difference",
    "observation_returns",
    "percent_change_of_level",
    "points_from_rows",
    "risk_window_stats",
]
