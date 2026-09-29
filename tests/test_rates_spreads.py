"""TIPS curve, 2s10s, 2s5s10s butterfly, and the fed funds target-range overlay."""

from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path

import pytest

from market_intelligence.analytics import active_curve_slope_names, active_fly_enabled
from market_intelligence.catalog import (
    CATALOG_BY_ID,
    CREDIT_SERIES,
    CURVE_TENORS,
    FED_FUNDS_TARGET_LOWER,
    FED_FUNDS_TARGET_UPPER,
    FLY_2S5S10S_METRIC,
    RATES_MAX_BACKFILL_SERIES,
    SLOPE_10Y2Y_METRIC,
    TIPS_TENORS,
)
from market_intelligence.components.market_chart import build_market_chart_payload, time_series_points
from market_intelligence.components.tenor_chart import build_category_line_option
from market_intelligence.curve_compare import (
    FED_FUNDS_UNAVAILABLE,
    fed_funds_overlay,
    resolve_complete_date,
    resolve_fed_funds_target,
    tips_observation_dates,
    tips_points_for_date,
)
from market_intelligence.freshness import SERIES_POLICIES
from market_intelligence.history_range import filter_history_rows, union_history_bounds
from market_intelligence.transforms import curve_butterfly, curve_slope

TENORS = list(CURVE_TENORS)
TIPS_ORDER = ("5Y", "10Y", "20Y", "30Y")
CHANGE = date(2022, 6, 16)


def _range_maps():
    lower = {
        date(2022, 5, 5): 0.25,
        date(2022, 6, 15): 0.25,
        CHANGE: 0.75,
        date(2022, 7, 28): 1.50,
    }
    upper = {
        date(2022, 5, 5): 0.50,
        date(2022, 6, 15): 0.50,
        CHANGE: 1.00,
        date(2022, 7, 28): 1.75,
    }
    return lower, upper


def test_catalog_maps_fed_funds_target_range_and_tips_order():
    lower = CATALOG_BY_ID[FED_FUNDS_TARGET_LOWER]
    upper = CATALOG_BY_ID[FED_FUNDS_TARGET_UPPER]
    assert lower.series_id == "DFEDTARL"
    assert upper.series_id == "DFEDTARU"
    assert lower.category == "policy" and upper.category == "policy"
    assert lower.value_kind == "percent" and upper.value_kind == "percent"
    assert "not the effective federal funds rate" in lower.notes.lower()
    assert CATALOG_BY_ID["DFF"].series_id == "DFF"
    assert CATALOG_BY_ID["DFF"].label != lower.label
    assert list(TIPS_TENORS) == list(TIPS_ORDER)
    assert list(TIPS_TENORS.values()) == ["DFII5", "DFII10", "DFII20", "DFII30"]
    assert "DFII7" not in TIPS_TENORS.values()
    assert SLOPE_10Y2Y_METRIC == "curve.slope_10Y2Y_bps"
    assert FLY_2S5S10S_METRIC == "curve.fly_2s5s10s_bps"
    assert FED_FUNDS_TARGET_LOWER in RATES_MAX_BACKFILL_SERIES
    assert FED_FUNDS_TARGET_UPPER in RATES_MAX_BACKFILL_SERIES
    assert SERIES_POLICIES["DFEDTARL"].observation_lag_sessions == 0
    assert SERIES_POLICIES["DFEDTARL"].same_day_available is True
    assert SERIES_POLICIES["DFF"].observation_lag_sessions == 1
    assert active_curve_slope_names(set(CREDIT_SERIES)) == []
    assert active_fly_enabled(set(CREDIT_SERIES)) is False
    assert active_curve_slope_names(set(RATES_MAX_BACKFILL_SERIES)) == ["10Y2Y"]
    assert active_fly_enabled(set(RATES_MAX_BACKFILL_SERIES)) is True


def test_treasury_axis_stays_maturity_only_when_policy_band_is_attached():
    series = [{"name": "Current", "values": [4.1, 4.15, 4.2, 4.25, 4.3, 4.35, 4.4, 4.5, 4.7, 4.8]}]
    bands = fed_funds_overlay(
        {"available": True, "curve_date": "2026-09-25", "effective_date": "2026-09-18", "lower": 4.25, "upper": 4.50, "carried": True}
    )["bands"]
    option = build_category_line_option(TENORS, series, y_title="percent", bands=bands, point_notes=["Fed funds target", "Lower: 4.25%", "Upper: 4.50%"])
    assert option["xAxis"]["data"] == TENORS
    assert "Fed funds lower" not in option["xAxis"]["data"]
    assert "Fed funds upper" not in option["xAxis"]["data"]
    mark = option["series"][0]["markLine"]["data"]
    assert {row["yAxis"] for row in mark} == {4.25, 4.50}
    assert option["series"][0]["markArea"]["data"]
    notes = option["series"][0]["data"][0]["notes"]
    assert "Fed funds target" in notes
    assert "Lower: 4.25%" in notes
    assert "Upper: 4.50%" in notes
    plain = build_category_line_option(TENORS, series)
    assert "markLine" not in plain["series"][0]
    assert "markArea" not in plain["series"][0]


def test_current_and_historical_target_range_never_looks_forward():
    lower, upper = _range_maps()
    before = resolve_fed_funds_target(lower, upper, date(2022, 6, 15))
    on_change = resolve_fed_funds_target(lower, upper, CHANGE)
    later = resolve_fed_funds_target(lower, upper, date(2022, 6, 17))
    assert before["lower"] == 0.25 and before["upper"] == 0.50
    assert before["effective_date"] == "2022-06-15"
    assert on_change["lower"] == 0.75 and on_change["upper"] == 1.00
    assert on_change["effective_date"] == "2022-06-16"
    assert later["lower"] == 0.75 and later["upper"] == 1.00
    assert later["carried"] is True
    assert resolve_fed_funds_target(lower, upper, date(2000, 1, 3)) is None
    mixed_upper = {date(2022, 6, 15): 0.50}
    mixed_lower = {date(2022, 6, 16): 0.75, date(2022, 6, 15): 0.25}
    held = resolve_fed_funds_target(mixed_lower, mixed_upper, date(2022, 6, 16))
    assert held["effective_date"] == "2022-06-15"
    assert held["lower"] == 0.25


def test_comparison_ranges_collapse_when_unchanged_and_stay_distinct_otherwise():
    current = {"available": True, "curve_date": "2026-09-25", "effective_date": "2026-09-18", "lower": 4.25, "upper": 4.50, "carried": True}
    same = {"available": True, "curve_date": "2026-06-01", "effective_date": "2026-06-01", "lower": 4.25, "upper": 4.50, "carried": False}
    other = {"available": True, "curve_date": "2022-06-01", "effective_date": "2022-05-05", "lower": 0.25, "upper": 0.50, "carried": True}
    unchanged = fed_funds_overlay(current, same)
    assert unchanged["unchanged"] is True
    assert len(unchanged["bands"]) == 1
    assert "unchanged" in unchanged["caption"].lower()
    changed = fed_funds_overlay(current, other)
    assert changed["unchanged"] is False
    assert [band["role"] for band in changed["bands"]] == ["current", "compare"]
    assert changed["bands"][0]["fill"] is True
    assert changed["bands"][1]["fill"] is True
    assert changed["bands"][0]["legend_label"] == "Fed Funds Range — Current"
    assert changed["bands"][1]["legend_label"] == "Fed Funds Range — Comparison"
    assert "2022" in changed["caption"] or "Jun 1, 2022" in changed["caption"]
    missing = fed_funds_overlay({"available": False}, other)
    assert missing["bands"] == []
    assert missing["caption"] == FED_FUNDS_UNAVAILABLE
    assert 0 not in [band.get("lower") for band in missing["bands"]]


def test_tips_curve_keeps_one_date_and_leaves_missing_tenors_missing():
    day = date(2024, 6, 3)
    prior = date(2024, 5, 31)
    by_series = {
        "DFII5": {prior: 1.70, day: 1.80},
        "DFII10": {prior: 1.90, day: 2.00},
        "DFII20": {prior: 2.10},
        "DFII30": {prior: 2.20, day: 2.30},
    }
    points = tips_points_for_date(by_series, day)
    assert [point["tenor"] for point in points] == list(TIPS_ORDER)
    assert points[2]["yield_pct"] is None
    assert points[2]["observation_date"] is None
    assert points[0]["yield_pct"] == 1.80
    assert points[0]["observation_date"] == "2024-06-03"
    assert all(point["yield_pct"] != 2.10 for point in points)
    dates = tips_observation_dates(by_series)
    weekend = resolve_complete_date(dates, date(2024, 6, 2))
    assert weekend["effective_date"] == prior
    assert weekend["fallback"] is True
    current = resolve_complete_date(dates, day)
    comparison = resolve_complete_date(dates, prior, not_after=current["effective_date"])
    assert comparison["effective_date"] == prior
    assert comparison["effective_date"] <= current["effective_date"]
    future = resolve_complete_date(dates, date(2024, 6, 4), not_after=day)
    assert future["found"] is False


def test_2s10s_and_butterfly_use_same_date_percent_points():
    day = date(2026, 9, 25)
    two = {day: 4.00}
    five = {day: 4.20}
    ten = {day: 4.35}
    slope = curve_slope(ten, two, day)
    assert slope.value == pytest.approx(35)
    assert slope.units == "bps"
    fly = curve_butterfly(two, five, {day: 4.30}, day)
    assert fly.value == pytest.approx(10)
    assert fly.detail["formula"] == "2*DGS5 - DGS2 - DGS10"
    assert fly.detail["same_date"] is True
    negative = curve_butterfly({day: 4.00}, {day: 3.90}, {day: 4.00}, day)
    assert negative.value == pytest.approx(-20)
    zero = curve_butterfly({day: 4.00}, {day: 4.00}, {day: 4.00}, day)
    assert zero.value == pytest.approx(0)
    for missing_two, missing_five, missing_ten in (
        ({}, five, ten),
        (two, {}, ten),
        (two, five, {}),
    ):
        result = curve_butterfly(missing_two, missing_five, missing_ten, day)
        assert result.value is None
        assert result.detail["missing_legs"]
    mismatched = curve_butterfly({day: 4.00}, {date(2026, 9, 24): 4.20}, {day: 4.30}, day)
    assert mismatched.value is None
    assert "DGS5" in mismatched.detail["missing_legs"]


def test_spread_history_window_is_the_union_and_does_not_invent_edges():
    slope = [
        {"as_of": "1976-06-01", "value": 40},
        {"as_of": "2026-09-25", "value": 34},
        {"as_of": "2026-09-24", "value": None},
    ]
    fly = [
        {"as_of": "1990-01-02", "value": -18},
        {"as_of": "2026-09-25", "value": 10},
    ]
    earliest, latest = union_history_bounds([slope, fly])
    assert earliest == date(1976, 6, 1)
    assert latest == date(2026, 9, 25)
    window = filter_history_rows(slope, start=earliest, end=latest)
    assert {row["as_of"] for row in window} == {"1976-06-01", "2026-09-25", "2026-09-24"}
    assert any(row["as_of"] == "2026-09-24" and row["value"] is None for row in window)
    assert filter_history_rows(slope, start=date(2026, 9, 25), end=date(2026, 9, 25))[0]["value"] == 34
    assert filter_history_rows(slope, start=date(2026, 9, 26), end=date(2026, 9, 25)) == []


def test_lightweight_spread_payload_keeps_gaps_and_optional_zero_line():
    rows = [
        {"as_of": "2026-09-24", "value": 12},
        {"as_of": "2026-09-25", "value": None},
        {"as_of": "2026-09-26", "value": -4},
    ]
    default_points = time_series_points(rows)
    assert [point["time"] for point in default_points] == ["2026-09-24", "2026-09-26"]
    gapped = time_series_points(rows, keep_missing=True)
    assert gapped[1] == {"time": "2026-09-25"}
    payload = build_market_chart_payload(gapped, series_label="2s10s", value_format="signed_bps", reference_price=0, keep_missing=True)
    assert payload["reference_price"] == 0
    assert payload["value_format"] == "signed_bps"
    assert payload["series"][0]["points"][1] == {"time": "2026-09-25"}
    plain = build_market_chart_payload(default_points, series_label="2s10s")
    assert "reference_price" not in plain
    assert all("value" in point for point in plain["series"][0]["points"])


def test_rates_page_renders_policy_band_tips_and_spread_history(monkeypatch):
    from streamlit.testing.v1 import AppTest

    from market_intelligence.curve_compare import FED_FUNDS_UNAVAILABLE

    root = Path(__file__).resolve().parents[1]
    current = "2026-09-23"
    tenors = ("3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y")

    def curve(day: str, level: float) -> list[dict]:
        return [
            {"tenor": tenor, "yield_pct": level + index / 100, "observation_date": day, "source_id": "TREASURY", "chg_prev_bps": 1}
            for index, tenor in enumerate(tenors)
        ]

    tips_curve = {
        "found": True,
        "effective_date": current,
        "requested_date": current,
        "fallback": False,
        "curve": [
            {"tenor": "5Y", "yield_pct": 1.8, "observation_date": current},
            {"tenor": "10Y", "yield_pct": 1.9, "observation_date": current},
            {"tenor": "20Y", "yield_pct": None, "observation_date": None},
            {"tenor": "30Y", "yield_pct": 2.1, "observation_date": current},
        ],
    }
    history = {
        SLOPE_10Y2Y_METRIC: [
            {"as_of": "2024-01-02", "value": 12},
            {"as_of": "2024-01-03", "value": None},
            {"as_of": "2026-09-23", "value": 35},
        ],
        FLY_2S5S10S_METRIC: [
            {"as_of": "1990-01-02", "value": -8},
            {"as_of": "2026-09-23", "value": 10},
        ],
    }

    def fake_cached(fn_name, *args, **kwargs):
        if fn_name == "rates_context":
            return {
                "curve": curve(current, 4.0),
                "slopes": {"10Y2Y": {"value": 35, "units": "bps"}},
                "curve_dates_mixed": False,
                "complete_curve_date": current,
                "curve_observation_dates": [current],
                "source_ids": ["TREASURY"],
                "fallback": False,
                "partial_newer": [],
                "real_yields": [],
                "inflation_compensation": [],
                "policy": [],
                "tips_curve": tips_curve,
                "units_note": "Yields in percent.",
            }
        if fn_name == "fed_funds_target_on_or_before":
            if args and args[0] < "2008-12-16":
                return {"available": False, "curve_date": args[0], "lower": None, "upper": None, "message": FED_FUNDS_UNAVAILABLE}
            return {"available": True, "curve_date": args[0], "effective_date": "2026-09-18", "lower": 4.25, "upper": 4.50, "carried": True}
        if fn_name == "metric_history":
            return history.get(args[0], [])
        if fn_name == "tips_curve_on_or_before":
            return tips_curve
        if fn_name == "tips_curve_bounds":
            return {"earliest_date": "2004-01-02", "latest_date": current}
        if fn_name == "treasury_complete_curve_bounds":
            return {"earliest_complete_date": "2000-01-03", "latest_complete_date": current}
        return {}

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_cached)
    at = AppTest.from_file(str(root / "pages" / "12_Rates_Curve.py"), default_timeout=30)
    at.run()
    assert not at.exception, [exc.value for exc in at.exception]
    text = "\n".join(str(widget.value) for widget in (*at.markdown, *at.caption, *at.info, *at.subheader, *at.header))
    assert "TIPS real-yield curve" in text
    assert "2s10s Treasury Spread" in text
    assert "2s5s10s Treasury Butterfly" in text
    assert "2×5Y − 2Y − 10Y" in text
    assert "Fed funds target" in text
    assert "4.25%" in text and "4.50%" in text
    assert at.radio[0].value == "None"
    assert len(at.date_input) == 2


def test_rates_page_does_not_overlay_policy_on_tips_or_call_providers():
    from market_intelligence.pages_ui import _render_tips_curve, render_rates_curve

    treasury = inspect.getsource(render_rates_curve)
    tips = inspect.getsource(_render_tips_curve)
    assert "fed_funds_target_on_or_before" in treasury
    assert "bands=overlay" in treasury
    assert "rates-treasury-curve" in treasury
    assert "fed_funds" not in tips
    assert "bands" not in tips
    assert "rates-tips-curve" in tips
    page = inspect.getsource(render_rates_curve) + inspect.getsource(_render_tips_curve)
    assert "fred_client" not in page
    assert "yahoo" not in page.lower()
