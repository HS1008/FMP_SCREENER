"""Background collector loop: wait for TWS, subscribe, queue, deliver."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import signal
import threading
import time
from datetime import datetime, timezone
from typing import Any

from ibkr_collector.config import CollectorConfig, load_config, write_example_config
from ibkr_collector.delivery import DeliveryError, IngestClient
from ibkr_collector.diagnostic import probe_socket
from ibkr_collector.lock import InstanceLock
from ibkr_collector.logging_setup import setup_logging
from ibkr_collector.queue import OutboundQueue
from ibkr_collector.readonly_client import ReadOnlyTwsClient
from ibkr_collector.secrets_win import read_ingest_token
from ibkr_collector.values import market_data_type_label, utcnow

logger = logging.getLogger("ibkr_collector.runner")

_STOP = threading.Event()


def request_stop(*_args: Any) -> None:
    _STOP.set()


def _install_stop_handlers() -> None:
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)
    try:
        import ctypes

        handler = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)(lambda _ctrl: (request_stop(), 1)[1])
        ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
        _install_stop_handlers._handler = handler  # keep reference
    except Exception:
        pass


def _contract_from_row(row: dict[str, str]):
    from ibapi.contract import Contract

    contract = Contract()
    contract.symbol = row["symbol"]
    contract.secType = row.get("sec_type") or "STK"
    contract.exchange = row.get("exchange") or "SMART"
    contract.currency = row.get("currency") or "USD"
    if row.get("primary_exchange"):
        contract.primaryExchange = row["primary_exchange"]
    return contract


def _record_id(payload: dict[str, Any]) -> str:
    body = {k: payload[k] for k in sorted(payload) if k != "record_id"}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return digest[:64]


def _source_ts(raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    text = str(raw)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return text
    except ValueError:
        pass
    try:
        return datetime.fromtimestamp(int(float(text)), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def quote_value_fingerprint(payload: dict[str, Any]) -> tuple[Any, ...]:
    """Identity for duplicate suppression: prices/sizes/type, not wall-clock timestamps."""
    return tuple(
        payload.get(key)
        for key in (
            "symbol",
            "con_id",
            "bid",
            "ask",
            "last_price",
            "mid",
            "bid_size",
            "ask_size",
            "last_size",
            "close_price",
            "market_data_type",
            "quote_status",
        )
    )


def _quote_status(ticks: dict[str, Any], entitlement: bool) -> str:
    if entitlement:
        return "ENTITLEMENT_ERROR"
    if any(ticks.get(k) is not None for k in ("bid", "ask", "last", "close")):
        if all(ticks.get(k) is not None for k in ("bid", "ask", "last")):
            return "OK"
        return "PARTIAL"
    return "UNAVAILABLE"


class CollectorRuntime:
    def __init__(self, cfg: CollectorConfig) -> None:
        self.cfg = cfg
        self.queue = OutboundQueue(cfg.queue_path, max_records=cfg.queue_max_records)
        self.delivery = IngestClient(cfg.ingest_url, read_ingest_token())
        self.state = "WAITING_FOR_TWS"
        self.last_socket_ok_at = None
        self.last_handshake_at = None
        self.last_tws_connect_at = None
        self.last_quote_at = None
        self.last_callback_at = None
        self.last_delivery_error = None
        self.market_data_type = "UNAVAILABLE"
        self.client = None
        self.reader = None
        self.subs: dict[str, dict[str, Any]] = {}
        self._last_quote_fp: dict[str, tuple[Any, ...]] = {}
        self.backoff = cfg.backoff_initial_sec
        self._last_heartbeat = 0.0
        self._last_quote_push = 0.0

    def _key(self, row: dict[str, str]) -> str:
        return "{0}:{1}:{2}".format(row.get("sec_type") or "STK", row["symbol"], row.get("currency") or "USD")

    def _set_state(self, state: str) -> None:
        if state != self.state:
            logger.info("state %s -> %s", self.state, state)
        self.state = state

    def _heartbeat_payload(self) -> dict[str, Any]:
        return {
            "collector_id": self.cfg.collector_id,
            "reported_state": self.state,
            "last_socket_ok_at": self.last_socket_ok_at,
            "last_api_handshake_at": self.last_handshake_at,
            "last_tws_connect_at": self.last_tws_connect_at,
            "last_quote_at": self.last_quote_at,
            "last_callback_at": self.last_callback_at,
            "market_data_type": self.market_data_type,
            "client_id": self.cfg.client_id,
            "watchlist": [row["symbol"] for row in self.cfg.watchlist],
            "details": {
                "queue_depth": self.queue.size(),
                "queue_overflow_count": getattr(self.queue, "overflow_count", 0),
                "subscriptions": len(self.subs),
            },
            "last_delivery_error_redacted": self.last_delivery_error,
        }

    def _deliver_heartbeat(self) -> None:
        if not self.delivery.configured():
            return
        try:
            self.delivery.send_heartbeat(self._heartbeat_payload())
            if self.state == "DELIVERY_FAILURE":
                self._set_state("CONNECTED" if self.client and self.client.isConnected() else self.state)
            self.last_delivery_error = None
        except DeliveryError as exc:
            self.last_delivery_error = str(exc)[:200]
            if self.state in {"CONNECTED", "API_AUTHENTICATED", "ENTITLEMENT_ERROR"}:
                self._set_state("DELIVERY_FAILURE")

    def _deliver_queue(self) -> None:
        if not self.delivery.configured():
            return
        batch = self.queue.peek(50)
        if not batch:
            return
        try:
            response = self.delivery.send_quotes(self.cfg.collector_id, batch)
            self._ack_quote_response(batch, response)
            self.last_delivery_error = None
        except DeliveryError as exc:
            self.queue.fail([row["record_id"] for row in batch], str(exc))
            self.last_delivery_error = str(exc)[:200]
            self._set_state("DELIVERY_FAILURE")

    def _ack_quote_response(self, batch: list[dict[str, Any]], response: dict[str, Any]) -> None:
        results = response.get("results") if isinstance(response, dict) else None
        if not isinstance(results, list):
            self.queue.fail([row["record_id"] for row in batch], "missing_per_record_results")
            self.last_delivery_error = "missing_per_record_results"
            self._set_state("DELIVERY_FAILURE")
            return
        by_id = {str(row.get("record_id") or ""): row for row in results if isinstance(row, dict)}
        ack_ids: list[str] = []
        for item in batch:
            record_id = item["record_id"]
            outcome = (by_id.get(record_id) or {}).get("outcome")
            reason = str((by_id.get(record_id) or {}).get("reason") or outcome or "missing_ack")[:200]
            if outcome in {"committed", "duplicate"}:
                ack_ids.append(record_id)
            elif outcome == "rejected":
                self.queue.quarantine([record_id], reason)
            else:
                self.queue.fail([record_id], reason)
        self.queue.ack(ack_ids)

    def _snapshot_quotes(self) -> None:
        if not self.client:
            return
        now = utcnow()
        types = []
        for key, sub in self.subs.items():
            req_id = sub["req_id"]
            ticks = dict(self.client.ticks.get(req_id) or {})
            entitlement = bool(ticks.get("entitlement_error"))
            md_code = self.client.market_data_types.get(req_id, ticks.get("market_data_type"))
            if md_code is None and ticks.get("delayed_ticks"):
                md_label = "DELAYED"
            elif md_code is None:
                md_label = "UNAVAILABLE"
            else:
                md_label = market_data_type_label(int(md_code))
            types.append(md_label)
            row = sub["row"]
            payload = {
                "symbol": row["symbol"],
                "sec_type": row.get("sec_type") or "STK",
                "con_id": sub.get("con_id"),
                "currency": row.get("currency") or "USD",
                "exchange": row.get("exchange") or "SMART",
                "primary_exchange": row.get("primary_exchange"),
                "display_name": row["symbol"],
                "quote_ts": now.isoformat(),
                "source_ts": _source_ts(ticks.get("last_timestamp")),
                "retrieved_at": now.isoformat(),
                "last_callback_at": ticks.get("last_callback_at"),
                "bid": ticks.get("bid"),
                "ask": ticks.get("ask"),
                "last_price": ticks.get("last"),
                "mid": _mid(ticks.get("bid"), ticks.get("ask")),
                "bid_size": ticks.get("bid_size"),
                "ask_size": ticks.get("ask_size"),
                "last_size": ticks.get("last_size"),
                "close_price": ticks.get("close"),
                "market_data_type": md_label,
                "delay_status": md_label,
                "quote_status": _quote_status(ticks, entitlement),
                "provenance": {
                    "collector_id": self.cfg.collector_id,
                    "client_id": self.cfg.client_id,
                    "tws_host": "127.0.0.1",
                    "code_version": "ibkr_collector_v1",
                },
            }
            payload["instrument_id"] = (
                "IBKR:{0}".format(sub["con_id"]) if sub.get("con_id") else None
            )
            payload["record_id"] = _record_id(payload)
            callback_at = ticks.get("last_callback_at")
            if callback_at:
                if self.last_callback_at is None or str(callback_at) > str(self.last_callback_at):
                    self.last_callback_at = callback_at
                # last_quote_at tracks a genuine TWS callback, never snapshot assembly time.
                self.last_quote_at = callback_at
            if any(payload.get(k) is not None for k in ("bid", "ask", "last_price", "close_price")):
                fingerprint = quote_value_fingerprint(payload)
                if self._last_quote_fp.get(key) != fingerprint:
                    put_status = self.queue.put(payload)
                    self._last_quote_fp[key] = fingerprint
                    if put_status == "overflow_dropped":
                        logger.warning("outbound queue overflow; oldest pending quote dropped")
        if types:
            preferred = [t for t in ("LIVE", "DELAYED", "FROZEN", "DELAYED_FROZEN", "UNAVAILABLE") if t in types]
            self.market_data_type = preferred[0] if preferred else "UNAVAILABLE"
        any_entitlement = any(bool((self.client.ticks.get(sub["req_id"]) or {}).get("entitlement_error")) for sub in self.subs.values())
        if any_entitlement and self.state == "CONNECTED":
            self._set_state("ENTITLEMENT_ERROR")
        elif self.state == "ENTITLEMENT_ERROR" and not any_entitlement:
            self._set_state("CONNECTED")

    def _qualify_and_subscribe(self) -> None:
        assert self.client is not None
        self.client.reqMarketDataType(3)
        wanted = {self._key(row): row for row in self.cfg.watchlist}
        for key in list(self.subs):
            if key not in wanted:
                try:
                    self.client.cancelMktData(self.subs[key]["req_id"])
                except Exception:
                    logger.debug("cancel leftover sub %s", key, exc_info=True)
                self.subs.pop(key, None)
        for key, row in wanted.items():
            if key in self.subs:
                continue
            time.sleep(0.25)
            detail_id = self.client.next_req_id()
            self.client.wait_event(detail_id, self.client.contract_details_done)
            self.client.reqContractDetails(detail_id, _contract_from_row(row))
            self.client.contract_details_done[detail_id].wait(8.0)
            details = self.client.contract_details.get(detail_id) or []
            contract = _contract_from_row(row)
            con_id = None
            if details and details[0].get("con_id"):
                con_id = int(details[0]["con_id"])
                contract.conId = con_id
            req_id = self.client.next_req_id()
            self.client.reqMktData(req_id, contract, "", False, False, [])
            self.subs[key] = {"req_id": req_id, "row": row, "con_id": con_id}
            logger.info("subscribed %s conId=%s req=%s", row["symbol"], con_id, req_id)

    def _disconnect(self) -> None:
        client = self.client
        self.client = None
        if client is None:
            return
        for sub in self.subs.values():
            try:
                client.cancelMktData(sub["req_id"])
            except Exception:
                pass
        self.subs.clear()
        self._last_quote_fp.clear()
        try:
            if client.isConnected():
                client.disconnect()
        except Exception:
            logger.debug("disconnect failed", exc_info=True)

    def _connect(self) -> bool:
        socket_ok = probe_socket(self.cfg.tws_host, self.cfg.tws_port)
        if not socket_ok["ok"]:
            self._set_state("WAITING_FOR_TWS")
            return False
        self.last_socket_ok_at = utcnow().isoformat()
        self._set_state("SOCKET_OPEN_HANDSHAKE_PENDING")
        client = ReadOnlyTwsClient()
        reader = threading.Thread(target=client.run, name="ibkr-collector-reader", daemon=True)
        client.connect(self.cfg.tws_host, self.cfg.tws_port, self.cfg.client_id)
        reader.start()
        if not client.handshake.wait(20.0):
            logger.warning("TWS socket open but API handshake timed out")
            try:
                client.disconnect()
            except Exception:
                pass
            return False
        self.client = client
        self.reader = reader
        now = utcnow().isoformat()
        self.last_handshake_at = now
        self.last_tws_connect_at = now
        self._set_state("CONNECTED")
        self.backoff = self.cfg.backoff_initial_sec
        try:
            self._qualify_and_subscribe()
        except Exception:
            logger.exception("subscription restore failed")
            self._disconnect()
            return False
        return True

    def _connected_loop_once(self) -> None:
        client = self.client
        if client is None or not client.isConnected() or client.disconnected.is_set():
            self._disconnect()
            self._set_state("DISCONNECTED")
            return
        now = time.monotonic()
        if now - self._last_quote_push >= self.cfg.quote_interval_sec:
            self._snapshot_quotes()
            self._deliver_queue()
            self._last_quote_push = now
        if now - self._last_heartbeat >= self.cfg.heartbeat_interval_sec:
            self._deliver_heartbeat()
            self._last_heartbeat = now

    def run(self) -> None:
        logger.info("collector starting; TWS %s:%s client_id=%s", self.cfg.tws_host, self.cfg.tws_port, self.cfg.client_id)
        while not _STOP.is_set():
            if self.client is None:
                if self._connect():
                    continue
                jitter = random.uniform(0, self.backoff * 0.2)
                sleep_for = min(self.cfg.backoff_max_sec, self.backoff) + jitter
                logger.info("waiting %.1fs for TWS (%s)", sleep_for, self.state)
                _STOP.wait(sleep_for)
                self.backoff = min(self.cfg.backoff_max_sec, max(self.cfg.backoff_initial_sec, self.backoff * 2))
                if time.monotonic() - self._last_heartbeat >= self.cfg.heartbeat_interval_sec:
                    self._deliver_heartbeat()
                    self._last_heartbeat = time.monotonic()
                continue
            self._connected_loop_once()
            _STOP.wait(0.5)
        self._disconnect()
        logger.info("collector stopped")


def run_forever() -> int:
    cfg = load_config()
    write_example_config(cfg.config_path)
    setup_logging(cfg.log_dir)
    lock = InstanceLock(cfg.lock_path)
    if not lock.acquire():
        logger.error("another collector instance holds the lock; exiting")
        return 4
    _install_stop_handlers()
    try:
        CollectorRuntime(cfg).run()
    finally:
        lock.release()
    return 0
