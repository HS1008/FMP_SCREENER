"""Collapsed stock groups skip heatmap mounts and risk; open groups read only their symbols."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from streamlit.testing.v1 import AppTest

from market_intelligence import markets_ui, stock_board
from market_intelligence.ibkr_live_universe import stock_groups_by_era, subsector_groups
from market_intelligence.navigation_links import PENDING_SCROLL_KEY, SUBSECTOR_ANCHOR, SUBSECTOR_SELECTOR_KEY
from market_intelligence.price_returns import PRICE_RETURN_BASIS
from market_intelligence.taxonomy import SECTOR_PROXIES

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def _history() -> dict:
    return {"prices": {}, "bounds": {}, "latest_price": {}, "returns": {}, "sessions": [], "series": {}}


def _aligned() -> dict:
    return {
        "available": True,
        "sectors": [
            {"label": name, "symbol": etf, "values": [0.01, 0.02, 0.03, None, None, None], "notes": [None] * 6}
            for name, etf in SECTOR_PROXIES.items()
        ],
        "spy_returns": {"1D": 0.01, "1W": 0.02, "1M": 0.03, "3M": None, "6M": None, "1Y": None},
        "method": "equal_dollar_daily_rebalance_v1",
        "endpoint": "2026-10-02",
        "adjustment_basis": "IBKR_ADJUSTED_LAST",
    }


def _quote(symbol: str, *, price: float, open_: float) -> dict:
    stamp = datetime(2026, 10, 12, 15, 10, tzinfo=ET)
    return {
        "symbol": symbol,
        "display_name": symbol,
        "instrument_id": symbol,
        "last_price": price,
        "quote_ts": stamp,
        "provenance": {
            "symbol": symbol,
            "current_price": price,
            "session_open": open_,
            "session_date": "2026-10-12",
            "last_close": 99.0,
            "last_close_date": "2026-10-09",
        },
    }


def _daily(symbol: str) -> list[dict]:
    rows = []
    day = date(2026, 10, 9)
    price = 100.0
    while len(rows) < 40:
        if day.weekday() < 5:
            rows.append(
                {
                    "symbol": symbol,
                    "bar_date": day,
                    "close_price": price,
                    "adj_close_price": price,
                    "adjustment_basis": PRICE_RETURN_BASIS,
                    "bar_quality": "COMPLETE",
                    "bar_ts": None,
                }
            )
            price += 0.5
        day = date.fromordinal(day.toordinal() - 1)
    rows.reverse()
    return rows


class _Board:
    def __init__(self) -> None:
        self.reads: list[tuple] = []
        self.mounts: list[str] = []
        self.risk_symbols: list[str] = []
        self.quotes_fail = False
        self.history_fail = False
        self.stock_values: list[list] = []
        self.quotes = [_quote("NVDA", price=102.0, open_=100.0), _quote("ORCL", price=98.0, open_=100.0)]

    def install(self, monkeypatch) -> None:
        real_heat = markets_ui.column_scaled_return_heatmap
        real_risk = stock_board.stock_risk_metrics

        def spy_heat(*args, **kwargs):
            key = str(kwargs.get("key"))
            self.mounts.append(key)
            if key.startswith("us_stock_heatmap_"):
                self.stock_values = [list(row) for row in args[2]]
            return real_heat(*args, **kwargs)

        def spy_risk(bars, **kwargs):
            self.risk_symbols.append("called")
            return real_risk(bars, **kwargs)

        def fake_read(fn_name, *args, **kwargs):
            self.reads.append((fn_name, args, kwargs))
            if fn_name == "us_markets_history":
                return _history()
            if fn_name == "aligned_us_equity_returns":
                assert kwargs.get("include_stock_baskets") is False
                return _aligned()
            if fn_name == "dashboard_price_bars":
                if self.history_fail:
                    raise RuntimeError("history down")
                requested = {str(symbol).upper() for symbol in (args[0] if args else ())}
                rows = []
                for symbol in sorted(requested):
                    rows.extend(_daily(symbol))
                return rows
            if fn_name == "stock_history_bars":
                return {"symbol": args[0], "bars": [], "earliest_stored": None, "data_version": None}
            raise AssertionError(fn_name)

        def fake_quote(fn_name, *args, **kwargs):
            self.reads.append(("quote:" + fn_name, args, kwargs))
            if fn_name == "dashboard_quotes_latest":
                if self.quotes_fail:
                    raise RuntimeError("quotes down")
                return list(self.quotes)
            if fn_name == "equity_live_context":
                return {"quotes_available": False, "by_symbol": {}}
            raise AssertionError(fn_name)

        fake_read.clear = lambda: None
        fake_quote.clear = lambda: None
        monkeypatch.setattr(markets_ui, "column_scaled_return_heatmap", spy_heat)
        monkeypatch.setattr(stock_board, "stock_risk_metrics", spy_risk)
        monkeypatch.setattr("market_intelligence.ui.cached_read", fake_read)
        monkeypatch.setattr("market_intelligence.ui.cached_quote_read", fake_quote)
        monkeypatch.setattr(markets_ui, "_now", lambda: datetime(2026, 10, 12, 15, 11, tzinfo=ET))


def _page(monkeypatch, board: _Board | None = None) -> tuple[AppTest, _Board]:
    board = board or _Board()
    board.install(monkeypatch)
    app = AppTest.from_file(str(ROOT / "pages" / "22_US_Markets.py"), default_timeout=60)
    app.run()
    assert not app.exception, [item.value for item in app.exception]
    return app, board


def _stock_mounts(board: _Board) -> list[str]:
    return [key for key in board.mounts if key.startswith("us_stock_heatmap_")]


def test_collapsed_groups_mount_no_stock_heatmap_and_skip_risk(monkeypatch):
    stock_board.clear_preparation_cache()
    _app, board = _page(monkeypatch)
    assert _stock_mounts(board) == []
    assert board.risk_symbols == []
    bar_calls = [call for call in board.reads if call[0] == "dashboard_price_bars"]
    assert len(bar_calls) == 1
    requested = set(bar_calls[0][1][0])
    subsector = {symbol for symbol, _label in dict(subsector_groups())["Tech"]} | {"SPY"}
    assert requested == subsector
    assert "NVDA" not in requested and "ORCL" not in requested
    aligned = [call for call in board.reads if call[0] == "aligned_us_equity_returns"]
    assert aligned and aligned[0][2]["include_stock_baskets"] is False


def test_opening_one_group_mounts_only_that_heatmap(monkeypatch):
    stock_board.clear_preparation_cache()
    app, board = _page(monkeypatch)
    board.mounts.clear()
    board.risk_symbols.clear()
    board.reads.clear()
    next(widget for widget in app.text_input if widget.label == "Search").set_value("ORCL").run()
    assert not app.exception, [item.value for item in app.exception]
    stock_mounts = _stock_mounts(board)
    assert len(stock_mounts) == 3, stock_mounts
    assert board.risk_symbols, "an open group calculates risk"
    bar_calls = [call for call in board.reads if call[0] == "dashboard_price_bars"]
    requested = set()
    for call in bar_calls:
        requested.update(call[1][0])
    assert "ORCL" in requested
    assert "NVDA" not in requested


def test_expand_all_then_collapse_all(monkeypatch):
    stock_board.clear_preparation_cache()
    app, board = _page(monkeypatch)
    board.mounts.clear()
    board.risk_symbols.clear()
    board.reads.clear()
    next(button for button in app.button if button.label == "Expand all").click().run()
    assert not app.exception, [item.value for item in app.exception]
    groups = [group for _title, _caption, era_groups in stock_groups_by_era() for group, _members in era_groups]
    assert len(_stock_mounts(board)) == len(groups)
    assert board.risk_symbols
    stock_reads = [
        call for call in board.reads if call[0] == "dashboard_price_bars" and "NVDA" in set(call[1][0])
    ]
    assert len(stock_reads) == 1
    requested = list(stock_reads[0][1][0])
    assert requested.count("NVDA") == 1
    board.mounts.clear()
    board.risk_symbols.clear()
    next(button for button in app.button if button.label == "Collapse all").click().run()
    assert not app.exception, [item.value for item in app.exception]
    assert _stock_mounts(board) == []
    assert board.risk_symbols == []


def test_quote_failure_keeps_completed_history_and_labels_the_failure(monkeypatch):
    stock_board.clear_preparation_cache()
    board = _Board()
    board.quotes_fail = True
    app, board = _page(monkeypatch, board)
    app.session_state[stock_board.group_state_key("Hyperscalers")] = True
    app.session_state[stock_board.OPEN_MAP_KEY] = {"Hyperscalers": True}
    board.mounts.clear()
    app.run()
    assert not app.exception, [item.value for item in app.exception]
    text = "\n".join(str(item.value) for item in app.caption)
    assert "Stored quote read failed (RuntimeError)" in text
    assert _stock_mounts(board), "history still draws when quotes fail"
    assert board.stock_values
    assert all(row[0] is None for row in board.stock_values)
    assert any(value is not None for row in board.stock_values for value in row[1:6])
    assert all(value is None for row in board.stock_values for value in row[6:]), "40 sessions is short of the 63-session risk window"


def test_search_sort_and_refresh_keep_the_clicked_ticker(monkeypatch):
    stock_board.clear_preparation_cache()
    app, board = _page(monkeypatch)
    captured: dict = {}
    real = markets_ui.column_scaled_return_heatmap

    def spy(*args, **kwargs):
        if str(kwargs.get("key", "")).startswith("us_stock_heatmap_"):
            captured["row_ids"] = list(kwargs["row_ids"])
            captured["columns"] = list(args[1])
            if captured.get("click") in kwargs["row_ids"]:
                kwargs["on_row_click"](captured.pop("click"))
        return real(*args, **kwargs)

    monkeypatch.setattr(markets_ui, "column_scaled_return_heatmap", spy)
    next(widget for widget in app.text_input if widget.label == "Search").set_value("NVDA").run()
    app.session_state[stock_board.SORT_KEY] = "Ticker"
    app.session_state[stock_board.SORT_DESC_KEY] = True
    captured["click"] = "NVDA"
    app.run()
    assert not app.exception, [item.value for item in app.exception]
    assert captured["row_ids"] == ["NVDA"]
    assert captured["columns"][0] == "1D"
    assert "click" not in captured, "the NVDA row was the click target"
    from market_intelligence.stock_dialog import STOCK_DIALOG_KEY

    assert app.session_state[STOCK_DIALOG_KEY] == "NVDA"
    assert app.session_state[stock_board.QUERY_KEY] == "NVDA"
    captured.clear()
    next(button for button in app.button if button.label == "Refresh").click().run()
    assert not app.exception, [item.value for item in app.exception]
    assert app.session_state[stock_board.QUERY_KEY] == "NVDA"
    assert app.session_state[stock_board.SORT_KEY] == "Ticker"
    assert next(widget for widget in app.text_input if widget.label == "Search").value == "NVDA"
    assert captured.get("row_ids") == ["NVDA"]


def test_sector_drill_opens_a_collapsed_subsector_section(monkeypatch):
    app, _board = _page(monkeypatch)
    key = stock_board.section_state_key("subsector-performance")
    app.session_state[key] = False
    app.run()
    assert not app.exception, [item.value for item in app.exception]
    closed = next(widget for widget in app.expander if widget.label == "Subsector Performance")
    assert closed.proto.expanded is False
    scrolls: list[str] = []
    monkeypatch.setattr("market_intelligence.navigation_links.components.html", lambda html, **kwargs: scrolls.append(html))
    real = markets_ui.column_scaled_return_heatmap

    def spy(*args, **kwargs):
        if str(kwargs.get("key", "")).startswith("us_sector_heatmap_"):
            kwargs["on_row_click"]("XLE")
        return real(*args, **kwargs)

    monkeypatch.setattr(markets_ui, "column_scaled_return_heatmap", spy)
    app.run()
    assert not app.exception, [item.value for item in app.exception]
    opened = next(widget for widget in app.expander if widget.label == "Subsector Performance")
    assert opened.proto.expanded is True
    assert app.session_state[SUBSECTOR_SELECTOR_KEY] == "Energy"
    assert PENDING_SCROLL_KEY not in app.session_state
    assert scrolls and SUBSECTOR_ANCHOR in scrolls[0]


def test_group_stays_open_when_its_label_changes(monkeypatch):
    board = _Board()
    app, board = _page(monkeypatch, board)
    app.session_state[stock_board.group_state_key("Semiconductors")] = True
    app.session_state[stock_board.OPEN_MAP_KEY] = {"Semiconductors": True}
    app.run()
    assert not app.exception, [item.value for item in app.exception]
    first = next(widget for widget in app.expander if str(widget.label).startswith("Semiconductors"))
    assert first.proto.expanded is True
    board.quotes = [_quote("NVDA", price=80.0, open_=100.0)]
    app.run()
    assert not app.exception, [item.value for item in app.exception]
    second = next(widget for widget in app.expander if str(widget.label).startswith("Semiconductors"))
    assert second.proto.expanded is True
    assert second.label != first.label
