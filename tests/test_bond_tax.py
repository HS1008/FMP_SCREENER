"""Deterministic tax-equivalent and after-tax bond fixtures."""

from __future__ import annotations

import pytest

from market_intelligence.bond_tax import (
    ASSET_CORPORATE,
    ASSET_MUNI,
    ASSET_TREASURY,
    BondTaxError,
    BondTaxInputs,
    TaxAssumptions,
    after_tax_yield_per_duration,
    analyze_bond_tax,
    compare_three,
    income_breakdown,
    income_on_amount,
    muni_treasury_ratio,
)


FED_24 = TaxAssumptions(federal_rate=0.24, state_rate=0.05, local_rate=0.0, niit_rate=0.038)


def test_muni_in_state_federal_exempt_uses_only_zero_tax():
    assumptions = TaxAssumptions(federal_rate=0.32, state_rate=0.06, local_rate=0.01, niit_rate=0.038, muni_in_state=True)
    result = analyze_bond_tax(BondTaxInputs(ASSET_MUNI, 3.6), assumptions)
    assert result.applicable_tax_rate == pytest.approx(0.0)
    assert result.after_tax_yield_pct == pytest.approx(3.6)
    assert result.taxable_equivalent_yield_pct == pytest.approx(3.6 / (1 - (0.32 + 0.06 + 0.01 + 0.038)))


def test_treasury_is_federally_taxable_and_state_exempt():
    result = analyze_bond_tax(BondTaxInputs(ASSET_TREASURY, 4.0), FED_24)
    assert result.applicable_tax_rate == pytest.approx(0.24 + 0.038)
    assert result.after_tax_yield_pct == pytest.approx(4.0 * (1 - 0.278))


def test_corporate_is_fully_taxable():
    result = analyze_bond_tax(BondTaxInputs(ASSET_CORPORATE, 5.0), FED_24)
    assert result.applicable_tax_rate == pytest.approx(0.24 + 0.05 + 0.038)
    assert result.after_tax_yield_pct == pytest.approx(5.0 * (1 - 0.328))


def test_amt_pab_muni_loses_federal_exemption():
    assumptions = TaxAssumptions(federal_rate=0.24, state_rate=0.05, local_rate=0.0, niit_rate=0.038, muni_in_state=True, muni_amt_or_pab=True)
    result = analyze_bond_tax(BondTaxInputs(ASSET_MUNI, 4.0), assumptions)
    assert result.applicable_tax_rate == pytest.approx(0.24 + 0.038)
    assert result.after_tax_yield_pct == pytest.approx(4.0 * (1 - 0.278))


def test_missing_tax_inputs_leave_after_tax_missing():
    result = analyze_bond_tax(BondTaxInputs(ASSET_CORPORATE, 5.0), TaxAssumptions(federal_rate=0.24))
    assert result.pretax_yield_pct == 5.0
    assert result.after_tax_yield_pct is None
    assert result.missing_reason == "missing_tax_inputs"


def test_missing_yield_stays_missing():
    result = analyze_bond_tax(BondTaxInputs(ASSET_MUNI, None), FED_24)
    assert result.after_tax_yield_pct is None
    assert result.missing_reason == "missing_yield"


def test_callable_uses_ytw():
    result = analyze_bond_tax(BondTaxInputs(ASSET_CORPORATE, 6.0, ytw_pct=4.5, callable=True), FED_24)
    assert result.yield_basis == "ytw"
    assert result.pretax_yield_pct == pytest.approx(4.5)


def test_combined_override_for_corporate():
    assumptions = TaxAssumptions(combined_override=0.40)
    corp = analyze_bond_tax(BondTaxInputs(ASSET_CORPORATE, 5.0), assumptions)
    assert corp.applicable_tax_rate == pytest.approx(0.40)
    assert corp.after_tax_yield_pct == pytest.approx(3.0)


def test_income_and_three_way_compare():
    comparison = compare_three(
        BondTaxInputs(ASSET_MUNI, 3.5, duration=7.0),
        BondTaxInputs(ASSET_TREASURY, 4.0, duration=7.0),
        BondTaxInputs(ASSET_CORPORATE, 5.2, duration=7.0),
        TaxAssumptions(federal_rate=0.24, state_rate=0.05, local_rate=0.0, niit_rate=0.038, muni_in_state=True),
        investment_amount=100_000,
    )
    muni = comparison["results"]["muni"]
    treasury = comparison["results"]["treasury"]
    assert muni["after_tax_yield_pct"] == pytest.approx(3.5)
    assert treasury["after_tax_yield_pct"] == pytest.approx(4.0 * (1 - 0.278))
    assert comparison["spreads"]["muni_minus_treasury_after_tax"] == pytest.approx(3.5 - 4.0 * (1 - 0.278))
    assert comparison["income"]["muni"]["after_tax_income"] == pytest.approx(3500.0)
    assert after_tax_yield_per_duration(analyze_bond_tax(BondTaxInputs(ASSET_MUNI, 3.5, duration=7), TaxAssumptions(federal_rate=0.24, state_rate=0.05, local_rate=0.0, niit_rate=0.038, muni_in_state=True)), 7) == pytest.approx(3.5 / 7)
    assert muni_treasury_ratio(3.5, 4.0) == pytest.approx(0.875)
    assert muni_treasury_ratio(3.5, 0) is None


def test_invalid_rate_raises():
    with pytest.raises(BondTaxError):
        analyze_bond_tax(BondTaxInputs(ASSET_TREASURY, 4.0), TaxAssumptions(federal_rate=1.2, state_rate=0, local_rate=0, niit_rate=0))


def test_income_missing_amount():
    assert income_on_amount(None, 4.0, 0.3)["gross_interest"] is None


def test_income_breakdown_splits_known_exemptions():
    muni = income_breakdown(100_000, 3.5, TaxAssumptions(federal_rate=0.32, state_rate=0.06, local_rate=0.0, niit_rate=0.038, muni_in_state=True), ASSET_MUNI)
    assert muni["gross_interest"] == pytest.approx(3500.0)
    assert muni["federal_tax"] == pytest.approx(0.0)
    assert muni["state_local_tax"] == pytest.approx(0.0)
    assert muni["after_tax_income"] == pytest.approx(3500.0)
    treasury = income_breakdown(100_000, 4.0, FED_24, ASSET_TREASURY)
    assert treasury["state_local_tax"] == pytest.approx(0.0)
    assert treasury["federal_tax"] == pytest.approx(4000.0 * 0.24)
    assert treasury["niit_tax"] == pytest.approx(4000.0 * 0.038)
