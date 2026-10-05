"""Formulas for the labor, housing, growth, and fiscal expansion."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import math

from market_intelligence.components.market_chart import build_market_chart_payload
from market_intelligence.macro_dashboard import aligned_ratio, scale_level
from market_intelligence.transforms import ann3m_pct, mom_change_ma3, qoq_annualized_pct, sum_12m, yoy_pct
from market_intelligence.treasury_auctions import classify_auction, monthly_gross_issuance
from market_intelligence.macro_dashboard import prepare_line_points


def test_payroll_change_average_uses_changes_not_levels():
    levels = {
        date(2024, 1, 1): 100.0,
        date(2024, 2, 1): 110.0,
        date(2024, 3, 1): 130.0,
        date(2024, 4, 1): 160.0,
    }
    result = mom_change_ma3(levels, date(2024, 4, 1), units="thousands")
    assert result.value == 20.0
    assert result.units == "thousands"
    incomplete = dict(levels)
    del incomplete[date(2024, 3, 1)]
    assert mom_change_ma3(incomplete, date(2024, 4, 1), units="thousands").value is None


def test_openings_per_unemployed_uses_matching_dates_and_units():
    openings = [
        {"as_of": date(2024, 1, 1), "value": 6000.0},
        {"as_of": date(2024, 2, 1), "value": 4000.0},
        {"as_of": date(2024, 3, 1), "value": 5000.0},
    ]
    unemployed = [
        {"as_of": date(2024, 1, 1), "value": 4000.0},
        {"as_of": date(2024, 3, 1), "value": 0.0},
        {"as_of": date(2024, 4, 1), "value": 4500.0},
    ]
    points = aligned_ratio(openings, unemployed)
    assert points == [{"as_of": date(2024, 1, 1), "value": 1.5}]


def test_wage_and_house_price_growth_use_calendar_lags():
    levels = {date(2023, month, 1): 100.0 for month in range(1, 13)}
    levels[date(2024, 1, 1)] = 101.0
    levels[date(2024, 4, 1)] = 104.0
    yoy = yoy_pct(levels, date(2024, 1, 1))
    assert math.isclose(yoy.value, 1.0)
    annualized = ann3m_pct(levels, date(2024, 4, 1))
    assert math.isclose(annualized.value, 100.0 * ((104.0 / 101.0) ** 4 - 1.0))
    assert yoy_pct({date(2024, 4, 1): 104.0}, date(2024, 4, 1)).value is None


def test_real_gdp_qoq_annualization_and_consumer_yoy():
    quarterly = {date(2024, 1, 1): 100.0, date(2024, 4, 1): 101.0}
    growth = qoq_annualized_pct(quarterly, date(2024, 4, 1))
    assert math.isclose(growth.value, 100.0 * ((101.0 / 100.0) ** 4 - 1.0))
    income = {date(2023, 6, 1): 200.0, date(2024, 6, 1): 206.0}
    assert math.isclose(yoy_pct(income, date(2024, 6, 1)).value, 3.0)


def test_quarterly_delinquency_is_not_forward_filled():
    rows = [
        {"as_of": date(2024, 1, 1), "value": 2.7},
        {"as_of": date(2024, 4, 1), "value": 2.8},
    ]
    points = prepare_line_points(rows, start=date(2024, 2, 1), end=date(2024, 3, 31))
    assert points == []
    kept = prepare_line_points(rows, start=date(2024, 1, 1), end=date(2024, 4, 1))
    assert [point["as_of"] for point in kept] == [date(2024, 1, 1), date(2024, 4, 1)]


def test_trailing_twelve_month_deficit_sign_and_matching_windows():
    months = {date(2024, month, 1): -100.0 for month in range(1, 13)}
    deficit = sum_12m(months, date(2024, 12, 1), units="millions")
    assert deficit.value == -1200.0
    assert scale_level("MTSDS133FMS.sum_12m", deficit.value) == 1200.0 / 1_000_000.0
    surplus = {day: 50.0 for day in months}
    assert scale_level("MTSDS133FMS.sum_12m", sum_12m(surplus, date(2024, 12, 1), units="millions").value) < 0
    gapped = dict(months)
    del gapped[date(2024, 6, 1)]
    assert sum_12m(gapped, date(2024, 12, 1), units="millions").value is None
    receipts = sum_12m({day: 10.0 for day in months}, date(2024, 12, 1), units="millions")
    outlays = sum_12m({day: 30.0 for day in months}, date(2024, 12, 1), units="millions")
    assert receipts.value == 120.0
    assert outlays.value == 360.0
    assert scale_level("MTSR133FMS.sum_12m", receipts.value) == 120.0 / 1_000_000.0
    assert scale_level("MTSO133FMS.sum_12m", outlays.value) == 360.0 / 1_000_000.0


def test_interest_burden_matches_quarterly_saar_observations():
    interest = [{"as_of": date(2024, 1, 1), "value": 800.0}, {"as_of": date(2024, 4, 1), "value": 900.0}]
    receipts = [{"as_of": date(2024, 1, 1), "value": 4000.0}, {"as_of": date(2024, 7, 1), "value": 4100.0}]
    points = aligned_ratio(interest, receipts, scale=100.0)
    assert points == [{"as_of": date(2024, 1, 1), "value": 20.0}]


def test_treasury_security_classification_and_monthly_gross():
    assert classify_auction({"security_type": "Bill"}) == "Bill"
    assert classify_auction({"security_type": "Note"}) == "Note"
    assert classify_auction({"security_type": "Bond"}) == "Bond"
    assert classify_auction({"security_type": "Note", "inflation_index_security": "Yes"}) is None
    assert classify_auction({"security_type": "Note", "floating_rate": "Yes"}) is None
    assert classify_auction({"security_type": "Bill", "cash_management_bill_cmb": "Yes"}) is None
    assert classify_auction({"security_type": "TIPS"}) is None
    assert classify_auction({"security_type": "FRN"}) is None
    assert classify_auction({"security_type": ""}) is None
    rows = [
        {"auction_date": "2024-06-03", "security_type": "Bill", "total_accepted": "100"},
        {"auction_date": "2024-06-18", "security_type": "Bill", "cash_management_bill_cmb": "Yes", "total_accepted": "999"},
        {"auction_date": "2024-06-20", "security_type": "Note", "inflation_index_security": "Yes", "total_accepted": "500"},
        {"auction_date": "2024-06-21", "security_type": "Note", "floating_rate": "No", "total_accepted": "40"},
        {"auction_date": "2024-06-25", "security_type": "Bond", "total_accepted": "null"},
        {"auction_date": "2024-07-11", "security_type": "Bond", "total_accepted": "25"},
        {"auction_date": "2024-08-01", "security_type": "Bill", "total_accepted": None},
    ]
    monthly = monthly_gross_issuance(rows, start=date(2024, 6, 1), end=date(2024, 8, 31))
    by_series = {series_id: dict(points) for series_id, points in monthly.items()}
    assert by_series["TREAS_GROSS_BILL"][date(2024, 6, 1)] == Decimal("100")
    assert by_series["TREAS_GROSS_BILL"][date(2024, 7, 1)] == Decimal("0")
    assert by_series["TREAS_GROSS_NOTE"][date(2024, 6, 1)] == Decimal("40")
    assert by_series["TREAS_GROSS_BOND"][date(2024, 6, 1)] == Decimal("0")
    assert by_series["TREAS_GROSS_BOND"][date(2024, 7, 1)] == Decimal("25")
    assert date(2024, 8, 1) in by_series["TREAS_GROSS_BILL"]


def test_histogram_and_left_axis_are_optional_payload_fields():
    plain = build_market_chart_payload([{"time": "2024-01-01", "value": 1.0}], series_label="Level")
    assert "style" not in plain["series"][0]
    assert "priceScale" not in plain["series"][0]
    styled = build_market_chart_payload(
        series=[
            {"label": "Change", "points": [{"time": "2024-01-01", "value": 10}], "style": "histogram"},
            {"label": "Claims", "points": [{"time": "2024-01-01", "value": 20}], "price_scale": "left"},
        ],
        align_union=False,
    )
    assert styled["series"][0]["style"] == "histogram"
    assert styled["series"][1]["priceScale"] == "left"
