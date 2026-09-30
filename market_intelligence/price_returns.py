"""Calendar price returns for dashboard stock and subsector tables.

The endpoint is the latest valid Yahoo price, including extended hours.
The reference is the last completed regular-session close on or before the
calendar lookback. Both prices are split-adjusted and are not dividend-adjusted.
A missing close, an invalid price, or a mixed adjustment basis is N/A.
"""

from __future__ import annotations

import calendar
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from market_intelligence.calendars import CAL_NYSE, NY_TZ, is_session, previous_session

PRICE_RETURN_SOURCE = "YAHOO_PRICE_DAILY"
PRICE_RETURN_BASIS = "SPLIT_ADJUSTED_PRICE"
PRICE_HORIZONS: tuple[str, ...] = ("1W", "1M", "3M", "6M", "1Y")
PRICE_RETURN_CAPTION = (
    "1W, 1M, 3M, 6M, and 1Y are price returns: the latest Yahoo price divided by the last "
    "regular-session close on or before a calendar lookback of 1 week, 1 month, 3 months, "
    "6 months, or 1 year, minus one. Cash dividends are excluded. A missing close is N/A."
)
BACKFILL_DAYS = 400
OVERLAP_DAYS = 10


def add_months(day: date, months: int) -> date:
    """Calendar month shift. Day 31 lands on the last day of the target month."""
    index = day.month - 1 + months
    year = day.year + index // 12
    month = index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def calendar_target(anchor: date, label: str) -> date:
    if label == "1W":
        return anchor - timedelta(days=7)
    months = {"1M": -1, "3M": -3, "6M": -6, "1Y": -12}[label]
    return add_months(anchor, months)


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value in (None, ""):
        return None
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def quote_anchor_date(quote_ts: datetime) -> date:
    """Trading date of the quote in America/New_York. A weekend print uses the prior session."""
    local = parse_timestamp(quote_ts)
    if local is None:
        raise ValueError("quote timestamp is required")
    day = local.astimezone(NY_TZ).date()
    if is_session(day, CAL_NYSE):
        return day
    return previous_session(day, CAL_NYSE)


def _positive(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _bar_day(row: Mapping[str, Any]) -> date | None:
    raw = row.get("bar_date")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def reference_close(bars: Sequence[Mapping[str, Any]], target: date) -> tuple[date, float] | None:
    """Last completed regular-session close on or before ``target``."""
    chosen: tuple[date, float] | None = None
    for row in bars:
        day = _bar_day(row)
        close = _positive(row.get("close"))
        if day is None or close is None or day > target:
            continue
        if str(row.get("quality") or "") == "PROVISIONAL":
            continue
        if str(row.get("basis") or "") != PRICE_RETURN_BASIS:
            continue
        if chosen is None or day > chosen[0]:
            chosen = (day, close)
    return chosen


def _span_consistent(bars: Sequence[Mapping[str, Any]], reference: date, anchor: date) -> bool:
    for row in bars:
        day = _bar_day(row)
        if day is None or day <= reference or day > anchor:
            continue
        if str(row.get("basis") or "") != PRICE_RETURN_BASIS:
            return False
    return True


def price_horizons(
    bars: Sequence[Mapping[str, Any]],
    endpoint: Any,
    anchor: date | None,
) -> dict[str, dict[str, Any]]:
    """Fractional price returns keyed by 1W, 1M, 3M, 6M, and 1Y."""
    price = _positive(endpoint)
    legs: dict[str, dict[str, Any]] = {}
    for label in PRICE_HORIZONS:
        target = calendar_target(anchor, label) if anchor is not None else None
        leg: dict[str, Any] = {"label": label, "value": None, "target": target, "reference": None, "reason": "N/A"}
        if price is None or anchor is None or target is None:
            leg["reason"] = "no price"
            legs[label] = leg
            continue
        ref = reference_close(bars, target)
        if ref is None:
            leg["reason"] = "no close on or before {0}".format(target.isoformat())
            legs[label] = leg
            continue
        if not _span_consistent(bars, ref[0], anchor):
            leg["reason"] = "adjustment mismatch"
            legs[label] = leg
            continue
        leg["value"] = price / ref[1] - 1.0
        leg["reference"] = ref[0]
        leg["reason"] = "close {0}".format(ref[0].isoformat())
        legs[label] = leg
    return legs


def commit_or_keep(existing: Mapping[date, Mapping[str, Any]], incoming: Sequence[Mapping[str, Any]] | None, *, failed: bool) -> dict[date, Mapping[str, Any]]:
    """A failed fetch leaves the stored bars untouched. A success upserts by session date."""
    kept = dict(existing)
    if failed or incoming is None:
        return kept
    for row in incoming:
        day = _bar_day(row)
        if day is None:
            continue
        kept[day] = row
    return kept


def plan_history_window(
    existing: Sequence[date],
    *,
    today: date,
    last_completed: date,
    session_open: bool,
    repair: bool,
) -> tuple[date, date, str] | None:
    """Return start, exclusive end, and kind. None means no history download this cycle.

    Gaps between the first and last stored session are fetched. Days before the
    first stored bar are left alone so an IPO is not backfilled before listing.
    """
    end = today + timedelta(days=1)
    floor = today - timedelta(days=BACKFILL_DAYS)
    dates = set(existing)
    if repair or not dates:
        return floor, end, "backfill"
    earliest = min(dates)
    latest = max(dates)
    holes: list[date] = []
    day = earliest
    limit = min(latest, last_completed)
    while day <= limit:
        if is_session(day, CAL_NYSE) and day not in dates:
            holes.append(day)
        day += timedelta(days=1)
    tail_missing = latest < last_completed
    if not holes and not tail_missing and not session_open:
        return None
    if holes:
        start = min(min(holes), latest) - timedelta(days=OVERLAP_DAYS)
        if (last_completed - start).days > 40:
            return max(floor, start), end, "repair"
        return max(floor, start), end, "incremental"
    start = latest - timedelta(days=OVERLAP_DAYS)
    return max(floor, start), end, "incremental"
