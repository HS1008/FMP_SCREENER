"""Calendar price returns for dashboard stock and subsector tables.

1W-1Y are completed close-to-close returns: the endpoint is the latest
completed regular-session close and the reference is the last completed close
on or before the calendar lookback from that endpoint. The live quote is never
an endpoint here; it only drives the 1D column (see ``return_policy``). Both
prices are split-adjusted and are not dividend-adjusted. A missing close, an
in-progress bar, or a mixed adjustment basis is N/A.
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
    "1W, 1M, 3M, 6M, and 1Y are completed close-to-close price returns: the latest completed "
    "regular-session close divided by the last completed close on or before a calendar lookback of "
    "1 week, 1 month, 3 months, 6 months, or 1 year from that close, minus one. The in-progress "
    "session and the live quote are not endpoints. Cash dividends are excluded. A missing close is N/A."
)
# Stored coverage target: at least three calendar years so the stock dialog's
# 3Y range has a reference close on or before its start. Extra days cover the
# first-session lookup before the 3Y anchor.
BACKFILL_YEARS = 3
BACKFILL_DAYS = BACKFILL_YEARS * 365 + 15
OVERLAP_DAYS = 10
# A series whose first stored bar sits this far after the floor is extended
# backwards once. Newly listed symbols never acquire older bars, so the attempt
# is remembered in the collector's progress state rather than repeated.
EXTEND_SLACK_DAYS = 30


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


def latest_completed_close(bars: Sequence[Mapping[str, Any]], *, not_after: date | None = None) -> tuple[date, float] | None:
    """Newest COMPLETE close on the price-return basis. Provisional (in-progress) bars are skipped."""
    chosen: tuple[date, float] | None = None
    for row in bars:
        day = _bar_day(row)
        close = _positive(row.get("close"))
        if day is None or close is None:
            continue
        if not_after is not None and day > not_after:
            continue
        if str(row.get("quality") or "") == "PROVISIONAL":
            continue
        if str(row.get("basis") or "") != PRICE_RETURN_BASIS:
            continue
        if chosen is None or day > chosen[0]:
            chosen = (day, close)
    return chosen


def completed_price_horizons(
    bars: Sequence[Mapping[str, Any]],
    *,
    not_after: date | None = None,
) -> dict[str, dict[str, Any]]:
    """1W-1Y returns as latest completed close / historical reference close - 1.

    The endpoint is the newest completed daily close (never the live quote and
    never an in-progress bar), so a quote update alone does not move these
    columns. Each leg also reports ``endpoint`` (the close date used).
    """
    latest = latest_completed_close(bars, not_after=not_after)
    if latest is None:
        legs = horizons_from_endpoint(bars, None, None)
        for leg in legs.values():
            leg["reason"] = "no completed close"
            leg["endpoint"] = None
        return legs
    legs = horizons_from_endpoint(bars, latest[1], latest[0])
    for leg in legs.values():
        leg["endpoint"] = latest[0]
        leg["endpoint_close"] = latest[1]
    return legs


def horizons_from_endpoint(
    bars: Sequence[Mapping[str, Any]],
    endpoint: Any,
    anchor: date | None,
) -> dict[str, dict[str, Any]]:
    """Fractional price returns keyed by 1W, 1M, 3M, 6M, and 1Y from one endpoint close.

    ``endpoint`` must be a completed close dated ``anchor``; dashboards call
    :func:`completed_price_horizons`, which picks that pair from the stored bars.
    """
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


def history_floor(today: date) -> date:
    return today - timedelta(days=BACKFILL_DAYS)


def needs_extension(earliest: date | None, *, today: date, coverage_floor: date | None) -> bool:
    """True when stored history starts well after the floor and no extension was recorded.

    ``coverage_floor`` is the floor the collector already requested for this
    symbol. Once an extension down to (or past) today's floor has been tried,
    a series that still starts later is a newly listed instrument and is left
    alone; it is never zero-filled or backfilled before its listing.
    """
    if earliest is None:
        return False
    floor = history_floor(today)
    if earliest <= floor + timedelta(days=EXTEND_SLACK_DAYS):
        return False
    return coverage_floor is None or coverage_floor > floor + timedelta(days=EXTEND_SLACK_DAYS)


def plan_history_window(
    existing: Sequence[date],
    *,
    today: date,
    last_completed: date,
    session_open: bool,
    repair: bool,
    coverage_floor: date | None = None,
) -> tuple[date, date, str] | None:
    """Return start, exclusive end, and kind. None means no history download this cycle.

    Gaps between the first and last stored session are fetched. A series that
    starts well after the three-year floor is extended backwards once
    (``"extend"``); afterwards days before the first stored bar are left alone
    so an IPO is not backfilled before listing.
    """
    end = today + timedelta(days=1)
    floor = history_floor(today)
    dates = set(existing)
    if repair or not dates:
        return floor, end, "backfill"
    earliest = min(dates)
    latest = max(dates)
    if needs_extension(earliest, today=today, coverage_floor=coverage_floor):
        return floor, min(end, earliest + timedelta(days=OVERLAP_DAYS)), "extend"
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
