"""Screenshot subsector list, calendar price returns, and incremental history planning."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from jobs.yahoo_dashboard_quotes import _acquire_lock
from market_intelligence.ibkr_live_universe import (
    APPROVED_EQUITY_ETF_SYMBOLS,
    STOCK_GROUPS,
    SUBSECTOR_ETFS,
    subsector_groups,
    yahoo_symbol,
)
from market_intelligence.price_returns import (
    PRICE_RETURN_BASIS,
    add_months,
    calendar_target,
    commit_or_keep,
    plan_history_window,
    price_horizons,
    quote_anchor_date,
)
from market_intelligence.yahoo_price_history import bar_quality, parse_daily_closes, series_has_split

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")

SCREENSHOT_GROUPS = (
    "Tech",
    "Financials",
    "Health Care",
    "Consumer Discretionary",
    "Communication Services",
    "Industrials",
    "Consumer Staples",
    "Energy",
    "Materials",
    "Real Estate",
    "Utilities",
)


def _bar(day: date, close: float, *, basis: str = PRICE_RETURN_BASIS, quality: str = "COMPLETE") -> dict:
    return {"bar_date": day, "close": close, "basis": basis, "quality": quality}


def test_screenshot_list_is_29_etfs_in_order_and_power_stays():
    assert len(SUBSECTOR_ETFS) == 29
    assert [group for group, _rows in subsector_groups()] == list(SCREENSHOT_GROUPS)
    assert SUBSECTOR_ETFS[:3] == (
        ("SMH", "Semiconductors", "Tech"),
        ("IGV", "Software", "Tech"),
        ("CIBR", "Cybersecurity", "Tech"),
    )
    assert SUBSECTOR_ETFS[-1] == ("GRID", "grid/transmission infrastructure", "Utilities")
    labels = {(symbol, label) for symbol, label, _group in SUBSECTOR_ETFS}
    assert ("XHB", "Homebuilders / housing ecosystem") in labels
    assert ("SOCL", "Social Media / internet platforms") in labels
    assert ("PAVE", "Infrastructure/engineering") in labels
    assert ("AMLP", "Midstream / pipelines") in labels
    assert ("SRVR", "Digital infrastructure/data centers") in labels
    symbols = [symbol for symbol, _label, _group in SUBSECTOR_ETFS]
    assert len(symbols) == len(set(symbols))
    assert all(yahoo_symbol(symbol) == symbol for symbol in symbols)
    assert all(APPROVED_EQUITY_ETF_SYMBOLS.count(symbol) == 1 for symbol in symbols)
    power = [symbol for name, members in STOCK_GROUPS if name.startswith("Power") for symbol in members]
    assert power == ["CEG", "VST", "TLN", "GEV", "ETN", "PWR", "CCJ", "BE"]
    page = (ROOT / "market_intelligence" / "markets_ui.py").read_text(encoding="utf-8")
    assert "Constituent" not in page
    assert "import yfinance" not in page
    cron = (ROOT / "scripts" / "install_yahoo_quote_cron.sh").read_text(encoding="utf-8")
    assert 'LINE="*/15 * * * * flock' in cron
    assert 'LINE="* * * * * flock' not in cron


def test_calendar_targets_handle_month_end_and_leap_day():
    assert calendar_target(date(2026, 3, 31), "1M") == date(2026, 2, 28)
    assert calendar_target(date(2024, 3, 31), "1M") == date(2024, 2, 29)
    assert calendar_target(date(2024, 2, 29), "1Y") == date(2023, 2, 28)
    assert add_months(date(2026, 3, 31), -6) == date(2025, 9, 30)
    assert calendar_target(date(2026, 9, 30), "1W") == date(2026, 9, 23)


def test_weekend_holiday_and_missing_history_stay_na():
    anchor = date(2026, 9, 30)
    after_target = price_horizons([_bar(date(2026, 9, 25), 80.0), _bar(date(2026, 9, 28), 90.0)], 100.0, anchor)
    assert after_target["1W"]["target"] == date(2026, 9, 23)
    assert after_target["1W"]["value"] is None
    sunday_anchor = date(2026, 9, 27)
    legs = price_horizons([_bar(date(2026, 9, 25), 80.0)], 100.0, sunday_anchor)
    assert legs["1W"]["target"] == date(2026, 9, 20)
    assert legs["1W"]["reference"] is None
    friday_only = price_horizons([_bar(date(2026, 9, 25), 80.0)], 100.0, date(2026, 10, 2))
    assert friday_only["1W"]["target"] == date(2026, 9, 25)
    assert friday_only["1W"]["reference"] == date(2026, 9, 25)
    assert friday_only["1W"]["value"] == pytest.approx(0.25)
    holiday = price_horizons([_bar(date(2025, 7, 3), 50.0)], 55.0, date(2025, 7, 11))
    assert holiday["1W"]["target"] == date(2025, 7, 4)
    assert holiday["1W"]["reference"] == date(2025, 7, 3)
    assert holiday["1W"]["value"] == pytest.approx(0.1)
    short = price_horizons([_bar(date(2026, 9, 29), 10.0)], 12.0, anchor)
    assert short["1Y"]["value"] is None
    assert short["1W"]["value"] is None
    assert price_horizons([_bar(date(2026, 9, 23), 0.0), _bar(date(2026, 9, 22), -5.0)], 10.0, anchor)["1W"]["value"] is None
    assert price_horizons([], 10.0, anchor)["1M"]["value"] is None
    assert price_horizons([_bar(date(2026, 9, 23), 80.0)], None, anchor)["1W"]["value"] is None


def test_reference_uses_the_last_completed_close_on_or_before_the_target():
    bars = [
        _bar(date(2026, 9, 23), 80.0),
        _bar(date(2026, 9, 24), 81.0, quality="PROVISIONAL"),
        _bar(date(2026, 9, 30), 99.0, quality="PROVISIONAL"),
    ]
    legs = price_horizons(bars, 100.0, date(2026, 9, 30))
    assert legs["1W"]["reference"] == date(2026, 9, 23)
    assert legs["1W"]["value"] == pytest.approx(100.0 / 80.0 - 1.0)
    mixed = [
        _bar(date(2026, 9, 23), 80.0),
        _bar(date(2026, 9, 26), 90.0, basis="SPLIT_ADJUSTED_UNKNOWN_DIVIDEND"),
    ]
    assert price_horizons(mixed, 100.0, date(2026, 9, 30))["1W"]["reason"] == "adjustment mismatch"
    weekend_quote = quote_anchor_date(datetime(2026, 9, 26, 15, 0, tzinfo=ET))
    assert weekend_quote == date(2026, 9, 25)


def test_failed_fetch_keeps_prices_and_gaps_are_repaired_without_a_full_download():
    existing = {
        date(2026, 9, 28): _bar(date(2026, 9, 28), 10.0),
        date(2026, 9, 29): _bar(date(2026, 9, 29), 11.0),
    }
    kept = commit_or_keep(existing, [{"bar_date": date(2026, 9, 30), "close": 99.0}], failed=True)
    assert set(kept) == set(existing)
    assert kept[date(2026, 9, 29)]["close"] == 11.0
    updated = commit_or_keep(existing, [_bar(date(2026, 9, 29), 12.0), _bar(date(2026, 9, 29), 13.0)], failed=False)
    assert updated[date(2026, 9, 29)]["close"] == 13.0
    assert len(updated) == 2
    closed = plan_history_window(
        [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)],
        today=date(2026, 10, 1),
        last_completed=date(2026, 9, 30),
        session_open=False,
        repair=False,
    )
    assert closed is None
    gap = plan_history_window(
        [date(2026, 9, 28), date(2026, 9, 29), date(2026, 10, 1)],
        today=date(2026, 10, 2),
        last_completed=date(2026, 10, 1),
        session_open=False,
        repair=False,
    )
    assert gap is not None
    assert gap[2] in {"incremental", "repair"}
    assert gap[0] <= date(2026, 9, 30)
    assert (gap[1] - gap[0]).days < 400
    overlap = plan_history_window(
        [date(2026, 9, 28), date(2026, 9, 29)],
        today=date(2026, 9, 30),
        last_completed=date(2026, 9, 29),
        session_open=True,
        repair=False,
    )
    assert overlap is not None and overlap[2] == "incremental"
    assert (overlap[1] - overlap[0]).days < 40
    fresh = plan_history_window([], today=date(2026, 9, 30), last_completed=date(2026, 9, 29), session_open=False, repair=False)
    assert fresh is not None and fresh[2] == "backfill"
    repaired = plan_history_window(
        [date(2026, 9, 30)],
        today=date(2026, 9, 30),
        last_completed=date(2026, 9, 29),
        session_open=False,
        repair=True,
    )
    assert repaired is not None and repaired[2] == "backfill"


def test_split_flag_does_not_rewrite_an_already_adjusted_close():
    frame = pd.DataFrame(
        {
            "Close": [100.0, 110.0],
            "Open": [99.0, 108.0],
            "High": [101.0, 111.0],
            "Low": [98.0, 107.0],
            "Volume": [10, 11],
            "Stock Splits": [0.0, 4.0],
        },
        index=pd.to_datetime(["2026-09-28", "2026-09-29"]),
    )
    rows = parse_daily_closes(frame, "SMH")
    assert [row["close"] for row in rows] == [100.0, 110.0]
    assert series_has_split(rows) is True
    assert rows[0]["basis"] == PRICE_RETURN_BASIS


def test_provisional_bar_becomes_complete_after_the_close():
    session = date(2026, 9, 30)
    assert bar_quality(session, datetime(2026, 9, 30, 15, 0, tzinfo=ET)) == "PROVISIONAL"
    assert bar_quality(date(2026, 9, 29), datetime(2026, 9, 30, 15, 0, tzinfo=ET)) == "COMPLETE"
    assert bar_quality(session, datetime(2026, 9, 30, 16, 5, tzinfo=ET)) == "COMPLETE"
    black_friday = date(2025, 11, 28)
    assert bar_quality(black_friday, datetime(2025, 11, 28, 12, 30, tzinfo=ET)) == "PROVISIONAL"
    assert bar_quality(black_friday, datetime(2025, 11, 28, 13, 5, tzinfo=ET)) == "COMPLETE"


def test_a_second_collector_does_not_start(tmp_path):
    lock = tmp_path / "yahoo_dashboard_quotes.lock"
    first = _acquire_lock(lock)
    assert first is not None
    assert _acquire_lock(lock) is None
