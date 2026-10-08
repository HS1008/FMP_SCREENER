"""Approved IBKR live universe and open-to-current 1D."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ibkr_collector.session_open import choose_latest_open, needs_open_refresh
from market_intelligence.ibkr_live_universe import (
    APPROVED_EQUITY_ETF_COUNT,
    APPROVED_EQUITY_ETF_SYMBOLS,
    APPROVED_EXTRA_INDEXES,
    EXPECTED_IBKR_LIVE_COUNT,
    STOCK_GROUPS,
    UnapprovedSubscription,
    approved_contracts,
    assert_subscription_allowed,
    display_quote_rows,
    stock_heatmap_rows,
    stock_horizon_values,
    unique_stock_symbols,
)
from market_intelligence.taxonomy import index_etf_label
from market_intelligence.live_session import ibkr_mark_price, latest_opened_rth_session, open_to_current_return

ET = ZoneInfo("America/New_York")


def test_named_catalog_is_not_padded_to_the_heading_counts():
    """100 names: the equity/ETF book, seven Global Markets regional ETFs, plus VIX. The eight Power names are included."""
    stocks = unique_stock_symbols()
    assert len(stocks) == 47
    assert APPROVED_EQUITY_ETF_COUNT == 99
    assert APPROVED_EXTRA_INDEXES == frozenset({"VIX"})
    assert EXPECTED_IBKR_LIVE_COUNT == 100
    assert all(symbol in APPROVED_EQUITY_ETF_SYMBOLS for symbol in ("VEA", "VGK", "EWJ", "VWO", "MCHI", "INDA", "EWZ"))
    power = [symbol for name, members in STOCK_GROUPS if name.startswith("Power") for symbol in members]
    assert power == ["CEG", "VST", "TLN", "GEV", "ETN", "PWR", "CCJ", "BE"]


def test_display_duplicates_subscribe_once_and_unapproved_symbols_are_rejected():
    rows = approved_contracts()
    symbols = [row["symbol"] for row in rows]
    assert len(symbols) == len(set(symbols)) == EXPECTED_IBKR_LIVE_COUNT
    assert symbols.count("NVDA") == 1
    assert symbols.count("TSLA") == 1
    assert symbols.count("SPCX") == 1
    listed = [symbol for _name, members in STOCK_GROUPS for symbol in members]
    assert listed.count("NVDA") == 2
    assert listed.count("TSLA") == 2
    assert listed.count("SPCX") == 2
    vix = next(row for row in rows if row["symbol"] == "VIX")
    assert vix["sec_type"] == "IND"
    assert vix["exchange"] == "CBOE"
    assert_subscription_allowed("SPY", "STK")
    assert_subscription_allowed("VIX", "IND")
    with pytest.raises(UnapprovedSubscription):
        assert_subscription_allowed("TLT", "STK")
    with pytest.raises(UnapprovedSubscription):
        assert_subscription_allowed("VIX", "STK")


def test_open_to_current_examples():
    assert open_to_current_return(105, 100) == pytest.approx(0.05)
    assert open_to_current_return(103, 100) == pytest.approx(0.03)
    assert open_to_current_return(106, 100) == pytest.approx(0.06)
    assert open_to_current_return(105, 0) is None
    assert open_to_current_return(105, None) is None
    assert open_to_current_return(None, 100) is None
    price, field = ibkr_mark_price(105, 104, 106)
    assert price == 105 and field == "last"
    mid, mid_field = ibkr_mark_price(None, 100, 102)
    assert mid == pytest.approx(101) and mid_field == "midpoint"
    assert ibkr_mark_price(None, None, None) == (None, None)
    assert ibkr_mark_price(None, 100, None) == (None, None)


def test_denominator_stays_on_the_latest_opened_regular_session():
    tuesday_regular = datetime(2026, 9, 29, 10, 45, tzinfo=ET)
    tuesday_after = datetime(2026, 9, 29, 18, 30, tzinfo=ET)
    wednesday_pre = datetime(2026, 9, 30, 4, 0, tzinfo=ET)
    weekend = datetime(2026, 9, 26, 12, 0, tzinfo=ET)
    mourning_friday_pre = datetime(2025, 1, 10, 4, 0, tzinfo=ET)
    assert latest_opened_rth_session(tuesday_regular) == tuesday_regular.date()
    assert latest_opened_rth_session(tuesday_after) == tuesday_regular.date()
    assert latest_opened_rth_session(wednesday_pre).isoformat() == "2026-09-29"
    assert latest_opened_rth_session(weekend).isoformat() == "2026-09-25"
    assert latest_opened_rth_session(mourning_friday_pre).isoformat() == "2025-01-08"


def test_session_open_cache_does_not_rerequest_a_current_open():
    from datetime import date

    expected = date(2026, 9, 29)
    now = datetime(2026, 9, 29, 15, 0, tzinfo=ET)
    cached = {"session_date": "2026-09-29", "open": 100.0, "fetched_at": now.isoformat()}
    assert needs_open_refresh(cached, expected, now) is False
    stale = {"session_date": "2026-09-26", "open": 100.0, "fetched_at": "2026-09-26T20:00:00+00:00"}
    assert needs_open_refresh(stale, expected, now) is True
    assert choose_latest_open([(date(2026, 9, 26), 100.0), (date(2026, 9, 29), 101.5)]) == (date(2026, 9, 29), 101.5)
    assert choose_latest_open([(date(2026, 9, 29), 0.0)]) is None


def test_stock_heatmap_reuses_one_quote_and_uses_the_open_return():
    quotes = [
        {
            "display_name": "NVDA",
            "last_price": 90.0,
            "market_data_type": "LIVE",
            "provenance": {
                "symbol": "NVDA",
                "current_price": 105.0,
                "current_price_field": "last",
                "session_open": 100.0,
                "session_date": "2026-09-29",
                "open_to_current": 0.05,
            },
        }
    ]
    rows = [row for row in stock_heatmap_rows(quotes) if row["symbol"] == "NVDA"]
    assert len(rows) == 2
    assert {row["group"] for row in rows} == {"Mag 8", "Semiconductors"}
    assert rows[0]["price"] == 105.0
    assert rows[0]["open_to_current"] == pytest.approx(0.05)
    assert rows[0]["price"] == rows[1]["price"]
    missing = stock_heatmap_rows([])
    assert all(row["open_to_current"] is None for row in missing)


def test_quote_table_keeps_one_priced_row_and_omits_unapproved_names():
    rows = display_quote_rows(
        [
            {"display_name": "TLT", "last_price": 90.0, "retrieved_at": "2026-09-30T14:00:00+00:00"},
            {"display_name": "CIBR", "instrument_id": "IBKR:1", "last_price": None, "retrieved_at": "2026-09-30T12:00:00+00:00"},
            {"display_name": "CIBR", "instrument_id": "IBKR:2", "last_price": 70.1, "retrieved_at": "2026-09-30T13:00:00+00:00"},
            {"display_name": "CIBR", "instrument_id": "IBKR:3", "last_price": None, "retrieved_at": "2026-09-30T15:00:00+00:00"},
        ]
    )
    symbols = [row["display_name"] for row in rows]
    assert symbols == ["CIBR"]
    assert rows[0]["last_price"] == 70.1
    assert index_etf_label("RSP", "Equal-Weight S&P 500") == "RSP · Equal-Weight S&P 500"


def test_stock_horizons_use_the_open_for_1d_and_stored_windows_after_that():
    values = stock_horizon_values(0.01, {"1D": 0.5, "1W": 0.02, "1M": None, "3M": -0.1, "6M": 0.2, "1Y": 0.3})
    assert values[0] == pytest.approx(0.01)
    assert values[1] == pytest.approx(0.02)
    assert values[2] is None
    assert values[3] == pytest.approx(-0.1)
    assert values[4] == pytest.approx(0.2)
    assert values[5] == pytest.approx(0.3)
    assert len(values) == 6


def test_config_watchlist_cannot_add_an_unapproved_symbol(tmp_path):
    from ibkr_collector.config import load_config

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"watchlist": [{"symbol": "TLT", "sec_type": "STK"}, {"symbol": "SPY", "sec_type": "STK"}]}),
        encoding="utf-8",
    )
    cfg = load_config(path)
    symbols = [row["symbol"] for row in cfg.watchlist]
    assert "TLT" not in symbols
    assert "VIX" in symbols
    assert len(symbols) == EXPECTED_IBKR_LIVE_COUNT
