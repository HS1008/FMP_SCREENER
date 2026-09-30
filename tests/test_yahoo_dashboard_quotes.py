"""Yahoo dashboard quotes: mapping, since-open, sessions, and failure handling."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import math
import pandas as pd
import pytest

from market_intelligence.ibkr_live_universe import (
    EXPECTED_IBKR_LIVE_COUNT,
    STOCK_GROUPS,
    approved_contracts,
    yahoo_symbol,
)
from market_intelligence.yahoo_dashboard import (
    backoff_seconds,
    bar_session_date,
    build_observations,
    classify_session,
    early_close_dates,
    newest_observation,
    observation_record,
    reference_open,
    regular_close,
    session_open_at,
    should_poll,
    since_open_fraction,
    split_blocks_comparison,
    valid_price,
)

ET = ZoneInfo("America/New_York")


def _et(day: str, hour: int, minute: int = 0) -> datetime:
    year, month, dom = (int(part) for part in day.split("-"))
    return datetime(year, month, dom, hour, minute, tzinfo=ET)


def test_canonical_universe_maps_every_symbol_including_vix_and_power():
    symbols = [row["symbol"] for row in approved_contracts()]
    assert len(symbols) == EXPECTED_IBKR_LIVE_COUNT == 93
    assert "VIX" in symbols
    assert yahoo_symbol("VIX") == "^VIX"
    assert yahoo_symbol("BRK.B") == "BRK-B"
    assert yahoo_symbol("SPY") == "SPY"
    power = [symbol for name, members in STOCK_GROUPS if name.startswith("Power") for symbol in members]
    assert power == ["CEG", "VST", "TLN", "GEV", "ETN", "PWR", "CCJ", "BE"]
    assert set(power) <= set(symbols)
    mapped = {yahoo_symbol(symbol) for symbol in symbols}
    assert "^VIX" in mapped
    assert len(mapped) == len(symbols)


def test_naive_daily_bar_keeps_the_session_date():
    assert bar_session_date(pd.Timestamp("2026-09-30")) == date(2026, 9, 30)
    assert bar_session_date(pd.Timestamp("2026-09-30 13:30:00+00:00")) == date(2026, 9, 30)


def test_since_open_arithmetic_uses_the_regular_open():
    now = _et("2026-09-30", 11, 0)
    value = since_open_fraction(101.5, _et("2026-09-30", 10, 45), 100.0, date(2026, 9, 30), now)
    assert value == pytest.approx(0.015)


def test_regular_premarket_postmarket_weekend_holiday_early_close_and_dst():
    regular = classify_session(_et("2026-09-30", 10, 0))
    pre = classify_session(_et("2026-09-30", 4, 0))
    post = classify_session(_et("2026-09-30", 16, 30))
    weekend = classify_session(_et("2026-10-03", 12, 0))
    holiday = classify_session(_et("2026-11-26", 12, 0))
    assert regular == "regular"
    assert pre == "premarket"
    assert post == "postmarket"
    assert weekend == "closed"
    assert holiday == "closed"
    black_friday = date(2026, 11, 27)
    assert black_friday in early_close_dates(2026)
    assert regular_close(black_friday).hour == 13
    assert classify_session(_et("2026-11-27", 12, 0)) == "regular"
    assert classify_session(_et("2026-11-27", 13, 30)) == "postmarket"
    winter = session_open_at(date(2026, 1, 5)).astimezone(timezone.utc)
    summer = session_open_at(date(2026, 7, 6)).astimezone(timezone.utc)
    assert (winter.hour, winter.minute) == (14, 30)
    assert (summer.hour, summer.minute) == (13, 30)
    premarket_now = _et("2026-09-30", 4, 0)
    assert since_open_fraction(101, _et("2026-09-30", 4, 5), 100, date(2026, 9, 29), premarket_now) == pytest.approx(0.01)
    weekend_now = _et("2026-10-03", 11, 0)
    assert since_open_fraction(102, _et("2026-10-02", 15, 0), 100, date(2026, 10, 2), weekend_now) == pytest.approx(0.02)
    holiday_now = _et("2026-11-26", 12, 0)
    assert since_open_fraction(99, _et("2026-11-25", 15, 0), 100, date(2026, 11, 25), holiday_now) == pytest.approx(-0.01)


def test_new_regular_session_does_not_pair_an_old_price_with_the_new_open():
    now = _et("2026-09-30", 9, 31)
    assert since_open_fraction(101, _et("2026-09-30", 9, 29), 100, date(2026, 9, 30), now) is None
    assert since_open_fraction(101, _et("2026-09-30", 9, 31), 100, date(2026, 9, 30), now) == pytest.approx(0.01)


def test_missing_or_stale_open_is_unavailable():
    now = _et("2026-09-30", 11, 0)
    assert since_open_fraction(101, _et("2026-09-30", 10, 0), None, date(2026, 9, 30), now) is None
    assert since_open_fraction(101, _et("2026-09-30", 10, 0), 100, date(2026, 9, 29), now) is None


def test_newest_session_observation_beats_an_older_regular_price():
    older = _et("2026-09-30", 15, 59)
    newer = _et("2026-09-30", 16, 5)
    chosen = newest_observation(
        [
            (older, 100, "regular"),
            (newer, 102, "postmarket"),
            (None, 999, "missing"),
            (newer + pd.Timedelta(0), 0, "invalid"),
        ]
    )
    assert chosen is not None
    assert chosen[1] == 102
    assert chosen[2] == "postmarket"


def test_out_of_order_and_duplicate_updates_keep_the_newest_valid_price():
    first = _et("2026-09-30", 10, 0)
    second = _et("2026-09-30", 10, 1)
    chosen = newest_observation([(second, 5, "a"), (first, 9, "b"), (second, 7, "c")])
    assert chosen == (second.astimezone(timezone.utc), 5, "a")


def test_invalid_prices_and_partial_symbol_failure():
    assert valid_price(None) is None
    assert valid_price(0) is None
    assert valid_price(-1) is None
    assert valid_price(float("nan")) is None
    assert valid_price(math.inf) is None
    assert valid_price(10) == 10
    minute_index = pd.to_datetime(["2026-09-30 14:00:00+00:00", "2026-09-30 14:01:00+00:00"])
    minutes = pd.DataFrame(
        [[101.0], [102.0]],
        index=minute_index,
        columns=pd.MultiIndex.from_tuples([("Close", "SPY")]),
    )
    daily = pd.DataFrame(
        [[100.0]],
        index=pd.to_datetime(["2026-09-30 13:30:00+00:00"]),
        columns=pd.MultiIndex.from_tuples([("Open", "SPY")]),
    )
    rows = build_observations(["SPY", "QQQ"], minutes, daily, now=_et("2026-09-30", 10, 1))
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["SPY"]["last_price"] == 102
    assert by_symbol["SPY"]["open_to_current"] == pytest.approx(0.02)
    assert by_symbol["QQQ"]["last_price"] is None
    assert by_symbol["QQQ"]["quote_error"]


def test_observation_keeps_the_source_timestamp():
    fetched = _et("2026-09-30", 10, 5)
    row = {
        "symbol": "SPY",
        "yahoo_symbol": "SPY",
        "last_price": 101,
        "quote_ts": _et("2026-09-30", 10, 1),
        "session": "regular",
        "session_open": 100,
        "session_date": "2026-09-30",
        "open_basis": "regular_session_open",
        "open_to_current": 0.01,
        "quote_error": None,
        "price_field": "minute_close",
    }
    record = observation_record(row, retrieved_at=fetched)
    assert record is not None
    assert record["quote_ts"] != record["retrieved_at"]
    assert record["quote_ts"] == _et("2026-09-30", 10, 1).astimezone(timezone.utc).isoformat()
    assert record["provenance"]["since_open_pct"] == pytest.approx(1.0)
    same = dict(row)
    same["quote_ts"] = fetched
    assert observation_record(same, retrieved_at=fetched) is None


def test_split_makes_since_open_unavailable():
    assert split_blocks_comparison([date(2026, 9, 30)], date(2026, 9, 29), date(2026, 9, 30)) is True
    daily = pd.DataFrame(
        {"Open": [50.0, 100.0], "Stock Splits": [0.0, 2.0]},
        index=pd.to_datetime(["2026-09-29 13:30:00+00:00", "2026-09-30 13:30:00+00:00"]),
    )
    minutes = pd.DataFrame(
        {"Close": [101.0]},
        index=pd.to_datetime(["2026-09-30 12:00:00+00:00"]),
    )
    rows = build_observations(["SPY"], minutes, daily, now=_et("2026-09-30", 8, 0))
    assert rows[0]["last_price"] == 101
    assert rows[0]["open_to_current"] is None
    assert "corporate action" in rows[0]["quote_error"]


def test_vix_uses_caret_symbol_and_does_not_invent_extended_hours():
    assert classify_session(_et("2026-09-30", 8, 0), instrument="VIX") == "closed"
    assert classify_session(_et("2026-09-30", 10, 0), instrument="VIX") == "regular"
    assert classify_session(_et("2026-09-30", 16, 30), instrument="VIX") == "closed"
    now = _et("2026-09-30", 8, 30)
    assert since_open_fraction(18, _et("2026-09-30", 8, 0), 17, date(2026, 9, 30), now) is None
    opened = reference_open({date(2026, 9, 30): 17.5}, [], now=_et("2026-09-30", 10, 0))
    assert opened == (date(2026, 9, 30), 17.5, "regular_session_open")


def test_first_bar_proxy_is_labeled_and_last_valid_quote_is_retained_on_error():
    now = _et("2026-09-30", 10, 0)
    bars = [(_et("2026-09-30", 9, 31), 100.2), (_et("2026-09-30", 9, 20), 99.0)]
    opened = reference_open({}, bars, now)
    assert opened is not None
    assert opened[2] == "first_regular_bar"
    bad = observation_record(
        {"symbol": "SPY", "last_price": None, "quote_ts": now, "yahoo_symbol": "SPY"},
        retrieved_at=now,
    )
    assert bad is None


def test_stale_print_during_the_session_is_not_a_closed_market_failure():
    from market_intelligence.live_session import quote_observation_status

    now = _et("2026-09-30", 12, 0)
    fresh = quote_observation_status(_et("2026-09-30", 11, 50), now)
    old = quote_observation_status(_et("2026-09-30", 10, 0), now)
    closed = quote_observation_status(_et("2026-10-02", 15, 59), _et("2026-10-03", 12, 0))
    assert fresh == "PROVIDER"
    assert old == "STALE"
    assert closed == "PROVIDER"


def test_closed_session_skips_a_recent_success_and_backoff_grows():
    now = _et("2026-10-03", 12, 0)
    assert should_poll(now, now) is False
    assert should_poll(now, None) is True
    assert backoff_seconds(3) > backoff_seconds(1)
