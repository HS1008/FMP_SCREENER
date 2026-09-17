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


def test_sofr_is_tplus1_not_same_day_treasury_policy():
    from market_intelligence.calendars import NY_TZ
    from market_intelligence.due_state import series_unit_due
    from market_intelligence.freshness import expected_latest_published, policy_for

    policy = policy_for(series_id="SOFR")
    assert policy is not None
    assert policy.same_day_available is False
    assert policy.observation_lag_sessions == 1
    assert policy.typical_release == time(8, 0)

    before = datetime(2026, 9, 15, 7, 0, tzinfo=NY_TZ)  # Tuesday before 08:00
    after = datetime(2026, 9, 15, 8, 30, tzinfo=NY_TZ)
    late = datetime(2026, 9, 15, 16, 0, tzinfo=NY_TZ)
    # Monday 14 Sep is prior session; Friday 11 Sep is the prior published print before Tue 08:00.
    assert expected_latest_published(policy=policy, now=before) == date(2026, 9, 11)
    assert expected_latest_published(policy=policy, now=after) == date(2026, 9, 14)
    # After close must NOT flip to same-day Tuesday.
    assert expected_latest_published(policy=policy, now=late) == date(2026, 9, 14)

    weekend = datetime(2026, 9, 19, 12, 0, tzinfo=NY_TZ)  # Saturday
    assert expected_latest_published(policy=policy, now=weekend) == date(2026, 9, 17)  # Thu (Fri publishes Mon)

    # Before publish: latest=Fri is current; after publish with only Fri stored -> due for Mon.
    assert assess_freshness(date(2026, 9, 11), "D", now=before, series_id="SOFR").status == LATEST_AVAILABLE
    after_missing = assess_freshness(date(2026, 9, 11), "D", now=after, series_id="SOFR")
    assert after_missing.status in {INGESTION_OVERDUE, AWAITING_RELEASE, STALE}
    assert series_unit_due(
        step="fred",
        source_id="FRED",
        series_id="SOFR",
        latest_observation=date(2026, 9, 11),
        now=after,
    ).due is True
    assert series_unit_due(
        step="fred",
        source_id="FRED",
        series_id="SOFR",
        latest_observation=date(2026, 9, 14),
        now=after,
    ).due is False


def test_dff_is_tplus1_h15_afternoon_not_same_day():
    from market_intelligence.calendars import NY_TZ
    from market_intelligence.due_state import series_unit_due
    from market_intelligence.freshness import expected_latest_published, policy_for

    policy = policy_for(series_id="DFF")
    assert policy is not None
    assert policy.same_day_available is False
    assert policy.observation_lag_sessions == 1
    assert policy.typical_release == time(16, 15)

    morning = datetime(2026, 9, 16, 10, 0, tzinfo=NY_TZ)  # Wed before H.15
    evening = datetime(2026, 9, 16, 16, 30, tzinfo=NY_TZ)
    # Before 16:15 Wed: Tuesday's print not yet expected on FRED → Monday.
    assert expected_latest_published(policy=policy, now=morning) == date(2026, 9, 14)
    assert expected_latest_published(policy=policy, now=evening) == date(2026, 9, 15)
    # Must never expect calendar-same-day Wednesday.
    assert expected_latest_published(policy=policy, now=evening) != date(2026, 9, 16)

    assert assess_freshness(date(2026, 9, 14), "D", now=morning, series_id="DFF").status == LATEST_AVAILABLE
    due = series_unit_due(
        step="fred",
        source_id="FRED",
        series_id="DFF",
        latest_observation=date(2026, 9, 14),
        now=evening,
    )
    assert due.due is True
    done = series_unit_due(
        step="fred",
        source_id="FRED",
        series_id="DFF",
        latest_observation=date(2026, 9, 15),
        now=evening,
    )
    assert done.due is False
    assert done.outcome_if_skip == "SKIPPED_ALREADY_CURRENT"
