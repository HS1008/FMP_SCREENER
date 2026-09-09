"""Quote freshness is not inferred from collector CONNECTED alone."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from market_intelligence.quote_status import (
    CODE_CONNECTED_EMPTY,
    CODE_CONNECTED_STALE,
    CODE_FRESH,
    CODE_NO_SOURCE,
    CODE_OFFLINE_CACHED,
    derive_quote_status,
    morning_overnight_section,
    overview_caption,
)


NOW = datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc)


def test_no_source_is_not_fresh_and_does_not_call_ibkr():
    status = derive_quote_status(collectors=[], quotes=[], now=NOW)
    assert status["code"] == CODE_NO_SOURCE
    assert status["fresh_quotes"] is False
    assert status["calls_ibkr"] is False
    assert "not labeled as overnight" in overview_caption(status).lower()
    assert morning_overnight_section(status)["status"] != "OK"


def test_connected_without_quotes_is_not_fresh():
    status = derive_quote_status(
        collectors=[{"collector_id": "laptop", "observed_state": "CONNECTED", "reported_state": "CONNECTED", "last_heartbeat_at": NOW.isoformat()}],
        quotes=[],
        now=NOW,
    )
    assert status["code"] == CODE_CONNECTED_EMPTY
    assert "no recent quotes" in status["headline"].lower()
    assert morning_overnight_section(status)["status"] != "OK"


def test_fresh_delayed_quotes_require_actual_receipt_time():
    received = NOW - timedelta(minutes=2)
    status = derive_quote_status(
        collectors=[{"collector_id": "laptop", "observed_state": "CONNECTED", "last_heartbeat_at": NOW.isoformat(), "last_quote_at": received.isoformat(), "last_ingest_ok_at": received.isoformat()}],
        quotes=[{"instrument_id": "SPY", "market_data_type": "DELAYED", "last_callback_at": received.isoformat(), "retrieved_at": received.isoformat()}],
        now=NOW,
    )
    assert status["code"] == CODE_FRESH
    assert "delayed" in status["headline"].lower()
    assert status["coverage"]["quote_instruments"] == 1
    assert morning_overnight_section(status)["status"] == "OK"


def test_connected_with_stale_quotes_says_no_recent():
    old = NOW - timedelta(hours=36)
    status = derive_quote_status(
        collectors=[{"collector_id": "laptop", "observed_state": "CONNECTED", "last_heartbeat_at": NOW.isoformat(), "last_quote_at": old.isoformat()}],
        quotes=[{"instrument_id": "SPY", "market_data_type": "DELAYED", "last_callback_at": old.isoformat()}],
        now=NOW,
    )
    assert status["code"] == CODE_CONNECTED_STALE
    assert "no recent quotes" in status["headline"].lower()


def test_offline_collector_shows_last_available_quotes():
    old = NOW - timedelta(hours=3)
    status = derive_quote_status(
        collectors=[{"collector_id": "laptop", "observed_state": "COLLECTOR_OFFLINE", "reported_state": "CONNECTED", "last_quote_at": old.isoformat()}],
        quotes=[{"instrument_id": "QQQ", "market_data_type": "DELAYED", "last_callback_at": old.isoformat()}],
        now=NOW,
    )
    assert status["code"] == CODE_OFFLINE_CACHED
    assert "offline" in status["headline"].lower()
    assert "last available" in status["headline"].lower()
