"""Unit tests for the Windows IBKR collector (no live TWS, no orders)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ibkr_collector.lock import InstanceLock
from ibkr_collector.queue import OutboundQueue
from ibkr_collector.values import BLOCKED_ECLIENT_METHODS, classify_error, finite_or_none, market_data_type_label
from ibkr_ingest.validate import PayloadError, validate_heartbeat, validate_quote_batch


def test_finite_or_none_treats_ib_sentinels_as_null():
    assert finite_or_none(-1) is None
    assert finite_or_none(None) is None
    assert finite_or_none(650.25) == 650.25


def test_market_data_types_are_labelled():
    assert market_data_type_label(1) == "LIVE"
    assert market_data_type_label(3) == "DELAYED"
    assert market_data_type_label(2) == "FROZEN"
    assert market_data_type_label(4) == "DELAYED_FROZEN"
    assert market_data_type_label(None) == "UNAVAILABLE"


def test_error_2186_is_entitlement_not_connectivity():
    assert classify_error(2186) == "entitlement"
    assert classify_error(2104) == "info"
    assert classify_error(502) == "connectivity"


def test_order_methods_are_on_the_block_list():
    assert "placeOrder" in BLOCKED_ECLIENT_METHODS
    assert "reqPositions" in BLOCKED_ECLIENT_METHODS
    assert "reqExecutions" in BLOCKED_ECLIENT_METHODS
    assert "reqMktData" not in BLOCKED_ECLIENT_METHODS


def test_queue_is_idempotent_and_bounded(tmp_path: Path):
    q = OutboundQueue(tmp_path / "q.sqlite", max_records=3)
    for i in range(5):
        q.put({"record_id": "r{0}".format(i), "n": i})
    q.put({"record_id": "r4", "n": 99})
    assert q.size() == 3
    batch = q.peek(10)
    ids = [row["record_id"] for row in batch]
    assert ids == ["r2", "r3", "r4"]
    q.ack(["r2"])
    assert q.size() == 2


def test_instance_lock_prevents_duplicates(tmp_path: Path):
    path = tmp_path / "collector.lock"
    a = InstanceLock(path)
    b = InstanceLock(path)
    assert a.acquire() is True
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()


def test_quote_batch_rejects_orders_and_unknown_fields():
    with pytest.raises(PayloadError):
        validate_quote_batch({"collector_id": "x", "quotes": [{"record_id": "a", "symbol": "SPY", "quote_ts": "2026-01-01T00:00:00+00:00", "place_order": True}]})
    with pytest.raises(PayloadError):
        validate_quote_batch({"collector_id": "x", "sql": "drop", "quotes": []})
    cid, rows = validate_quote_batch(
        {
            "collector_id": "harin-laptop",
            "quotes": [
                {
                    "record_id": "abc",
                    "symbol": "SPY",
                    "sec_type": "STK",
                    "quote_ts": "2026-01-01T15:00:00+00:00",
                    "bid": 500.1,
                    "ask": 500.2,
                    "last_price": None,
                    "market_data_type": "DELAYED",
                    "con_id": 756733,
                    "currency": "USD",
                    "provenance": {"collector_id": "harin-laptop", "client_id": 71, "tws_host": "127.0.0.1"},
                }
            ],
        }
    )
    assert cid == "harin-laptop" and rows[0]["bid"] == 500.1 and rows[0]["last_price"] is None


def test_heartbeat_rejects_account_fields():
    with pytest.raises(PayloadError):
        validate_heartbeat({"collector_id": "x", "reported_state": "CONNECTED", "account": "U12345"})
    payload = validate_heartbeat(
        {
            "collector_id": "harin-laptop",
            "reported_state": "WAITING_FOR_TWS",
            "client_id": 71,
            "watchlist": ["SPY", "TLT"],
        }
    )
    assert payload["reported_state"] == "WAITING_FOR_TWS"


def test_socket_probe_reports_closed_port():
    from ibkr_collector.diagnostic import probe_socket

    result = probe_socket("127.0.0.1", 1, timeout=0.2)
    assert result["ok"] is False
    assert result["error"]


def test_quote_fingerprint_ignores_clock_fields():
    from ibkr_collector.runner import quote_value_fingerprint

    a = {"symbol": "SPY", "con_id": 756733, "bid": None, "close_price": 765.96, "quote_ts": "t1", "retrieved_at": "r1", "market_data_type": "DELAYED", "quote_status": "PARTIAL"}
    b = dict(a, quote_ts="t2", retrieved_at="r2")
    c = dict(a, close_price=766.0)
    assert quote_value_fingerprint(a) == quote_value_fingerprint(b)
    assert quote_value_fingerprint(a) != quote_value_fingerprint(c)


def test_task_xml_does_not_wake_or_require_admin():
    from ibkr_collector.service_windows import _task_xml
    from pathlib import Path

    xml = _task_xml(Path("pythonw.exe"), Path("C:/repo"), "dipka")
    assert "WakeToRun>false" in xml
    assert "LeastPrivilege" in xml
    assert "IgnoreNew" in xml
    assert "LogonTrigger" in xml
    assert "ibkr_collector run" in xml
