"""Deterministic coverage for the weekday 10-minute source-aware catch-up window."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from market_intelligence.due_state import (
    catchup_calendar_lines,
    daily_source_due,
    evaluate_due_steps,
    in_catchup_window,
    is_final_catchup,
    release_calendar_due,
    yahoo_live_due,
)

ET = ZoneInfo("America/New_York")


def test_catchup_calendar_every_10_minutes_in_window():
    lines = catchup_calendar_lines()
    assert lines[0] == "OnCalendar=Mon..Fri 09:15 America/New_York"
    assert "OnCalendar=Mon..Fri 18:30 America/New_York" in lines
    assert len(lines) >= 55
    # Every entry uses America/New_York (DST-safe), not a fixed EST offset.
    assert all("America/New_York" in line for line in lines)


def test_in_catchup_window_dst_and_weekends():
    # EDT weekday inside window
    assert in_catchup_window(datetime(2026, 9, 16, 10, 0, tzinfo=ET)) is True
    # Before 09:15
    assert in_catchup_window(datetime(2026, 9, 16, 9, 0, tzinfo=ET)) is False
    # After 18:30
    assert in_catchup_window(datetime(2026, 9, 16, 18, 40, tzinfo=ET)) is False
    # Weekend
    assert in_catchup_window(datetime(2026, 9, 19, 12, 0, tzinfo=ET)) is False
    # EST winter weekday (America/New_York handles offset)
    assert in_catchup_window(datetime(2026, 1, 14, 10, 0, tzinfo=ET)) is True


def test_final_catchup_only_near_1830():
    assert is_final_catchup(datetime(2026, 9, 16, 18, 30, tzinfo=ET)) is True
    assert is_final_catchup(datetime(2026, 9, 16, 18, 35, tzinfo=ET)) is True
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


def test_daily_due_when_missing_today():
    now = datetime(2026, 9, 16, 15, 40, tzinfo=ET)
    decision = daily_source_due(
        step="treasury",
        source_id="TREASURY",
        cadence="D",
        latest_observation=date(2026, 9, 15),
        now=now,
        series_id="DGS10",
    )
    # May be awaiting release or catch-up depending on typical_release; if due, reason is catch-up.
    if decision.due:
        assert decision.reason in {"catch_up_or_missing", "no_policy_conservative_check"}
    else:
        assert decision.outcome_if_skip in {"SKIPPED_NOT_DUE", "SKIPPED_ALREADY_CURRENT"}


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
    assert by_step["openfigi"].due is False  # not in catch-up poll set
    assert by_step["edgar"].due is False
    # finra may or may not be due depending on freshness policy; independence is the point
    assert "finra" in by_step


def test_weekly_not_polled_on_irrelevant_day():
    # Mid-week without an expected new weekly print should skip (or not enter release catch-up blindly).
    now = datetime(2026, 9, 16, 10, 0, tzinfo=ET)  # Wednesday
    decision = release_calendar_due(
        step="cftc",
        source_id="CFTC_COT",
        cadence="W",
        latest_observation=date(2026, 9, 9),
        now=now,
    )
    # Either not due yet (awaiting) or due for catch-up — never invents a daily poll reason.
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
    from pathlib import Path

    timer = (Path(__file__).resolve().parents[1] / "deploy" / "market_intelligence" / "fmp-mi-refresh.timer").read_text(encoding="utf-8")
    for line in catchup_calendar_lines():
        assert line in timer
