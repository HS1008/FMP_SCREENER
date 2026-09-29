"""Calculation tests for the US Equities index, sector, and subsector views."""

from __future__ import annotations

import inspect
from datetime import date, timedelta

import pytest

from market_intelligence.components.tenor_chart import build_column_scaled_heatmap_option
from market_intelligence.markets_analytics import (
    MIN_SUBSECTOR_CONSTITUENTS,
    build_aligned_us_panel,
    classify_return,
    column_color_scale,
    column_color_scales,
    heatmap_cell_color,
    normalize_to_100,
    price_ratio_points,
    rebalanced_basket_return,
    return_spread,
    running_peak_drawdown,
    session_window_returns,
    shared_session_bounds,
    snapshot_rejection_reason,
    subsector_matrix,
)
from market_intelligence.markets_read import load_equity_eod_closes
from market_intelligence.taxonomy import KIND_CUSTOM_BASKET, KIND_THEME, cross_sector_themes, stock_subsector_baskets


def test_indexed_to_100_uses_the_first_price_as_the_base():
    assert normalize_to_100([100, 105, 110]) == [100, 105, 110]
    assert normalize_to_100([200, 210, 220]) == [100.0, 105.0, 110.0]
    assert normalize_to_100([None, 50, 75]) == [None, 100.0, 150.0]


def test_price_ratio_rises_when_rsp_outperforms_spy():
    rsp = [(date(2024, 1, 2), 100.0), (date(2024, 1, 3), 102.0)]
    spy = [(date(2024, 1, 2), 100.0), (date(2024, 1, 3), 101.0)]
    points = price_ratio_points(rsp, spy)
    assert points[1][1] > points[0][1]
    assert points[1][1] == pytest.approx(102.0 / 101.0)


def test_relative_sector_performance_is_a_return_spread():
    assert return_spread(0.05, 0.03) == pytest.approx(0.02)
    assert return_spread(0.02, 0.04) == pytest.approx(-0.02)
    assert return_spread(0.05, None) is None
    assert return_spread(None, 0.03) is None


def test_running_peak_drawdown_matches_the_price_path():
    path = running_peak_drawdown([100, 110, 99, 121, 108.9])
    assert path == pytest.approx([0.0, 0.0, -0.10, 0.0, -0.10])
    assert running_peak_drawdown([100, None, 90])[1] is None
    assert running_peak_drawdown([100, None, 90])[2] == pytest.approx(-0.10)


def test_each_heatmap_column_has_its_own_scale():
    scales = column_color_scales([[0.02, 0.30], [-0.02, -0.10]])
    assert scales[0]["max_abs"] == pytest.approx(0.02)
    assert scales[1]["max_abs"] == pytest.approx(0.30)
    assert heatmap_cell_color(0.02, scales[0]["max_abs"]) == "rgb(61, 140, 90)"
    assert heatmap_cell_color(0.02, scales[1]["max_abs"]) != heatmap_cell_color(0.02, scales[0]["max_abs"])
    flat = column_color_scale([0.0, 0.0])
    assert flat["max_abs"] == 0.0
    assert flat["degenerate"] is True
    assert heatmap_cell_color(0.0, 0.0) == "rgb(44, 48, 54)"
    single = column_color_scale([0.04])
    assert single["max_abs"] == pytest.approx(0.04)
    assert column_color_scale([None, None])["max_abs"] == 0.0


def test_column_scaled_heatmap_prints_percentages_and_keeps_scales_apart():
    option = build_column_scaled_heatmap_option(
        ["Technology", "Energy"],
        ["1D", "1Y"],
        [[0.0124, 0.1462], [-0.02, None]],
        notes=[["Constituents: 4", None], [None, None]],
    )
    cells = option["series"][0]["data"]
    by_key = {(cell["row"], cell["column"]): cell for cell in cells}
    assert by_key[("Technology", "1D")]["display"] == "+1.24%"
    assert by_key[("Technology", "1Y")]["display"] == "+14.62%"
    assert by_key[("Energy", "1Y")]["display"] == "N/A"
    assert by_key[("Technology", "1D")]["itemStyle"]["color"] == heatmap_cell_color(0.0124, 0.02)
    assert by_key[("Technology", "1Y")]["itemStyle"]["color"] == "rgb(61, 140, 90)"
    assert by_key[("Technology", "1D")]["itemStyle"]["color"] != heatmap_cell_color(0.0124, 0.1462)
    assert by_key[("Energy", "1D")]["itemStyle"]["color"] == "rgb(179, 64, 64)"
    assert by_key[("Technology", "1D")]["note"] == "Constituents: 4"
    assert "visualMap" not in option


def test_return_sign_classification():
    assert classify_return(0.01) == "positive"
    assert classify_return(-0.01) == "negative"
    assert classify_return(0) == "neutral"
    assert classify_return(0.0) == "neutral"
    assert classify_return(None) == "unavailable"
    assert classify_return(float("nan")) == "unavailable"


def test_session_gaps_do_not_fabricate_returns():
    """A weekend is one session step. A multi-day hole is not a stitched 1D return."""
    weekend = {
        date(2024, 1, 2): 100.0,
        date(2024, 1, 3): 100.0,
        date(2024, 1, 4): 100.0,
        date(2024, 1, 5): 100.0,
        date(2024, 1, 8): 110.0,
    }
    monday = session_window_returns(weekend, date(2024, 1, 8))
    assert monday["1D"] == pytest.approx(0.10)
    assert monday["1W"] is None
    gapped = {
        date(2024, 1, 2): 100.0,
        date(2024, 1, 3): 100.0,
        date(2024, 1, 4): 100.0,
        date(2024, 1, 5): 100.0,
        date(2024, 1, 16): 110.0,
    }
    hole = session_window_returns(gapped, date(2024, 1, 16))
    assert hole["1D"] is None
    assert hole["1M"] is None
    six = {
        date(2024, 1, 2): 100.0,
        date(2024, 1, 3): 100.0,
        date(2024, 1, 4): 100.0,
        date(2024, 1, 5): 100.0,
        date(2024, 1, 8): 100.0,
        date(2024, 1, 9): 110.0,
    }
    week = session_window_returns(six, date(2024, 1, 9))
    assert week["1W"] == pytest.approx(0.10)
    assert week["1M"] is None
    assert week["1Y"] is None


def _record(prices: dict[date, float], basis: str = "IBKR_ADJUSTED_LAST") -> dict:
    return {"prices": prices, "adjustment_basis": basis, "provider": "IBKR"}


def test_rebalanced_basket_is_not_the_mean_of_holding_period_returns():
    calendar = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    members = {
        "AAA": {calendar[0]: 100.0, calendar[1]: 110.0, calendar[2]: 110.0},
        "BBB": {calendar[0]: 100.0, calendar[1]: 100.0, calendar[2]: 120.0},
    }
    basket = rebalanced_basket_return(members, calendar, calendar[0], calendar[2])
    holding_period_mean = ((110.0 / 100.0 - 1.0) + (120.0 / 100.0 - 1.0)) / 2.0
    assert basket == pytest.approx(0.155)
    assert basket != pytest.approx(holding_period_mean)
    assert MIN_SUBSECTOR_CONSTITUENTS == 2
    gapped_calendar = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 16)]
    missing_session = {
        "AAA": {gapped_calendar[0]: 100.0, gapped_calendar[1]: 110.0, gapped_calendar[3]: 110.0},
        "BBB": {gapped_calendar[0]: 100.0, gapped_calendar[1]: 100.0, gapped_calendar[3]: 120.0},
    }
    assert rebalanced_basket_return(missing_session, gapped_calendar, gapped_calendar[0], gapped_calendar[3]) is None
    skipped = {
        "AAA": {gapped_calendar[0]: 100.0, gapped_calendar[3]: 110.0},
        "BBB": {gapped_calendar[0]: 100.0, gapped_calendar[3]: 120.0},
    }
    assert rebalanced_basket_return(skipped, gapped_calendar, gapped_calendar[0], gapped_calendar[3]) is None
    quiet = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    unobserved = {
        "AAA": {quiet[0]: 100.0, quiet[2]: 110.0},
        "BBB": {quiet[0]: 100.0, quiet[2]: 120.0},
    }
    assert rebalanced_basket_return(unobserved, quiet, quiet[0], quiet[2]) is None
    hole = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 16), date(2024, 1, 17)]
    assert shared_session_bounds(hole, 1) == (date(2024, 1, 16), date(2024, 1, 17))
    assert shared_session_bounds(hole, 5) is None
    assert shared_session_bounds([date(2024, 1, 5), date(2024, 1, 8)], 1) == (date(2024, 1, 5), date(2024, 1, 8))


def test_nyse_calendar_rejects_a_missing_weekday_and_keeps_a_holiday():
    """Tuesday to Thursday skips Wednesday. Friday to the Tuesday after Memorial Day does not."""
    missing = session_window_returns({date(2024, 1, 2): 100.0, date(2024, 1, 4): 110.0}, date(2024, 1, 4))
    assert missing["1D"] is None
    assert shared_session_bounds([date(2024, 1, 2), date(2024, 1, 4)], 1) is None
    holiday = session_window_returns({date(2024, 5, 24): 100.0, date(2024, 5, 28): 110.0}, date(2024, 5, 28))
    assert holiday["1D"] == pytest.approx(0.10)
    assert shared_session_bounds([date(2024, 5, 24), date(2024, 5, 28)], 1) == (date(2024, 5, 24), date(2024, 5, 28))
    mourning = session_window_returns({date(2025, 1, 8): 100.0, date(2025, 1, 10): 110.0}, date(2025, 1, 10))
    assert mourning["1D"] == pytest.approx(0.10)
    new_year = session_window_returns({date(2021, 12, 30): 100.0, date(2022, 1, 3): 110.0}, date(2022, 1, 3))
    assert new_year["1D"] == pytest.approx(0.10)
    sandy = session_window_returns({date(2012, 10, 26): 100.0, date(2012, 10, 31): 90.0}, date(2012, 10, 31))
    assert sandy["1D"] == pytest.approx(-0.10)


def test_aligned_panel_rejects_stale_missing_and_foreign_basis_series():
    dates = [date(2024, 1, 2) + timedelta(days=offset) for offset in range(6)]
    endpoint = dates[-1]
    spy = {day: 100.0 for day in dates}
    spy[endpoint] = 101.0
    nvda = {day: 100.0 for day in dates}
    nvda[endpoint] = 110.0
    amd = {day: 100.0 for day in dates[1:]}
    amd[dates[-2]] = 100.0
    amd[endpoint] = 90.0
    xlk = {day: 200.0 for day in dates}
    xlk[dates[-2]] = 202.0
    xlk[endpoint] = 206.0
    xlb = {dates[0]: 50.0, endpoint: 55.0}
    records = {
        "SPY": _record(spy),
        "XLK": _record(xlk),
        "XLE": _record({day: 40.0 for day in dates[:-1]}),
        "XLB": _record(xlb),
        "XLU": _record({day: 30.0 for day in dates}, basis="SPLIT_ADJUSTED_UNKNOWN_DIVIDEND"),
        "NVDA": _record(nvda),
        "AMD": _record(amd),
    }
    panel = build_aligned_us_panel(records)
    assert panel["available"] is True
    assert panel["endpoint"] == endpoint
    assert panel["windows"]["1D"] == {"start": dates[-2], "end": endpoint}
    assert panel["windows"]["1W"] == {"start": dates[0], "end": endpoint}
    assert panel["windows"]["1Y"] is None
    assert panel["spy_returns"]["1D"] == pytest.approx(101.0 / 100.0 - 1.0)
    assert panel["spy_returns"]["1W"] == pytest.approx(0.01)
    tech = next(row for row in panel["sectors"] if row["label"] == "Technology")
    energy = next(row for row in panel["sectors"] if row["label"] == "Energy")
    materials = next(row for row in panel["sectors"] if row["label"] == "Materials")
    utilities = next(row for row in panel["sectors"] if row["label"] == "Utilities")
    assert tech["values"][0] == pytest.approx(206.0 / 202.0 - 1.0)
    assert tech["values"][1] == pytest.approx(206.0 / 200.0 - 1.0)
    assert energy["values"][0] is None
    assert "Stale or missing endpoint" in energy["notes"][0]
    assert materials["values"][0] is None
    assert materials["values"][1] == pytest.approx(55.0 / 50.0 - 1.0)
    assert utilities["values"][0] is None
    assert utilities["notes"][0] == "Incompatible adjustment basis"
    matrix = subsector_matrix(panel["sectors"], panel["spy_returns"], mode="relative")
    relative_tech = next(row for row in matrix["rows"] if row["label"] == "Technology")
    assert relative_tech["values"][0] == pytest.approx(tech["values"][0] - panel["spy_returns"]["1D"])
    assert matrix["scales"][0]["max_abs"] != matrix["scales"][5]["max_abs"] or matrix["scales"][5]["max_abs"] == 0
    ai = next(row for row in panel["subsectors"]["Technology"] if row["industry"] == "AI Compute / GPUs")
    assert ai["classification"] == "curated_basket"
    assert ai["values"][0] == pytest.approx(0.0)
    assert ai["counts"][0] == 2
    assert ai["values"][1] is None
    assert ai["counts"][1] == 1
    assert ai["values"][5] is None
    assert "Financials" not in panel["subsectors"]
    labels = {row["industry"] for rows in panel["subsectors"].values() for row in rows}
    assert "Cloud / Data Infrastructure" not in labels
    assert "Cloud / Data Infrastructure" in panel["themes_omitted"]
    stale = snapshot_rejection_reason(
        {"as_of": dates[0].isoformat(), "adjustment_basis": "IBKR_ADJUSTED_LAST", "metrics": {"ret_1d": 0.99}},
        endpoint=endpoint,
        adjustment_basis="IBKR_ADJUSTED_LAST",
    )
    assert stale == "stale_endpoint"
    assert tech["values"][0] != pytest.approx(0.99)
    foreign = snapshot_rejection_reason(
        {"as_of": endpoint, "adjustment_basis": "SPLIT_ADJUSTED_UNKNOWN_DIVIDEND", "metrics": {"ret_1d": 0.99}},
        endpoint=endpoint,
        adjustment_basis="IBKR_ADJUSTED_LAST",
    )
    assert foreign == "incompatible_adjustment_basis"
    aligned_snapshot = snapshot_rejection_reason(
        {"as_of": endpoint, "adjustment_basis": "IBKR_ADJUSTED_LAST", "metrics": {"ret_1d": 0.99}},
        endpoint=endpoint,
        adjustment_basis="IBKR_ADJUSTED_LAST",
    )
    assert aligned_snapshot == "snapshot_not_canonical"


def test_stale_constituent_does_not_set_the_basket_endpoint():
    dates = [date(2024, 1, 2) + timedelta(days=offset) for offset in range(6)]
    endpoint = dates[-1]
    spy = {day: 100.0 for day in dates}
    nvda = {day: 100.0 for day in dates}
    nvda[endpoint] = 110.0
    amd = {day: 100.0 for day in dates[:-1]}
    panel = build_aligned_us_panel(
        {"SPY": _record(spy), "NVDA": _record(nvda), "AMD": _record(amd)}
    )
    ai = next(row for row in panel["subsectors"]["Technology"] if row["industry"] == "AI Compute / GPUs")
    assert ai["values"][0] is None
    assert ai["counts"][0] == 1
    amd_row = next(member for member in ai["constituents"] if member["symbol"] == "AMD")
    assert amd_row["rejection"] == "stale_or_missing_endpoint"


def test_taxonomy_keeps_curated_baskets_and_names_themes():
    located = [
        (basket.parent_sector, basket.label)
        for basket in stock_subsector_baskets()
        for symbol in basket.members
        if symbol == "NVDA"
    ]
    assert ("Technology", "AI Compute / GPUs") in located
    assert all(sector != "Financials" for sector, _label in located)
    assert all(basket.kind == KIND_CUSTOM_BASKET for basket in stock_subsector_baskets())
    assert all(basket.kind == KIND_THEME for basket in cross_sector_themes())
    assert "Cloud / Data Infrastructure" in {basket.label for basket in cross_sector_themes()}
    assert "Cloud / Data Infrastructure" not in {basket.label for basket in stock_subsector_baskets()}


def test_constituent_close_query_is_read_only_and_equity_eod_only():
    source = inspect.getsource(load_equity_eod_closes)
    assert "mi_v_equity_daily_closes" in source
    assert "source_id = 'EQUITY_EOD'" in source
    assert "adjustment_basis" in source
    assert "mi_market_bars" not in source
    assert "INSERT" not in source.upper()
    assert "UPDATE" not in source.upper()
