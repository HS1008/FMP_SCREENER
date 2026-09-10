"""Exchange and publication calendars with historical applicability.

Federal holidays are not a universal market calendar. NYSE is open on Columbus Day
and Veterans Day; it is closed on Good Friday. Juneteenth is federal from 2021 and
an NYSE holiday from 2022. Observed dates can fall in the preceding year
(New Year 2022 observed Friday 2021-12-31).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")

CAL_US_FEDERAL = "US_FEDERAL"
CAL_NYSE = "NYSE"
CAL_US_TREASURY = "US_TREASURY"


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def observed_weekday(d: date) -> date:
    """Saturday -> Friday; Sunday -> Monday. Adjacent-year dates are kept."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def good_friday(year: int) -> date:
    return easter_sunday(year) - timedelta(days=2)


def _federal_named(year: int) -> dict[str, date]:
    named = {
        "new_year": observed_weekday(date(year, 1, 1)),
        "mlk": _nth_weekday(year, 1, 0, 3),
        "presidents": _nth_weekday(year, 2, 0, 3),
        "memorial": _last_weekday(year, 5, 0),
        "independence": observed_weekday(date(year, 7, 4)),
        "labor": _nth_weekday(year, 9, 0, 1),
        "columbus": _nth_weekday(year, 10, 0, 2),
        "veterans": observed_weekday(date(year, 11, 11)),
        "thanksgiving": _nth_weekday(year, 11, 3, 4),
        "christmas": observed_weekday(date(year, 12, 25)),
    }
    if year >= 2021:
        named["juneteenth"] = observed_weekday(date(year, 6, 19))
    return named


def holiday_set(calendar: str, year: int) -> set[date]:
    """Holidays whose *observed* date may land in ``year`` or an adjacent year."""
    cal = (calendar or CAL_US_FEDERAL).upper()
    out: set[date] = set()
    for y in (year - 1, year, year + 1):
        named = _federal_named(y)
        if cal == CAL_NYSE:
            keep = {
                "new_year",
                "mlk",
                "presidents",
                "memorial",
                "independence",
                "labor",
                "thanksgiving",
                "christmas",
            }
            if y >= 2022:
                keep.add("juneteenth")
            out.update(named[k] for k in keep if k in named)
            out.add(good_friday(y))
        elif cal == CAL_US_TREASURY:
            keep = {
                "new_year",
                "mlk",
                "presidents",
                "memorial",
                "independence",
                "labor",
                "columbus",
                "veterans",
                "thanksgiving",
                "christmas",
            }
            if y >= 2021:
                keep.add("juneteenth")
            out.update(named[k] for k in keep if k in named)
            out.add(good_friday(y))
        else:
            out.update(named.values())
    return {d for d in out if d.year == year}


def is_session(d: date, calendar: str = CAL_NYSE) -> bool:
    if d.weekday() >= 5:
        return False
    holidays = holiday_set(calendar, d.year) | holiday_set(calendar, d.year - 1) | holiday_set(calendar, d.year + 1)
    return d not in holidays


def previous_session(d: date, calendar: str = CAL_NYSE) -> date:
    cur = d - timedelta(days=1)
    while not is_session(cur, calendar):
        cur -= timedelta(days=1)
    return cur


def next_session(d: date, calendar: str = CAL_NYSE) -> date:
    cur = d + timedelta(days=1)
    while not is_session(cur, calendar):
        cur += timedelta(days=1)
    return cur


def last_completed_session(now: datetime, calendar: str = CAL_NYSE, *, session_close: time = time(16, 0)) -> date:
    """Last session whose regular close has passed in America/New_York.

    Early-close days still produce a completed daily bar after the early close;
    this helper uses the regular close unless a caller passes a different time.
    """
    local = now.astimezone(NY_TZ) if now.tzinfo else now.replace(tzinfo=NY_TZ)
    candidate = local.date()
    if is_session(candidate, calendar) and local.timetz().replace(tzinfo=None) >= session_close:
        return candidate
    return previous_session(candidate, calendar)


def sessions_between(start: date, end: date, calendar: str = CAL_NYSE) -> int:
    """Sessions strictly after ``start`` up to and including ``end`` (0 if end <= start)."""
    if end <= start:
        return 0
    count = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if is_session(cur, calendar):
            count += 1
        cur += timedelta(days=1)
    return count


__all__ = [
    "CAL_NYSE",
    "CAL_US_FEDERAL",
    "CAL_US_TREASURY",
    "NY_TZ",
    "easter_sunday",
    "good_friday",
    "holiday_set",
    "is_session",
    "last_completed_session",
    "next_session",
    "observed_weekday",
    "previous_session",
    "sessions_between",
]
