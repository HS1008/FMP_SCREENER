"""FRED provider_latest must ignore missing tokens such as '.'."""

from datetime import date
from decimal import Decimal

from market_intelligence.ingest_fred import latest_usable_observation_date, returned_coverage_last_date
from market_intelligence.nulls import normalize_numeric
from market_intelligence.store import ObservationInput


def _row(day: str, raw: str) -> ObservationInput:
    return ObservationInput(date.fromisoformat(day), raw)


def test_friday_numeric_weekend_dot_uses_friday_as_provider_latest():
    rows = [
        _row("2026-09-11", "4.26"),  # Friday DGS10
        _row("2026-09-12", "."),  # Saturday
        _row("2026-09-13", "."),  # Sunday
    ]
    assert latest_usable_observation_date(rows) == date(2026, 9, 11)
    assert returned_coverage_last_date(rows) == date(2026, 9, 13)


def test_treasury_holiday_dot_does_not_advance_provider_latest():
    rows = [
        _row("2026-07-02", "4.35"),
        _row("2026-07-03", "."),  # observed Independence Day style gap
        _row("2026-07-04", "."),
    ]
    assert latest_usable_observation_date(rows) == date(2026, 7, 2)
    assert returned_coverage_last_date(rows) == date(2026, 7, 4)


def test_wti_henry_hub_style_missing_token():
    rows = [
        _row("2026-09-10", "68.12"),
        _row("2026-09-11", "."),
        _row("2026-09-12", "n/a"),
    ]
    assert latest_usable_observation_date(rows) == date(2026, 9, 10)
    value, reason = normalize_numeric(".")
    assert value is None and reason and "missing_token" in reason


def test_latest_numeric_observation_unchanged():
    rows = [
        _row("2026-09-10", "4.20"),
        _row("2026-09-11", "4.22"),
        _row("2026-09-12", "4.25"),
    ]
    assert latest_usable_observation_date(rows) == date(2026, 9, 12)
    assert returned_coverage_last_date(rows) == date(2026, 9, 12)


def test_all_observations_missing_returns_none():
    rows = [_row("2026-09-12", "."), _row("2026-09-13", ""), _row("2026-09-14", "NA")]
    assert latest_usable_observation_date(rows) is None
    assert returned_coverage_last_date(rows) == date(2026, 9, 14)


def test_missing_values_remain_null_never_zero():
    value, reason = normalize_numeric(".")
    assert value is None
    assert value != 0 and value != Decimal("0")
    assert reason.startswith("missing_token")
    zero, zero_reason = normalize_numeric("0")
    assert zero == Decimal("0") and zero_reason is None


def test_malformed_non_missing_text_is_skipped_for_provider_latest():
    rows = [_row("2026-09-10", "4.10"), _row("2026-09-11", "not-a-number")]
    assert latest_usable_observation_date(rows) == date(2026, 9, 10)
