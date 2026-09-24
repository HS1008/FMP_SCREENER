"""Complete-curve date resolution for Rates & Curve comparisons."""

from datetime import date

import pytest

from market_intelligence.curve_compare import (
    COMPARE_CUSTOM,
    COMPARE_MONTH,
    COMPARE_NONE,
    COMPARE_PRIOR,
    COMPARE_WEEK,
    REASON_AFTER_CURRENT,
    REASON_NO_CURVE,
    comparison_target,
    resolve_complete_date,
)

CURRENT = date(2026, 9, 23)
COMPLETE = (
    date(2026, 6, 15),
    date(2026, 9, 11),
    date(2026, 9, 16),
    date(2026, 9, 18),
    date(2026, 9, 23),
)


def test_exact_valid_treasury_date():
    resolved = resolve_complete_date(COMPLETE, date(2026, 6, 15), not_after=CURRENT)
    assert resolved["found"] is True
    assert resolved["effective_date"] == date(2026, 6, 15)
    assert resolved["fallback"] is False
    assert resolved["requested_date"] == date(2026, 6, 15)


def test_weekend_uses_previous_complete_curve():
    sunday = date(2026, 9, 20)
    resolved = resolve_complete_date(COMPLETE, sunday, not_after=CURRENT)
    assert resolved["effective_date"] == date(2026, 9, 18)
    assert resolved["fallback"] is True
    assert resolved["requested_date"] == sunday


def test_non_print_date_uses_previous_complete_curve():
    # Monday with no complete print (holiday or partial session) is not in COMPLETE.
    holiday = date(2026, 9, 21)
    resolved = resolve_complete_date(COMPLETE, holiday, not_after=CURRENT)
    assert holiday not in COMPLETE
    assert resolved["effective_date"] == date(2026, 9, 18)
    assert resolved["fallback"] is True


def test_before_available_history_is_a_controlled_miss():
    resolved = resolve_complete_date(COMPLETE, date(2020, 1, 2), not_after=CURRENT)
    assert resolved["found"] is False
    assert resolved["effective_date"] is None
    assert resolved["reason"] == REASON_NO_CURVE


def test_date_after_current_curve_is_rejected():
    resolved = resolve_complete_date(COMPLETE, date(2026, 9, 24), not_after=CURRENT)
    assert resolved["found"] is False
    assert resolved["effective_date"] is None
    assert resolved["reason"] == REASON_AFTER_CURRENT


def test_incomplete_date_is_not_a_complete_comparison():
    incomplete = date(2026, 9, 22)
    resolved = resolve_complete_date(COMPLETE, incomplete, not_after=CURRENT)
    assert resolved["effective_date"] != incomplete
    assert resolved["effective_date"] == date(2026, 9, 18)


def test_quick_comparison_anchors():
    assert comparison_target(COMPARE_NONE, CURRENT) is None
    assert comparison_target(COMPARE_PRIOR, CURRENT) == date(2026, 9, 22)
    assert comparison_target(COMPARE_WEEK, CURRENT) == date(2026, 9, 16)
    assert comparison_target(COMPARE_MONTH, CURRENT) == date(2026, 8, 23)
    assert comparison_target(COMPARE_CUSTOM, CURRENT, date(2026, 6, 15)) == date(2026, 6, 15)


def test_quick_comparisons_resolve_to_complete_curves_not_after_current():
    prior = resolve_complete_date(COMPLETE, comparison_target(COMPARE_PRIOR, CURRENT), not_after=CURRENT)
    week = resolve_complete_date(COMPLETE, comparison_target(COMPARE_WEEK, CURRENT), not_after=CURRENT)
    month = resolve_complete_date(COMPLETE, comparison_target(COMPARE_MONTH, CURRENT), not_after=CURRENT)
    assert prior["effective_date"] == date(2026, 9, 18)
    assert week["effective_date"] == date(2026, 9, 16)
    assert week["fallback"] is False
    assert month["effective_date"] == date(2026, 6, 15)
    assert month["fallback"] is True
    for resolved in (prior, week, month):
        assert resolved["found"] is True
        assert resolved["effective_date"] <= CURRENT


def test_unknown_comparison_mode_is_rejected():
    with pytest.raises(ValueError):
        comparison_target("2 weeks", CURRENT)
