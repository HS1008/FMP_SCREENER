"""Equal-dollar baskets and aligned 1D RS."""

from datetime import date

import pytest

from market_intelligence.baskets import daily_rebalanced_equal_weight, ratio_change_rs
from market_intelligence.equity_eod import basket_snapshot_metrics, compute_metrics


def test_equal_dollar_invariant_to_price_scale():
    cheap = {date(2026, 9, 9): 10.0, date(2026, 9, 10): 11.0}
    rich = {date(2026, 9, 9): 100.0, date(2026, 9, 10): 100.0}
    index = daily_rebalanced_equal_weight({"A": cheap, "B": rich})
    last = index[-1]
    assert last.ret_1d == pytest.approx(0.05)
    scaled = {d: px * 7.0 for d, px in cheap.items()}
    again = daily_rebalanced_equal_weight({"A": scaled, "B": rich})
    assert again[-1].ret_1d == pytest.approx(0.05)


def test_mean_price_is_not_equal_weight():
    cheap = {date(2026, 9, 9): 10.0, date(2026, 9, 10): 11.0}
    rich = {date(2026, 9, 9): 100.0, date(2026, 9, 10): 100.0}
    mean_ret = ((11 + 100) / 2) / ((10 + 100) / 2) - 1.0
    assert abs(mean_ret - 0.0090909) < 1e-6
    ew = daily_rebalanced_equal_weight({"A": cheap, "B": rich})[-1].ret_1d
    assert ew != mean_ret


def test_rs_is_ratio_change_not_excess():
    # +2% vs +1% => 1.02/1.01 - 1
    rs = ratio_change_rs(102, 100, 101, 100)
    assert abs(rs - (1.02 / 1.01 - 1)) < 1e-12
    assert abs(rs - 0.01) > 1e-6


def test_unobservable_rebalance_step_is_not_a_zero_return():
    start = date(2026, 9, 9)
    hole = date(2026, 9, 10)
    end = date(2026, 9, 11)
    prices = {
        "A": {start: 10.0, end: 11.0},
        "B": {start: 100.0, end: 100.0},
    }
    stopped = daily_rebalanced_equal_weight(prices, calendar=(start, hole, end))
    assert [point.as_of for point in stopped] == [start]
    assert all(point.ret_1d is None for point in stopped)
    assert stopped[-1].level == pytest.approx(1.0)


def test_legitimate_closures_keep_the_equal_weight_step():
    """Jan 9 2025 was a mourning-day closure. The Jan 8 to Jan 10 step is one session."""
    left = date(2025, 1, 8)
    right = date(2025, 1, 10)
    members = {
        "AAA": {left: 100.0, right: 110.0},
        "BBB": {left: 200.0, right: 200.0},
    }
    bench = {left: 100.0, right: 100.0}
    index = daily_rebalanced_equal_weight(members, end=right)
    assert [point.as_of for point in index] == [left, right]
    assert index[-1].ret_1d == pytest.approx(0.05)
    metrics, coverage, published = basket_snapshot_metrics(
        members, bench, right, adjustment_basis="IBKR_ADJUSTED_LAST"
    )
    assert published[-1].as_of == right
    assert coverage["index_truncated"] is False
    assert metrics["ret_1d"] == pytest.approx(0.05)
    # Friday Sep 10 2001 to Monday Sep 17 2001 spans the four-day closure.
    attack_left = date(2001, 9, 10)
    attack_right = date(2001, 9, 17)
    attack = {
        "AAA": {attack_left: 100.0, attack_right: 90.0},
        "BBB": {attack_left: 100.0, attack_right: 100.0},
    }
    attack_index = daily_rebalanced_equal_weight(attack, end=attack_right)
    assert attack_index[-1].as_of == attack_right
    assert attack_index[-1].ret_1d == pytest.approx(-0.05)


def test_resumed_prices_after_a_missing_weekday_do_not_extend_the_snapshot():
    tuesday = date(2024, 1, 2)
    wednesday = date(2024, 1, 3)
    thursday = date(2024, 1, 4)
    friday = date(2024, 1, 5)
    gapped = {
        "AAA": {tuesday: 100.0, wednesday: 110.0, friday: 121.0},
        "BBB": {tuesday: 100.0, wednesday: 100.0, friday: 100.0},
    }
    bench = {tuesday: 100.0, wednesday: 100.0, friday: 100.0}
    stopped = daily_rebalanced_equal_weight(gapped, end=friday)
    assert [point.as_of for point in stopped] == [tuesday, wednesday]
    metrics, coverage, index = basket_snapshot_metrics(gapped, bench, friday, adjustment_basis="IBKR_ADJUSTED_LAST")
    assert [point.as_of for point in index] == [tuesday, wednesday]
    assert metrics["ret_1d"] is None
    assert metrics["ret_1w"] is None
    assert metrics["rs_chg_1d"] is None
    assert coverage["index_truncated"] is True
    assert coverage["index_as_of"] == wednesday.isoformat()
    assert coverage["requested_as_of"] == friday.isoformat()
    complete = {
        "AAA": {tuesday: 100.0, wednesday: 110.0, thursday: 110.0, friday: 121.0},
        "BBB": {tuesday: 100.0, wednesday: 100.0, thursday: 100.0, friday: 100.0},
    }
    published, published_coverage, published_index = basket_snapshot_metrics(
        complete,
        {**bench, thursday: 100.0},
        friday,
        adjustment_basis="IBKR_ADJUSTED_LAST",
    )
    assert published_index[-1].as_of == friday
    assert published_coverage["index_truncated"] is False
    assert published["ret_1d"] == pytest.approx(0.05)


def test_missing_session_is_null_zero_is_valid():
    assert ratio_change_rs(100, None, 100, 100) is None
    asset = {date(2026, 9, 8): 100.0, date(2026, 9, 10): 102.0}
    bench = {date(2026, 9, 8): 100.0, date(2026, 9, 9): 100.5, date(2026, 9, 10): 101.0}
    metrics, coverage = compute_metrics(asset, bench, date(2026, 9, 10))
    assert metrics["ret_1d"] is None or coverage["aligned_with_benchmark"] is False
    flat = {date(2026, 9, 9): 100.0, date(2026, 9, 10): 100.0}
    metrics2, _ = compute_metrics(flat, flat, date(2026, 9, 10))
    assert metrics2["ret_1d"] == 0.0
    assert metrics2["rs_chg_1d"] == 0.0
