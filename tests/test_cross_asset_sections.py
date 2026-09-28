"""FOREX, CFTC positioning, commodities, and crypto analytics."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from market_intelligence.due_state import evaluate_due_steps
from market_intelligence.ingest_eia import _candidate_queries

from market_intelligence.cftc_positions import (
    CONTRACT_BY_CODE,
    DISAGG_CATEGORIES,
    TFF_CATEGORIES,
    align_price_to_position,
    enrich_category_history,
    net_contracts,
    net_oi_pct,
    parse_report_rows,
    scheduled_publication_date,
)
from market_intelligence.commodity_analytics import same_date_ratio, window_returns
from market_intelligence.cross_asset_universe import (
    CURRENCY_VS_USD,
    EIA_FUNDAMENTALS,
    FX_WINDOWS,
    INSTRUMENT_BY_SYMBOL,
    YAHOO_CROSS_ASSET,
)
from market_intelligence.crypto_analytics import calendar_return, drawdown_series, utc_observation_date
from market_intelligence.fx_analytics import FX_WINDOWS as FX_RETURN_WINDOWS
from market_intelligence.fx_analytics import levels_by_id, observation_return, oriented_fx_level
from market_intelligence.ingest_yahoo_cross_asset import bars_from_yahoo_frame, observation_date_for_bar
from market_intelligence.markets_analytics import normalize_selected_to_100

ROOT = Path(__file__).resolve().parents[1]


def _daily(start: date, values: list[float]) -> list[tuple[date, float]]:
    return [(start + timedelta(days=index), value) for index, value in enumerate(values)]


def test_yahoo_symbols_match_the_verified_universe():
    expected = {
        "DX-Y.NYB": "DXY",
        "EURUSD=X": "EURUSD",
        "GBPUSD=X": "GBPUSD",
        "USDJPY=X": "USDJPY",
        "AUDUSD=X": "AUDUSD",
        "USDCAD=X": "USDCAD",
        "USDCHF=X": "USDCHF",
        "CL=F": "CL",
        "BZ=F": "BZ",
        "NG=F": "NG",
        "GC=F": "GC",
        "SI=F": "SI",
        "HG=F": "HG",
        "ZC=F": "ZC",
        "ZW=F": "ZW",
        "ZS=F": "ZS_F",
        "BTC-USD": "BTC",
        "ETH-USD": "ETH",
    }
    for symbol, instrument_id in expected.items():
        row = INSTRUMENT_BY_SYMBOL[symbol]
        assert row.instrument_id == instrument_id
    assert {row.yahoo_symbol for row in YAHOO_CROSS_ASSET} >= set(expected)
    assert "official continuous settlement" not in (ROOT / "market_intelligence" / "commodity_analytics.py").read_text(encoding="utf-8").lower() or "not an official" in (ROOT / "market_intelligence" / "commodity_analytics.py").read_text(encoding="utf-8").lower()


def test_fx_orientation_inverts_only_usd_quoted_pairs():
    assert oriented_fx_level("EURUSD", 1.10) == 1.10
    assert oriented_fx_level("GBPUSD", 1.25) == 1.25
    assert oriented_fx_level("AUDUSD", 0.70) == 0.70
    assert oriented_fx_level("USDJPY", 150.0) == 1.0 / 150.0
    assert oriented_fx_level("USDCAD", 1.25) == 0.8
    assert oriented_fx_level("USDCHF", 0.90) == 1.0 / 0.90
    assert oriented_fx_level("USDJPY", None) is None
    assert oriented_fx_level("USDJPY", 0) is None


def test_fx_return_direction_uses_foreign_currency_strength():
    start = date(2024, 1, 1)
    eur = levels_by_id(
        [{"instrument_id": "EURUSD", "bar_date": start + timedelta(days=i), "close": 1.0 + i * 0.01} for i in range(6)],
        instrument_id="EURUSD",
        orient=True,
    )
    jpy = levels_by_id(
        [{"instrument_id": "USDJPY", "bar_date": start + timedelta(days=i), "close": 100.0 + i * 10} for i in range(6)],
        instrument_id="USDJPY",
        orient=True,
    )
    assert observation_return(eur, 1) > 0
    assert observation_return(jpy, 1) < 0
    assert [label for label, _lag in FX_RETURN_WINDOWS] == ["1D", "1W", "1M", "3M", "6M", "1Y"]
    assert observation_return(eur[:2], 5) is None


def test_normalized_fx_starts_at_100_and_keeps_gaps():
    start = date(2024, 1, 1)
    series = {
        "EURUSD": [(start, 1.0), (start + timedelta(days=1), None), (start + timedelta(days=2), 1.1)],
        "GBPUSD": [(start, 2.0), (start + timedelta(days=2), 2.2)],
    }
    result = normalize_selected_to_100(series, ["EURUSD", "GBPUSD"])
    assert result["start"] == start
    assert result["series"]["EURUSD"][0] == (start, 100.0)
    assert result["series"]["GBPUSD"][0] == (start, 100.0)
    assert all(day != start + timedelta(days=1) for day, _value in result["series"]["EURUSD"])


def test_crypto_utc_dates_do_not_shift_and_weekends_stay():
    london_monday = datetime(2026, 9, 28, 0, 30, tzinfo=timezone(timedelta(hours=1)))
    assert utc_observation_date(london_monday) == date(2026, 9, 27)
    utc_midnight = datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)
    assert observation_date_for_bar(utc_midnight, "CRYPTO") == date(2026, 9, 28)
    london_fx = datetime(2026, 9, 28, 0, 0, tzinfo=timezone(timedelta(hours=1)))
    assert observation_date_for_bar(london_fx, "FX") == date(2026, 9, 28)
    saturday = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)
    frame = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [83000.0], "Adj Close": [83000.0]},
        index=pd.DatetimeIndex([saturday]),
    )
    bars = bars_from_yahoo_frame(frame, INSTRUMENT_BY_SYMBOL["BTC-USD"])
    assert bars[0].bar_date == date(2026, 9, 26)
    assert bars[0].bar_date.weekday() == 5


def test_crypto_windows_are_calendar_days_and_drawdown_is_never_positive():
    start = date(2024, 1, 1)
    points = _daily(start, [100.0 + index for index in range(400)])
    assert calendar_return(points, days=7) is not None
    week_ago = points[-1][0] - timedelta(days=7)
    assert dict(points)[week_ago]
    assert calendar_return(points, days=7) == points[-1][1] / dict(points)[week_ago] - 1
    missing = [item for item in points if item[0] != week_ago]
    assert calendar_return(missing, days=7) is None
    drawdown = drawdown_series(points)
    assert drawdown
    assert all(value <= 0 for _day, value in drawdown)
    short = _daily(start, [100.0, 90.0, 80.0])
    assert drawdown_series(short) == []
    source = (ROOT / "market_intelligence" / "crypto_analytics.py").read_text(encoding="utf-8")
    assert "trailing_drawdown" not in source
    assert "RETURN_WINDOWS" not in source
    assert [label for label, days in (("1D", 1), ("7D", 7), ("1M", 30), ("3M", 90), ("1Y", 365))]
    assert calendar_return(points, days=1) > 0


def test_commodity_same_date_ratio_and_eia_mapping():
    left = [(date(2024, 1, 1), 2000.0), (date(2024, 1, 2), 2100.0)]
    right = [(date(2024, 1, 1), 4.0), (date(2024, 1, 3), 5.0)]
    assert same_date_ratio(left, right) == [(date(2024, 1, 1), 500.0)]
    points = _daily(date(2024, 1, 1), [float(100 + i) for i in range(300)])
    returns = window_returns(points)
    assert set(returns) == {label for label, _lag in FX_WINDOWS}
    assert returns["1D"] is not None
    aliases = {row["alias"]: row for row in EIA_FUNDAMENTALS}
    assert aliases["crude_stocks"]["series_id"] == "WCESTUS1"
    assert aliases["cushing_crude_stocks"]["series_id"] == "WCESTCUS1"
    assert aliases["crude_production"]["series_id"] == "WCRFPUS2"
    assert aliases["working_gas_storage"]["series_id"] == "NW2_EPG0_SWO_R48_BCF"
    cushing = _candidate_queries("petroleum/stoc/wstk/data", "WCESTCUS1", "cushing_crude_stocks")
    assert cushing[0][1]["facets[series][]"] == "WCESTCUS1"
    assert any(facets.get("facets[duoarea][]") == "YCUOK" for _route, facets in cushing)
    assert "ZS_F" == INSTRUMENT_BY_SYMBOL["ZS=F"].instrument_id
    assert all(row["frequency"] == "weekly" for row in EIA_FUNDAMENTALS)


def test_cftc_contracts_and_category_fields_are_explicit():
    assert CONTRACT_BY_CODE["13874A"].market_name == "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE"
    assert CONTRACT_BY_CODE["209742"].market_key == "nasdaq"
    assert CONTRACT_BY_CODE["239742"].market_key == "russell"
    assert CONTRACT_BY_CODE["042601"].market_key == "ust2"
    assert CONTRACT_BY_CODE["044601"].market_key == "ust5"
    assert CONTRACT_BY_CODE["043602"].market_key == "ust10"
    assert CONTRACT_BY_CODE["020601"].market_key == "ust30"
    assert CONTRACT_BY_CODE["099741"].market_key == "eur"
    assert CONTRACT_BY_CODE["097741"].market_key == "jpy"
    assert CONTRACT_BY_CODE["096742"].market_key == "gbp"
    assert CONTRACT_BY_CODE["090741"].market_key == "cad"
    assert CONTRACT_BY_CODE["232741"].market_key == "aud"
    assert CONTRACT_BY_CODE["092741"].market_key == "chf"
    assert CONTRACT_BY_CODE["06765A"].market_key == "wti"
    assert CONTRACT_BY_CODE["023651"].market_name == "NAT GAS NYME - NEW YORK MERCANTILE EXCHANGE"
    assert CONTRACT_BY_CODE["088691"].market_key == "gold"
    assert CONTRACT_BY_CODE["084691"].market_key == "silver"
    assert CONTRACT_BY_CODE["085692"].market_key == "copper"
    assert CONTRACT_BY_CODE["1170E1"].market_key == "vix"
    assert [row[2] for row in TFF_CATEGORIES] == [
        "dealer_positions_long_all",
        "asset_mgr_positions_long",
        "lev_money_positions_long",
        "other_rept_positions_long",
    ]
    assert DISAGG_CATEGORIES[1][3] == "swap__positions_short_all"


def _tff_row(**overrides):
    row = {
        "cftc_contract_market_code": "13874A",
        "market_and_exchange_names": "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
        "report_date_as_yyyy_mm_dd": "2024-01-02T00:00:00.000",
        "open_interest_all": "1000",
        "dealer_positions_long_all": "100",
        "dealer_positions_short_all": "40",
        "asset_mgr_positions_long": "200",
        "asset_mgr_positions_short": "50",
        "lev_money_positions_long": "300",
        "lev_money_positions_short": "100",
        "other_rept_positions_long": "10",
        "other_rept_positions_short": "30",
    }
    row.update(overrides)
    return row


def test_cftc_parse_net_and_publication_date():
    parsed, rejected = parse_report_rows([_tff_row()])
    assert rejected == 0
    leveraged = next(row for row in parsed if row["trader_category"] == "leveraged_funds")
    assert leveraged["position_date"] == date(2024, 1, 2)
    assert leveraged["scheduled_publication_date"] == date(2024, 1, 5)
    assert net_contracts(300, 100) == 200
    assert net_oi_pct(300, 100, 1000) == 20.0
    assert leveraged["long_contracts"] == 300
    assert leveraged["short_contracts"] == 100
    assert leveraged["open_interest"] == 1000
    assert scheduled_publication_date(date(2024, 1, 3)) is None
    mismatched, rejected_name = parse_report_rows([_tff_row(market_and_exchange_names="NOT THE CONTRACT")])
    assert mismatched == []
    assert rejected_name == 1


def test_cftc_metrics_missing_oi_changes_percentiles_and_price_alignment():
    assert net_oi_pct(10, 4, None) is None
    assert net_oi_pct(10, 4, 0) is None
    start = date(2020, 1, 7)
    points = []
    for index in range(160):
        points.append(
            {
                "position_date": start + timedelta(days=7 * index),
                "long_contracts": 100 + index,
                "short_contracts": 40,
                "open_interest": 1000,
            }
        )
    history = enrich_category_history(points)
    latest = history[-1]
    assert latest["change_1w"] == latest["net_oi_pct"] - history[-2]["net_oi_pct"]
    assert latest["change_4w"] == latest["net_oi_pct"] - history[-5]["net_oi_pct"]
    assert 0 <= latest["percentile_1y"] <= 100
    assert 0 <= latest["percentile_3y"] <= 100
    assert latest["zscore"] is not None
    assert history[0]["percentile_1y"] is None
    flat = enrich_category_history(
        [
            {"position_date": start + timedelta(days=7 * index), "long_contracts": 50, "short_contracts": 50, "open_interest": 100}
            for index in range(60)
        ]
    )
    assert flat[-1]["zscore"] is None
    position = date(2024, 6, 4)
    prices = {date(2024, 6, 5): 10.0, date(2024, 6, 3): 9.0, date(2024, 5, 1): 8.0}
    assert align_price_to_position(position, prices) == (date(2024, 6, 3), 9.0)
    assert align_price_to_position(position, {position: 11.0, date(2024, 6, 5): 99.0}) == (position, 11.0)
    assert align_price_to_position(position, {date(2024, 6, 5): 99.0}) is None


def test_reads_use_curated_views_not_raw_tables():
    source = (ROOT / "market_intelligence" / "cross_asset_read.py").read_text(encoding="utf-8")
    for relation in ("mi_market_bars", "mi_macro_observations", "mi_eia_observations", "mi_cftc_position_observations"):
        assert "FROM {0}".format(relation) not in source
    for view in (
        "mi_v_yahoo_cross_asset_history",
        "mi_v_cftc_position_history",
        "mi_v_eia_history",
        "mi_v_macro_observations_current",
        "mi_v_market_monitor_closes",
    ):
        assert view in source


def test_cross_asset_due_follows_each_source_calendar():
    et = ZoneInfo("America/New_York")
    saturday = datetime(2026, 9, 19, 12, 0, tzinfo=et)
    weekend = evaluate_due_steps(
        now=saturday,
        env={},
        freshness={
            ("YAHOO_FX", "fx_daily"): date(2026, 9, 18),
            ("YAHOO_FUTURES_PROXY", "commodity_futures_proxy"): date(2026, 9, 18),
            ("YAHOO_CRYPTO", "crypto_daily"): date(2026, 9, 18),
        },
        configured_steps=["yahoo_cross_asset", "treasury"],
    )
    by_step = {item.step: item for item in weekend}
    assert by_step["treasury"].due is False
    assert by_step["treasury"].reason == "outside_catchup_window"
    assert by_step["yahoo_cross_asset"].due is True
    friday = datetime(2026, 9, 18, 16, 0, tzinfo=et)
    current_tff = evaluate_due_steps(
        now=friday,
        env={},
        freshness={("CFTC_COT", "tff_disaggregated_positions"): date(2026, 9, 15)},
        configured_steps=["cftc"],
    )
    legacy_only = evaluate_due_steps(
        now=friday,
        env={},
        freshness={("CFTC_COT", "commitment_of_traders"): date(2026, 9, 15)},
        configured_steps=["cftc"],
    )
    assert current_tff[0].due is False
    assert legacy_only[0].due is True


def test_pages_are_database_only_and_render_required_sections():
    ui = (ROOT / "market_intelligence" / "cross_asset_ui.py").read_text(encoding="utf-8")
    pages = (ROOT / "market_intelligence" / "pages_ui.py").read_text(encoding="utf-8")
    for blob in (ui, pages):
        assert "yfinance" not in blob
        assert "urllib" not in blob
        assert "fred_client" not in blob
        assert "ingest_yahoo" not in blob
    for heading in (
        "US Dollar Index",
        "Major Currencies vs USD",
        "FX Performance vs USD",
        "FX Return Heatmap",
        "Major FX Pairs",
        "Cross-Asset Positioning",
        "Net Positioning / Open Interest",
        "Positioning Percentile",
        "Positioning Change",
        "Price vs Positioning",
        "Commodity Performance",
        "Commodity Return Heatmap",
        "U.S. Commercial Crude Inventories",
        "Cushing Crude Inventories",
        "U.S. Crude Oil Production",
        "U.S. Natural Gas Storage",
        "Gold/Copper",
        "Bitcoin",
        "Ethereum",
        "Bitcoin vs Ethereum",
        "BTC / ETH Relative Strength",
        "Crypto Performance",
        "52-Week Drawdown",
        "Methodology & sources",
    ):
        assert heading in ui
    assert [label for _instrument, label in CURRENCY_VS_USD] == ["EUR", "GBP", "JPY", "AUD", "CAD", "CHF"]
