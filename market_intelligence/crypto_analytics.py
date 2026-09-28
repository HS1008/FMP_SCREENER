"""BTC and ETH calendar-day analytics.

Yahoo crypto daily bars are labeled in UTC. The calendar date is the UTC date
of the provider timestamp so a local timezone cannot move the day. Weekends
are kept. Windows are exact calendar-day offsets (1, 7, 30, 90, 365), not
equity sessions and not a 5-session week.

The 52-week drawdown is the latest close versus the maximum close on
observations from 365 calendar days earlier through that date. It is missing
until an observation exists on or before the start of that window. The result
is never positive.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from market_intelligence.markets_analytics import _finite


CRYPTO_WINDOWS = (
    ("1D", 1),
    ("7D", 7),
    ("1M", 30),
    ("3M", 90),
    ("1Y", 365),
)
DRAWDOWN_DAYS = 365

CRYPTO_METHODOLOGY = (
    "Bitcoin and Ethereum use Yahoo BTC-USD and ETH-USD daily closes. "
    "The observation date is the provider timestamp converted to UTC before the calendar day is taken. "
    "Weekend observations are retained. "
    "Performance windows are exact calendar offsets: 1 day, 7 days, 30 days, 90 days, and 365 days. "
    "A window is missing when that earlier calendar date has no stored close. "
    "52-week drawdown is the close divided by the trailing 365-calendar-day maximum, minus one. "
    "It uses no 252-session equity window. It is never positive. "
    "BTC/ETH rising means Bitcoin outperformed Ethereum."
)


def utc_observation_date(value: datetime | date) -> date:
    """Calendar date in UTC. Naive datetimes are treated as already UTC."""
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).date()
    return value


def calendar_return(points: Sequence[tuple[date, float]], *, days: int) -> float | None:
    if days < 1 or not points:
        return None
    end_day, end_value = points[-1]
    past = dict(points).get(end_day - timedelta(days=days))
    if past is None or past == 0 or end_value is None or end_value <= 0:
        return None
    return end_value / past - 1.0


def window_returns(points: Sequence[tuple[date, float]]) -> dict[str, float | None]:
    return {label: calendar_return(points, days=days) for label, days in CRYPTO_WINDOWS}


def drawdown_series(points: Sequence[tuple[date, float]], *, days: int = DRAWDOWN_DAYS) -> list[tuple[date, float]]:
    """Trailing calendar drawdown. Missing until the window has a starting observation."""
    if days < 1:
        return []
    ordered = sorted((day, value) for day, value in points if value is not None and value > 0)
    out: list[tuple[date, float]] = []
    for index, (day, value) in enumerate(ordered):
        start = day - timedelta(days=days)
        if not any(prior <= start for prior, _value in ordered[: index + 1]):
            continue
        window = [level for prior, level in ordered[: index + 1] if start <= prior <= day]
        if not window:
            continue
        peak = max(window)
        if peak <= 0:
            continue
        drawdown = min(0.0, value / peak - 1.0)
        out.append((day, drawdown))
    return out


def latest_drawdown(points: Sequence[tuple[date, float]], *, days: int = DRAWDOWN_DAYS) -> float | None:
    series = drawdown_series(points, days=days)
    if not series:
        return None
    return series[-1][1]


def same_date_ratio(left: Sequence[tuple[date, float]], right: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    right_map = {day: value for day, value in right if value}
    ratios: list[tuple[date, float]] = []
    for day, value in left:
        other = right_map.get(day)
        if value is None or other is None or other == 0:
            continue
        ratios.append((day, value / other))
    return ratios
