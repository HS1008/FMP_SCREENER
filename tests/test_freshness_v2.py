"""Source-specific freshness: latest available vs elapsed-time overstatement."""

from datetime import date, datetime, time

from market_intelligence.calendars import CAL_NYSE, CAL_US_TREASURY, is_session, observed_weekday
from market_intelligence.freshness import (
    AWAITING_RELEASE,
    CURRENT_TO_SOURCE,
    INGESTION_OVERDUE,
    INVALID_FUTURE,
    LATEST_AVAILABLE,
    MISSING,
    STALE,
    STALE_INGESTION,
    assess_freshness,
    is_business_day,
)


def test_daily_four_business_days_is_not_fresh():
    # Reproduced against the old policy: 2026-09-03 vs 2026-09-10 was FRESH.
    result = assess_freshness(date(2026, 9, 3), "D", date(2026, 9, 10), series_id="DGS10")
    assert result.status in {INGESTION_OVERDUE, STALE}
    assert result.status != LATEST_AVAILABLE
    assert result.policy_version == "freshness_policy_v2"


def test_future_observation_is_invalid():
    result = assess_freshness(date(2026, 9, 11), "D", date(2026, 9, 10), series_id="DGS10")
    assert result.status == INVALID_FUTURE
    assert result.age_days == -1


def test_new_year_observed_in_prior_year():
    assert observed_weekday(date(2022, 1, 1)) == date(2021, 12, 31)
    assert is_business_day(date(2021, 12, 31)) is False
    assert is_session(date(2021, 12, 31), CAL_US_TREASURY) is False
    assert is_session(date(2021, 12, 31), CAL_NYSE) is False


def test_nyse_open_on_columbus_day_closed_good_friday():
    # 2024-10-14 Columbus Day: NYSE open, Treasury closed.
    assert is_session(date(2024, 10, 14), CAL_NYSE) is True
    assert is_session(date(2024, 10, 14), CAL_US_TREASURY) is False
    assert is_session(date(2024, 3, 29), CAL_NYSE) is False  # Good Friday


def test_missing_and_awaiting():
    assert assess_freshness(None, "D", date(2026, 9, 10), series_id="DGS10").status == MISSING
    before_publish = datetime(2026, 9, 10, 10, 0, tzinfo=__import__("market_intelligence.calendars", fromlist=["NY_TZ"]).NY_TZ)
    result = assess_freshness(date(2026, 9, 9), "D", now=before_publish, series_id="DGS10")
    assert result.status in {LATEST_AVAILABLE, AWAITING_RELEASE}


def test_monthly_reference_is_not_stale_before_next_release():
    # December CPI is still the latest published print on 1 Feb (January CPI is mid-month).
    result = assess_freshness(date(2024, 12, 1), "M", date(2025, 2, 1), series_id="CPIAUCSL")
    assert result.status == LATEST_AVAILABLE
    later = assess_freshness(date(2024, 9, 1), "M", date(2025, 2, 1), series_id="CPIAUCSL")
    assert later.status in {STALE, INGESTION_OVERDUE}


def test_fetch_success_does_not_change_observation_date():
    first = assess_freshness(date(2026, 9, 9), "D", date(2026, 9, 10), series_id="DGS10")
    again = assess_freshness(date(2026, 9, 9), "D", date(2026, 9, 10), series_id="DGS10", transport_status="OK")
    assert first.expected_latest == again.expected_latest
    assert again.transport_status == "OK"


def test_saturday_claims_are_current_on_monday_before_thursday_release():
    from market_intelligence.calendars import NY_TZ

    monday = datetime(2026, 9, 14, 21, 0, tzinfo=NY_TZ)
    result = assess_freshness(date(2026, 9, 5), "W", now=monday, series_id="ICSA", transport_status="OK")
    assert result.status == LATEST_AVAILABLE
    assert result.expected_latest == date(2026, 9, 5)


def test_finra_t_plus_one_monday_evening_is_current_to_friday():
    from market_intelligence.calendars import NY_TZ

    monday = datetime(2026, 9, 14, 21, 0, tzinfo=NY_TZ)
    result = assess_freshness(date(2026, 9, 11), "D", now=monday, source_id="FINRA_QUERY", transport_status="OK")
    assert result.status == LATEST_AVAILABLE
    assert result.expected_latest == date(2026, 9, 11)


def test_recent_success_marks_publication_lag_not_ingestion_failure():
    result = assess_freshness(date(2026, 7, 1), "M", date(2026, 9, 14), series_id="M2SL", transport_status="OK")
    assert result.status == LATEST_AVAILABLE


def test_db_behind_upstream_is_stale_ingestion():
    result = assess_freshness(
        date(2026, 9, 11),
        "D",
        date(2026, 9, 14),
        source_id="EQUITY_EOD",
        transport_status="OK",
        upstream_latest=date(2026, 9, 14),
    )
    assert result.status == STALE_INGESTION


def test_fred_friday_print_on_monday_evening_is_current_to_source():
    from market_intelligence.calendars import NY_TZ
    from market_intelligence.freshness import health_label, provider_latest_from_coverage

    monday = datetime(2026, 9, 14, 21, 0, tzinfo=NY_TZ)
    result = assess_freshness(
        date(2026, 9, 11),
        "D",
        now=monday,
        series_id="DGS10",
        transport_status="OK",
        last_success_at=monday,
        upstream_latest=date(2026, 9, 11),
    )
    assert result.status == CURRENT_TO_SOURCE
    assert health_label(result.status) == "HEALTHY_PUBLICATION_LAG"
    wti = assess_freshness(
        date(2026, 9, 9),
        "D",
        now=monday,
        series_id="DCOILWTICO",
        transport_status="OK",
        last_success_at=monday,
        upstream_latest=date(2026, 9, 9),
    )
    assert wti.status == CURRENT_TO_SOURCE
    assert provider_latest_from_coverage({"provider_latest_observation_date": "2026-09-11"}) == date(2026, 9, 11)


def test_utc_calendar_date_is_not_used_when_now_is_new_york():
    from market_intelligence.calendars import NY_TZ

    # 01:50 UTC 15 Sep is still Monday evening in New York. UTC CURRENT_DATE would expect Tuesday.
    monday_evening = datetime(2026, 9, 14, 21, 50, tzinfo=NY_TZ)
    result = assess_freshness(
        date(2026, 9, 11),
        "D",
        now=monday_evening,
        series_id="DGS10",
        transport_status="OK",
        last_success_at=monday_evening,
        upstream_latest=date(2026, 9, 11),
    )
    assert result.status == CURRENT_TO_SOURCE
    utc_date_only = assess_freshness(date(2026, 9, 11), "D", date(2026, 9, 15), series_id="DGS10")
    assert utc_date_only.status in {INGESTION_OVERDUE, STALE}


def test_historical_provider_latest_does_not_mask_calendar_stale():
    result = assess_freshness(
        date(2024, 12, 31),
        "D",
        date(2026, 9, 14),
        series_id="DGS10",
        transport_status="OK",
        upstream_latest=date(2024, 12, 31),
    )
    assert result.status == STALE
