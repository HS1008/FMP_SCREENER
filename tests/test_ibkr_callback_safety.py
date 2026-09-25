"""Callback-thread safety: nested lock, cancellation, and connection-generation isolation."""

from __future__ import annotations

import threading

from ibkr_collector.readonly_client import _ReadOnlyCallbacks
from ibkr_collector.values import BLOCKED_ECLIENT_METHODS


class _FakeClient(_ReadOnlyCallbacks):
    def __init__(self) -> None:
        self._init_readonly_state()


def _run_on_thread(fn) -> None:
    done = threading.Event()
    error: list[BaseException] = []

    def _target() -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=_target, name="ibkr-callback-regression", daemon=True)
    thread.start()
    assert done.wait(1.0), "callback thread blocked on the non-reentrant client lock"
    if error:
        raise error[0]


def test_historical_error_kinds_release_waiters_without_nested_lock():
    client = _FakeClient()
    for code, kind in ((162, "historical"), (354, "entitlement"), (420, "pacing"), (200, "invalid_contract")):
        req_id = client.next_req_id()
        event = client.mark_historical(req_id)
        _run_on_thread(lambda req=req_id, err=code: client.error(req, 0, err, "sanitized"))
        assert event.is_set(), kind
        assert req_id not in client.historical_pending


def test_delayed_notice_10167_does_not_mark_entitlement_or_block():
    client = _FakeClient()
    req_id = client.next_req_id()
    _run_on_thread(lambda: client.error(req_id, 0, 10167, "Requested market data is not subscribed"))
    assert client.ticks.get(req_id, {}).get("entitlement_error") is not True
    kinds = {row["kind"] for row in client.errors if row["req_id"] == req_id}
    assert kinds == {"info"}


def test_late_callback_after_cancel_does_not_update_ticks():
    client = _FakeClient()
    req_id = client.next_req_id()
    client.retire_request(req_id)
    client.tickPrice(req_id, 4, 123.45, None)
    assert req_id not in client.ticks


class _Bar:
    date = "20260912"
    open = 1.0
    high = 1.0
    low = 1.0
    close = 1.0
    volume = 0


def test_late_historical_bar_after_cancel_is_ignored():
    client = _FakeClient()
    req_id = client.next_req_id()
    client.mark_historical(req_id)
    client.retire_request(req_id)
    client.historicalData(req_id, _Bar())
    assert client.historical_bars.get(req_id) in (None, [])


def test_previous_generation_cannot_update_a_new_request():
    client = _FakeClient()
    old_id = client.next_req_id()
    client.tickPrice(old_id, 4, 10.0, None)
    client.begin_connection_generation()
    new_id = client.next_req_id()
    client.tickPrice(old_id, 4, 99.0, None)
    client.tickPrice(new_id, 4, 11.0, None)
    assert client.ticks[old_id]["last"] == 10.0
    assert client.ticks[new_id]["last"] == 11.0
    assert old_id != new_id


def test_reconnect_releases_historical_waiters():
    client = _FakeClient()
    req_id = client.next_req_id()
    event = client.mark_historical(req_id)
    client.connectionClosed()
    assert event.is_set()
    assert req_id not in client.historical_pending


def test_order_methods_remain_blocked_on_live_class():
    from ibkr_collector.readonly_client import OrderMethodBlocked, ReadOnlyTwsClient

    client = ReadOnlyTwsClient()
    for name in ("placeOrder", "reqPositions", "reqExecutions", "reqAccountUpdates", "reqGlobalCancel"):
        assert name in BLOCKED_ECLIENT_METHODS
        fn = getattr(client, name)
        try:
            fn()
            raise AssertionError("{0} was callable".format(name))
        except OrderMethodBlocked:
            pass
