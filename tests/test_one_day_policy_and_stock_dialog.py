"""Standardized Yahoo 1D policy, ET timestamps, completed close-to-close horizons, and the stock dialog."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from streamlit.testing.v1 import AppTest

from market_intelligence import markets_ui
from market_intelligence.components.market_chart import build_market_chart_payload
from market_intelligence.live_1d_ui import freshness_summary, one_day_note, ratio_note
from market_intelligence.markets_analytics import _cell_source_note, _quote_leg, overlay_stored_quote_returns
from market_intelligence.price_returns import PRICE_RETURN_BASIS, completed_price_horizons
from market_intelligence.return_policy import (
    BASIS_LAST_CLOSE,
    BASIS_SESSION_OPEN,
    EOD_ONLY,
    format_eastern,
    one_day_from_quote,
    one_day_return,
    policy_for,
    policy_rows,
    quote_freshness,
    ratio_one_day,
    session_state,
)
from market_intelligence.stock_dialog import (
    MODE_CUMULATIVE,
    MODE_PRICE,
    STOCK_DIALOG_KEY,
    completed_closes,
    range_start,
    window_series,
)
from market_intelligence.taxonomy import constituent_company_name

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
UTC = timezone.utc


def _et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


# ---- 1D policy around session boundaries ----------------------------------------------------------


def test_us_equity_1d_switches_from_open_to_close_and_back_at_the_next_open():
    # Wednesday 2026-10-07, regular session. Open 500.00, prior close 495.00.
    kwargs = dict(session_open=500.0, session_open_date="2026-10-07", last_close=None, last_close_date=None)
    active = one_day_return("SPY", price=505.0, price_ts=_et(2026, 10, 7, 15, 15), now=_et(2026, 10, 7, 15, 15), **kwargs)
    assert active.basis == BASIS_SESSION_OPEN and active.value == pytest.approx(0.01)
    assert active.reference_date == date(2026, 10, 7) and active.basis_label == "Since session open"
    # 16:10 the same day with only yesterday's close stored: pending, never yesterday's close.
    after_close_pending = one_day_return(
        "SPY", price=506.0, price_ts=_et(2026, 10, 7, 16, 10), now=_et(2026, 10, 7, 16, 10),
        session_open=500.0, session_open_date="2026-10-07", last_close=495.0, last_close_date="2026-10-06",
    )
    assert after_close_pending.value is None and after_close_pending.basis == BASIS_LAST_CLOSE
    assert "pending" in after_close_pending.reason
    # Today's close stored: post-market print over today's completed close.
    after_close = one_day_return(
        "SPY", price=506.0, price_ts=_et(2026, 10, 7, 16, 10), now=_et(2026, 10, 7, 16, 10),
        session_open=500.0, session_open_date="2026-10-07", last_close=504.0, last_close_date="2026-10-07",
    )
    assert after_close.basis == BASIS_LAST_CLOSE and after_close.value == pytest.approx(506.0 / 504.0 - 1.0)
    # Saturday keeps Friday's completed close as the denominator.
    weekend = one_day_return(
        "SPY", price=506.0, price_ts=_et(2026, 10, 9, 19, 55), now=_et(2026, 10, 10, 11, 0),
        session_open=501.0, session_open_date="2026-10-09", last_close=503.0, last_close_date="2026-10-09",
    )
    assert weekend.basis == BASIS_LAST_CLOSE and weekend.reference_date == date(2026, 10, 9)
    # Next open at 09:31 with a quote from last night: pending until a print from the new session arrives.
    stale_at_open = one_day_return(
        "SPY", price=506.0, price_ts=_et(2026, 10, 7, 19, 55), now=_et(2026, 10, 8, 9, 31),
        session_open=500.0, session_open_date="2026-10-07", last_close=504.0, last_close_date="2026-10-07",
    )
    assert stale_at_open.value is None and stale_at_open.basis == BASIS_SESSION_OPEN and "pending" in stale_at_open.reason
    fresh_at_open = one_day_return(
        "SPY", price=507.0, price_ts=_et(2026, 10, 8, 9, 31), now=_et(2026, 10, 8, 9, 32),
        session_open=505.5, session_open_date="2026-10-08", last_close=504.0, last_close_date="2026-10-07",
    )
    assert fresh_at_open.basis == BASIS_SESSION_OPEN and fresh_at_open.value == pytest.approx(507.0 / 505.5 - 1.0)


def test_early_close_and_holiday_follow_the_nyse_calendar():
    # 2026-11-27 (day after Thanksgiving) closes at 13:00 ET.
    state = session_state(policy_for("SPY"), _et(2026, 11, 27, 13, 30))
    assert state.active is False and state.session_date == date(2026, 11, 27)
    assert state.close_at.astimezone(ET).hour == 13
    # Thanksgiving itself is not a session: the most recent completed session is Wednesday.
    holiday = session_state(policy_for("SPY"), _et(2026, 11, 26, 12, 0))
    assert holiday.active is False and holiday.session_date == date(2026, 11, 25)


def test_non_equity_sessions_use_their_own_clocks():
    # CME Globex: Tuesday 18:30 ET belongs to Wednesday's trade date, which is active.
    cme = session_state(policy_for("CL"), _et(2026, 10, 6, 18, 30))
    assert cme.active and cme.session_date == date(2026, 10, 7)
    # Tuesday 17:30 ET is the daily break after Tuesday's 17:00 close: closed, Tuesday completed.
    cme_break = session_state(policy_for("CL"), _et(2026, 10, 6, 17, 30))
    assert cme_break.active is False and cme_break.session_date == date(2026, 10, 6)
    # CBOT grains roll at 19:00 Chicago; 13:30 CT is after the 13:20 close.
    grain = session_state(policy_for("ZC"), datetime(2026, 10, 6, 13, 30, tzinfo=ZoneInfo("America/Chicago")))
    assert grain.active is False and grain.session_date == date(2026, 10, 6)
    # Spot FX rolls at 17:00 New York; Sunday 18:00 ET is Monday's session.
    fx = session_state(policy_for("EURUSD"), _et(2026, 10, 4, 18, 0))
    assert fx.active and fx.session_date == date(2026, 10, 5)
    fx_weekend = session_state(policy_for("EURUSD"), _et(2026, 10, 3, 12, 0))
    assert fx_weekend.active is False and fx_weekend.session_date == date(2026, 10, 2)
    # Crypto: UTC day, always active, weekends included.
    crypto = session_state(policy_for("BTC"), datetime(2026, 10, 4, 23, 30, tzinfo=UTC))
    assert crypto.active and crypto.session_date == date(2026, 10, 4)
    crypto_rolled = session_state(policy_for("BTC"), datetime(2026, 10, 5, 0, 5, tzinfo=UTC))
    assert crypto_rolled.session_date == date(2026, 10, 5)
    # EOD-only instruments never get a live 1D.
    move = one_day_return("MOVE", price=100.0, price_ts=_et(2026, 10, 7, 12, 0), now=_et(2026, 10, 7, 12, 0),
                          session_open=99.0, session_open_date="2026-10-07", last_close=None, last_close_date=None)
    assert move.basis == EOD_ONLY and move.value is None
    table = {row["symbol"]: row for row in policy_rows()}
    assert table["SPY"]["policy_id"] == "US_EQUITY_RTH" and table["BTC"]["policy_id"] == "CRYPTO_UTC_DAILY"
    assert table["DXY"]["policy_id"] == "ICE_USDX" and table["BZ"]["policy_id"] == "ICE_BRENT" and table["MOVE"]["live"] == "no"


def test_ratio_1d_uses_both_legs_on_one_basis_and_never_mixes_sessions():
    now = _et(2026, 10, 7, 15, 0)
    common = dict(price_ts=now, now=now, last_close=None, last_close_date=None)
    a = one_day_return("QQQ", price=404.0, session_open=400.0, session_open_date="2026-10-07", **common)
    b = one_day_return("SPY", price=505.0, session_open=500.0, session_open_date="2026-10-07", **common)
    ratio = ratio_one_day(a, b)
    assert ratio.value == pytest.approx((404.0 / 505.0) / (400.0 / 500.0) - 1.0)
    stale = one_day_return("SPY", price=505.0, session_open=500.0, session_open_date="2026-10-06", **common)
    assert ratio_one_day(a, stale).value is None
    note = ratio_note(a, b, ratio, labels=("QQQ", "SPY"))
    assert "QQQ observed Oct 7, 2026, 3:00 PM EDT" in note and "SPY observed" in note


def test_eastern_timestamps_and_freshness():
    assert format_eastern(datetime(2026, 10, 7, 19, 15, tzinfo=UTC)) == "Oct 7, 2026, 3:15 PM EDT"
    assert format_eastern(datetime(2026, 1, 7, 14, 5, tzinfo=UTC)) == "Jan 7, 2026, 9:05 AM EST"
    now = _et(2026, 10, 7, 15, 15)
    assert quote_freshness("SPY", _et(2026, 10, 7, 15, 10), now=now) == "current"
    assert quote_freshness("SPY", _et(2026, 10, 7, 14, 10), now=now) == "delayed"
    assert quote_freshness("SPY", _et(2026, 10, 5, 15, 10), now=now) == "stale"
    assert quote_freshness("SPY", None, now=now) == "unavailable"
    assert quote_freshness("MOVE", _et(2026, 10, 7, 15, 10), now=now) == "eod"


def test_stored_quote_rows_resolve_and_notes_carry_basis_and_last_updated():
    now = _et(2026, 10, 7, 15, 15)
    row = {
        "symbol": "NVDA",
        "last_price": 101.0,
        "quote_ts": _et(2026, 10, 7, 15, 14),
        "provenance": {"current_price": 101.0, "session_open": 100.0, "session_date": "2026-10-07", "last_close": 99.0, "last_close_date": "2026-10-06"},
    }
    one_day = one_day_from_quote("NVDA", row, now=now)
    assert one_day.value == pytest.approx(0.01) and one_day.basis == BASIS_SESSION_OPEN
    note = one_day_note(one_day, symbol="NVDA", now=now)
    assert note.startswith("Since session open") and "Last updated: Oct 7, 2026, 3:14 PM EDT" in note and "current" in note
    summary = freshness_summary({"NVDA": one_day, "SPY": one_day_from_quote("SPY", None, now=now)}, now=now)
    assert "Since session open: 1" in summary and "no quote: 1" in summary and "Oct 7, 2026, 3:14 PM EDT" in summary
    # An unchanged old quote keeps its own timestamp: the label does not advance with render time.
    later = one_day_from_quote("NVDA", row, now=_et(2026, 10, 7, 15, 45))
    assert "3:14 PM EDT" in one_day_note(later, symbol="NVDA", now=_et(2026, 10, 7, 15, 45))


# ---- longer horizons: completed close-to-close ---------------------------------------------------


def _bars(end: date, closes: list[float]) -> list[dict]:
    rows = []
    day = end
    for close in reversed(closes):
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        rows.append({"bar_date": day, "close": close, "basis": PRICE_RETURN_BASIS, "quality": "COMPLETE"})
        day -= timedelta(days=1)
    rows.reverse()
    return rows


def test_longer_horizons_ignore_quotes_and_provisional_bars():
    bars = _bars(date(2026, 10, 6), [100.0 + i for i in range(260)])
    legs = completed_price_horizons(bars)
    assert legs["1W"]["endpoint"] == date(2026, 10, 6)
    assert legs["1W"]["value"] == pytest.approx(359.0 / 354.0 - 1.0)
    provisional = dict(bars[-1], bar_date=date(2026, 10, 7), close=999.0, quality="PROVISIONAL")
    assert completed_price_horizons(bars + [provisional])["1W"]["value"] == legs["1W"]["value"]


# ---- overlay accepts policy bases -----------------------------------------------------------------


def test_quote_leg_accepts_policy_bases_and_cell_note_names_them():
    stamp = "2026-10-07T19:14:00+00:00"
    legs = {
        "XLK": {"live_return": 0.01, "return_basis": BASIS_SESSION_OPEN, "session_open_date": "2026-10-07", "current": {"market_data_status": "PROVIDER", "observation_ts": stamp}},
        "SPY": {"live_return": 0.005, "return_basis": BASIS_LAST_CLOSE, "session_open_date": "2026-10-07", "current": {"market_data_status": "PROVIDER", "observation_ts": stamp}},
    }
    leg = _quote_leg("XLK", legs, panel_basis="SPLIT_ADJUSTED_PRICE")
    assert leg is not None and leg["basis"] == BASIS_SESSION_OPEN and leg["current_session"] == date(2026, 10, 7)
    note = _cell_source_note({"status": "PROVIDER", "basis": BASIS_SESSION_OPEN, "current_session": "2026-10-07", "updated": stamp}, count=None, relative=False)
    assert "Since session open" in note and "Last updated: Oct 7, 2026, 3:14 PM EDT" in note
    panel = {
        "available": True,
        "adjustment_basis": "SPLIT_ADJUSTED_PRICE",
        "spy_returns": {"1D": 0.002},
        "sectors": [{"symbol": "XLK", "industry": "Tech", "values": [0.0, 0.1]}],
        "subsectors": {},
    }
    overlaid = overlay_stored_quote_returns(panel, legs)
    assert overlaid["sectors"][0]["values"][0] == pytest.approx(0.01)
    # XLK (since open) and SPY (since last close) are on different bases: relative 1D must not pair them.
    from market_intelligence.markets_analytics import quote_periods_match

    assert quote_periods_match(overlaid["sectors"][0]["quote_1d_detail"], overlaid["spy_quote_1d"]) is False


# ---- stock dialog ---------------------------------------------------------------------------------


def test_window_series_rebases_cumulative_and_excludes_provisional_bars():
    now = _et(2026, 10, 7, 12, 0)
    bars = [
        {"bar_date": date(2023, 10, 9), "close_price": 50.0, "adjustment_basis": PRICE_RETURN_BASIS, "bar_quality": "COMPLETE"},
        {"bar_date": date(2026, 10, 5), "close_price": 100.0, "adjustment_basis": PRICE_RETURN_BASIS, "bar_quality": "COMPLETE"},
        {"bar_date": date(2026, 10, 6), "close_price": 110.0, "adjustment_basis": PRICE_RETURN_BASIS, "bar_quality": "COMPLETE"},
        {"bar_date": date(2026, 10, 7), "close_price": 999.0, "adjustment_basis": PRICE_RETURN_BASIS, "bar_quality": "COMPLETE"},  # in-progress session
        {"bar_date": date(2026, 10, 1), "close_price": 1.0, "adjustment_basis": "TOTAL_RETURN", "bar_quality": "COMPLETE"},  # wrong basis
    ]
    closes = completed_closes(bars, symbol="NVDA", now=now)
    assert [day for day, _close in closes] == [date(2023, 10, 9), date(2026, 10, 5), date(2026, 10, 6)]
    one_month = window_series(closes, kind="1M", mode=MODE_CUMULATIVE)
    assert [point["value"] for point in one_month["points"]] == [pytest.approx(0.0), pytest.approx(10.0)]
    assert one_month["requested_start"] == date(2026, 10, 6) - timedelta(days=30)
    price = window_series(closes, kind="3Y", mode=MODE_PRICE)
    assert [point["value"] for point in price["points"]] == [50.0, 100.0, 110.0] and price["base"] == 50.0
    assert range_start("YTD", date(2026, 10, 6)) == date(2026, 1, 1) and range_start("3Y", date(2026, 10, 6)) == date(2023, 10, 6)
    assert window_series([], kind="3Y", mode=MODE_PRICE)["points"] == []
    assert constituent_company_name("NVDA") == "NVIDIA Corp." and constituent_company_name("AAPL") == "Apple Inc."


def test_chart_payload_supports_an_initial_range_only_with_range_buttons():
    points = [{"time": "2026-10-01", "value": 1.0}]
    assert build_market_chart_payload(points, ranges=True, initial_range="3Y")["initial_range"] == "3Y"
    assert "initial_range" not in build_market_chart_payload(points, ranges=False, initial_range="3Y")
    assert "initial_range" not in build_market_chart_payload(points, ranges=True, initial_range="5Y")
    js = (ROOT / "market_intelligence" / "components" / "market_chart" / "frontend" / "chart.js").read_text(encoding="utf-8")
    assert "initial_range" in js and 'setAttribute("aria-pressed"' in js


def test_stock_history_read_model_is_scoped_to_one_symbol_and_window():
    source = (ROOT / "market_intelligence" / "read_models.py").read_text(encoding="utf-8")
    start = source.index("def stock_history_bars(")
    body = source[start : source.index("def observation_histories(")]
    assert "symbol = :symbol" in body and "bar_date >= :since" in body and "mi_v_yahoo_price_daily" in body
    assert "import yfinance" not in (ROOT / "market_intelligence" / "stock_dialog.py").read_text(encoding="utf-8")


# ---- end-to-end: a stock click opens the dialog inside the US Equities page --------------------------


def _random_closes(count: int, seed: int) -> list[float]:
    value = 100.0
    out = []
    state = seed
    for _ in range(count):
        state = (state * 1103515245 + 12345) % (2**31)
        value *= 1.0 + ((state / 2**31) - 0.5) * 0.02
        out.append(value)
    return out


def _history_bars(symbol: str, since: date | None) -> list[dict]:
    rows = []
    day = date(2026, 10, 6)
    for close in reversed(_random_closes(760, seed=11)):
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        if since is None or day >= since:
            rows.append({"bar_date": day, "close_price": close, "adj_close_price": close, "adjustment_basis": PRICE_RETURN_BASIS, "bar_quality": "COMPLETE", "bar_ts": None, "retrieved_at": None})
        day -= timedelta(days=1)
    rows.reverse()
    return rows


def _fake_read_factory(calls: list):
    def _fake_read(fn_name, *args, **kwargs):
        calls.append((fn_name, args, kwargs))
        if fn_name == "us_markets_history":
            return {"series": {}, "returns": {}, "latest_price": {}, "bounds": {}, "providers": {}}
        if fn_name == "aligned_us_equity_returns":
            return {"available": False}
        if fn_name == "dashboard_price_bars":
            return [dict(row, symbol="NVDA") for row in _history_bars("NVDA", date(2025, 6, 1))]
        if fn_name == "stock_history_bars":
            symbol = args[0]
            rows = _history_bars(symbol, kwargs.get("since"))
            return {"symbol": symbol, "bars": rows, "earliest_stored": rows[0]["bar_date"] if rows else None, "data_version": None}
        raise AssertionError(fn_name)

    return _fake_read


def _fake_quote_read(fn_name, *args, **kwargs):
    if fn_name == "equity_live_context":
        return {"quotes_available": False, "by_symbol": {}, "quotes_as_of_label": "Live quotes unavailable"}
    if fn_name == "dashboard_quotes_latest":
        stamp = datetime(2026, 10, 6, 16, 5, tzinfo=ET)
        return [
            {
                "symbol": symbol,
                "last_price": 100.0,
                "quote_ts": stamp,
                "quote_status": "OK",
                "market_data_type": "DELAYED",
                "provenance": {"current_price": 100.0, "session_open": 99.0, "session_date": "2026-10-06", "last_close": 98.0, "last_close_date": "2026-10-06", "current_price_field": "regularMarketPrice"},
            }
            for symbol in ("NVDA", "SPY", "XLK")
        ]
    raise AssertionError(fn_name)


def test_stock_click_opens_a_dialog_that_reads_only_that_symbol(monkeypatch):
    calls: list = []
    monkeypatch.setattr("market_intelligence.ui.cached_read", _fake_read_factory(calls))
    monkeypatch.setattr("market_intelligence.ui.cached_quote_read", _fake_quote_read)
    captured: dict = {}
    real = markets_ui.column_scaled_return_heatmap

    def spy(*args, **kwargs):
        if kwargs.get("key", "").startswith("us_stock_heatmap_"):
            captured.setdefault("row_ids", list(kwargs["row_ids"]))
            captured.setdefault("columns", list(args[1]))
            if captured.get("click") in kwargs["row_ids"]:
                kwargs["on_row_click"](captured.pop("click"))
        return real(*args, **kwargs)

    monkeypatch.setattr(markets_ui, "column_scaled_return_heatmap", spy)
    at = AppTest.from_file(str(ROOT / "pages" / "22_US_Markets.py"), default_timeout=40)
    at.run()
    assert not at.exception, [item.value for item in at.exception]
    assert "NVDA" in captured["row_ids"], "rows are identified by the canonical ticker"
    assert captured["columns"][0] == "1D"
    assert not [call for call in calls if call[0] == "stock_history_bars"], "no history is read before a click"
    assert STOCK_DIALOG_KEY not in at.session_state
    captured["click"] = "NVDA"
    at.run()
    assert not at.exception, [item.value for item in at.exception]
    assert at.session_state[STOCK_DIALOG_KEY] == "NVDA"
    history_calls = [call for call in calls if call[0] == "stock_history_bars"]
    assert history_calls and all(call[1] == ("NVDA",) for call in history_calls), "only the clicked symbol is read"
    assert all(call[2]["since"] <= date(2023, 10, 7) for call in history_calls), "the read window covers three calendar years"
    text = "\n".join(str(c.value) for c in at.caption)
    assert "Cumulative %" in text and "cash dividends are excluded" in text
    assert any("Latest quote 100.00" in str(m.value) and "Last updated: Oct 6, 2026, 4:05 PM EDT" in str(m.value) for m in at.markdown)
    # Dismissing runs clear_stock_dialog (the on_dismiss callback), which drops the
    # request so a persisted component click cannot reopen the dialog.
    del at.session_state[STOCK_DIALOG_KEY]
    at.run()
    assert not at.exception, [item.value for item in at.exception]
    assert STOCK_DIALOG_KEY not in at.session_state
    before = len([call for call in calls if call[0] == "stock_history_bars"])
    at.run()
    assert len([call for call in calls if call[0] == "stock_history_bars"]) == before, "no dialog, no history read"
