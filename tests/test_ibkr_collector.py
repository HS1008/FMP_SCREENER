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
    assert "reqHistoricalData" not in BLOCKED_ECLIENT_METHODS
    assert "reqSecDefOptParams" not in BLOCKED_ECLIENT_METHODS
    assert "exerciseOptions" in BLOCKED_ECLIENT_METHODS
    assert classify_error(420) == "pacing"
    assert classify_error(354) == "entitlement"


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
    q2 = OutboundQueue(tmp_path / "q2.sqlite", max_records=2)
    for i in range(4):
        q2.put({"record_id": "x{0}".format(i), "n": i})
    assert q2.size() == 2
    assert q2.overflow_count == 2
    q2.quarantine(["x2"], "poison")
    assert q2.size() == 1


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
    cid, rows, rejected = validate_quote_batch(
        {
            "collector_id": "x",
            "quotes": [{"record_id": "a", "symbol": "SPY", "quote_ts": "2026-01-01T00:00:00+00:00", "place_order": True}],
        }
    )
    assert cid == "x" and rows == [] and rejected[0]["outcome"] == "rejected"
    with pytest.raises(PayloadError):
        validate_quote_batch({"collector_id": "x", "sql": "drop", "quotes": []})
    cid, rows, rejected = validate_quote_batch(
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
    assert rejected == []


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


def test_heartbeat_allows_collector_offline():
    payload = validate_heartbeat({"collector_id": "harin-laptop", "reported_state": "COLLECTOR_OFFLINE", "client_id": 71})
    assert payload["reported_state"] == "COLLECTOR_OFFLINE"


def test_ack_keeps_rejected_and_missing_results(tmp_path):
    from ibkr_collector.runner import CollectorRuntime

    runtime = CollectorRuntime.__new__(CollectorRuntime)
    runtime.queue = OutboundQueue(tmp_path / "ack.sqlite", max_records=10)
    runtime.queue.put({"record_id": "a"})
    runtime.queue.put({"record_id": "b"})
    runtime.queue.put({"record_id": "c"})
    runtime.last_delivery_error = None
    runtime.state = "CONNECTED"
    runtime._set_state = lambda state: setattr(runtime, "state", state)
    batch = [{"record_id": "a"}, {"record_id": "b"}, {"record_id": "c"}]
    runtime._ack_quote_response(
        batch,
        {
            "results": [
                {"record_id": "a", "outcome": "committed"},
                {"record_id": "b", "outcome": "rejected", "reason": "poison"},
            ]
        },
    )
    pending = [row["record_id"] for row in runtime.queue.peek(10)]
    assert pending == ["c"]
    quarantined = runtime.queue._conn.execute("SELECT record_id FROM outbound WHERE status='quarantined'").fetchall()
    assert quarantined == [("b",)]


def test_provision_token_does_not_print_the_token(tmp_path, monkeypatch, capsys):
    from ibkr_collector import service_windows

    stored = []
    monkeypatch.setattr(service_windows, "write_ingest_token", lambda token: stored.append(token))
    path = tmp_path / "tok"
    path.write_text("super-secret-ingest-token\n", encoding="utf-8")
    assert service_windows.provision_token(str(path)) == 0
    out = capsys.readouterr()
    assert stored == ["super-secret-ingest-token"]
    assert "super-secret-ingest-token" not in out.out
    assert "super-secret-ingest-token" not in out.err
    source = Path(__file__).resolve().parents[1] / "ibkr_collector" / "service_windows.py"
    assert "sys.stdout.write(token" not in source.read_text(encoding="utf-8")


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


def test_eod_task_xml_is_weekday_1620_with_client_72():
    from ibkr_collector.service_windows import _eod_task_xml
    from pathlib import Path

    xml = _eod_task_xml(Path("python.exe"), Path("C:/repo"), "dipka")
    assert "16:20:00" in xml
    assert "fetch-eod --client-id 72" in xml
    assert "<Monday />" in xml and "<Friday />" in xml
    assert "StartWhenAvailable>true" in xml
    assert "RestartOnFailure" in xml
    assert "PT12M" in xml
    assert "<Count>3</Count>" in xml


def test_uninstall_removes_collector_and_eod_tasks(monkeypatch):
    from ibkr_collector import service_windows
    from ibkr_collector.service_windows import EOD_TASK_NAME, TASK_NAME

    calls: list[list[str]] = []

    class _Result:
        def __init__(self, code: int = 0):
            self.returncode = code
            self.stdout = ""
            self.stderr = ""

    def fake_run(args):
        calls.append(list(args))
        return _Result(0)

    monkeypatch.setattr(service_windows.os, "name", "nt")
    monkeypatch.setattr(service_windows, "stop", lambda: 0)
    monkeypatch.setattr(service_windows, "_run_schtasks", fake_run)
    assert service_windows.uninstall() == 0
    deleted = [" ".join(c) for c in calls if c and c[0] == "/Delete"]
    assert any(TASK_NAME in item for item in deleted)
    assert any(EOD_TASK_NAME in item for item in deleted)


def test_install_eod_refuses_non_eastern_timezone(monkeypatch, capsys):
    from ibkr_collector import service_windows

    monkeypatch.setattr(service_windows.os, "name", "nt")
    monkeypatch.setattr(service_windows, "_windows_timezone_id", lambda: "Pacific Standard Time")
    monkeypatch.delenv("MI_IBKR_EOD_ALLOW_NON_ET", raising=False)
    assert service_windows.install_eod() == 3
    err = capsys.readouterr().err
    assert "Eastern Standard Time" in err
    assert "Pacific Standard Time" in err
