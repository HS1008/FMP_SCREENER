"""Individual-stock vol/Sharpe, per-column heatmap kinds, sector drill navigation, FX header and heatmap."""

from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from market_intelligence import markets_ui
from market_intelligence.calendars import CAL_NYSE, is_session
from market_intelligence.components.link_table import link_table_click_row_id, link_table_rows
from market_intelligence.components.tenor_chart import (
    HEATMAP_COLUMN_KINDS,
    build_column_scaled_heatmap_option,
    heatmap_click_row_id,
)
from market_intelligence.cross_asset_read import FX_CARD_WINDOWS, forex_header_cards, fx_header_card
from market_intelligence.cross_asset_ui import fx_card_html
from market_intelligence.cross_asset_universe import FX_INSTRUMENTS, FX_WINDOWS
from market_intelligence.fx_analytics import latest_window_returns
from market_intelligence.ibkr_live_universe import SECTOR_ETFS, STOCK_RETURN_HORIZONS
from market_intelligence.markets_analytics import column_color_scale, magnitude_cell_color, magnitude_color_scale
from market_intelligence.navigation_links import (
    PENDING_SCROLL_KEY,
    SUBSECTOR_ANCHOR,
    SUBSECTOR_SELECTOR_KEY,
    select_subsector,
    subsector_drill,
    subsector_group_for,
)
from market_intelligence.price_returns import PRICE_RETURN_BASIS
from market_intelligence.risk_metrics import (
    RISK_COLUMN_KINDS,
    RISK_COLUMN_LABELS,
    RISK_COLUMNS,
    RISK_WINDOWS,
    STOCK_RISK_CAPTION,
    annualized_vol_and_sharpe,
    risk_cell_note,
    risk_metrics_by_symbol,
    risk_row_cells,
    stock_risk_metrics,
    window_closes,
)
from market_intelligence.taxonomy import SECTOR_PROXIES
from market_intelligence.yahoo_price_history import parse_daily_closes, series_has_dividend, series_needs_repair

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


# ---- fixtures ------------------------------------------------------------------------------------


def _sessions(end: date, count: int) -> list[date]:
    days: list[date] = []
    day = end
    while len(days) < count:
        if is_session(day, CAL_NYSE):
            days.append(day)
        day = date.fromordinal(day.toordinal() - 1)
    days.reverse()
    return days


def _bars(closes: list[float], *, end: date = date(2026, 10, 2), adj_ratio: float = 0.9, quality: str = "COMPLETE") -> list[dict]:
    days = _sessions(end, len(closes))
    return [
        {"bar_date": day, "close": close, "adj_close": close * adj_ratio, "basis": PRICE_RETURN_BASIS, "quality": quality}
        for day, close in zip(days, closes)
    ]


def _random_walk(count: int, *, seed: int, start: float = 100.0) -> list[float]:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0004, 0.015, size=count - 1)
    levels = [start]
    for step in steps:
        levels.append(levels[-1] * (1.0 + step))
    return levels


# ---- volatility and Sharpe ------------------------------------------------------------------------


def test_vol_and_sharpe_match_an_independent_pandas_calculation():
    closes = _random_walk(253, seed=7)
    bars = _bars(closes)
    metrics = stock_risk_metrics(bars)
    adj = pd.Series([row["adj_close"] for row in bars])
    for label, count in RISK_WINDOWS:
        window = adj.iloc[-(count + 1):]
        returns = window.pct_change().dropna()
        assert len(returns) == count
        expected_vol = returns.std(ddof=1) * math.sqrt(252)
        expected_sharpe = returns.mean() / returns.std(ddof=1) * math.sqrt(252)
        assert metrics[label]["vol"] == pytest.approx(expected_vol, rel=1e-12)
        assert metrics[label]["sharpe"] == pytest.approx(expected_sharpe, rel=1e-12)
        assert metrics[label]["returns"] == count
        assert metrics[label]["end"] == date(2026, 10, 2)
        assert metrics[label]["reason"] is None
    # Hand check with a tiny window: closes 100, 110, 99 -> returns +10%, -10%.
    stats = annualized_vol_and_sharpe([100.0, 110.0, 99.0])
    sample_sd = np.std([0.10, -0.10], ddof=1)
    assert stats["vol"] == pytest.approx(sample_sd * math.sqrt(252))
    assert stats["sharpe"] == pytest.approx(0.0 / sample_sd * math.sqrt(252))


def test_split_only_close_is_not_used_when_the_adjusted_close_is_missing():
    """The 1W-1Y return basis never leaks into the risk columns."""
    bars = _bars(_random_walk(64, seed=1))
    bars[10] = {**bars[10], "adj_close": None}
    metrics = stock_risk_metrics(bars)
    assert metrics["3M"]["vol"] is None and metrics["3M"]["sharpe"] is None
    assert metrics["3M"]["reason"].startswith("no dividend-adjusted close")
    mixed = _bars(_random_walk(64, seed=2))
    mixed[5] = {**mixed[5], "basis": "SPLIT_ADJUSTED_UNKNOWN_DIVIDEND"}
    assert stock_risk_metrics(mixed)["3M"]["reason"].startswith("adjustment mismatch")


def test_insufficient_history_missing_session_and_provisional_bar_stay_na():
    short = _bars(_random_walk(60, seed=3))
    metrics = stock_risk_metrics(short)
    assert metrics["3M"]["vol"] is None and metrics["3M"]["reason"].startswith("missing session")
    assert metrics["1Y"]["vol"] is None
    exact = _bars(_random_walk(64, seed=4))
    assert stock_risk_metrics(exact)["3M"]["vol"] is not None
    assert stock_risk_metrics(exact)["1Y"]["vol"] is None
    gap = [row for row in _bars(_random_walk(65, seed=5)) if row["bar_date"] != date(2026, 9, 15)]
    assert stock_risk_metrics(gap)["3M"]["reason"] == "missing session 2026-09-15"
    # The newest bar is excluded while PROVISIONAL, so the window ends one session earlier.
    live = _bars(_random_walk(65, seed=6))
    live[-1] = {**live[-1], "quality": "PROVISIONAL"}
    done = stock_risk_metrics(live)["3M"]
    assert done["end"] == date(2026, 10, 1) and done["vol"] is not None
    assert stock_risk_metrics([])["3M"]["reason"] == "no completed sessions"
    weekend = _bars(_random_walk(64, seed=8)) + [{"bar_date": date(2026, 10, 3), "close": 1.0, "adj_close": 1.0, "basis": PRICE_RETURN_BASIS, "quality": "COMPLETE"}]
    assert stock_risk_metrics(weekend)["3M"]["end"] == date(2026, 10, 2)


def test_zero_volatility_leaves_sharpe_na_and_vol_zero():
    flat = _bars([50.0] * 64)
    metrics = stock_risk_metrics(flat)["3M"]
    assert metrics["vol"] == 0.0
    assert metrics["sharpe"] is None
    assert metrics["reason"] == "zero volatility"
    values, notes = risk_row_cells({"3M": metrics, "1Y": {"vol": None, "sharpe": None, "reason": "missing session 2026-01-02"}})
    assert values == [0.0, None, None, None]
    assert "N/A: zero volatility" in notes[1]
    assert "risk-free 0%" in notes[1] and "√252" in notes[0]
    assert "64 completed sessions" in notes[0] and "253 completed sessions" in notes[2]
    assert "N/A: missing session 2026-01-02" in notes[3]
    assert "zero" not in notes[0]  # the volatility cell itself is valid


def test_window_requires_consecutive_sessions_and_positive_closes():
    bars = _bars(_random_walk(64, seed=9))
    bars[3] = {**bars[3], "adj_close": -1.0}
    assert window_closes(bars, 63)["closes"] == []
    ok = window_closes(_bars(_random_walk(70, seed=10)), 63)
    assert len(ok["closes"]) == 64 and ok["reason"] is None
    assert window_closes(bars, 0)["reason"] == "no completed sessions"


def test_risk_metrics_by_symbol_computes_each_symbol_once_from_the_shared_read():
    bars = {"AAPL": _bars(_random_walk(64, seed=11)), "MSFT": []}
    out = risk_metrics_by_symbol(bars, ["AAPL", "aapl", "MSFT", ""])
    assert set(out) == {"AAPL", "MSFT"}
    assert out["AAPL"]["3M"]["vol"] is not None
    assert out["MSFT"]["3M"]["reason"] == "no completed sessions"


def test_risk_columns_and_caption_follow_the_specification():
    assert RISK_COLUMN_LABELS == ("Vol 3M", "Sharpe 3M", "Vol 1Y", "Sharpe 1Y")
    assert RISK_COLUMN_KINDS == ("volatility", "sharpe", "volatility", "sharpe")
    assert dict(RISK_WINDOWS) == {"3M": 63, "1Y": 252}
    assert "0% risk-free" in STOCK_RISK_CAPTION and "√252" in STOCK_RISK_CAPTION
    assert "dividend- and split-adjusted" in STOCK_RISK_CAPTION
    note = risk_cell_note("1Y", {"vol": 0.2, "sharpe": 1.0, "start": date(2025, 10, 1), "end": date(2026, 10, 2)}, kind="sharpe")
    assert "253 completed sessions 2025-10-01 → 2026-10-02" in note


# ---- heatmap component -------------------------------------------------------------------------


def test_heatmap_column_kinds_format_and_scale_separately():
    columns = ["1D", "Vol 3M", "Sharpe 3M"]
    values = [[0.0123, 0.25, 1.5], [-0.0456, 0.50, -0.5], [None, None, None]]
    option = build_column_scaled_heatmap_option(["A", "B", "C"], columns, values, column_kinds=["return", "volatility", "sharpe"])
    cells = {(cell["row"], cell["column"]): cell for cell in option["series"][0]["data"]}
    assert cells[("A", "1D")]["display"] == "+1.23%"
    assert cells[("A", "Vol 3M")]["display"] == "25.00%"
    assert cells[("B", "Sharpe 3M")]["display"] == "-0.50"
    assert cells[("C", "1D")]["display"] == "N/A" and cells[("C", "Vol 3M")]["display"] == "N/A"
    # Volatility: amber magnitude scale, never green/red. The higher value is the darker amber.
    low_vol = cells[("A", "Vol 3M")]["itemStyle"]["color"]
    high_vol = cells[("B", "Vol 3M")]["itemStyle"]["color"]
    assert low_vol == "rgb(44, 48, 54)" and high_vol == "rgb(191, 134, 46)"
    assert cells[("B", "1D")]["itemStyle"]["color"] != high_vol
    # Sharpe: symmetric around zero on its own column scale, positive green, negative red.
    assert cells[("A", "Sharpe 3M")]["itemStyle"]["color"] == "rgb(61, 140, 90)"
    assert cells[("B", "Sharpe 3M")]["itemStyle"]["color"] == "rgb(89, 53, 57)"
    # Missing cells are neutral and the plotted value for Sharpe is the decimal, not a percent.
    assert cells[("C", "Sharpe 3M")]["itemStyle"]["color"] == "rgb(44, 48, 54)"
    assert cells[("A", "Sharpe 3M")]["value"][2] == 1.5
    assert cells[("A", "Vol 3M")]["value"][2] == pytest.approx(25.0)
    assert option["yAxis"]["triggerEvent"] is False and "rowIds" not in option
    with pytest.raises(ValueError):
        build_column_scaled_heatmap_option(["A"], ["x"], [[1.0]], column_kinds=["ratio"])
    assert HEATMAP_COLUMN_KINDS == ("return", "volatility", "sharpe")


def test_magnitude_scale_degenerates_safely():
    assert magnitude_color_scale([None, None])["degenerate"] is True
    assert magnitude_color_scale([0.2, 0.2])["degenerate"] is True
    assert magnitude_cell_color(0.2, magnitude_color_scale([0.2, 0.2])) == "rgb(44, 48, 54)"
    assert magnitude_cell_color(None, magnitude_color_scale([0.1, 0.3])) == "rgb(44, 48, 54)"
    mid = magnitude_cell_color(0.2, magnitude_color_scale([0.1, 0.3]))
    assert mid == "rgb(118, 91, 50)"


def test_clickable_heatmap_carries_stable_row_ids_not_display_text():
    option = build_column_scaled_heatmap_option(
        ["Technology", "Energy"],
        ["1D"],
        [[0.01], [-0.02]],
        row_ids=["XLK", "XLE"],
        clickable=True,
    )
    assert option["clickable"] is True
    assert option["rowIds"] == ["XLK", "XLE"]
    assert option["yAxis"]["triggerEvent"] is True
    assert option["series"][0]["emphasis"]["disabled"] is False
    assert [cell["rowId"] for cell in option["series"][0]["data"]] == ["XLK", "XLE"]
    assert heatmap_click_row_id({"rowId": "XLK", "nonce": "1"}) == "XLK"
    assert heatmap_click_row_id({"rowId": "", "nonce": "1"}) is None
    assert heatmap_click_row_id(None) is None
    js = (ROOT / "market_intelligence" / "components" / "tenor_chart" / "frontend" / "chart.js").read_text(encoding="utf-8")
    assert 'setTriggerValue("row_click"' in js
    assert 'params.componentType === "yAxis"' in js, "row labels are clickable, not just cells"
    assert "pointer" in js
    assert "min_width" in js


def test_heatmap_click_dispatches_once_per_browser_click(monkeypatch):
    """A rerun for any other reason must not replay the last click; a new click on the same row must."""
    from market_intelligence.components import tenor_chart

    class _Result(dict):
        def __getattr__(self, name):
            return self[name]

    events: list = []
    mounted: dict = {}

    def fake_component():
        def mount(*, data, key, width, height, **kwargs):
            mounted["kwargs"] = kwargs
            return _Result({"row_click": events[-1] if events else None})

        return mount

    monkeypatch.setattr(tenor_chart, "_component", fake_component)
    monkeypatch.setattr(tenor_chart.st, "session_state", {})
    seen: list = []
    option = build_column_scaled_heatmap_option(["Technology"], ["1D"], [[0.01]], row_ids=["XLK"], clickable=True)
    tenor_chart.render_echarts(option, key="k", on_row_click=seen.append)
    assert "on_row_click_change" in mounted["kwargs"]
    assert seen == []
    events.append({"rowId": "XLK", "nonce": "a"})
    tenor_chart.render_echarts(option, key="k", on_row_click=seen.append)
    tenor_chart.render_echarts(option, key="k", on_row_click=seen.append)  # same trigger replayed: ignored
    assert seen == ["XLK"]
    events.append({"rowId": "XLK", "nonce": "b"})  # repeated click on the same sector fires again
    tenor_chart.render_echarts(option, key="k", on_row_click=seen.append)
    assert seen == ["XLK", "XLK"]
    events.append({"rowId": "XLE", "nonce": "c"})
    tenor_chart.render_echarts(option, key="k", on_row_click=seen.append)
    assert seen == ["XLK", "XLK", "XLE"]


# ---- sector drill navigation -----------------------------------------------------------------------


def test_sector_resolver_accepts_etf_canonical_name_and_group_label_only():
    groups = dict(SECTOR_ETFS)
    for sector, etf in SECTOR_PROXIES.items():
        assert subsector_group_for(etf) == groups[etf]
        assert subsector_group_for(sector) == groups[etf]
        assert subsector_group_for(groups[etf]) == groups[etf]
    assert subsector_group_for("xlk") == "Tech"
    assert subsector_group_for("") is None and subsector_group_for(None) is None and subsector_group_for("+1.23%") is None
    drill = subsector_drill("XLE")
    assert drill == {"route_id": "us_markets", "anchor": SUBSECTOR_ANCHOR, "state": {SUBSECTOR_SELECTOR_KEY: "Energy"}}


def test_select_subsector_primes_selector_and_one_scroll_request(monkeypatch):
    from market_intelligence import navigation_links

    state: dict = {}
    monkeypatch.setattr(navigation_links.st, "session_state", state)
    assert select_subsector("XLF") is True
    assert state == {SUBSECTOR_SELECTOR_KEY: "Financials", PENDING_SCROLL_KEY: SUBSECTOR_ANCHOR}
    assert select_subsector("not-a-sector") is False
    assert state[SUBSECTOR_SELECTOR_KEY] == "Financials"


def _fake_us_read(fn_name, *args, **kwargs):
    if fn_name == "us_markets_history":
        return {"prices": {}, "bounds": {}, "latest_price": {}, "returns": {}, "sessions": []}
    if fn_name == "aligned_us_equity_returns":
        sectors = [
            {"label": name, "symbol": etf, "values": [0.01, 0.02, 0.03, None, None, None], "notes": [None] * 6}
            for name, etf in SECTOR_PROXIES.items()
        ]
        return {
            "available": True,
            "reason": None,
            "method": "equal_dollar_daily_rebalance_v1",
            "source_id": "EQUITY_EOD",
            "endpoint": "2026-10-02",
            "adjustment_basis": "IBKR_ADJUSTED_LAST",
            "provider": "IBKR",
            "spy_returns": {"1D": 0.01, "1W": 0.02, "1M": 0.03, "3M": None, "6M": None, "1Y": None},
            "sectors": sectors,
            "subsectors": {},
            "themes_omitted": [],
        }
    if fn_name == "dashboard_price_bars":
        rows = []
        for symbol in ("NVDA", "SPY"):
            for row in _bars(_random_walk(260, seed=12)):
                rows.append(
                    {
                        "symbol": symbol,
                        "bar_date": row["bar_date"],
                        "close_price": row["close"],
                        "adj_close_price": row["adj_close"] if symbol == "NVDA" else None,
                        "adjustment_basis": row["basis"],
                        "bar_quality": row["quality"],
                        "bar_ts": None,
                    }
                )
        return rows
    raise AssertionError(fn_name)


def _fake_us_quote_read(fn_name, *args, **kwargs):
    if fn_name == "equity_live_context":
        return {"quotes_available": False, "by_symbol": {}, "quotes_as_of_label": "Live quotes unavailable"}
    if fn_name == "dashboard_quotes_latest":
        stamp = datetime(2026, 10, 2, 16, 5, tzinfo=ET)
        return [
            {
                "symbol": symbol,
                "last_price": 100.0,
                "quote_ts": stamp,
                "quote_status": "OK",
                "market_data_type": "DELAYED",
                "provenance": {"current_price": 100.0, "open_to_current": 0.01, "session_date": "2026-10-02", "current_price_field": "regularMarketPrice"},
            }
            for symbol in ("NVDA", "SPY", "XLK")
        ]
    raise AssertionError(fn_name)


def _us_app(monkeypatch) -> AppTest:
    monkeypatch.setattr("market_intelligence.ui.cached_read", _fake_us_read)
    monkeypatch.setattr("market_intelligence.ui.cached_quote_read", _fake_us_quote_read)
    at = AppTest.from_file(str(ROOT / "pages" / "22_US_Markets.py"), default_timeout=40)
    at.run()
    assert not at.exception, [item.value for item in at.exception]
    return at


def test_us_markets_sector_click_selects_subsector_and_scrolls_in_one_run(monkeypatch):
    """Drive the heatmap click through the shared handler inside a real page run."""
    captured: dict = {}
    real = markets_ui.column_scaled_return_heatmap

    def spy(*args, **kwargs):
        if kwargs.get("key", "").startswith("us_sector_heatmap_"):
            captured["row_ids"] = list(kwargs["row_ids"])
            captured["handler"] = kwargs["on_row_click"]
            if captured.get("click"):
                kwargs["on_row_click"](captured.pop("click"))
        if kwargs.get("key", "").startswith("us_stock_heatmap_"):
            captured.setdefault("stock_columns", list(args[1]))
            captured.setdefault("stock_kinds", list(kwargs.get("column_kinds") or []))
            captured.setdefault("stock_values", [list(row) for row in args[2]])
            captured.setdefault("stock_notes", [list(row) for row in kwargs.get("notes") or []])
        return real(*args, **kwargs)

    monkeypatch.setattr(markets_ui, "column_scaled_return_heatmap", spy)
    scrolls: list = []
    monkeypatch.setattr("market_intelligence.navigation_links.components.html", lambda html, **kwargs: scrolls.append(html))
    at = _us_app(monkeypatch)
    assert captured["row_ids"] == list(SECTOR_PROXIES.values()), "rows are identified by the sector ETF, not the label"
    assert next(w for w in at.selectbox if w.label == "Sector").value == "Tech"
    assert scrolls == [], "no scroll without a navigation action"
    captured["click"] = "XLE"  # a numeric cell in the Energy row reports the same id as its label
    at.run()
    assert not at.exception, [item.value for item in at.exception]
    assert next(w for w in at.selectbox if w.label == "Sector").value == "Energy"
    assert at.session_state[SUBSECTOR_SELECTOR_KEY] == "Energy"
    assert PENDING_SCROLL_KEY not in at.session_state, "the one-time scroll request is cleared after handling"
    assert len(scrolls) == 1 and '"subsector-performance"' in scrolls[0]
    at.run()  # an unrelated rerun keeps the selection and does not scroll again
    assert next(w for w in at.selectbox if w.label == "Sector").value == "Energy"
    assert len(scrolls) == 1
    captured["click"] = "XLE"  # repeated click on the same sector scrolls again
    at.run()
    assert len(scrolls) == 2 and next(w for w in at.selectbox if w.label == "Sector").value == "Energy"
    # The frontend does not re-execute an identical iframe body, so a repeated
    # request for the same anchor must differ from the previous one.
    assert scrolls[1] != scrolls[0]
    captured["click"] = "XLK"
    at.run()
    assert next(w for w in at.selectbox if w.label == "Sector").value == "Tech" and len(scrolls) == 3


def test_individual_stock_heatmap_appends_four_risk_columns_after_the_returns(monkeypatch):
    captured: dict = {}
    real = markets_ui.column_scaled_return_heatmap

    def spy(*args, **kwargs):
        if kwargs.get("key", "").startswith("us_stock_heatmap_") and "columns" not in captured:
            captured["columns"] = list(args[1])
            captured["kinds"] = list(kwargs.get("column_kinds") or [])
            captured["labels"] = list(args[0])
            captured["values"] = [list(row) for row in args[2]]
            captured["notes"] = [list(row) for row in kwargs.get("notes") or []]
        return real(*args, **kwargs)

    monkeypatch.setattr(markets_ui, "column_scaled_return_heatmap", spy)
    at = _us_app(monkeypatch)
    returns = list(markets_ui._since_open_columns(STOCK_RETURN_HORIZONS))
    assert captured["columns"] == returns + list(RISK_COLUMN_LABELS)
    assert captured["kinds"] == ["return"] * len(returns) + list(RISK_COLUMN_KINDS)
    nvda = captured["labels"].index(next(label for label in captured["labels"] if label.startswith("NVDA")))
    row = captured["values"][nvda]
    assert len(row) == len(returns) + 4
    vol_3m, sharpe_3m, vol_1y, sharpe_1y = row[-4:]
    assert vol_3m is not None and vol_1y is not None and sharpe_3m is not None and sharpe_1y is not None
    assert vol_3m != vol_1y
    expected = stock_risk_metrics(_bars(_random_walk(260, seed=12)))
    assert vol_3m == pytest.approx(expected["3M"]["vol"]) and sharpe_1y == pytest.approx(expected["1Y"]["sharpe"])
    other = next(index for index, label in enumerate(captured["labels"]) if not label.startswith("NVDA"))
    assert captured["values"][other][-4:] == [None, None, None, None], "no stored bars -> N/A, never zero"
    assert "N/A: no completed sessions" in captured["notes"][other][-1]
    text = "\n".join(str(c.value) for c in at.caption)
    assert "0% risk-free" in text and "Vol 3M / 1Y" in text


# ---- Overview link table ---------------------------------------------------------------------------


def test_link_table_rows_validate_ids_and_tones():
    rows = link_table_rows([{"id": "XLK", "cells": [{"text": "Technology"}, {"text": "+1.00%", "tone": "positive"}, "—"], "help": "open"}])
    assert rows == [{"id": "XLK", "cells": [{"text": "Technology", "tone": "plain"}, {"text": "+1.00%", "tone": "positive"}, {"text": "—", "tone": "plain"}], "help": "open"}]
    with pytest.raises(ValueError):
        link_table_rows([{"id": "", "cells": []}])
    with pytest.raises(ValueError):
        link_table_rows([{"id": "X", "cells": [{"text": "a", "tone": "loud"}]}])
    assert link_table_click_row_id({"rowId": "XLK"}) == "XLK" and link_table_click_row_id("XLK") is None
    js = (ROOT / "market_intelligence" / "components" / "link_table" / "frontend" / "table.js").read_text(encoding="utf-8")
    assert 'setTriggerValue("row_click"' in js and "dataset.rowId" in js


# ---- FX header and heatmap -------------------------------------------------------------------------


def test_fx_header_has_every_configured_instrument_and_dxy_exactly_once():
    latest = date(2026, 10, 2)
    levels = {}
    for index, spec in enumerate(FX_INSTRUMENTS):
        days = _sessions(latest, 30)
        levels[spec.instrument_id] = [(day, 100.0 + index + offset * 0.1) for offset, day in enumerate(days)]
    levels["USDCHF"] = levels["USDCHF"][:-10]  # newest observation 10 sessions old -> stale
    levels.pop("AUDUSD")  # nothing stored -> N/A card, still present
    cards = forex_header_cards(levels, latest=latest)
    assert [card["instrument_id"] for card in cards] == [spec.instrument_id for spec in FX_INSTRUMENTS]
    assert [card["label"] for card in cards].count("DXY") == 1
    assert "USD/CNH" in [card["label"] for card in cards]
    assert len(cards) == len(FX_INSTRUMENTS) == 8
    eur = next(card for card in cards if card["instrument_id"] == "EURUSD")
    assert set(eur["returns"]) == {"1D", "1W", "1M"} and eur["as_of"] == latest and eur["stale"] is False
    # Quote convention: a rising level is a positive return and is not inverted for USD/xxx pairs.
    jpy = next(card for card in cards if card["instrument_id"] == "USDJPY")
    assert jpy["returns"]["1D"] > 0 and jpy["returns"]["1W"] > 0
    assert jpy["returns"]["1D"] == pytest.approx(latest_window_returns(levels["USDJPY"], FX_CARD_WINDOWS)["1D"])
    chf = next(card for card in cards if card["instrument_id"] == "USDCHF")
    assert chf["stale"] is True and chf["value"] is not None
    aud = next(card for card in cards if card["instrument_id"] == "AUDUSD")
    assert aud["value"] is None and aud["as_of"] is None and aud["returns"] == {"1D": None, "1W": None, "1M": None}
    html = fx_card_html(aud)
    assert "N/A" in html and "no stored observation" in html
    html_chf = fx_card_html(chf)
    assert "stale" in html_chf and "USD/CHF" in html_chf
    html_jpy = fx_card_html(jpy)
    assert "+" in html_jpy and "1D" in html_jpy and "1M" in html_jpy
    assert fx_header_card("DXY", "DXY", [], latest=None)["returns"] == {"1D": None, "1W": None, "1M": None}


def test_fx_heatmap_is_colored_per_column():
    columns = [label for label, _lag in FX_WINDOWS]
    values = [[0.001, 0.05, None, 0.0, 0.0, 0.15], [-0.002, -0.01, None, 0.0, 0.0, -0.3]]
    option = build_column_scaled_heatmap_option(["EUR", "JPY"], columns, values)
    cells = {(cell["row"], cell["column"]): cell for cell in option["series"][0]["data"]}
    # 1D and 1Y columns have their own symmetric limits: the +0.1% 1D and the +15% 1Y are both half of their column max.
    assert cells[("EUR", "1D")]["itemStyle"]["color"] == cells[("EUR", "1Y")]["itemStyle"]["color"] != "rgb(44, 48, 54)"
    assert cells[("JPY", "1D")]["itemStyle"]["color"] == "rgb(179, 64, 64)"  # column minimum: full red
    assert cells[("JPY", "1Y")]["itemStyle"]["color"] == "rgb(179, 64, 64)"
    # All-missing and all-zero columns stay neutral; missing cells read N/A.
    for row in ("EUR", "JPY"):
        assert cells[(row, "1M")]["display"] == "N/A" and cells[(row, "1M")]["itemStyle"]["color"] == "rgb(44, 48, 54)"
        assert cells[(row, "3M")]["display"] == "+0.00%" and cells[(row, "3M")]["itemStyle"]["color"] == "rgb(44, 48, 54)"
    assert column_color_scale([0.0, 0.0])["degenerate"] is True
    source = (ROOT / "market_intelligence" / "cross_asset_ui.py").read_text(encoding="utf-8")
    assert 'column_scaled_return_heatmap(heat_labels' in source
    assert 'return_heatmap(list(row_labels), list(columns), values, key=key)' in source, "commodities keep their heatmap"


# ---- ingest: dividend-adjusted closes -------------------------------------------------------------


def test_yahoo_rows_carry_adj_close_and_a_dividend_triggers_repair():
    frame = pd.DataFrame(
        {
            "Close": [100.0, 101.0],
            "Adj Close": [98.0, 101.0],
            "Open": [99.0, 100.0],
            "High": [101.0, 102.0],
            "Low": [98.0, 99.0],
            "Volume": [10, 11],
            "Stock Splits": [0.0, 0.0],
            "Dividends": [0.0, 0.5],
        },
        index=pd.to_datetime(["2026-09-28", "2026-09-29"]),
    )
    rows = parse_daily_closes(frame, "AAPL")
    assert [row["adj_close"] for row in rows] == [98.0, 101.0]
    assert [row["close"] for row in rows] == [100.0, 101.0], "the split-only close is unchanged"
    assert series_has_dividend(rows) is True and series_needs_repair(rows) is True
    assert series_has_dividend(rows[:1]) is False and series_needs_repair(rows[:1]) is False
    no_adj = parse_daily_closes(frame.drop(columns=["Adj Close", "Dividends"]), "AAPL")
    assert all(row["adj_close"] is None for row in no_adj) and series_needs_repair(no_adj) is False


def test_migration_048_exposes_adj_close_on_the_dashboard_view_and_read_model_guards_it():
    sql = (ROOT / "db" / "migrations" / "048_yahoo_price_daily_adj_close.sql").read_text(encoding="utf-8")
    assert "CREATE OR REPLACE VIEW mi_v_yahoo_price_daily" in sql and "b.adj_close_price" in sql
    assert "DROP" not in sql.upper()
    read = (ROOT / "market_intelligence" / "read_models.py").read_text(encoding="utf-8")
    assert "NULL AS adj_close_price" in read
    ingest = (ROOT / "market_intelligence" / "yahoo_price_history.py").read_text(encoding="utf-8")
    assert "adj_close_price = EXCLUDED.adj_close_price" in ingest
    assert "symbols_missing_adj_close" in ingest and "adj_close_backfilled" in ingest
    page = (ROOT / "market_intelligence" / "markets_ui.py").read_text(encoding="utf-8")
    assert "import yfinance" not in page and '"adj_close": row.get("adj_close_price")' in page
