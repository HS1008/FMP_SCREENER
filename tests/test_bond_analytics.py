"""Bond analytics (bounded domain) and disabled external adapters.

Pure-math tests run everywhere; the stored-bond batch test needs FMP_TEST_DATABASE_URL.
"""

from __future__ import annotations

import io
import json
import urllib.request
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from market_intelligence import adapters, bonds
from market_intelligence.bonds import BondAnalyticsError, BondTerms

FLAT_4 = {"DGS1": 4.0, "DGS2": 4.0, "DGS5": 4.0, "DGS10": 4.0, "DGS30": 4.0}


def _fixed(coupon=5.0, freq=2, maturity=date(2030, 1, 15), **kw) -> BondTerms:
    return BondTerms(bond_id="TEST", coupon_rate_pct=coupon, coupon_frequency=freq, maturity_date=maturity, settlement_days=0, **kw)


# ---- pure math ---------------------------------------------------------------------------------------


def test_par_bond_on_coupon_date_yields_its_coupon_and_roundtrips():
    terms = _fixed()
    settle = date(2025, 1, 15)
    assert bonds.price_from_yield(terms, settle, 5.0) == pytest.approx(100.0, abs=1e-9)
    assert bonds.yield_from_price(terms, settle, 100.0) == pytest.approx(5.0, abs=1e-8)
    for y in (0.5, 3.0, 7.25, 12.0):
        p = bonds.price_from_yield(terms, settle, y)
        assert bonds.yield_from_price(terms, settle, p) == pytest.approx(y, abs=1e-8)
    # Price/yield inverse relation.
    assert bonds.price_from_yield(terms, settle, 6.0) < 100.0 < bonds.price_from_yield(terms, settle, 4.0)


def test_duration_matches_closed_form_for_par_bond():
    terms = _fixed()
    settle = date(2025, 1, 15)
    mac, mod, conv = bonds.durations(terms, settle, 5.0, 100.0)
    # Closed form for a par bond: Macaulay = (1+y)/y * (1 - 1/(1+y)^n) in periods, y=2.5%, n=10.
    y, n = 0.025, 10
    expected_periods = (1 + y) / y * (1 - (1 + y) ** (-n))
    assert mac == pytest.approx(expected_periods / 2, rel=1e-9)
    assert mod == pytest.approx(mac / (1 + y), rel=1e-12)
    assert conv > 0
    # Modified duration approximates dP/dy: bump 1bp.
    p_up = bonds.price_from_yield(terms, settle, 5.01)
    p_dn = bonds.price_from_yield(terms, settle, 4.99)
    numeric_mod = -(p_up - p_dn) / (2 * 0.0001) / 100.0
    assert numeric_mod == pytest.approx(mod, rel=1e-4)


def test_accrued_interest_30_360_and_act_act():
    terms = _fixed(day_count="30/360")
    # Coupon 2.5 per period; 3 months of a 6-month period = 90/180.
    assert bonds.accrued_interest(terms, date(2025, 4, 15)) == pytest.approx(1.25)
    assert bonds.accrued_interest(terms, date(2025, 1, 15)) == 0.0
    aa = _fixed(day_count="ACT/ACT")
    # Jan 15 -> Apr 15 = 90 actual days over 181 (Jan 15 -> Jul 15).
    assert bonds.accrued_interest(aa, date(2025, 4, 15)) == pytest.approx(2.5 * 90 / 181)
    # Dirty = clean + accrued and the between-coupon yield stays continuous around par.
    mid = bonds.analyze_bond(terms, as_of=date(2025, 4, 15), clean_price=100.0)
    assert mid.dirty_price == pytest.approx(101.25)
    assert mid.ytm == pytest.approx(5.0, abs=0.05)


def test_zero_coupon_bond_has_no_accrued_and_ytm_from_discount():
    zero = BondTerms(bond_id="Z", coupon_rate_pct=0.0, coupon_frequency=2, maturity_date=date(2030, 1, 15), coupon_type="ZERO", settlement_days=0)
    result = bonds.analyze_bond(zero, as_of=date(2025, 1, 15), clean_price=78.35)
    assert result.accrued_interest == 0.0
    t = (date(2030, 1, 15) - date(2025, 1, 15)).days / 365.0
    expected = 2 * ((100 / 78.35) ** (1 / (2 * t)) - 1) * 100
    assert result.ytm == pytest.approx(expected, abs=1e-6)
    assert result.macaulay_duration == pytest.approx(t, rel=1e-9)
    assert result.support_status == bonds.SUPPORT_PARTIAL  # no curve -> spreads unavailable, disclosed
    with_curve = bonds.analyze_bond(zero, as_of=date(2025, 1, 15), clean_price=78.35, treasury_curve=FLAT_4)
    assert with_curve.support_status == bonds.SUPPORT_FULL and with_curve.z_spread_bps is not None


def test_flat_curve_gives_flat_zeros_and_zero_spread_for_curve_priced_bond():
    zeros = bonds.bootstrap_zero_curve(FLAT_4, max_years=10)
    assert len(zeros) == 20
    assert all(z == pytest.approx(4.0, abs=1e-9) for _, z in zeros)
    terms = _fixed()
    settle = date(2025, 1, 15)
    clean = bonds.price_from_yield(terms, settle, 4.0) - bonds.accrued_interest(terms, settle)
    result = bonds.analyze_bond(terms, as_of=date(2025, 1, 15), clean_price=clean, treasury_curve=FLAT_4)
    assert result.g_spread_bps == pytest.approx(0.0, abs=1e-6)
    assert result.z_spread_bps == pytest.approx(0.0, abs=1e-6)
    par = bonds.analyze_bond(terms, as_of=date(2025, 1, 15), clean_price=100.0, treasury_curve=FLAT_4)
    assert par.g_spread_bps == pytest.approx(100.0, abs=1e-6)
    assert par.z_spread_bps == pytest.approx(100.0, abs=0.5)
    assert par.oas_bps is None
    assert par.as_row()["detail_json"]["oas_status"] == bonds.OAS_STATUS


def test_g_spread_requires_curve_coverage_and_interpolates_linearly():
    assert bonds.interpolate_par_yield({"DGS2": 4.0, "DGS10": 5.0}, 6.0) == pytest.approx(4.5)
    assert bonds.interpolate_par_yield({"DGS2": 4.0, "DGS10": 5.0}, 1.0) is None
    assert bonds.interpolate_par_yield({"DGS2": 4.0, "DGS10": 5.0}, 12.0) is None
    assert bonds.interpolate_par_yield({"DGS2": 4.0}, 2.0) is None
    long_bond = _fixed(maturity=date(2050, 1, 15))
    result = bonds.analyze_bond(long_bond, as_of=date(2025, 1, 15), clean_price=100.0, treasury_curve={"DGS2": 4.0, "DGS10": 5.0})
    assert result.g_spread_bps is None and result.z_spread_bps is None
    # Spreads unavailable -> PARTIAL, never FULL; the reasons are explicit.
    assert result.support_status == bonds.SUPPORT_PARTIAL
    assert result.detail["statuses"]["g_spread"] == "CURVE_COVERAGE_INSUFFICIENT"
    assert result.detail["statuses"]["z_spread"] == "CURVE_COVERAGE_INSUFFICIENT"
    assert result.detail["curve_coverage"]["tenor_count"] == 2
    no_curve = bonds.analyze_bond(_fixed(), as_of=date(2025, 1, 15), clean_price=100.0)
    assert no_curve.g_spread_bps is None and no_curve.z_spread_bps is None
    assert no_curve.detail["spreads"] == "no treasury curve supplied" and no_curve.support_status == bonds.SUPPORT_PARTIAL
    assert no_curve.detail["statuses"] == {"ytm": "OK", "durations": "OK", "g_spread": "NO_CURVE", "z_spread": "NO_CURVE"}


# ---- review finding: unverified root-finding -----------------------------------------------------------


def test_reviewed_defect_price_1_on_one_year_bond_is_out_of_domain_not_200pct_yield():
    terms = BondTerms(bond_id="X", coupon_rate_pct=5.0, coupon_frequency=2, maturity_date=date(2025, 1, 1), settlement_days=0)
    settle = date(2024, 1, 1)
    with pytest.raises(bonds.OutOfDomain):
        bonds.yield_from_price(terms, settle, 1.00)
    result = bonds.analyze_bond(terms, as_of=settle, clean_price=1.00 - bonds.accrued_interest(terms, settle), treasury_curve=FLAT_4)
    assert result.support_status == bonds.SUPPORT_UNSUPPORTED_PRICE_DOMAIN
    assert result.ytm is None and result.modified_duration is None and result.g_spread_bps is None and result.z_spread_bps is None
    assert result.dirty_price == pytest.approx(1.0) and result.detail["statuses"]["ytm"] == "OUT_OF_DOMAIN"
    assert "no root inside the supported domain" in result.detail["reason"]


def test_every_published_yield_reprices_within_tolerance_across_domain():
    terms = _fixed(maturity=date(2040, 1, 15))
    settle = date(2025, 3, 3)  # between coupon dates
    lo, hi = bonds.YIELD_DOMAIN_PCT
    for y in (lo + 0.01, -3.0, 0.0, 0.01, 2.5, 9.99, 25.0, 80.0, hi - 0.01):
        dirty = bonds.price_from_yield(terms, settle, y)
        solved = bonds.yield_from_price(terms, settle, dirty)
        assert solved == pytest.approx(y, abs=1e-7)
        assert bonds.price_from_yield(terms, settle, solved) == pytest.approx(dirty, abs=bonds.REPRICE_TOL)
    # Just outside the domain on either side is refused rather than clamped.
    with pytest.raises(bonds.OutOfDomain):
        bonds.yield_from_price(terms, settle, bonds.price_from_yield(terms, settle, hi + 1.0))
    with pytest.raises(bonds.OutOfDomain):
        bonds.yield_from_price(terms, settle, bonds.price_from_yield(terms, settle, lo - 1.0))


def test_distressed_but_in_domain_bond_gets_verified_values():
    terms = _fixed()
    result = bonds.analyze_bond(terms, as_of=date(2025, 1, 15), clean_price=30.0, treasury_curve=FLAT_4)
    assert result.support_status == bonds.SUPPORT_FULL
    assert 30 < result.ytm < 50
    assert result.detail["repriced_dirty"] == pytest.approx(30.0, abs=bonds.REPRICE_TOL)
    assert result.z_spread_bps == pytest.approx((result.ytm - 4.0) * 100.0, rel=0.02)  # flat 4% curve


def test_nonfinite_negative_and_impossible_prices_never_yield_numbers():
    terms = _fixed()
    for bad in (float("nan"), float("inf"), -float("inf")):
        result = bonds.analyze_bond(terms, as_of=date(2025, 1, 15), clean_price=bad, treasury_curve=FLAT_4)
        assert result.support_status == bonds.SUPPORT_UNSUPPORTED_PRICE_DOMAIN and result.ytm is None and result.clean_price is None
    negative = bonds.analyze_bond(terms, as_of=date(2025, 4, 15), clean_price=-5.0)
    assert negative.support_status == bonds.SUPPORT_UNSUPPORTED_PRICE_DOMAIN and negative.ytm is None
    with pytest.raises(BondAnalyticsError):
        bonds.yield_from_price(terms, date(2025, 1, 15), 0.0)
    with pytest.raises(BondAnalyticsError):
        bonds.yield_from_price(terms, date(2025, 1, 15), float("nan"))
    # A price above the -10% yield price is an impossible (out-of-domain) negative yield.
    too_rich = bonds.price_from_yield(terms, date(2025, 1, 15), bonds.YIELD_DOMAIN_PCT[0]) + 1.0
    result = bonds.analyze_bond(terms, as_of=date(2025, 1, 15), clean_price=too_rich)
    assert result.support_status == bonds.SUPPORT_UNSUPPORTED_PRICE_DOMAIN
    # Mildly negative yields are inside the domain.
    slightly_rich = bonds.price_from_yield(terms, date(2025, 1, 15), -0.5)
    ok = bonds.analyze_bond(terms, as_of=date(2025, 1, 15), clean_price=slightly_rich)
    assert ok.ytm == pytest.approx(-0.5, abs=1e-7)
    with pytest.raises(BondAnalyticsError):
        BondTerms(bond_id="N", coupon_rate_pct=float("nan"), coupon_frequency=2, maturity_date=date(2030, 1, 1)).validate()
    with pytest.raises(BondAnalyticsError):
        BondTerms(bond_id="N", coupon_rate_pct=5.0, coupon_frequency=2, maturity_date=date(2030, 1, 1), redemption=float("inf")).validate()


def test_z_spread_requires_curve_spanning_maturity_and_enough_tenors_and_reprices():
    terms = _fixed(maturity=date(2035, 1, 15))  # 10y
    settle = date(2025, 1, 15)
    # Only 4 tenors but longest is 5y: extrapolation would be needed -> refused.
    short_curve = {"DGS1": 4.0, "DGS2": 4.1, "DGS3": 4.2, "DGS5": 4.3}
    result = bonds.analyze_bond(terms, as_of=settle, clean_price=95.0, treasury_curve=short_curve)
    assert result.z_spread_bps is None and result.detail["statuses"]["z_spread"] == "CURVE_COVERAGE_INSUFFICIENT"
    assert "beyond longest quoted tenor" in result.detail["z_spread_error"]
    sparse = {"DGS2": 4.0, "DGS10": 4.5, "DGS30": 4.7}
    result = bonds.analyze_bond(terms, as_of=settle, clean_price=95.0, treasury_curve=sparse)
    assert result.z_spread_bps is None and "quoted tenors" in result.detail["z_spread_error"]
    assert result.g_spread_bps is not None  # G-spread only needs interpolation coverage
    good = {"DGS1": 4.0, "DGS2": 4.1, "DGS3": 4.2, "DGS5": 4.3, "DGS7": 4.4, "DGS10": 4.5}
    result = bonds.analyze_bond(terms, as_of=settle, clean_price=95.0, treasury_curve=good)
    assert result.support_status == bonds.SUPPORT_FULL and result.detail["curve_coverage"]["short_end_flat_extrapolation"] is True
    # Independent re-pricing of the published Z-spread over the same zero curve.
    zeros = bonds.bootstrap_zero_curve(good, max_years=10)
    s = result.z_spread_bps / 1e4
    pv = sum(a / (1 + (bonds._zero_rate_at(zeros, t) / 100 + s) / 2) ** (2 * t) for t, a in bonds.cashflows(terms, settle))
    assert pv == pytest.approx(result.dirty_price, abs=bonds.REPRICE_TOL)
    # A Z-spread beyond +5000bp is refused, not clamped to the bracket edge.
    deep = bonds.analyze_bond(terms, as_of=settle, clean_price=2.0, treasury_curve=good)
    assert deep.z_spread_bps is None and deep.detail["statuses"]["z_spread"] in ("OUT_OF_DOMAIN", "NOT_COMPUTED")


# ---- independent references (Microsoft Excel documentation examples) ------------------------------------


def test_matches_excel_price_yield_duration_documented_examples():
    # PRICE(2008-02-15, 2017-11-15, 5.75%, 6.5%, 100, 2, basis 0 = US 30/360) = 94.63436162
    terms = BondTerms(bond_id="XL", coupon_rate_pct=5.75, coupon_frequency=2, maturity_date=date(2017, 11, 15), settlement_days=0)
    settle = date(2008, 2, 15)
    clean = bonds.price_from_yield(terms, settle, 6.5) - bonds.accrued_interest(terms, settle)
    assert clean == pytest.approx(94.63436162, abs=1e-6)
    # YIELD(2008-02-15, 2016-11-15, 5.75%, 95.04287, 100, 2, basis 0) = 6.5%
    terms2 = BondTerms(bond_id="XL2", coupon_rate_pct=5.75, coupon_frequency=2, maturity_date=date(2016, 11, 15), settlement_days=0)
    assert bonds.yield_from_price(terms2, settle, 95.04287 + bonds.accrued_interest(terms2, settle)) == pytest.approx(6.5, abs=1e-5)
    # DURATION / MDURATION(2008-01-01, 2016-01-01, 8%, 9%, 2, basis 1 = ACT/ACT) = 5.993775 / 5.73567
    terms3 = BondTerms(bond_id="XL3", coupon_rate_pct=8.0, coupon_frequency=2, maturity_date=date(2016, 1, 1), day_count="ACT/ACT", settlement_days=0)
    dirty = bonds.price_from_yield(terms3, date(2008, 1, 1), 9.0)
    mac, mod, _ = bonds.durations(terms3, date(2008, 1, 1), 9.0, dirty)
    assert mac == pytest.approx(5.993775, abs=1e-6) and mod == pytest.approx(5.73567, abs=1e-5)
    # Convexity cross-check by central finite difference of the price function.
    y = 9.0
    p0 = bonds.price_from_yield(terms3, date(2008, 1, 1), y)
    p_up = bonds.price_from_yield(terms3, date(2008, 1, 1), y + 0.01)
    p_dn = bonds.price_from_yield(terms3, date(2008, 1, 1), y - 0.01)
    numeric_conv = (p_up + p_dn - 2 * p0) / (p0 * 0.0001**2)
    _, _, conv = bonds.durations(terms3, date(2008, 1, 1), y, p0)
    assert conv == pytest.approx(numeric_conv, rel=1e-3)


# ---- schedules: first coupon, stubs, EOM, holidays --------------------------------------------------------


def test_first_coupon_date_defines_short_stub_accrual_and_first_cash_flow():
    terms = BondTerms(bond_id="S", coupon_rate_pct=5.0, coupon_frequency=2, maturity_date=date(2030, 1, 15), settlement_days=0, issue_date=date(2024, 3, 1), first_coupon_date=date(2024, 7, 15))
    settle = date(2024, 5, 1)
    sched = bonds.build_schedule(terms, settle)
    assert sched.stub == "SHORT_FIRST" and sched.period_start == date(2024, 3, 1) and sched.period_end == date(2024, 7, 15)
    # 30/360: 134 stub days of a 180-day regular period -> first coupon 2.5 * 134/180.
    assert sched.first_coupon_amount == pytest.approx(2.5 * 134 / 180)
    assert bonds.accrued_interest(terms, settle) == pytest.approx(2.5 * 60 / 180)
    flows = bonds.cashflows(terms, settle)
    assert flows[0][1] == pytest.approx(sched.first_coupon_amount) and flows[1][1] == pytest.approx(2.5)
    assert flows[0][0] == pytest.approx((74 / 180) / 2)  # 74 30/360-days to the first coupon
    assert len(flows) == 12
    result = bonds.analyze_bond(terms, as_of=settle, clean_price=100.0)
    assert result.ytm == pytest.approx(5.0, abs=0.01) and result.detail["schedule"]["stub"] == "SHORT_FIRST"
    # After the first coupon the schedule is regular again and the stub no longer matters.
    later = bonds.build_schedule(terms, date(2025, 3, 1))
    assert later.stub == "NONE" and later.period_start == date(2025, 1, 15)
    # Long first coupon: issue before the notional period start.
    long_terms = BondTerms(bond_id="L", coupon_rate_pct=6.0, coupon_frequency=2, maturity_date=date(2030, 1, 15), settlement_days=0, issue_date=date(2023, 12, 1), first_coupon_date=date(2024, 7, 15))
    long_sched = bonds.build_schedule(long_terms, date(2024, 2, 1))
    assert long_sched.stub == "LONG_FIRST" and long_sched.first_coupon_amount == pytest.approx(3.0 * 224 / 180)
    assert bonds.cashflows(long_terms, date(2024, 2, 1))[0][0] == pytest.approx((164 / 180) / 2)  # 164 30/360-days to first coupon
    assert bonds.accrued_interest(long_terms, date(2024, 2, 1)) == pytest.approx(3.0 * 60 / 180)  # accrues from issue


def test_first_coupon_date_is_validated_not_ignored():
    with pytest.raises(BondAnalyticsError, match="requires issue_date"):
        BondTerms(bond_id="A", coupon_rate_pct=5.0, coupon_frequency=2, maturity_date=date(2030, 1, 15), first_coupon_date=date(2024, 7, 15)).validate()
    with pytest.raises(BondAnalyticsError, match="after issue_date"):
        BondTerms(bond_id="A", coupon_rate_pct=5.0, coupon_frequency=2, maturity_date=date(2030, 1, 15), issue_date=date(2024, 8, 1), first_coupon_date=date(2024, 7, 15)).validate()
    irregular = BondTerms(bond_id="I", coupon_rate_pct=5.0, coupon_frequency=2, maturity_date=date(2030, 1, 15), settlement_days=0, issue_date=date(2024, 3, 1), first_coupon_date=date(2024, 8, 1))
    result = bonds.analyze_bond(irregular, as_of=date(2024, 5, 1), clean_price=100.0)
    assert result.support_status == bonds.SUPPORT_UNSUPPORTED_TERMS and result.ytm is None and "irregular" in result.detail["reason"]
    # Inside the first period without a pinned first coupon date: refuse to guess.
    unknown = BondTerms(bond_id="U", coupon_rate_pct=5.0, coupon_frequency=2, maturity_date=date(2030, 1, 15), settlement_days=0, issue_date=date(2024, 3, 1))
    result = bonds.analyze_bond(unknown, as_of=date(2024, 5, 1), clean_price=100.0)
    assert result.support_status == bonds.SUPPORT_UNSUPPORTED_TERMS and "refusing to guess" in result.detail["reason"]
    # ...but the same bond after its first coupon is regular and supported.
    assert bonds.analyze_bond(unknown, as_of=date(2025, 3, 1), clean_price=100.0).ytm is not None
    # ACT/ACT stubs are out of scope.
    aa = BondTerms(bond_id="AA", coupon_rate_pct=5.0, coupon_frequency=2, maturity_date=date(2030, 1, 15), settlement_days=0, day_count="ACT/ACT", issue_date=date(2024, 3, 1), first_coupon_date=date(2024, 7, 15))
    assert "ACT/ACT" in bonds.analyze_bond(aa, as_of=date(2024, 5, 1), clean_price=100.0).detail["reason"]
    # Row-level terms carry first_coupon_date through.
    row = {"bond_id": "R", "coupon_rate": 5, "coupon_frequency": 2, "maturity_date": date(2030, 1, 15), "issue_date": date(2024, 3, 1), "first_coupon_date": date(2024, 7, 15), "terms_status": "SOURCE_PROVIDED"}
    assert bonds.terms_from_row(row).first_coupon_date == date(2024, 7, 15)


def test_end_of_month_roll_and_unadjusted_default():
    eom = BondTerms(bond_id="E", coupon_rate_pct=4.0, coupon_frequency=2, maturity_date=date(2030, 2, 28), settlement_days=0, end_of_month=True)
    assert bonds.coupon_dates(eom, date(2025, 1, 15))[:3] == [date(2025, 2, 28), date(2025, 8, 31), date(2026, 2, 28)]
    plain = BondTerms(bond_id="P", coupon_rate_pct=4.0, coupon_frequency=2, maturity_date=date(2030, 2, 28), settlement_days=0)
    assert bonds.coupon_dates(plain, date(2025, 1, 15))[:3] == [date(2025, 2, 28), date(2025, 8, 28), date(2026, 2, 28)]
    # 31st maturity: backward roll clamps to month ends without the flag (30/31 clamp) and with it.
    m31 = BondTerms(bond_id="M", coupon_rate_pct=4.0, coupon_frequency=4, maturity_date=date(2030, 1, 31), settlement_days=0)
    assert bonds.coupon_dates(m31, date(2029, 3, 1))[:3] == [date(2029, 4, 30), date(2029, 7, 31), date(2029, 10, 31)]
    assert bonds.terms_from_row({"bond_id": "R", "coupon_rate": 5, "coupon_frequency": 2, "maturity_date": date(2030, 1, 31), "terms_status": "VERIFIED", "business_day_convention": "END_OF_MONTH"}).end_of_month is True


def test_settlement_skips_supplied_holidays_and_labels_calendar():
    # 2025-01-20 is a US holiday (MLK day) in reality; the default calendar is weekends only and says so.
    assert bonds.settlement_from(date(2025, 1, 17), 1) == date(2025, 1, 20)
    assert bonds.settlement_from(date(2025, 1, 17), 1, holidays={date(2025, 1, 20)}) == date(2025, 1, 21)
    t1 = BondTerms(bond_id="T1", coupon_rate_pct=5.0, coupon_frequency=2, maturity_date=date(2030, 1, 15), settlement_days=1)
    result = bonds.analyze_bond(t1, as_of=date(2025, 1, 17), clean_price=100.0)
    assert result.detail["settlement_calendar"] == "WEEKENDS_ONLY" and result.settlement_date == date(2025, 1, 20)
    with_hol = bonds.analyze_bond(t1, as_of=date(2025, 1, 17), clean_price=100.0, holidays=[date(2025, 1, 20)])
    assert with_hol.settlement_date == date(2025, 1, 21) and with_hol.detail["settlement_calendar"] == "WEEKENDS_PLUS_1_HOLIDAYS"
    assert result.detail["payment_date_adjustment"] == "IGNORED_STREET_CONVENTION"


def test_act_360_coupons_are_actual_days_over_basis_and_accrued_consistent():
    terms = BondTerms(bond_id="A360", coupon_rate_pct=6.0, coupon_frequency=2, maturity_date=date(2026, 1, 15), settlement_days=0, day_count="ACT/360")
    settle = date(2025, 2, 14)  # 30 actual days after 2025-01-15
    assert bonds.accrued_interest(terms, settle) == pytest.approx(6.0 * 30 / 360)
    flows = bonds.cashflows(terms, settle)
    # First coupon covers Jan 15 -> Jul 15 (181 days); second Jul 15 -> Jan 15 (184 days) + redemption.
    assert flows[0][1] == pytest.approx(6.0 * 181 / 360) and flows[1][1] == pytest.approx(6.0 * 184 / 360 + 100)
    assert flows[0][0] == pytest.approx((151 / 181) / 2)


def test_unsupported_structures_are_flagged_not_faked():
    callable_bond = _fixed(callable=True)
    result = bonds.analyze_bond(callable_bond, as_of=date(2025, 1, 15), clean_price=100.0, treasury_curve=FLAT_4)
    assert result.support_status == bonds.SUPPORT_YTM_ONLY_CALLABLE
    assert result.ytm == pytest.approx(5.0, abs=1e-8)
    assert result.modified_duration is None and result.g_spread_bps is None and result.z_spread_bps is None
    floater = BondTerms(bond_id="F", coupon_rate_pct=None, coupon_frequency=4, maturity_date=date(2030, 1, 15), coupon_type="FLOATING", settlement_days=0)
    result = bonds.analyze_bond(floater, as_of=date(2025, 1, 15), clean_price=99.0)
    assert result.support_status == bonds.SUPPORT_UNSUPPORTED_FLOATING
    assert result.ytm is None and result.dirty_price is None
    with pytest.raises(BondAnalyticsError):
        _fixed(freq=3).validate()
    with pytest.raises(BondAnalyticsError):
        _fixed(day_count="ACT/ACT/ISDA").validate()
    with pytest.raises(BondAnalyticsError):
        BondTerms(bond_id="X", coupon_rate_pct=None, coupon_frequency=2, maturity_date=date(2030, 1, 1)).validate()
    with pytest.raises(BondAnalyticsError, match="maturity"):
        bonds.analyze_bond(_fixed(), as_of=date(2030, 1, 15), clean_price=100.0)


def test_terms_from_row_refuses_unverified_or_incomplete_terms():
    base = {"bond_id": "B1", "coupon_rate": "5", "coupon_frequency": 2, "maturity_date": date(2030, 1, 15), "terms_status": "SOURCE_PROVIDED", "coupon_type": "FIXED", "day_count": "30/360"}
    assert bonds.terms_from_row(base).coupon_rate_pct == 5.0
    with pytest.raises(BondAnalyticsError, match="not source-verified"):
        bonds.terms_from_row(dict(base, terms_status="UNVERIFIED"))
    with pytest.raises(BondAnalyticsError, match="incomplete"):
        bonds.terms_from_row(dict(base, maturity_date=None))


def test_settlement_skips_weekends():
    assert bonds.settlement_from(date(2025, 1, 17), 1) == date(2025, 1, 20)  # Fri -> Mon
    assert bonds.settlement_from(date(2025, 1, 15), 2) == date(2025, 1, 17)
    assert bonds.settlement_from(date(2025, 1, 15), 0) == date(2025, 1, 15)


# ---- external adapters ------------------------------------------------------------------------------


def test_adapters_are_disabled_or_configuration_required_and_refuse_to_fetch():
    statuses = adapters.probe_all({})
    assert set(statuses) == {"IBKR_MARKET_DATA", "FINRA_TRACE", "SEC_EDGAR"}
    assert statuses["IBKR_MARKET_DATA"].access_status == adapters.ACCESS_DISABLED
    assert statuses["FINRA_TRACE"].access_status == adapters.ACCESS_DISABLED
    assert statuses["SEC_EDGAR"].access_status == adapters.ACCESS_CONFIGURATION_REQUIRED
    assert not any(s.enabled for s in statuses.values())
    with pytest.raises(adapters.AdapterDisabled):
        adapters.IBKRMarketDataAdapter().fetch_quotes(["TLT"], env={"MI_IBKR_MARKET_DATA_ENABLED": "1"})
    assert not any(name.lower().startswith(("place", "order", "submit")) for name in dir(adapters.IBKRMarketDataAdapter))
    trace = adapters.TraceAdapter()
    assert trace.probe({"MI_TRACE_ENABLED": "1"}).access_status == adapters.ACCESS_CONFIGURATION_REQUIRED
    assert trace.probe({"MI_TRACE_ENABLED": "1", "FINRA_API_CLIENT_ID": "x", "FINRA_API_CLIENT_SECRET": "y"}).access_status == adapters.ACCESS_ENTITLEMENT_REQUIRED
    with pytest.raises(adapters.AdapterDisabled):
        trace.fetch_trades(["912828ZT0"], env={"MI_TRACE_ENABLED": "1", "FINRA_API_CLIENT_ID": "x", "FINRA_API_CLIENT_SECRET": "y"})


def test_edgar_requires_contact_user_agent_and_opt_in_and_never_calls_when_disabled():
    calls: list[urllib.request.Request] = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        calls.append(request)
        return _Resp(json.dumps({"cik": 1318605, "name": "TEST CO"}).encode())

    edgar = adapters.EdgarAdapter(opener=opener, min_interval_s=0)
    assert edgar.probe({}).access_status == adapters.ACCESS_CONFIGURATION_REQUIRED
    assert edgar.probe({"SEC_USER_AGENT": "NoContactHere"}).access_status == adapters.ACCESS_CONFIGURATION_REQUIRED
    assert edgar.probe({"SEC_USER_AGENT": "FMP Research ops@example.com"}).access_status == adapters.ACCESS_DISABLED
    with pytest.raises(adapters.AdapterDisabled):
        edgar.submissions("1318605", env={"SEC_USER_AGENT": "FMP Research ops@example.com"})
    assert calls == []
    env = {"SEC_USER_AGENT": "FMP Research ops@example.com", "MI_EDGAR_ENABLED": "1"}
    assert edgar.probe(env).enabled is True
    payload = edgar.submissions("1318605", env=env)
    assert payload["name"] == "TEST CO"
    assert calls[0].full_url == "https://data.sec.gov/submissions/CIK0001318605.json"
    assert calls[0].get_header("User-agent") == "FMP Research ops@example.com"
    with pytest.raises(ValueError):
        adapters.EdgarAdapter.normalize_cik("not-a-cik")


def test_refresh_plan_reports_external_adapter_status_without_db(capsys):
    from jobs.market_intelligence_refresh import run

    code = run(["--all-configured", "--dry-run", "--json"], env={"HOME": "/tmp"})
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    external = payload["plan"]["external_adapters"]
    assert external["IBKR_MARKET_DATA"]["access_status"] == "DISABLED"
    assert external["SEC_EDGAR"]["access_status"] == "CONFIGURATION_REQUIRED"
    assert not any(v["enabled"] for v in external.values())


# ---- stored bonds (PostgreSQL) -------------------------------------------------------------------------


def _seed_curve(conn, as_of: date) -> None:
    from market_intelligence import store

    store.upsert_source_registry(conn)
    for sid, value in FLAT_4.items():
        store.upsert_macro_series(
            conn,
            series_id=sid,
            source_id="FRED",
            provider_series_id=sid,
            spec_fields={"category": "rates", "subcategory": "nominal_curve", "catalog_version": "fred_catalog_v1", "source_url": "https://fred.stlouisfed.org/series/{0}".format(sid), "notes": "", "export_scope": "ATTRIBUTION_REQUIRED", "expected_frequency": "D"},
            meta={"title": sid, "units": "Percent", "frequency_short": "D", "seasonal_adjustment_short": "NSA"},
            metadata_status="VALIDATED",
            mismatches=None,
        )
        store.upsert_observations(conn, series_id=sid, rows=[store.ObservationInput(as_of, str(value))], retrieved_at=datetime(2025, 1, 15, 22, tzinfo=timezone.utc), run_id=None)


def _seed_bond(conn, bond_id: str, *, terms_status: str = "SOURCE_PROVIDED", callable_flag: bool = False, quote: tuple[float | None, float | None] | None = (99.5, 100.5), last: float | None = None, quote_ts: str = "2025-01-14T21:00:00+00:00") -> None:
    conn.execute(
        text(
            """
            INSERT INTO mi_bond_securities (bond_id, issuer_name, currency, coupon_type, coupon_rate, coupon_frequency, issue_date, maturity_date,
                day_count, settlement_days, redemption, callable, putable, source_id, terms_status)
            VALUES (:bond_id, 'Test Issuer', 'USD', 'FIXED', 5.0, 2, '2020-01-15', '2030-01-15', '30/360', 1, 100, :callable, FALSE, 'SEC_EDGAR', :terms_status)
            """
        ),
        {"bond_id": bond_id, "callable": callable_flag, "terms_status": terms_status},
    )
    if quote is not None or last is not None:
        bid, ask = quote if quote is not None else (None, None)
        conn.execute(
            text(
                """
                INSERT INTO mi_bond_quotes (bond_id, source_id, quote_ts, price_kind, bid_price, ask_price, last_price, price_per, delay_status, entitlement_status, retrieved_at)
                VALUES (:bond_id, 'TEST_QUOTES', :quote_ts, 'MID', :bid, :ask, :last, '100', 'DELAYED', 'TEST', NOW())
                """
            ),
            {"bond_id": bond_id, "bid": bid, "ask": ask, "last": last, "quote_ts": quote_ts},
        )


def test_stored_bond_batch_uses_fred_curve_and_reports_skips(mi_db):
    as_of = date(2025, 1, 14)
    with mi_db.begin() as conn:
        _seed_curve(conn, as_of)
        _seed_bond(conn, "OK_BULLET")
        _seed_bond(conn, "CALLABLE", callable_flag=True)
        _seed_bond(conn, "UNVERIFIED", terms_status="UNVERIFIED")
        _seed_bond(conn, "NO_QUOTE", quote=None)
        _seed_bond(conn, "LAST_ONLY", quote=None, last=98.0, quote_ts="2025-01-10T21:00:00+00:00")
        _seed_bond(conn, "DISTRESSED_OOD", quote=(0.5, 1.5))
        report = bonds.compute_stored_bond_analytics(conn, as_of=as_of)
    assert report.curve_date == as_of and report.curve_points == len(FLAT_4)
    assert report.curve_tenors == list(FLAT_4) and "DGS3MO" in report.curve_missing_tenors
    assert report.computed == 4
    assert report.support == {bonds.SUPPORT_FULL: 2, bonds.SUPPORT_YTM_ONLY_CALLABLE: 1, bonds.SUPPORT_UNSUPPORTED_PRICE_DOMAIN: 1}
    skipped = {s["bond_id"]: s["reason"] for s in report.skipped}
    assert "not source-verified" in skipped["UNVERIFIED"]
    assert "no quote" in skipped["NO_QUOTE"]
    with mi_db.connect() as conn:
        rows = conn.execute(text("SELECT bond_id, settlement_date, price_kind, clean_price, ytm, g_spread_bps, z_spread_bps, oas_bps, support_status, analytics_version, detail_json FROM mi_bond_analytics ORDER BY bond_id")).mappings().all()
    by_id = {r["bond_id"]: r for r in rows}
    assert set(by_id) == {"OK_BULLET", "CALLABLE", "LAST_ONLY", "DISTRESSED_OOD"}
    assert {r["analytics_version"] for r in rows} == {bonds.ANALYTICS_VERSION}
    ok = by_id["OK_BULLET"]
    assert ok["settlement_date"] == date(2025, 1, 15)
    assert float(ok["clean_price"]) == pytest.approx(100.0)
    assert float(ok["ytm"]) == pytest.approx(5.0, abs=1e-6)
    assert float(ok["g_spread_bps"]) == pytest.approx(100.0, abs=1e-4)
    assert ok["oas_bps"] is None
    assert ok["detail_json"]["oas_status"] == bonds.OAS_STATUS
    assert ok["detail_json"]["curve_date"] == "2025-01-14" and ok["detail_json"]["quote_status"] == "SAME_DAY"
    assert ok["price_kind"] == "MID" and ok["detail_json"]["price_kind_fallback"] is False
    assert by_id["CALLABLE"]["g_spread_bps"] is None
    # A MID request served from a LAST print is stored as LAST (review finding), with its age disclosed.
    last_only = by_id["LAST_ONLY"]
    assert last_only["price_kind"] == "LAST" and last_only["detail_json"]["requested_price_kind"] == "MID" and last_only["detail_json"]["price_kind_fallback"] is True
    assert last_only["detail_json"]["quote_status"] == "STALE_4D" and last_only["detail_json"]["quote_age_days"] == 4
    # Impossible price is recorded with NULL analytics and an explicit status, not a 150% yield.
    ood = by_id["DISTRESSED_OOD"]
    assert ood["support_status"] == bonds.SUPPORT_UNSUPPORTED_PRICE_DOMAIN and ood["ytm"] is None and ood["z_spread_bps"] is None
    # Idempotent re-run: same rows, no duplicates.
    with mi_db.begin() as conn:
        again = bonds.compute_stored_bond_analytics(conn, as_of=as_of)
    with mi_db.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM mi_bond_analytics")).scalar()
    assert again.computed == 4 and count == 4


def test_bond_analytics_job_dry_run_writes_nothing(mi_db, capsys):
    from jobs.bond_analytics import run

    with mi_db.begin() as conn:
        _seed_curve(conn, date(2025, 1, 14))
        _seed_bond(conn, "OK_BULLET")
    assert run(["--as-of", "2025-01-14", "--dry-run", "--json"], engine=mi_db) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "DRY_RUN" and payload["computed"] == 1
    with mi_db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM mi_bond_analytics")).scalar() == 0
    assert run(["--as-of", "2025-01-14", "--json"], engine=mi_db) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "SUCCEEDED"
    with mi_db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM mi_bond_analytics")).scalar() == 1
        run_row = conn.execute(text("SELECT status, dataset FROM mi_ingestion_runs WHERE source_id = 'ANALYTICS' AND dataset = 'bond_analytics'")).first()
    assert run_row is not None and run_row[0] == "SUCCEEDED"
