"""Stock-board preparation: sort, coverage, scoped reads, and cached history."""

from __future__ import annotations

from datetime import date

import pytest

from market_intelligence import markets_read, read_models, stock_board
from market_intelligence.ibkr_live_universe import STOCK_GROUPS, stock_groups_by_era
from market_intelligence.price_returns import PRICE_RETURN_BASIS
from market_intelligence.risk_metrics import stock_risk_metrics
from market_intelligence.taxonomy import SECTOR_PROXIES, stock_subsector_baskets


def _bars(symbol: str, closes: list[float]) -> list[dict]:
    rows = []
    day = date(2026, 10, 2)
    for close in reversed(closes):
        while day.weekday() >= 5:
            day = date.fromordinal(day.toordinal() - 1)
        rows.append(
            {
                "bar_date": day,
                "close": close,
                "adj_close": close,
                "basis": PRICE_RETURN_BASIS,
                "quality": "COMPLETE",
            }
        )
        day = date.fromordinal(day.toordinal() - 1)
    rows.reverse()
    return rows


def test_group_keys_are_stable_and_unique():
    keys = [stock_board.group_state_key(group) for group, _members in STOCK_GROUPS]
    assert len(keys) == len(set(keys))
    assert stock_board.group_state_key("Semiconductors") == stock_board.group_state_key("Semiconductors")
    assert "2.00" not in stock_board.group_state_key("Semiconductors")


def test_sort_puts_missing_values_last_and_breaks_ties_by_ticker():
    rows = [{"symbol": symbol} for symbol in ("MSFT", "AAPL", "NVDA", "AMD")]
    values = {
        "MSFT": {"1D": 0.02},
        "AAPL": {"1D": 0.02},
        "NVDA": {"1D": None},
        "AMD": {"1D": -0.01},
    }
    descending = [row["symbol"] for row in stock_board.sort_rows(rows, values, sort_by="1D", descending=True)]
    assert descending == ["AAPL", "MSFT", "AMD", "NVDA"]
    ascending = [row["symbol"] for row in stock_board.sort_rows(rows, values, sort_by="1D", descending=False)]
    assert ascending == ["AMD", "AAPL", "MSFT", "NVDA"]
    assert [row["symbol"] for row in stock_board.sort_rows(rows, values, sort_by=stock_board.SORT_CATALOG, descending=True)] == [
        "MSFT",
        "AAPL",
        "NVDA",
        "AMD",
    ]
    assert [row["symbol"] for row in stock_board.sort_rows(rows, values, sort_by=stock_board.SORT_TICKER, descending=True)] == [
        "NVDA",
        "MSFT",
        "AMD",
        "AAPL",
    ]


def test_search_matches_ticker_group_and_curated_company_only():
    assert stock_board.member_matches("nvda", group="Mag 8", era="Buildout", symbol="NVDA")
    assert stock_board.member_matches("palantir", group="AI Applications", era="Physical", symbol="PLTR")
    assert stock_board.member_matches("quantum", group="Quantum", era="Physical", symbol="IONQ")
    assert not stock_board.member_matches("not-a-name", group="Quantum", era="Physical", symbol="IONQ")
    assert stock_board.company_name("PLTR") == "Palantir Technologies Inc."
    assert stock_board.company_name("ZZZZ") == ""


def test_column_view_keeps_every_metric_reachable():
    returns = ["1D", "1W"]
    risks = ["Vol 3M", "Sharpe 3M"]
    values = [[0.1, 0.2, 0.3, 0.4]]
    notes = [["a", "b", "c", "d"]]
    columns, kinds, sliced, sliced_notes = stock_board.slice_columns("Return", returns, risks, ["volatility", "sharpe"], values, notes)
    assert columns == returns and kinds == ["return", "return"] and sliced == [[0.1, 0.2]] and sliced_notes == [["a", "b"]]
    columns, kinds, sliced, sliced_notes = stock_board.slice_columns("Risk", returns, risks, ["volatility", "sharpe"], values, notes)
    assert columns == risks and kinds == ["volatility", "sharpe"] and sliced == [[0.3, 0.4]]
    columns, _kinds, sliced, _notes = stock_board.slice_columns("All", returns, risks, ["volatility", "sharpe"], values, notes)
    assert columns == returns + risks and sliced == values
    with pytest.raises(ValueError):
        stock_board.slice_columns("Other", returns, risks, ["volatility", "sharpe"], values, notes)


def test_coverage_label_shows_partial_median_and_pending():
    label = stock_board.group_summary_label("Power — Nuclear", count=6, available=[0.10], pending=2, stale=1)
    assert label.startswith("Power — Nuclear · 6 names · 1D available 1/6 · median 1D +10.00%")
    assert "pending 2" in label and "stale 1" in label
    empty = stock_board.group_summary_label("EDA / Chip Design", count=2, available=[])
    assert "1D available 0/2" in empty and "median" not in empty


def test_history_cache_misses_when_a_stored_bar_is_corrected():
    stock_board.clear_preparation_cache()
    bars = _bars("NVDA", [100.0, 101.0, 102.0])
    first = stock_board.cached_horizons("NVDA", bars)
    assert stock_board.cached_horizons("NVDA", bars) is first
    corrected = [dict(row) for row in bars]
    corrected[-1] = dict(corrected[-1], close=110.0)
    assert stock_board.cached_horizons("NVDA", corrected) is not first
    risk = stock_board.cached_risk("NVDA", bars)
    assert stock_board.cached_risk("NVDA", bars) is risk
    assert risk.keys() == stock_risk_metrics(bars).keys()


def test_empty_symbol_batch_does_not_query():
    class Boom:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("empty symbol batch must not query")

    assert read_models.dashboard_price_bars(Boom(), symbols=()) == []
    assert read_models.dashboard_price_bars(Boom(), symbols=("", "  ")) == []


def test_symbol_batch_is_one_deduped_in_clause(monkeypatch):
    calls: list[tuple[str, dict | None]] = []

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def first(self):
            return (1,)

        def mappings(self):
            return self

        def all(self):
            return self._rows

    class Conn:
        def execute(self, statement, params=None):
            calls.append((str(statement), params))
            return Result([])

    monkeypatch.setattr(read_models, "_view_exists", lambda _conn, _name: True)
    assert read_models.dashboard_price_bars(Conn(), symbols=("nvda", "NVDA", "AMD")) == []
    sql, params = calls[-1]
    assert "symbol IN" in sql and "450 days" in sql and "bar_quality" in sql
    assert params["symbols"] == ["NVDA", "AMD"]
    assert "INTERVAL '450 days'" in sql


def test_us_page_flag_omits_stock_baskets_without_removing_them_from_the_shared_read(monkeypatch):
    seen: list[list[str]] = []

    def fake_closes(_conn, symbols):
        seen.append(list(symbols))
        return {}

    monkeypatch.setattr(markets_read, "load_equity_eod_closes", fake_closes)
    monkeypatch.setattr(markets_read, "load_monitor_price_records", lambda *_args, **_kwargs: {})

    class Conn:
        def begin_nested(self):
            raise RuntimeError("monitor view is absent in this test")

    markets_read.aligned_us_equity_returns(Conn(), include_stock_baskets=False)
    markets_read.aligned_us_equity_returns(Conn(), include_stock_baskets=True)
    basket_only = {symbol for basket in stock_subsector_baskets() for symbol in basket.members} - set(SECTOR_PROXIES.values()) - {"SPY"}
    assert basket_only
    assert not (set(seen[0]) & basket_only)
    assert set(seen[1]) & basket_only
    assert [title for title, _caption, _groups in stock_groups_by_era()]
