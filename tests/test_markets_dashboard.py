"""US Markets and Global Markets analytics, pages, and backfill rules."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from market_intelligence.equity_eod import RETURN_WINDOWS, bars_from_yahoo_frame
from market_intelligence.freshness import SERIES_POLICIES
from market_intelligence.markets_analytics import (
    DRAWDOWN_SESSIONS,
    GLOBAL_METHODOLOGY,
    HORIZON_SESSIONS,
    US_METHODOLOGY,
    as_day,
    heatmap_rows,
    normalize_selected_to_100,
    normalized_ratio,
    sector_bar_pairs,
    session_window_returns,
    trailing_drawdown,
)
from market_intelligence.markets_read import choose_provider, load_monitor_history, yahoo_backfill_symbols
from market_intelligence.taxonomy import (
    GLOBAL_MARKET_ETFS,
    MARKET_MONITOR_SYMBOLS,
    SECTOR_PROXIES,
    UNIVERSE_SYMBOLS,
    US_HEATMAP_SYMBOLS,
)

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def _day(offset: int, start: date = date(2024, 1, 1)) -> date:
    return start + timedelta(days=offset)


def _flat(n: int, price: float = 100.0, start: date = date(2024, 1, 1)) -> list[tuple[date, float]]:
    return [(_day(i, start), price) for i in range(n)]


def test_drawdown_is_negative_at_a_lower_close_and_zero_at_a_new_high():
    below = _flat(251, 100.0) + [(date(2024, 9, 8), 90.0)]
    path = trailing_drawdown(below)
    assert len(path) == 1
    assert path[0][1] == pytest.approx(-0.10)

    rising = [(_day(i), 50.0 + i) for i in range(DRAWDOWN_SESSIONS)]
    highs = trailing_drawdown(rising)
    assert highs[-1][1] == 0.0
    assert all(value <= 0 for _day, value in highs)


def test_drawdown_missing_until_252_sessions_and_nulls_do_not_count():
    assert trailing_drawdown(_flat(251)) == []
    with_nulls = _flat(250) + [(date(2024, 9, 8), None), (date(2024, 9, 9), 90.0)]
    assert trailing_drawdown(with_nulls) == []
    assert trailing_drawdown([(date(2024, 1, 1), None)]) == []
    long_enough = _flat(251) + [(date(2024, 9, 8), None), (date(2024, 9, 9), 90.0), (date(2024, 9, 10), 90.0)]
    # 251 valid prices of 100, then a null, then two 90s: the first 90 completes 252.
    path = trailing_drawdown(long_enough)
    assert path[0][1] == pytest.approx(-0.10)
    assert all(value <= 0 for _day, value in path)


def test_normalization_uses_one_common_start_and_does_not_forward_fill():
    spy = [(date(2024, 1, 1), 200.0), (date(2024, 1, 2), 210.0), (date(2024, 1, 4), 220.0)]
    qqq = [(date(2024, 1, 2), 100.0), (date(2024, 1, 3), 110.0), (date(2024, 1, 4), 120.0)]
    both = normalize_selected_to_100({"SPY": spy, "QQQ": qqq}, ["SPY", "QQQ"])
    assert both["start"] == date(2024, 1, 2)
    assert both["series"]["SPY"][0] == (date(2024, 1, 2), 100.0)
    assert both["series"]["QQQ"][0] == (date(2024, 1, 2), 100.0)
    assert both["series"]["SPY"][1][1] == pytest.approx(100.0 * 220.0 / 210.0)
    assert date(2024, 1, 3) not in {day for day, _value in both["series"]["SPY"]}
    assert date(2024, 1, 3) in {day for day, _value in both["series"]["QQQ"]}

    spy_only = normalize_selected_to_100({"SPY": spy, "QQQ": qqq}, ["SPY"])
    assert spy_only["start"] == date(2024, 1, 1)
    assert spy_only["series"]["SPY"][0][1] == 100.0


def test_date_only_values_do_not_shift_timezone():
    assert as_day("2024-03-10") == date(2024, 3, 10)
    assert as_day(date(2024, 3, 10)) == date(2024, 3, 10)
    normalized = normalize_selected_to_100(
        {"SPY": [{"date": "2024-03-10", "value": 50.0}, {"date": "2024-03-11", "value": 55.0}]},
        ["SPY"],
    )
    assert normalized["start"] == date(2024, 3, 10)
    assert normalized["series"]["SPY"][0][0].isoformat() == "2024-03-10"


def test_ratio_direction_rises_when_the_numerator_outperforms():
    spy = [(date(2024, 1, 1), 100.0), (date(2024, 1, 2), 100.0)]
    qqq = [(date(2024, 1, 1), 100.0), (date(2024, 1, 2), 110.0)]
    ratio = normalized_ratio(qqq, spy)
    assert ratio["points"][0][1] == 100.0
    assert ratio["points"][1][1] == pytest.approx(110.0)
    weaker = normalized_ratio([(date(2024, 1, 1), 100.0), (date(2024, 1, 2), 90.0)], spy)
    assert weaker["points"][1][1] < weaker["points"][0][1]


def test_return_windows_match_canonical_sessions():
    assert list(HORIZON_SESSIONS.values()) == list(RETURN_WINDOWS.values())
    assert HORIZON_SESSIONS == {"1D": 1, "1W": 5, "1M": 21, "3M": 63, "6M": 126, "1Y": 252}
    start = date(2024, 1, 1)
    series = {start + timedelta(days=i): 100.0 for i in range(253)}
    series[start + timedelta(days=252)] = 110.0
    returns = session_window_returns(series, start + timedelta(days=252))
    assert returns["1Y"] == pytest.approx(0.10)
    assert returns["1W"] == pytest.approx(0.10)
    assert returns["1D"] == pytest.approx(0.10)
    short = {start + timedelta(days=i): 100.0 for i in range(5)}
    assert session_window_returns(short, start + timedelta(days=4))["1W"] is None


def test_heatmap_keeps_fixed_order_and_session_columns():
    returns = {symbol: {"1D": 0.01, "1W": None, "1M": 0.02, "3M": None, "6M": None, "1Y": None} for symbol in US_HEATMAP_SYMBOLS}
    matrix = heatmap_rows([(symbol, symbol) for symbol in US_HEATMAP_SYMBOLS], returns)
    assert matrix["columns"] == ["1D", "1W", "1M", "3M", "6M", "1Y"]
    assert [row["symbol"] for row in matrix["rows"]] == list(US_HEATMAP_SYMBOLS)
    assert matrix["rows"][0]["values"][1] is None


def test_sector_metrics_reuse_snapshot_fields_and_live_only_when_complete():
    rows = []
    for name, etf in SECTOR_PROXIES.items():
        rows.append(
            {
                "canonical_sector": name,
                "instrument_id": etf,
                "metrics": {
                    "ret_1d": 0.01,
                    "ret_1m": 0.05,
                    "live_ret_1d": 0.02,
                    "rs_chg_1d": -0.01,
                    "live_rs_chg_1d": -0.02,
                },
            }
        )
    assert len(rows) == 11
    assert SECTOR_PROXIES["Technology"] == "XLK"
    live_pairs, live_state = sector_bar_pairs(rows, "1D", kind="return")
    assert live_state.startswith("Live 1D")
    assert {value for _label, value in live_pairs} == {0.02}
    rows[-1]["metrics"]["live_ret_1d"] = None
    eod_pairs, eod_state = sector_bar_pairs(rows, "1D", kind="return")
    assert "Finalized EOD" in eod_state
    assert {value for _label, value in eod_pairs} == {0.01}
    month_pairs, month_state = sector_bar_pairs(rows, "1M", kind="return")
    assert "21 stored sessions" in month_state
    assert {value for _label, value in month_pairs} == {0.05}
    rs_pairs, rs_state = sector_bar_pairs(rows, "1D", kind="rs")
    assert "Finalized EOD" in rs_state or rs_state.startswith("Live")
    assert all(value == -0.02 or value == -0.01 for _label, value in rs_pairs)


def test_global_proxy_map_is_the_curated_usd_etf_set():
    mapping = dict(GLOBAL_MARKET_ETFS)
    assert mapping == {
        "SPY": "United States",
        "VEA": "Developed ex-US",
        "VGK": "Europe",
        "EWJ": "Japan",
        "VWO": "Emerging Markets",
        "MCHI": "China",
        "INDA": "India",
        "EWZ": "Brazil",
    }
    assert "EEM" not in mapping and "FXI" not in mapping
    for symbol in ("DIA", "VEA", "VGK", "EWJ", "VWO", "MCHI", "INDA", "EWZ"):
        assert symbol in MARKET_MONITOR_SYMBOLS
        assert symbol not in UNIVERSE_SYMBOLS
        assert symbol in SERIES_POLICIES


def test_yahoo_backfill_does_not_overwrite_another_provider():
    eligible, skipped = yahoo_backfill_symbols({"SPY": {"IBKR"}, "DIA": set(), "VEA": {"YAHOO"}, "QQQ": {"IBKR", "YAHOO"}})
    assert eligible == ["DIA", "VEA"]
    assert {row["symbol"] for row in skipped} == {"SPY", "QQQ"}
    assert choose_provider({"IBKR", "YAHOO"}) == "IBKR"
    assert choose_provider({"YAHOO"}) == "YAHOO"
    assert choose_provider(set()) is None


def test_backfill_is_outside_the_scheduled_refresh():
    from jobs.market_intelligence_refresh import build_parser, plan

    scheduled = plan(build_parser().parse_args(["--all-configured"]), {})
    names = [step["step"] for step in scheduled["steps"]]
    assert "equity_markets" in names
    assert "equity_markets_backfill" not in names
    assert "equity_markets_coverage" not in names
    incremental = next(step for step in scheduled["steps"] if step["step"] == "equity_markets")
    assert incremental["mode"] == "incremental"
    explicit = plan(build_parser().parse_args(["--equity-markets-backfill"]), {})
    assert explicit["steps"][0]["mode"] == "max"
    host = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")
    assert "--equity-markets-backfill" in host
    assert "--equity-markets-coverage" in host
    assert "equity_markets_max_backfill.done" in host


def test_market_monitor_due_after_the_close_and_not_before():
    from market_intelligence.due_state import evaluate_due_steps

    before = evaluate_due_steps(
        now=datetime(2026, 9, 24, 15, 0, tzinfo=ET),
        env={},
        freshness={},
        configured_steps=["equity_markets"],
    )
    after = evaluate_due_steps(
        now=datetime(2026, 9, 24, 16, 30, tzinfo=ET),
        env={},
        freshness={},
        configured_steps=["equity_markets"],
    )
    assert before[0].due is False
    assert before[0].reason != "unknown_step"
    assert after[0].due is True
    assert after[0].reason != "unknown_step"


def test_methodology_documents_the_required_rules():
    us = "\n".join(US_METHODOLOGY)
    global_text = "\n".join(GLOBAL_METHODOLOGY)
    for phrase in ("adjusted", "100", "252", "rs_chg", "Live 1D", "XLK"):
        assert phrase in us
    assert "deferred" in global_text.lower()
    assert "USD" in global_text
    assert "VEA" in global_text


def _bars(symbol: str, sessions: int = 40) -> list[dict[str, float | str]]:
    day = date(2024, 1, 2)
    price = 100.0 + (sum(ord(ch) for ch in symbol) % 17)
    rows = []
    for _index in range(sessions):
        rows.append({"date": day.isoformat(), "value": price})
        day += timedelta(days=1)
        price += 0.4
    return rows


def _history(symbols: list[str]) -> dict:
    bars = {symbol: _bars(symbol) for symbol in symbols}
    return {
        "bars": bars,
        "meta": {
            symbol: {
                "provider": "YAHOO",
                "adjustment_basis": "SPLIT_ADJUSTED_UNKNOWN_DIVIDEND",
                "adjustment_bases": ["SPLIT_ADJUSTED_UNKNOWN_DIVIDEND"],
                "source_id": "EQUITY_EOD",
                "rows": len(bars[symbol]),
                "earliest": bars[symbol][0]["date"],
                "latest": bars[symbol][-1]["date"],
            }
            for symbol in symbols
        },
        "returns": {
            symbol: {"1D": 0.01, "1W": 0.02, "1M": 0.03, "3M": 0.04, "6M": 0.05, "1Y": None}
            for symbol in symbols
        },
        "latest_price": {symbol: bars[symbol][-1]["value"] for symbol in symbols},
        "bounds": {"earliest": "2024-01-02", "latest": bars[symbols[0]][-1]["date"]},
    }


def _fake_read(fn_name, *args, **kwargs):
    if fn_name == "us_markets_history":
        return _history(["SPY", "QQQ", "IWM", "DIA", "RSP"])
    if fn_name == "global_markets_history":
        return _history(["SPY", "VEA", "VGK", "EWJ", "VWO", "MCHI", "INDA", "EWZ"])
    if fn_name == "sectors_context":
        rows = []
        for name, etf in SECTOR_PROXIES.items():
            rows.append(
                {
                    "sector_key": name,
                    "entity_kind": "SECTOR",
                    "canonical_sector": name,
                    "instrument_id": etf,
                    "as_of": "2024-02-10",
                    "source_id": "EQUITY_EOD",
                    "metrics": {
                        "ret_1d": 0.01,
                        "ret_1w": 0.02,
                        "ret_1m": 0.03,
                        "ret_3m": 0.04,
                        "ret_6m": 0.05,
                        "ret_12m": 0.06,
                        "rs_chg_1d": 0.001,
                        "rs_chg_1w": 0.002,
                        "rs_chg_1m": 0.003,
                        "rs_chg_3m": 0.004,
                        "rs_chg_6m": 0.005,
                        "rs_chg_12m": 0.006,
                    },
                }
            )
        return {"datasets": {"ETF_RS_VS_SPY": rows}}
    if fn_name == "equity_live_context":
        return {"by_symbol": {}, "quotes_available": False, "quotes_as_of_label": "Live quotes unavailable", "spy": None}
    raise AssertionError(fn_name)


def _texts(at: AppTest) -> str:
    chunks = []
    for widget in (*at.title, *at.subheader, *at.caption, *at.markdown, *at.info, *at.expander):
        chunks.append(str(getattr(widget, "label", None) or widget.value))
    return "\n".join(chunks)


def test_us_and_global_pages_render_required_sections(monkeypatch):
    monkeypatch.setattr("market_intelligence.ui.cached_read", _fake_read)
    us = AppTest.from_file(str(ROOT / "pages" / "22_US_Markets.py"), default_timeout=40)
    us.run()
    assert not us.exception, [item.value for item in us.exception]
    us_text = _texts(us)
    for heading in (
        "U.S. Equity Performance",
        "QQQ / SPY",
        "IWM / SPY",
        "RSP / SPY",
        "Sector Performance",
        "Sector Relative Strength vs SPY",
        "U.S. Market Return Heatmap",
        "Drawdown From 52-Week High",
        "Methodology & sources",
    ):
        assert heading in us_text
    assert len(us.dataframe) == 0
    global_page = AppTest.from_file(str(ROOT / "pages" / "23_Global_Markets.py"), default_timeout=40)
    global_page.run()
    assert not global_page.exception, [item.value for item in global_page.exception]
    global_text = _texts(global_page)
    for heading in (
        "Global Equity Performance",
        "Global Market Performance",
        "US vs Developed ex-US vs Emerging Markets",
        "VEA / SPY",
        "VWO / SPY",
        "Global Return Heatmap",
        "Methodology & sources",
    ):
        assert heading in global_text
    assert len(global_page.dataframe) == 0


def test_chart_code_does_not_call_providers_or_the_database():
    files = [
        ROOT / "market_intelligence" / "markets_ui.py",
        ROOT / "market_intelligence" / "components" / "market_chart" / "frontend" / "chart.js",
        ROOT / "market_intelligence" / "components" / "tenor_chart" / "frontend" / "chart.js",
        ROOT / "pages" / "22_US_Markets.py",
        ROOT / "pages" / "23_Global_Markets.py",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "fetch(" not in text
        assert "XMLHttpRequest" not in text
        assert "query1.finance.yahoo" not in text
        assert "ib_insync" not in text
        assert "postgresql://" not in text
        assert "psycopg" not in text
    ui = (ROOT / "market_intelligence" / "markets_ui.py").read_text(encoding="utf-8")
    assert "import yfinance" not in ui
    assert "st.line_chart" not in ui
    assert "plotly" not in ui.lower()


def test_yahoo_one_ticker_frame_uses_grouped_columns_and_skips_null_closes():
    index = pd.to_datetime(["2024-12-10", "2024-12-11"])
    columns = pd.MultiIndex.from_tuples([("SPY", "Close"), ("SPY", "Open")])
    frame = pd.DataFrame([[589.2, 591.0], [None, 592.0]], index=index, columns=columns)
    bars = bars_from_yahoo_frame(frame, ["SPY"])
    assert len(bars) == 1
    assert bars[0].instrument_id == "SPY"
    assert bars[0].bar_date == date(2024, 12, 10)
    assert bars[0].adj_close == pytest.approx(589.2)
    flat = pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2024-12-12"]))
    flat_bars = bars_from_yahoo_frame(flat, ["DIA"])
    assert len(flat_bars) == 1
    assert flat_bars[0].instrument_id == "DIA"
    assert flat_bars[0].adj_close == pytest.approx(100.0)


def test_dashboard_history_reads_the_readonly_closes_view():
    source = inspect.getsource(load_monitor_history)
    assert "FROM mi_v_equity_daily_closes" in source
    assert "FROM mi_market_bars" not in source


def test_equity_markets_progress_does_not_prefix_json_stdout():
    text = (ROOT / "jobs" / "market_intelligence_refresh.py").read_text(encoding="utf-8")
    for marker in ("equity_markets phase=ingest_start", "equity_markets phase=ingest_done"):
        start = text.index(marker)
        window = text[start : start + 500]
        assert "sys.stderr" in window
