"""Source/series-specific publication freshness (versioned).

Compares a stored observation to the *expected latest published* observation,
not merely elapsed time since the economic reference date. Transport status is
never mixed into the observation timestamp: a successful fetch does not refresh
``observation_date``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Mapping

from market_intelligence.calendars import (
    CAL_NYSE,
    CAL_US_FEDERAL,
    CAL_US_TREASURY,
    NY_TZ,
    is_session,
    last_completed_session,
    next_session,
    previous_session,
    sessions_between,
)

FRESHNESS_POLICY_VERSION = "freshness_policy_v2"

LATEST_AVAILABLE = "LATEST_AVAILABLE"
AWAITING_RELEASE = "AWAITING_RELEASE"
INGESTION_OVERDUE = "INGESTION_OVERDUE"
STALE = "STALE"
MISSING = "MISSING"
INVALID_FUTURE = "INVALID_FUTURE"
TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
UNKNOWN = "UNKNOWN"
NEVER_ATTEMPTED = "NEVER_ATTEMPTED"

# Backward-compatible alias used by older chips/tests. Means latest available only.
FRESH = LATEST_AVAILABLE

# Cadence-only fallback when no series policy is registered (conservative).
CADENCE_TOLERANCE = {
    "D": {"sessions": 1, "calendar": CAL_US_TREASURY},
    "W": {"days": 10},
    "BW": {"days": 18},
    "M": {"days": 45},
    "Q": {"days": 110},
    "SA": {"days": 200},
    "A": {"days": 400},
    "MIXED": {"days": 45},
    "ON_DEMAND": {"days": None},
    "INTRADAY": {"hours": 18},
}


@dataclass(frozen=True)
class FreshnessPolicy:
    calendar: str
    cadence: str
    typical_release: time | None = None
    timezone: str = "America/New_York"
    publication_guaranteed: bool = False
    overdue_sessions: int = 1
    stale_sessions: int = 3
    reference_lag_days: int = 0
    same_day_available: bool = False
    notes: str = ""


def _daily_treasury() -> FreshnessPolicy:
    return FreshnessPolicy(
        calendar=CAL_US_TREASURY,
        cadence="D",
        typical_release=time(15, 30),
        publication_guaranteed=False,
        overdue_sessions=1,
        stale_sessions=3,
        same_day_available=True,
        notes="Treasury par yields: indicative quotations near 15:30 ET; not a guaranteed API publish time.",
    )


def _fred_macro_monthly(*, lag_days: int = 20) -> FreshnessPolicy:
    return FreshnessPolicy(
        calendar=CAL_US_FEDERAL,
        cadence="M",
        typical_release=time(8, 30),
        overdue_sessions=5,
        stale_sessions=45,
        reference_lag_days=lag_days,
        notes="Monthly reference period plus a later release. Lag is typical, not official.",
    )


def _fred_macro_quarterly() -> FreshnessPolicy:
    return FreshnessPolicy(
        calendar=CAL_US_FEDERAL,
        cadence="Q",
        typical_release=time(8, 30),
        overdue_sessions=10,
        stale_sessions=120,
        reference_lag_days=40,
        notes="Quarterly reference period plus a later vintage/release.",
    )


SERIES_POLICIES: dict[str, FreshnessPolicy] = {
    **{sid: _daily_treasury() for sid in (
        "DGS1MO", "DGS3MO", "DGS6MO", "DGS1", "DGS2", "DGS3", "DGS5", "DGS7", "DGS10", "DGS20", "DGS30",
        "DFII5", "DFII10", "DFII20", "DFII30",
        "UST_NOM_1M", "UST_NOM_1_5M", "UST_NOM_2M", "UST_NOM_3M", "UST_NOM_4M", "UST_NOM_6M",
        "UST_NOM_1Y", "UST_NOM_2Y", "UST_NOM_3Y", "UST_NOM_5Y", "UST_NOM_7Y", "UST_NOM_10Y",
        "UST_NOM_20Y", "UST_NOM_30Y",
        "UST_REAL_5Y", "UST_REAL_7Y", "UST_REAL_10Y", "UST_REAL_20Y", "UST_REAL_30Y",
        "DFF", "SOFR",
    )},
    **{sid: FreshnessPolicy(calendar=CAL_NYSE, cadence="D", typical_release=time(16, 0), overdue_sessions=1, stale_sessions=3, same_day_available=True, notes="US equity/ETF last completed session.") for sid in (
        "SPY", "XLK", "XLF", "XLE", "XLY", "XLP", "XLV", "XLI", "XLB", "XLU", "XLRE", "XLC", "SMH", "XSD",
        "equity_eod", "precomputed_sector_bundles",
    )},
    "PAYEMS": _fred_macro_monthly(lag_days=10),
    "UNRATE": _fred_macro_monthly(lag_days=10),
    "CPIAUCSL": _fred_macro_monthly(lag_days=18),
    "CPILFESL": _fred_macro_monthly(lag_days=18),
    "PCEPI": _fred_macro_monthly(lag_days=35),
    "PCEPILFE": _fred_macro_monthly(lag_days=35),
    "INDPRO": _fred_macro_monthly(lag_days=20),
    "RSAFS": _fred_macro_monthly(lag_days=18),
    "GDPC1": _fred_macro_quarterly(),
    "ICSA": FreshnessPolicy(calendar=CAL_US_FEDERAL, cadence="W", typical_release=time(8, 30), overdue_sessions=2, stale_sessions=14, notes="Weekly claims, typically Thursday."),
    "CCSA": FreshnessPolicy(calendar=CAL_US_FEDERAL, cadence="W", typical_release=time(8, 30), overdue_sessions=2, stale_sessions=14),
}

SOURCE_DEFAULT_CALENDAR = {
    "TREASURY": CAL_US_TREASURY,
    "FRED": CAL_US_TREASURY,
    "EQUITY_EOD": CAL_NYSE,
    "FMP_LEGACY": CAL_NYSE,
    "YAHOO": CAL_NYSE,
}


def policy_for(*, series_id: str | None = None, source_id: str | None = None, cadence: str | None = None) -> FreshnessPolicy | None:
    if series_id and series_id in SERIES_POLICIES:
        return SERIES_POLICIES[series_id]
    if source_id and source_id in SOURCE_DEFAULT_CALENDAR:
        cal = SOURCE_DEFAULT_CALENDAR[source_id]
        freq = str(cadence or "D").upper()
        if freq in {"D", "INTRADAY"}:
            return FreshnessPolicy(calendar=cal, cadence=freq, overdue_sessions=1, stale_sessions=3)
    return None


@dataclass(frozen=True)
class FreshnessAssessment:
    status: str
    tolerance_days: int | None
    age_days: int | None
    stale_after: date | None
    policy_version: str = FRESHNESS_POLICY_VERSION
    expected_latest: date | None = None
    transport_status: str | None = None
    reference_period: str | None = None
    release_time: str | None = None


def us_federal_holidays(year: int) -> set[date]:
    from market_intelligence.calendars import holiday_set

    return {d for d in holiday_set(CAL_US_FEDERAL, year) if d.year == year} | {
        d for d in holiday_set(CAL_US_FEDERAL, year + 1) if d.year == year
    } | {d for d in holiday_set(CAL_US_FEDERAL, year - 1) if d.year == year}


def is_business_day(d: date, calendar: str = CAL_US_FEDERAL) -> bool:
    return is_session(d, calendar)


def business_days_between(start: date, end: date, calendar: str = CAL_US_FEDERAL) -> int:
    return sessions_between(start, end, calendar)


def _add_months(year: int, month: int, delta: int) -> date:
    raw = month - 1 + delta
    return date(year + raw // 12, raw % 12 + 1, 1)


def expected_latest_published(
    *,
    policy: FreshnessPolicy,
    now: datetime,
) -> date:
    local = now.astimezone(NY_TZ) if now.tzinfo else now.replace(tzinfo=NY_TZ)
    today = local.date()
    if policy.cadence == "INTRADAY":
        return today if is_session(today, policy.calendar) else previous_session(today, policy.calendar)
    if policy.cadence == "D":
        close = policy.typical_release or time(16, 0)
        if policy.publication_guaranteed:
            return last_completed_session(local, policy.calendar, session_close=close)
        # Not a guaranteed publish time: after the typical window the previous
        # completed session is expected; before it, the session before that may
        # still be the latest *available* print.
        if policy.same_day_available and is_session(today, policy.calendar) and local.timetz().replace(tzinfo=None) >= close:
            return today
        return previous_session(today, policy.calendar)
    if policy.cadence == "W":
        return today - timedelta(days=min(today.weekday(), 6))
    if policy.cadence == "M":
        # Reference month M is typically released `lag` days into month M+1.
        # Walk back until that release date is on or before today.
        ref = _add_months(today.year, today.month, -1)
        lag = max(policy.reference_lag_days, 1)
        while True:
            release_on = _add_months(ref.year, ref.month, 1) + timedelta(days=lag - 1)
            if today >= release_on:
                return ref
            ref = _add_months(ref.year, ref.month, -1)
    if policy.cadence == "Q":
        q_month = ((today.month - 1) // 3) * 3 + 1
        ref = _add_months(today.year, q_month, -3)
        lag = max(policy.reference_lag_days, 1)
        while True:
            release_on = _add_months(ref.year, ref.month, 3) + timedelta(days=lag - 1)
            if today >= release_on:
                return ref
            ref = _add_months(ref.year, ref.month, -3)
    return previous_session(today, policy.calendar)


def assess_freshness(
    latest_observation: date | None,
    cadence: str | None = None,
    today: date | None = None,
    *,
    series_id: str | None = None,
    source_id: str | None = None,
    now: datetime | None = None,
    release_time: datetime | None = None,
    reference_period: str | None = None,
    transport_status: str | None = None,
) -> FreshnessAssessment:
    transport = (transport_status or "").upper() or None
    clock = now
    if clock is None:
        if today is not None:
            clock = datetime.combine(today, time(23, 59), tzinfo=NY_TZ)
        else:
            clock = datetime.now(NY_TZ)
    eval_day = clock.astimezone(NY_TZ).date() if clock.tzinfo else clock.date()

    if transport in {"FAILED", "TRANSPORT_FAILED"} and latest_observation is None:
        return FreshnessAssessment(TRANSPORT_FAILURE, None, None, None, transport_status=transport)

    if latest_observation is None:
        return FreshnessAssessment(MISSING if series_id or source_id else UNKNOWN, None, None, None, transport_status=transport)

    if latest_observation > eval_day:
        age = (eval_day - latest_observation).days
        return FreshnessAssessment(
            INVALID_FUTURE,
            None,
            age,
            None,
            expected_latest=eval_day,
            transport_status=transport,
            reference_period=reference_period,
            release_time=release_time.isoformat() if release_time else None,
        )

    policy = policy_for(series_id=series_id, source_id=source_id, cadence=cadence)
    age = (eval_day - latest_observation).days
    if policy is None:
        rule = CADENCE_TOLERANCE.get(str(cadence or "").upper())
        if rule is None:
            return FreshnessAssessment(UNKNOWN, None, age, None, transport_status=transport)
        if "sessions" in rule:
            cal = str(rule.get("calendar") or CAL_US_TREASURY)
            expected = last_completed_session(clock, cal, session_close=time(16, 0))
            gap = sessions_between(latest_observation, expected, cal)
            tol = int(rule["sessions"])
            if latest_observation > expected:
                status = INVALID_FUTURE
            elif latest_observation == expected:
                status = LATEST_AVAILABLE
            elif gap <= 0:
                status = AWAITING_RELEASE
            elif gap <= tol:
                status = INGESTION_OVERDUE
            else:
                status = STALE
            return FreshnessAssessment(status, tol, age, expected, expected_latest=expected, transport_status=transport)
        if rule.get("hours") is not None:
            expected = eval_day
            status = LATEST_AVAILABLE if age <= 0 else (INGESTION_OVERDUE if age == 1 else STALE)
            return FreshnessAssessment(status, 1, age, expected, expected_latest=expected, transport_status=transport)
        tol_days = rule.get("days")
        if tol_days is None:
            return FreshnessAssessment(UNKNOWN, None, age, None, transport_status=transport)
        status = LATEST_AVAILABLE if age <= int(tol_days) else STALE
        return FreshnessAssessment(status, int(tol_days), age, latest_observation + timedelta(days=int(tol_days)), expected_latest=None, transport_status=transport)

    expected = expected_latest_published(policy=policy, now=clock)
    if latest_observation > expected and latest_observation <= eval_day:
        # Observation is newer than the conservative expected print (API appeared early).
        status = LATEST_AVAILABLE
        gap = 0
    elif latest_observation == expected:
        status = LATEST_AVAILABLE
        gap = 0
    else:
        gap = sessions_between(latest_observation, expected, policy.calendar) if policy.cadence in {"D", "INTRADAY"} else (expected - latest_observation).days
        if gap <= 0:
            status = AWAITING_RELEASE
        elif gap <= policy.overdue_sessions:
            status = INGESTION_OVERDUE
        elif gap <= policy.stale_sessions:
            status = STALE
        else:
            status = STALE
    if transport in {"FAILED", "TRANSPORT_FAILED"} and status != LATEST_AVAILABLE:
        # Keep observation freshness; callers also see transport separately.
        pass
    return FreshnessAssessment(
        status,
        policy.overdue_sessions,
        age,
        expected,
        expected_latest=expected,
        transport_status=transport,
        reference_period=reference_period,
        release_time=release_time.isoformat() if release_time else None,
    )


def is_current_status(status: str | None) -> bool:
    return str(status or "").upper() in {LATEST_AVAILABLE, FRESH, "CURRENT", "OK", AWAITING_RELEASE}


__all__ = [
    "AWAITING_RELEASE",
    "CADENCE_TOLERANCE",
    "FRESH",
    "FRESHNESS_POLICY_VERSION",
    "FreshnessAssessment",
    "FreshnessPolicy",
    "INGESTION_OVERDUE",
    "INVALID_FUTURE",
    "LATEST_AVAILABLE",
    "MISSING",
    "NEVER_ATTEMPTED",
    "SERIES_POLICIES",
    "STALE",
    "TRANSPORT_FAILURE",
    "UNKNOWN",
    "assess_freshness",
    "business_days_between",
    "expected_latest_published",
    "is_business_day",
    "is_current_status",
    "policy_for",
    "us_federal_holidays",
]
