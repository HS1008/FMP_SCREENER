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
CURRENT_TO_SOURCE = "CURRENT_TO_SOURCE"
STALE_INGESTION = "STALE_INGESTION"
STALE_UPSTREAM = "STALE_UPSTREAM"
ON_DEMAND = "ON_DEMAND"
MISSING = "MISSING"
INVALID_FUTURE = "INVALID_FUTURE"
TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
UNKNOWN = "UNKNOWN"
NEVER_ATTEMPTED = "NEVER_ATTEMPTED"
RECENT_SUCCESS_HOURS = 36

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
    week_ending: str | None = None
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
    "ICSA": FreshnessPolicy(calendar=CAL_US_FEDERAL, cadence="W", typical_release=time(8, 30), overdue_sessions=8, stale_sessions=21, week_ending="SAT", notes="Week-ending Saturday; typically published the following Thursday 8:30 ET."),
    "CCSA": FreshnessPolicy(calendar=CAL_US_FEDERAL, cadence="W", typical_release=time(8, 30), overdue_sessions=8, stale_sessions=21, week_ending="SAT", notes="Week-ending Saturday; typically published the following Thursday 8:30 ET."),
    "WALCL": FreshnessPolicy(calendar=CAL_US_FEDERAL, cadence="W", typical_release=time(16, 30), overdue_sessions=8, stale_sessions=21, week_ending="WED", notes="H.4.1 Wednesday level; typically published Thursday."),
    "WTREGEN": FreshnessPolicy(calendar=CAL_US_FEDERAL, cadence="W", typical_release=time(16, 30), overdue_sessions=8, stale_sessions=21, week_ending="WED", notes="H.4.1 week average ending Wednesday."),
    "WRESBAL": FreshnessPolicy(calendar=CAL_US_FEDERAL, cadence="W", typical_release=time(16, 30), overdue_sessions=8, stale_sessions=21, week_ending="WED", notes="H.4.1 week average ending Wednesday."),
    "M2SL": _fred_macro_monthly(lag_days=32),
    **{sid: _daily_treasury() for sid in ("T5YIE", "T10YIE", "T5YIFR", "RRPONTSYD")},
    **{sid: FreshnessPolicy(calendar=CAL_US_TREASURY, cadence="D", typical_release=time(16, 0), overdue_sessions=2, stale_sessions=6, same_day_available=True, notes="ICE BofA OAS via FRED; often lags the cash session.") for sid in (
        "BAMLC0A0CM", "BAMLH0A0HYM2", "BAMLC0A1CAAA", "BAMLC0A2CAA", "BAMLC0A3CA", "BAMLC0A4CBBB",
        "BAMLH0A1HYBB", "BAMLH0A2HYB", "BAMLH0A3HYC",
    )},
    "DCOILWTICO": FreshnessPolicy(calendar=CAL_US_FEDERAL, cadence="D", overdue_sessions=2, stale_sessions=6, notes="EIA WTI spot via FRED. Weekends and holidays are missing, not zero."),
    "DHHNGSP": FreshnessPolicy(calendar=CAL_US_FEDERAL, cadence="D", overdue_sessions=2, stale_sessions=6, notes="EIA Henry Hub spot via FRED. Weekends and holidays are missing, not zero."),
    "PCOPPUSDM": _fred_macro_monthly(lag_days=25),
}

SOURCE_DEFAULT_CALENDAR = {
    "TREASURY": CAL_US_TREASURY,
    "FRED": CAL_US_TREASURY,
    "FINRA_QUERY": CAL_NYSE,
    "EQUITY_EOD": CAL_NYSE,
    "FMP_LEGACY": CAL_NYSE,
    "YAHOO": CAL_NYSE,
    "IBKR": CAL_NYSE,
    "OPENBB_CBOE_OPTIONS": CAL_NYSE,
    "OPENBB_CBOE_VIX": CAL_NYSE,
    "IBKR_OPTIONS": CAL_NYSE,
    "IBKR_OPTIONS_STORAGE": CAL_NYSE,
    "MSRB_EMMA": CAL_US_FEDERAL,
    "IBKR_MUNICIPAL_BONDS": CAL_NYSE,
    "IBKR_CORPORATE_BONDS": CAL_NYSE,
    "CFTC_COT": CAL_US_FEDERAL,
    "EIA_ENERGY": CAL_US_FEDERAL,
}


def policy_for(*, series_id: str | None = None, source_id: str | None = None, cadence: str | None = None) -> FreshnessPolicy | None:
    if series_id and series_id in SERIES_POLICIES:
        return SERIES_POLICIES[series_id]
    if source_id and source_id in SOURCE_DEFAULT_CALENDAR:
        cal = SOURCE_DEFAULT_CALENDAR[source_id]
        freq = str(cadence or "D").upper()
        if freq in {"D", "INTRADAY"}:
            # Equity daily bars: expected observation is the last completed NYSE session.
            if cal == CAL_NYSE:
                same_day = source_id != "FINRA_QUERY"
                return FreshnessPolicy(
                    calendar=cal,
                    cadence=freq,
                    typical_release=time(18, 0) if source_id == "FINRA_QUERY" else time(16, 0),
                    overdue_sessions=1,
                    stale_sessions=3,
                    same_day_available=same_day,
                    notes="FINRA Query aggregates are T+1." if source_id == "FINRA_QUERY" else "US equity/ETF last completed session.",
                )
            return FreshnessPolicy(calendar=cal, cadence=freq, overdue_sessions=1, stale_sessions=3)
        if freq == "W" and source_id == "CFTC_COT":
            return FreshnessPolicy(
                calendar=CAL_US_FEDERAL,
                cadence="W",
                typical_release=time(15, 30),
                overdue_sessions=8,
                stale_sessions=21,
                week_ending="TUE",
                notes="COT as-of Tuesday; typically released Friday 15:30 ET.",
            )
        if freq == "W" and source_id == "EIA_ENERGY":
            return FreshnessPolicy(
                calendar=CAL_US_FEDERAL,
                cadence="W",
                typical_release=time(10, 30),
                overdue_sessions=8,
                stale_sessions=21,
                week_ending="FRI",
                notes="EIA weekly petroleum/gas publications; lag is typical, not official.",
            )
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
        return _expected_week_ending(policy=policy, local=local)
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


def _expected_week_ending(*, policy: FreshnessPolicy, local: datetime) -> date:
    """Week-ending observation date whose typical release has already occurred."""
    today = local.date()
    ending = (policy.week_ending or "MON").upper()
    weekday = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}.get(ending, 0)
    offset = (today.weekday() - weekday) % 7
    period_end = today - timedelta(days=offset)
    release = policy.typical_release or time(8, 30)
    if ending == "SAT":
        release_day = period_end + timedelta(days=5)
    elif ending == "WED":
        release_day = period_end + timedelta(days=1)
    elif ending == "TUE":
        release_day = period_end + timedelta(days=3)
    else:
        release_day = period_end
    release_at = datetime.combine(release_day, release, tzinfo=NY_TZ)
    if local < release_at:
        period_end = period_end - timedelta(days=7)
    return period_end


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
    last_success_at: datetime | None = None,
    upstream_latest: date | None = None,
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
    status = _classify_with_upstream(
        status,
        latest_observation=latest_observation,
        expected=expected,
        upstream_latest=upstream_latest,
        last_success_at=last_success_at,
        clock=clock,
        transport=transport,
    )
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


def _recent_success(last_success_at: datetime | None, clock: datetime) -> bool:
    if last_success_at is None:
        return False
    success = last_success_at if last_success_at.tzinfo else last_success_at.replace(tzinfo=NY_TZ)
    now = clock if clock.tzinfo else clock.replace(tzinfo=NY_TZ)
    return (now - success).total_seconds() <= RECENT_SUCCESS_HOURS * 3600


def _classify_with_upstream(
    status: str,
    *,
    latest_observation: date,
    expected: date | None,
    upstream_latest: date | None,
    last_success_at: datetime | None,
    clock: datetime,
    transport: str | None,
) -> str:
    """Separate publication lag from a failed ingest when the collector is current to the provider."""
    provider_latest = upstream_latest
    if provider_latest is None and transport == "OK" and _recent_success(last_success_at, clock):
        provider_latest = latest_observation
    if provider_latest is None:
        if status in {STALE, INGESTION_OVERDUE} and transport == "OK" and _recent_success(last_success_at, clock):
            return CURRENT_TO_SOURCE
        if status == STALE and last_success_at is not None and not _recent_success(last_success_at, clock):
            return STALE_INGESTION
        return status
    if latest_observation < provider_latest:
        return STALE_INGESTION
    if latest_observation == provider_latest:
        if status in {LATEST_AVAILABLE, AWAITING_RELEASE}:
            return status
        if expected is not None and latest_observation < expected:
            return STALE_UPSTREAM if not _recent_success(last_success_at, clock) and transport != "OK" else CURRENT_TO_SOURCE
        return CURRENT_TO_SOURCE if status in {STALE, INGESTION_OVERDUE} else status
    return status


def is_current_status(status: str | None) -> bool:
    return str(status or "").upper() in {LATEST_AVAILABLE, FRESH, "CURRENT", "OK", AWAITING_RELEASE, CURRENT_TO_SOURCE, ON_DEMAND}


__all__ = [
    "AWAITING_RELEASE",
    "CADENCE_TOLERANCE",
    "CURRENT_TO_SOURCE",
    "FRESH",
    "ON_DEMAND",
    "STALE_INGESTION",
    "STALE_UPSTREAM",
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
