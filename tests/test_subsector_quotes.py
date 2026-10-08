"""Subsector coverage, constituent labels, and stored-quote precedence."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta

import pytest

from ibkr_collector.runner import contract_subscription_key, subscriptions_to_open
from ibkr_collector.values import BLOCKED_ECLIENT_METHODS
from market_intelligence import markets_ui
from market_intelligence.live_session import (
    ET,
    SOURCE_IBKR,
    SOURCE_YAHOO_LIVE,
    heatmap_freshness_label,
    live_return,
    market_session_state,
    quote_data_status,
    resolve_current_price,
)
from market_intelligence.markets_analytics import build_aligned_us_panel, overlay_stored_quote_returns, subsector_matrix
from market_intelligence.sector_mapping import canonical_sector_name, resolve_provider_sector
from market_intelligence.taxonomy import (
    NO_SUBSECTOR_CLASSIFICATION,
    BasketDef,
    KIND_CUSTOM_BASKET,
    constituent_label,
    subsector_coverage,
)


def _record(prices: dict[date, float]) -> dict:
    return {"prices": prices, "adjustment_basis": "IBKR_ADJUSTED_LAST", "provider": "IBKR"}


def test_sector_aliases_normalize_without_inventing_industries():
    expected = {
        "Information Technology": "Technology",
        " information technology ": "Technology",
        "Communication": "Communication Services",
        "Communications": "Communication Services",
        "Consumer Cyclical": "Consumer Discretionary",
        "Consumer Defensive": "Consumer Staples",
        "Financial Services": "Financials",
        "Healthcare": "Health Care",
        "HealthCare": "Health Care",
        "Basic Materials": "Materials",
    }
    for raw, canonical in expected.items():
        assert canonical_sector_name(raw) == canonical
    assert resolve_provider_sector("AI").canonical_sector is None
    assert canonical_sector_name("") is None


def test_non_technology_sectors_explain_missing_classification():
    coverage = subsector_coverage()
    assert coverage["Technology"]["status"] == "available"
    assert coverage["Technology"]["baskets"]
    for name in (
        "Financials",
        "Health Care",
        "Industrials",
        "Consumer Discretionary",
        "Consumer Staples",
        "Energy",
        "Materials",
        "Utilities",
        "Real Estate",
        "Communication Services",
    ):
        assert coverage[name]["status"] == "unavailable"
        assert coverage[name]["reason"] == NO_SUBSECTOR_CLASSIFICATION
        assert coverage[name]["baskets"] == ()


def test_alias_parent_sector_joins_the_canonical_heatmap():
    dates = [date(2024, 1, 2) + timedelta(days=offset) for offset in range(6)]
    endpoint = dates[-1]
    spy = {day: 100.0 for day in dates}
    spy[endpoint] = 101.0
    left = {day: 100.0 for day in dates}
    left[endpoint] = 110.0
    right = {day: 100.0 for day in dates}
    right[endpoint] = 90.0
    quiet = {day: 50.0 for day in dates}
    baskets = (
        BasketDef("BANKS", "Banks", "Financial Services", None, ("JPM", "BAC"), KIND_CUSTOM_BASKET),
        BasketDef("SEMIS", "Semiconductors", "Information Technology", None, ("NVDA", "AMD"), KIND_CUSTOM_BASKET),
        BasketDef("BLANK", "Missing", "", None, ("AAA", "BBB"), KIND_CUSTOM_BASKET),
    )
    panel = build_aligned_us_panel(
        {
            "SPY": _record(spy),
            "JPM": _record(left),
            "BAC": _record(right),
            "NVDA": _record(left),
            "AMD": _record(right),
        },
        baskets=baskets,
    )
    assert "Financials" in panel["subsectors"]
    assert "Technology" in panel["subsectors"]
    assert "" not in panel["subsectors"]
    banks = panel["subsectors"]["Financials"][0]
    semis = panel["subsectors"]["Technology"][0]
    assert banks["values"][0] == pytest.approx(0.0)
    assert semis["values"][0] == pytest.approx(0.0)
    assert banks["counts"][0] == 2
    matrix = subsector_matrix(panel["subsectors"]["Financials"], panel["spy_returns"], mode="absolute")
    assert matrix["rows"][0]["label"] == "Banks"
    assert "Constituents: 2" in matrix["rows"][0]["notes"][0]
    assert banks["constituents"][0]["symbol"] == "BAC"
    assert constituent_label("BAC", banks["constituents"][0]["company"]) == "BAC"


def test_one_missing_member_does_not_blank_the_basket():
    dates = [date(2024, 1, 2) + timedelta(days=offset) for offset in range(4)]
    endpoint = dates[-1]
    prices = {day: 100.0 for day in dates}
    up = dict(prices)
    up[endpoint] = 110.0
    flat = dict(prices)
    baskets = (BasketDef("GROUP", "Group", "Financials", None, ("AAA", "BBB", "CCC"), KIND_CUSTOM_BASKET),)
    panel = build_aligned_us_panel(
        {
            "SPY": _record(prices),
            "AAA": _record(up),
            "BBB": _record(flat),
            "CCC": _record({day: 100.0 for day in dates[:-1]}),
        },
        baskets=baskets,
    )
    row = panel["subsectors"]["Financials"][0]
    assert row["counts"][0] == 2
    assert row["values"][0] == pytest.approx(0.05)


def test_constituent_label_keeps_ticker_as_the_value():
    assert constituent_label("NVDA") == "NVDA — NVIDIA Corp."
    assert constituent_label("nvda", "NVDA") == "NVDA — NVIDIA Corp."
    assert constituent_label("ZZZZ") == "ZZZZ"


def test_live_ibkr_beats_delayed_and_delayed_is_not_labeled_live():
    now = datetime(2026, 9, 16, 15, 30, tzinfo=ET)
    current = date(2026, 9, 16)
    candidates = [
        {
            "symbol": "XLK",
            "source_id": SOURCE_IBKR,
            "last_price": 200.0,
            "quote_ts": datetime(2026, 9, 16, 15, 20, tzinfo=ET),
            "market_data_type": "DELAYED",
        },
        {
            "symbol": "XLK",
            "source_id": SOURCE_IBKR,
            "last_price": 210.0,
            "quote_ts": datetime(2026, 9, 16, 15, 21, tzinfo=ET),
            "market_data_type": "LIVE",
        },
        {
            "symbol": "XLK",
            "source_id": SOURCE_YAHOO_LIVE,
            "last_price": 190.0,
            "quote_ts": datetime(2026, 9, 16, 15, 29, tzinfo=ET),
        },
    ]
    chosen = resolve_current_price(candidates, symbol="XLK", current_session=current, now=now)
    assert chosen is not None
    assert chosen.price == pytest.approx(210.0)
    assert chosen.market_data_status == "LIVE"
    delayed = resolve_current_price(candidates[:1], symbol="XLK", current_session=current, now=now)
    assert delayed is not None and delayed.market_data_status == "DELAYED"
    label = heatmap_freshness_label(
        statuses=["DELAYED"],
        updated=datetime(2026, 9, 16, 19, 31, 8, tzinfo=ET),
        market_state="OPEN",
    )
    assert label.startswith("IBKR Delayed · updated 19:31:08 ET")
    assert not label.startswith("IBKR Live")


def _aligned_quote(ret: float, *, status: str = "LIVE", basis: str = "IBKR_ADJUSTED_LAST", baseline: str = "2024-01-04", current: str = "2024-01-05") -> dict:
    return {
        "live_return": ret,
        "current_session": current,
        "baseline_session": baseline,
        "basis": basis,
        "current": {"market_data_status": status, "observation_ts": "2026-09-16T19:31:08+00:00", "session_date": current},
        "prior_close": {"adjustment_basis": basis, "session_date": baseline},
    }


def test_partial_live_quote_keeps_the_eod_basket_and_relative_pair():
    assert live_return(101.0, 100.0) == pytest.approx(0.01)
    dates = [date(2024, 1, 2) + timedelta(days=offset) for offset in range(3)]
    endpoint = dates[-1]
    spy = {day: 100.0 for day in dates}
    nvda = {day: 100.0 for day in dates}
    nvda[endpoint] = 110.0
    amd = {day: 100.0 for day in dates}
    amd[endpoint] = 90.0
    panel = build_aligned_us_panel({"SPY": _record(spy), "NVDA": _record(nvda), "AMD": _record(amd)})
    untouched = overlay_stored_quote_returns(panel, {})
    tech = next(row for row in untouched["sectors"] if row["label"] == "Technology")
    original = next(row for row in panel["sectors"] if row["label"] == "Technology")
    assert tech["values"] == original["values"]
    assert untouched["quote_freshness"]["active"] is False
    quoted = overlay_stored_quote_returns(
        panel,
        {"NVDA": _aligned_quote(0.02), "AMD": {"live_return": None, "current": {}}},
    )
    basket = next(row for row in quoted["subsectors"]["Technology"] if row["industry"] == "AI Compute / GPUs")
    nvda_row = next(member for member in basket["constituents"] if member["symbol"] == "NVDA")
    amd_row = next(member for member in basket["constituents"] if member["symbol"] == "AMD")
    original_basket = next(row for row in panel["subsectors"]["Technology"] if row["industry"] == "AI Compute / GPUs")
    original_nvda = next(member for member in original_basket["constituents"] if member["symbol"] == "NVDA")
    assert nvda_row["returns"]["1D"] == pytest.approx(original_nvda["returns"]["1D"])
    assert nvda_row["returns"]["1D"] == pytest.approx(0.10)
    assert nvda_row["quote_1d"] == "HISTORICAL"
    assert nvda_row["quote_unused"] == "unaligned"
    assert amd_row["quote_1d"] == "HISTORICAL"
    assert basket["values"][0] == pytest.approx(original_basket["values"][0])
    assert basket["values"][0] != pytest.approx((0.02 + amd_row["returns"]["1D"]) / 2.0)
    assert basket["values"][1:] == original_basket["values"][1:]
    assert basket["counts"][0] == original_basket["counts"][0]
    relative = subsector_matrix(
        quoted["subsectors"]["Technology"],
        quoted["spy_returns"],
        mode="relative",
        spy_eod_returns=quoted["spy_eod_returns"],
        spy_quote=quoted["spy_quote_1d"],
    )
    shown = relative["rows"][0]
    assert shown["values"][0] == pytest.approx(original_basket["values"][0] - panel["spy_returns"]["1D"])
    assert "EQUITY_EOD" in shown["notes"][0]
    assert "IBKR Live" not in shown["notes"][0]


def test_aligned_live_basket_uses_only_that_session_and_labels_the_cell():
    dates = [date(2024, 1, 2) + timedelta(days=offset) for offset in range(3)]
    endpoint = dates[-1]
    flat = {day: 100.0 for day in dates}
    panel = build_aligned_us_panel({"SPY": _record(flat), "NVDA": _record(flat), "AMD": _record(flat), "XLK": _record(flat)})
    quoted = overlay_stored_quote_returns(
        panel,
        {
            "NVDA": _aligned_quote(0.02),
            "AMD": _aligned_quote(0.04),
            "XLK": _aligned_quote(0.03),
            "SPY": _aligned_quote(0.01),
        },
    )
    basket = next(row for row in quoted["subsectors"]["Technology"] if row["industry"] == "AI Compute / GPUs")
    assert basket["values"][0] == pytest.approx(0.03)
    assert basket["counts"][0] == 2
    assert basket["quote_1d"] == "LIVE"
    member_returns = {member["symbol"]: member["returns"]["1D"] for member in basket["constituents"]}
    assert member_returns["NVDA"] == pytest.approx(0.02)
    assert member_returns["AMD"] == pytest.approx(0.04)
    absolute = subsector_matrix(quoted["subsectors"]["Technology"], quoted["spy_returns"], mode="absolute", spy_quote=quoted["spy_quote_1d"])
    assert "IBKR Live" in absolute["rows"][0]["notes"][0]
    assert "Constituents: 2" in absolute["rows"][0]["notes"][0]
    assert "2024-01-05 vs 2024-01-04" in absolute["rows"][0]["notes"][0]
    relative = subsector_matrix(
        quoted["subsectors"]["Technology"],
        quoted["spy_returns"],
        mode="relative",
        spy_eod_returns=quoted["spy_eod_returns"],
        spy_quote=quoted["spy_quote_1d"],
    )
    assert relative["rows"][0]["values"][0] == pytest.approx(0.02)
    assert "same session and basis as SPY" in relative["rows"][0]["notes"][0]
    tech = next(row for row in quoted["sectors"] if row["symbol"] == "XLK")
    assert tech["values"][0] == pytest.approx(0.03)
    assert tech["eod_values"][0] == pytest.approx(0.0)
    mismatched = overlay_stored_quote_returns(
        panel,
        {"NVDA": _aligned_quote(0.02), "AMD": _aligned_quote(0.04), "XLK": _aligned_quote(0.03)},
    )
    live_basket = next(row for row in mismatched["subsectors"]["Technology"] if row["industry"] == "AI Compute / GPUs")
    assert live_basket["values"][0] == pytest.approx(0.03)
    eod_relative = subsector_matrix(
        mismatched["subsectors"]["Technology"],
        mismatched["spy_returns"],
        mode="relative",
        spy_eod_returns=mismatched["spy_eod_returns"],
        spy_quote=mismatched["spy_quote_1d"],
    )
    assert eod_relative["rows"][0]["values"][0] == pytest.approx(0.0)
    assert "EQUITY_EOD" in eod_relative["rows"][0]["notes"][0]
    assert "IBKR Live" not in eod_relative["rows"][0]["notes"][0]
    foreign_basis = overlay_stored_quote_returns(
        panel,
        {"NVDA": _aligned_quote(0.02, basis="SPLIT_ADJUSTED_UNKNOWN_DIVIDEND"), "AMD": _aligned_quote(0.04)},
    )
    fallback = next(row for row in foreign_basis["subsectors"]["Technology"] if row["industry"] == "AI Compute / GPUs")
    assert fallback["values"][0] == pytest.approx(0.0)
    assert fallback["quote_1d"] == "HISTORICAL"
    other_session = overlay_stored_quote_returns(
        panel,
        {"NVDA": _aligned_quote(0.02, baseline="2024-01-03"), "AMD": _aligned_quote(0.04)},
    )
    split = next(row for row in other_session["subsectors"]["Technology"] if row["industry"] == "AI Compute / GPUs")
    assert split["values"][0] == pytest.approx(0.0)
    mixed_status = overlay_stored_quote_returns(
        panel,
        {"NVDA": _aligned_quote(0.02, status="LIVE"), "AMD": _aligned_quote(0.04, status="DELAYED"), "SPY": _aligned_quote(0.01)},
    )
    mixed = next(row for row in mixed_status["subsectors"]["Technology"] if row["industry"] == "AI Compute / GPUs")
    assert mixed["values"][0] == pytest.approx(0.03)
    assert mixed["quote_1d"] == "MIXED"
    mixed_matrix = subsector_matrix(mixed_status["subsectors"]["Technology"], mixed_status["spy_returns"], mode="absolute")
    assert "Mixed" in mixed_matrix["rows"][0]["notes"][0]
    assert "DELAYED" in mixed_matrix["rows"][0]["notes"][0]
    caption = heatmap_freshness_label(statuses=["LIVE", "HISTORICAL"], updated=None, market_state="OPEN")
    assert caption.startswith("Mixed 1D sources")
    assert not caption.startswith("IBKR Live")


def test_stale_quote_is_not_called_live_when_the_market_is_closed():
    closed = heatmap_freshness_label(
        statuses=["LIVE"],
        updated=datetime(2026, 9, 16, 21, 0, tzinfo=ET),
        market_state="CLOSED",
    )
    assert closed.startswith("IBKR Live source · market CLOSED")
    assert market_session_state(datetime(2026, 9, 19, 15, 0, tzinfo=ET)) == "CLOSED"
    assert market_session_state(datetime(2026, 9, 16, 14, 0, tzinfo=ET)) == "OPEN"
    assert quote_data_status({"source_id": SOURCE_IBKR, "market_data_type": "FROZEN"}) == "FROZEN"


def test_duplicate_subscriptions_and_contract_identity():
    first = contract_subscription_key({"symbol": "BRK B", "sec_type": "STK", "primary_exchange": "NYSE", "currency": "USD"})
    second = contract_subscription_key({"symbol": "BRK B", "sec_type": "STK", "primary_exchange": "NYSE", "currency": "USD", "con_id": ""})
    other = contract_subscription_key({"symbol": "BRK B", "sec_type": "STK", "primary_exchange": "NASDAQ", "currency": "USD"})
    assert first == second
    assert first != other
    opened = subscriptions_to_open({first}, [first, first, other, other])
    assert opened == [other]


def test_dashboard_quote_path_does_not_submit_orders():
    for module in (markets_ui,):
        source = inspect.getsource(module)
        assert "placeOrder" not in source
        assert "reqMktData" not in source
    assert "placeOrder" in BLOCKED_ECLIENT_METHODS
    runtime = __import__("ibkr_collector.runner", fromlist=["CollectorRuntime"]).CollectorRuntime
    streaming = inspect.getsource(runtime._subscribe_row) + inspect.getsource(runtime._heal_one_silent_quote)
    assert "placeOrder" not in inspect.getsource(runtime._qualify_and_subscribe)
    assert streaming.count('reqMktData(req_id, contract, "", False, False, [])') == 2
