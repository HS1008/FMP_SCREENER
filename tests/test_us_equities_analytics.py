"""Calculation tests for the US Equities index, sector, and subsector views."""

from __future__ import annotations

import inspect
from datetime import date, timedelta

import pytest

from market_intelligence.components.tenor_chart import build_column_scaled_heatmap_option
from market_intelligence.markets_analytics import (
    HORIZON_SESSIONS,
    MIN_SUBSECTOR_CONSTITUENTS,
    aggregate_subsectors,
    classify_return,
    column_color_scale,
    column_color_scales,
    compose_subsector_view,
    constituent_horizon_rows,
    equal_weight_return,
    heatmap_cell_color,
    is_constituent_subsector,
    normalize_to_100,
    price_ratio_points,
    return_spread,
    running_peak_drawdown,
    sector_heatmap_matrix,
    session_window_returns,
    snapshot_subsector_table,
    subsector_matrix,
)
from market_intelligence.markets_read import load_equity_eod_closes
from market_intelligence.taxonomy import KIND_ETF_COMPARISON, stock_subsector_baskets


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


def test_equal_weight_drops_missing_returns():
    value, count = equal_weight_return([0.10, 0.04, -0.02])
    assert value == pytest.approx(0.04)
    assert count == 3
    skipped, skipped_count = equal_weight_return([0.10, None, 0.02])
    assert skipped == pytest.approx(0.06)
    assert skipped_count == 2
    missing, missing_count = equal_weight_return([0.10, None])
    assert missing is None
    assert missing_count == 1
    assert MIN_SUBSECTOR_CONSTITUENTS == 2


def test_subsector_filter_and_relative_spread():
    rows = aggregate_subsectors(
        [
            {"symbol": "JPM", "sector": "Financials", "industry": "Banks", "returns": {"1M": 0.10, "1Y": None}},
            {"symbol": "BAC", "sector": "Financials", "industry": "Banks", "returns": {"1M": 0.04, "1Y": 0.20}},
            {"symbol": "C", "sector": "Financials", "industry": "Banks", "returns": {"1M": -0.02, "1Y": 0.10}},
            {"symbol": "NVDA", "sector": "Technology", "industry": "Software", "returns": {"1M": 0.08, "1Y": 0.40}},
            {"symbol": "MSFT", "sector": "Technology", "industry": "Software", "returns": {"1M": 0.02, "1Y": None}},
        ]
    )
    financials = {row["industry"] for row in rows["Financials"]}
    technology = {row["industry"] for row in rows["Technology"]}
    assert financials == {"Banks"}
    assert "Software" not in financials
    assert technology == {"Software"}
    banks = rows["Financials"][0]
    assert banks["values"][list(HORIZON_SESSIONS).index("1M")] == pytest.approx(0.04)
    assert banks["values"][list(HORIZON_SESSIONS).index("1Y")] == pytest.approx(0.15)
    assert banks["counts"][list(HORIZON_SESSIONS).index("1Y")] == 2
    matrix = subsector_matrix(rows["Financials"], {"1M": 0.05, "1Y": 0.05}, mode="relative")
    month = matrix["rows"][0]["values"][list(HORIZON_SESSIONS).index("1M")]
    assert month == pytest.approx(0.04 - 0.05)
    assert matrix["rows"][0]["label"] == "Banks"


def test_horizon_isolation_for_a_short_history():
    start = date(2024, 1, 2)
    bars = {
        "NVDA": [{"date": (start + timedelta(days=i)).isoformat(), "value": 100.0 + i} for i in range(10)],
        "AMD": [{"date": (start + timedelta(days=i)).isoformat(), "value": 50.0} for i in range(10)],
    }
    rows = [row for row in constituent_horizon_rows(bars) if row["industry"] == "AI Compute / GPUs"]
    assert {row["symbol"] for row in rows} == {"NVDA", "AMD"}
    assert rows[0]["returns"]["1D"] is not None
    assert rows[0]["returns"]["1Y"] is None
    grouped = aggregate_subsectors(rows)
    ai = grouped["Technology"][0]
    assert ai["values"][list(HORIZON_SESSIONS).index("1D")] is not None
    assert ai["values"][list(HORIZON_SESSIONS).index("1Y")] is None


def test_taxonomy_maps_a_stock_to_its_canonical_industry():
    located = [
        (basket.parent_sector, basket.label)
        for basket in stock_subsector_baskets()
        for symbol in basket.members
        if symbol == "NVDA"
    ]
    assert ("Technology", "AI Compute / GPUs") in located
    assert all(sector != "Financials" for sector, _label in located)
    assert all(basket.kind != KIND_ETF_COMPARISON for basket in stock_subsector_baskets())
    assert is_constituent_subsector(
        {"industry_key": "AI Compute / GPUs", "coverage": {"kind": "CUSTOM_EQUAL_DOLLAR_BASKET", "membership": ["NVDA", "AMD"]}}
    )
    assert not is_constituent_subsector(
        {"industry_key": "KRE (regional banks ETF comparison)", "instrument_id": "KRE", "coverage": {"kind": "ETF_COMPARISON", "membership": ["KRE"]}}
    )


def test_snapshot_fallback_skips_etf_comparisons_and_thin_baskets():
    table = snapshot_subsector_table(
        {
            "Technology": [
                {
                    "industry_key": "Memory",
                    "metrics": {"ret_1d": 0.02, "ret_1m": 0.05},
                    "coverage": {"membership": ["MU"], "kind": "CUSTOM_EQUAL_DOLLAR_BASKET"},
                },
                {
                    "industry_key": "AI Compute / GPUs",
                    "metrics": {"ret_1d": 0.01, "ret_1m": 0.04},
                    "coverage": {"membership": ["NVDA", "AMD"], "kind": "CUSTOM_EQUAL_DOLLAR_BASKET"},
                },
            ],
            "Financials": [
                {
                    "industry_key": "KRE (regional banks ETF comparison)",
                    "instrument_id": "KRE",
                    "metrics": {"ret_1d": 0.03},
                    "coverage": {"kind": "ETF_COMPARISON", "membership": ["KRE"]},
                }
            ],
        }
    )
    assert [row["industry"] for row in table["Technology"]] == ["AI Compute / GPUs", "Memory"]
    memory = next(row for row in table["Technology"] if row["industry"] == "Memory")
    assert memory["values"][0] is None
    assert "Financials" not in table
    view = compose_subsector_view({}, table)
    assert view["Technology"]["method"] == "stored_basket"
    assert view["Financials"]["method"] == "unavailable"
    computed = aggregate_subsectors(
        [
            {"symbol": "NVDA", "sector": "Technology", "industry": "AI Compute / GPUs", "returns": {"1D": 0.02, "1W": 0.04}},
            {"symbol": "AMD", "sector": "Technology", "industry": "AI Compute / GPUs", "returns": {"1D": 0.00, "1W": 0.02}},
        ]
    )
    preferred = compose_subsector_view(computed, table)
    assert preferred["Technology"]["method"] == "equal_weight"


def test_sector_heatmap_relative_mode_uses_spy_from_the_same_horizon():
    rows = [
        {"canonical_sector": "Technology", "instrument_id": "XLK", "metrics": {"ret_1d": 0.02, "ret_1m": 0.07, "ret_12m": 0.30}},
        {"canonical_sector": "Energy", "instrument_id": "XLE", "metrics": {"ret_1d": -0.01, "ret_1m": None}},
    ]
    matrix = sector_heatmap_matrix(rows, {"1D": 0.01, "1M": 0.04, "1Y": 0.0}, mode="relative")
    labels = [row["label"] for row in matrix["rows"]]
    assert labels[0] == "Communication Services"
    tech = next(row for row in matrix["rows"] if row["label"] == "Technology")
    energy = next(row for row in matrix["rows"] if row["label"] == "Energy")
    assert tech["values"][0] == pytest.approx(0.01)
    assert tech["values"][2] == pytest.approx(0.03)
    assert energy["values"][2] is None
    assert tech["values"][5] == pytest.approx(0.30)
    assert matrix["scales"][0]["max_abs"] == pytest.approx(0.02)
    assert matrix["scales"][5]["max_abs"] == pytest.approx(0.30)


def test_constituent_close_query_is_read_only_and_equity_eod_only():
    source = inspect.getsource(load_equity_eod_closes)
    assert "mi_v_equity_daily_closes" in source
    assert "source_id = 'EQUITY_EOD'" in source
    assert "mi_market_bars" not in source
    assert "INSERT" not in source.upper()
    assert "UPDATE" not in source.upper()
