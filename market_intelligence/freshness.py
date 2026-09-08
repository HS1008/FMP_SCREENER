"""Release-cadence-aware freshness. Transport status and observation staleness are separate.

Tolerances are *release-lag* tolerances (versioned): how old the latest observation may be
before the dataset is STALE given normal publication lags. They are not investment
thresholds. Daily series use US business days (weekends + observed federal holidays).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

FRESHNESS_POLICY_VERSION = "freshness_policy_v1"

# Calendar-day tolerance for non-daily cadences; daily uses business days.
CADENCE_TOLERANCE = {
    "D": {"business_days": 4},
    "W": {"days": 12},
    "BW": {"days": 21},
    "M": {"days": 75},
    "Q": {"days": 215},
    "SA": {"days": 400},
    "A": {"days": 500},
    "MIXED": {"days": 75},
    "ON_DEMAND": {"days": None},
    "INTRADAY": {"days": 1},
}

FRESH = "FRESH"
STALE = "STALE"
UNKNOWN = "UNKNOWN"
NEVER_ATTEMPTED = "NEVER_ATTEMPTED"


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


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def us_federal_holidays(year: int) -> set[date]:
    """Observed US federal holidays (the release calendar FRED daily series follow)."""
    return {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),   # MLK
        _nth_weekday(year, 2, 0, 3),   # Presidents
        _last_weekday(year, 5, 0),     # Memorial
        _observed(date(year, 6, 19)),  # Juneteenth
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),   # Labor
        _nth_weekday(year, 10, 0, 2),  # Columbus
        _observed(date(year, 11, 11)),
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),
    }


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in us_federal_holidays(d.year)


def business_days_between(start: date, end: date) -> int:
    """Business days strictly after ``start`` up to and including ``end`` (0 if end <= start)."""
    if end <= start:
        return 0
    count = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if is_business_day(cur):
            count += 1
        cur += timedelta(days=1)
    return count


@dataclass(frozen=True)
class FreshnessAssessment:
    status: str
    tolerance_days: int | None
    age_days: int | None
    stale_after: date | None  # tolerance bound implied by the policy; NOT an official release date
    policy_version: str = FRESHNESS_POLICY_VERSION


def assess_freshness(latest_observation: date | None, cadence: str | None, today: date) -> FreshnessAssessment:
    if latest_observation is None:
        return FreshnessAssessment(UNKNOWN, None, None, None)
    rule = CADENCE_TOLERANCE.get(str(cadence or "").upper())
    if rule is None:
        return FreshnessAssessment(UNKNOWN, None, (today - latest_observation).days, None)
    age = (today - latest_observation).days
    if "business_days" in rule:
        bdays = business_days_between(latest_observation, today)
        tol = int(rule["business_days"])
        status = FRESH if bdays <= tol else STALE
        bound = latest_observation
        for _ in range(tol):
            bound += timedelta(days=1)
            while not is_business_day(bound):
                bound += timedelta(days=1)
        return FreshnessAssessment(status, tol, age, bound)
    tol_days = rule.get("days")
    if tol_days is None:
        return FreshnessAssessment(UNKNOWN, None, age, None)
    status = FRESH if age <= int(tol_days) else STALE
    return FreshnessAssessment(status, int(tol_days), age, latest_observation + timedelta(days=int(tol_days)))


__all__ = [
    "CADENCE_TOLERANCE",
    "FRESH",
    "FRESHNESS_POLICY_VERSION",
    "FreshnessAssessment",
    "NEVER_ATTEMPTED",
    "STALE",
    "UNKNOWN",
    "assess_freshness",
    "business_days_between",
    "is_business_day",
    "us_federal_holidays",
]
