"""Deterministic coverage for the weekday 10-minute source-aware catch-up window."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from market_intelligence.due_state import (
    build_freshness_index,
    catchup_calendar_lines,
    daily_source_due,
    evaluate_due_steps,
    evaluate_fred_catalog_due,
    fred_dataset_key,
    in_catchup_window,
    in_normal_polling_window,
    is_final_catchup,
    release_calendar_due,
    series_unit_due,
    yahoo_live_due,
)
from market_intelligence.freshness import expected_latest_published, policy_for

ET = ZoneInfo("America/New_York")


def test_catchup_calendar_every_10_minutes_in_window():
    lines = catchup_calendar_lines()
    assert lines[0] == "OnCalendar=Mon..Fri 09:15 America/New_York"
    assert "OnCalendar=Mon..Fri 18:30 America/New_York" in lines
    assert len(lines) >= 55
    assert all("America/New_York" in line for line in lines)


def test_in_catchup_window_includes_final_grace_not_1840():
    assert in_catchup_window(datetime(2026, 9, 16, 10, 0, tzinfo=ET)) is True
    assert in_catchup_window(datetime(2026, 9, 16, 9, 0, tzinfo=ET)) is False
    assert in_catchup_window(datetime(2026, 9, 16, 18, 29, tzinfo=ET)) is True
    assert in_catchup_window(datetime(2026, 9, 16, 18, 30, 0, tzinfo=ET)) is True
    assert in_catchup_window(datetime(2026, 9, 16, 18, 30, 37, tzinfo=ET)) is True
    assert in_catchup_window(datetime(2026, 9, 16, 18, 35, tzinfo=ET)) is True
    assert in_catchup_window(datetime(2026, 9, 16, 18, 40, tzinfo=ET)) is False
    assert in_catchup_window(datetime(2026, 9, 19, 12, 0, tzinfo=ET)) is False
    assert in_catchup_window(datetime(2026, 1, 14, 10, 0, tzinfo=ET)) is True


def test_final_catchup_grace_survives_systemd_jitter():
    assert in_normal_polling_window(datetime(2026, 9, 16, 18, 29, tzinfo=ET)) is True
    assert is_final_catchup(datetime(2026, 9, 16, 18, 29, tzinfo=ET)) is False
    assert is_final_catchup(datetime(2026, 9, 16, 18, 30, 0, tzinfo=ET)) is True
    assert is_final_catchup(datetime(2026, 9, 16, 18, 30, 37, tzinfo=ET)) is True
    assert is_final_catchup(datetime(2026, 9, 16, 18, 35, tzinfo=ET)) is True
    assert is_final_catchup(datetime(2026, 9, 16, 18, 40, tzinfo=ET)) is False
    assert is_final_catchup(datetime(2026, 9, 16, 16, 5, tzinfo=ET)) is False


def test_daily_already_current_skips_provider():
    now = datetime(2026, 9, 16, 15, 40, tzinfo=ET)
    decision = daily_source_due(
        step="treasury",
        source_id="TREASURY",
        cadence="D",
        latest_observation=date(2026, 9, 16),
        now=now,
        series_id="DGS10",
    )
    assert decision.due is False
    assert decision.outcome_if_skip == "SKIPPED_ALREADY_CURRENT"


def test_fred_series_a_current_b_due_only_requests_b():
    """Same FRED source_id: independent series must not block each other."""
    now = datetime(2026, 9, 16, 15, 40, tzinfo=ET)
    policy_a = policy_for(series_id="DFF")
    policy_b = policy_for(series_id="BAMLC0A0CM")
    assert policy_a is not None and policy_b is not None
    expected_a = expected_latest_published(policy=policy_a, now=now)
    expected_b = expected_latest_published(policy=policy_b, now=now)
    freshness = {
        ("FRED", fred_dataset_key("DFF")): expected_a,
        ("FRED", fred_dataset_key("BAMLC0A0CM")): expected_b - timedelta(days=5),
    }
    unit_a = series_unit_due(
        step="fred",
        source_id="FRED",
        series_id="DFF",
        latest_observation=freshness[("FRED", fred_dataset_key("DFF"))],
        now=now,
    )
    unit_b = series_unit_due(
        step="fred",
        source_id="FRED",
        series_id="BAMLC0A0CM",
        latest_observation=freshness[("FRED", fred_dataset_key("BAMLC0A0CM"))],
        now=now,
    )
    assert unit_a.due is False
    assert unit_a.outcome_if_skip == "SKIPPED_ALREADY_CURRENT"
    assert unit_b.due is True

    aggregate = evaluate_fred_catalog_due(
        now=now,
        freshness=freshness,
        series_ids=["DFF", "BAMLC0A0CM"],
    )
    assert aggregate.due is True
    assert aggregate.details["due_series"] == ["BAMLC0A0CM"]
    assert "DFF" not in aggregate.details["due_series"]


def test_fred_both_current_next_cycle_skips_both():
    now = datetime(2026, 9, 16, 15, 40, tzinfo=ET)
    freshness = {}
    for sid in ("DFF", "BAMLC0A0CM"):
        policy = policy_for(series_id=sid)
        assert policy is not None
        freshness[("FRED", fred_dataset_key(sid))] = expected_latest_published(policy=policy, now=now)
    aggregate = evaluate_fred_catalog_due(now=now, freshness=freshness, series_ids=["DFF", "BAMLC0A0CM"])
    assert aggregate.due is False
    assert aggregate.details["due_series"] == []
    assert aggregate.outcome_if_skip == "SKIPPED_ALREADY_CURRENT"


def test_fred_monthly_not_on_release_cycle_not_requested():
    now = datetime(2026, 9, 16, 11, 0, tzinfo=ET)
    policy = policy_for(series_id="CPIAUCSL")
    assert policy is not None
    expected = expected_latest_published(policy=policy, now=now)
    unit = series_unit_due(
        step="fred",
        source_id="FRED",
        series_id="CPIAUCSL",
        latest_observation=expected,
        now=now,
    )
    assert unit.due is False
    assert unit.outcome_if_skip in {"SKIPPED_ALREADY_CURRENT", "SKIPPED_NOT_DUE"}


def test_fred_daily_due_does_not_query_unrelated_monthly():
    now = datetime(2026, 9, 16, 15, 40, tzinfo=ET)
    daily_policy = policy_for(series_id="BAMLC0A0CM")
    monthly_policy = policy_for(series_id="CPIAUCSL")
    assert daily_policy is not None and monthly_policy is not None
    expected_monthly = expected_latest_published(policy=monthly_policy, now=now)
    freshness = {
        ("FRED", fred_dataset_key("BAMLC0A0CM")): None,
        ("FRED", fred_dataset_key("CPIAUCSL")): expected_monthly,
    }
    aggregate = evaluate_fred_catalog_due(
        now=now,
        freshness=freshness,
        series_ids=["BAMLC0A0CM", "CPIAUCSL"],
    )
    assert "CPIAUCSL" not in (aggregate.details or {}).get("due_series", [])
    if aggregate.due:
        assert aggregate.details["due_series"] == ["BAMLC0A0CM"]
    else:
        assert aggregate.details["due_series"] == []


def test_build_freshness_index_keys_source_and_dataset():
    rows = [
        {"source_id": "FRED", "dataset": "series:DFF", "latest_observation_date": date(2026, 9, 15)},
        {"source_id": "FRED", "dataset": "series:CPIAUCSL", "latest_observation_date": "2026-08-01"},
        {"source_id": "TREASURY", "dataset": "daily_treasury_xml", "latest_observation_date": date(2026, 9, 16)},
    ]
    index = build_freshness_index(rows)
    assert index[("FRED", "series:DFF")] == date(2026, 9, 15)
    assert index[("FRED", "series:CPIAUCSL")] == date(2026, 8, 1)
    assert index[("TREASURY", "daily_treasury_xml")] == date(2026, 9, 16)


def test_one_dataset_current_does_not_stop_another():
    now = datetime(2026, 9, 16, 15, 40, tzinfo=ET)
    decisions = evaluate_due_steps(
        now=now,
        env={"MI_YAHOO_LIVE_FALLBACK": "0", "MI_YAHOO_EOD_FALLBACK": "0"},
        latest_by_source={"TREASURY": date(2026, 9, 16), "FINRA_QUERY": date(2026, 9, 15)},
        configured_steps=["treasury", "finra", "openfigi", "edgar"],
    )
    by_step = {d.step: d for d in decisions}
    assert by_step["treasury"].due is False
    assert by_step["openfigi"].due is False
    assert by_step["edgar"].due is False
    assert "finra" in by_step


def test_weekly_not_polled_on_irrelevant_day():
    now = datetime(2026, 9, 16, 10, 0, tzinfo=ET)
    decision = release_calendar_due(
        step="cftc",
        source_id="CFTC_COT",
        cadence="W",
        latest_observation=date(2026, 9, 9),
        now=now,
    )
    assert decision.reason in {"awaiting_release", "release_catch_up", "already_current", "no_policy_skip"}


def test_yahoo_live_due_respects_flag_and_rth():
    rth = datetime(2026, 9, 16, 11, 0, tzinfo=ET)
    assert yahoo_live_due(enabled=True, now=rth).due is True
    assert yahoo_live_due(enabled=False, now=rth).due is False
    assert yahoo_live_due(enabled=True, now=datetime(2026, 9, 16, 16, 30, tzinfo=ET)).due is False
    assert yahoo_live_due(enabled=True, now=datetime(2026, 9, 19, 11, 0, tzinfo=ET)).due is False


def test_outside_catchup_window_marks_all_not_due():
    now = datetime(2026, 9, 16, 8, 0, tzinfo=ET)
    decisions = evaluate_due_steps(
        now=now,
        env={},
        latest_by_source={},
        configured_steps=["treasury", "fred"],
    )
    assert all(not d.due for d in decisions)
    assert all(d.reason == "outside_catchup_window" for d in decisions)


def test_timer_template_matches_catchup_calendar():
    timer = (Path(__file__).resolve().parents[1] / "deploy" / "market_intelligence" / "fmp-mi-refresh.timer").read_text(
        encoding="utf-8"
    )
    for line in catchup_calendar_lines():
        assert line in timer
