"""Ladder aggregation fixtures. No orders."""

from __future__ import annotations

import pytest

from market_intelligence.bond_ladder import LadderBond, aggregate_ladder, theoretical_rungs
from market_intelligence.bond_tax import TaxAssumptions


def test_theoretical_equal_rungs():
    rungs = theoretical_rungs(total_investment=100_000, start_year=1, end_year=5, interval_years=2, yield_pct=4.0, asset_class="treasury")
    assert [bond.maturity_year for bond in rungs] == [1, 3, 5]
    assert all(bond.principal == pytest.approx(100_000 / 3) for bond in rungs)


def test_aggregate_weights_and_missing_duration():
    bonds = [
        LadderBond("A", "treasury", 2027, 50_000, 4.0, 4.0, duration=2.0, rating="AAA", issuer="UST", state=None),
        LadderBond("B", "treasury", 2030, 50_000, 4.2, 4.2, duration=None, rating="AAA", issuer="UST"),
    ]
    out = aggregate_ladder(bonds)
    assert out["places_orders"] is False
    assert out["weighted_average_yield_pct"] == pytest.approx(4.1)
    assert out["weighted_duration"] == pytest.approx(2.0)
    assert out["missing_durations"] == 1
    assert out["estimated_coupon_income"] == pytest.approx(50_000 * 0.04 + 50_000 * 0.042)
    assert out["weighted_average_maturity"] == pytest.approx(2028.5)


def test_weighted_average_maturity_uses_principal():
    bonds = [
        LadderBond("A", "treasury", 2026, 10_000, 4.0, 4.0),
        LadderBond("B", "treasury", 2030, 90_000, 4.0, 4.0),
    ]
    out = aggregate_ladder(bonds)
    assert out["weighted_average_maturity"] == pytest.approx((10_000 * 2026 + 90_000 * 2030) / 100_000)


def test_ladder_after_tax_and_call_share():
    bonds = [
        LadderBond("M1", "muni", 2028, 40_000, 3.0, 3.0, callable=True, ytw_pct=2.5, state="NY", issuer="NYC"),
        LadderBond("M2", "muni", 2032, 60_000, 3.4, 3.4, callable=False, state="NY", issuer="MTA"),
    ]
    out = aggregate_ladder(bonds, TaxAssumptions(federal_rate=0.32, state_rate=0.06, local_rate=0.0, niit_rate=0.038, muni_in_state=True))
    assert out["call_exposure_share"] == pytest.approx(0.4)
    assert out["weighted_average_yield_pct"] == pytest.approx((40000 * 2.5 + 60000 * 3.4) / 100000)
    assert out["weighted_average_after_tax_yield_pct"] == pytest.approx(out["weighted_average_yield_pct"])
    assert out["state_concentration"]["NY"] == pytest.approx(100_000)
