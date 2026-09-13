"""Equal-dollar baskets and aligned 1D RS."""

from datetime import date

import pytest

from market_intelligence.baskets import daily_rebalanced_equal_weight, ratio_change_rs
from market_intelligence.equity_eod import compute_metrics


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
