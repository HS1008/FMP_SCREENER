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
from ibkr_collector.historical import parse_historical_bar
from ibkr_collector.session_open import choose_latest_open, needs_open_refresh
from ibkr_collector.delivery import DeliveryError, IngestClient
from ibkr_collector.diagnostic import probe_socket
from ibkr_collector.lock import InstanceLock
from ibkr_collector.logging_setup import setup_logging
from ibkr_collector.queue import OutboundQueue
from ibkr_collector.readonly_client import ReadOnlyTwsClient
from ibkr_collector.secrets_win import read_ingest_token
from ibkr_collector.values import (
    DELAYED_AVAILABLE_CODES,
    LINE_LIMIT_ERROR_CODES,
    market_data_type_label,
    utcnow,
)
from market_intelligence.ibkr_live_universe import (
    EXPECTED_IBKR_LIVE_COUNT,
    approved_contracts,
    assert_subscription_allowed,
)
from market_intelligence.live_session import ibkr_mark_price, latest_opened_rth_session, open_to_current_return

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


def contract_subscription_key(row: dict[str, Any]) -> str:
    """One line per contract. Ticker alone is not the identity."""
    return "|".join(
        (
            str(row.get("sec_type") or "STK"),
            str(row.get("symbol") or ""),
            str(row.get("exchange") or "SMART"),
            str(row.get("primary_exchange") or ""),
            str(row.get("currency") or "USD"),
            str(row.get("con_id") or ""),
        )
    )


def subscriptions_to_open(existing: set[str], wanted_keys: list[str]) -> list[str]:
    """Skip keys already subscribed, including duplicates inside the wanted list."""
    seen = set(existing)
    opened: list[str] = []
    for key in wanted_keys:
        if key in seen:
            continue
        seen.add(key)
        opened.append(key)
    return opened


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
    ) + tuple(
        (payload.get("provenance") or {}).get(key)
        for key in ("current_price", "current_price_field", "session_open", "session_date", "open_to_current")
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
        self._last_quote_queued: dict[str, float] = {}
        self.backoff = cfg.backoff_initial_sec
        self._last_heartbeat = 0.0
        self._last_quote_push = 0.0
        self._md_type = 1
        self._delayed_fallback_done = False
        self._line_limit_hit = False
        self._last_heal = 0.0
        self._last_open_request = 0.0
        self._last_coverage_log = 0.0
        self._sample_logged: set[str] = set()
        self.session_opens = self._load_session_opens()

    def _key(self, row: dict[str, str]) -> str:
        return contract_subscription_key(row)

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
            "watchlist": [row["symbol"] for row in approved_contracts()],
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
            logger.warning("heartbeat delivery failed: %s", self.last_delivery_error)
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
            results = response.get("results") if isinstance(response, dict) else None
            outcomes: dict[str, int] = {}
            if isinstance(results, list):
                for item in results:
                    if isinstance(item, dict):
                        name = str(item.get("outcome") or "missing")
                        outcomes[name] = outcomes.get(name, 0) + 1
            logger.info("quote batch sent n=%s outcomes=%s", len(batch), outcomes or "unparsed")
            self._ack_quote_response(batch, response)
            self.last_delivery_error = None
        except DeliveryError as exc:
            self.queue.fail([row["record_id"] for row in batch], str(exc))
            self.last_delivery_error = str(exc)[:200]
            logger.warning("quote delivery failed: %s", self.last_delivery_error)
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
        priced = 0
        delayed_n = 0
        unpriced: list[str] = []
        for key, sub in self.subs.items():
            req_id = sub["req_id"]
            ticks = dict(self.client.ticks.get(req_id) or {})
            entitlement = bool(ticks.get("entitlement_error"))
            if entitlement and self.client is not None:
                for err in reversed(self.client.errors):
                    if err.get("req_id") == req_id and err.get("kind") == "entitlement":
                        ticks["entitlement_code"] = err.get("error_code")
                        ticks["entitlement_message"] = err.get("error_string")
                        break
            md_code = self.client.market_data_types.get(req_id, ticks.get("market_data_type"))
            if md_code is None and ticks.get("delayed_ticks"):
                md_label = "DELAYED"
            elif md_code is None:
                md_label = "UNAVAILABLE"
            else:
                md_label = market_data_type_label(int(md_code))
            types.append(md_label)
            if ticks.get("delayed_ticks"):
                delayed_n += 1
            if any(ticks.get(k) is not None for k in ("bid", "ask", "last", "close")):
                priced += 1
            else:
                unpriced.append(str(sub["row"]["symbol"]))
            row = sub["row"]
            mark, mark_field = ibkr_mark_price(ticks.get("last"), ticks.get("bid"), ticks.get("ask"))
            cached_open = self.session_opens.get(str(row["symbol"]).upper()) or {}
            session_open = cached_open.get("open")
            change = open_to_current_return(mark, session_open)
            error_text = None
            if entitlement:
                error_text = "IBKR {0}: {1}".format(
                    ticks.get("entitlement_code") or "entitlement",
                    ticks.get("entitlement_message") or "market data entitlement",
                )
            elif cached_open.get("error"):
                error_text = str(cached_open.get("error"))
            elif sub.get("silent_exhausted"):
                error_text = "IBKR sent no bid, ask, or last on this line"
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
                    "symbol": row["symbol"],
                    "current_price": mark,
                    "current_price_field": mark_field,
                    "session_open": session_open,
                    "session_date": cached_open.get("session_date"),
                    "open_to_current": change,
                    "quote_error": error_text,
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
            if row["symbol"] in {"SPY", "QQQ", "SMH", "KRE", "XBI", "NVDA", "WULF", "CRWV", "COHR", "PLTR", "VIX"}:
                if row["symbol"] not in self._sample_logged and (
                    mark is not None or error_text or ticks.get("last") is not None or ticks.get("close") is not None
                ):
                    self._sample_logged.add(row["symbol"])
                    logger.info(
                        "sample %s status=%s md=%s field=%s px=%s last=%s bid=%s ask=%s close=%s open=%s session=%s chg=%s err=%s",
                        row["symbol"],
                        payload["quote_status"],
                        md_label,
                        mark_field,
                        mark,
                        payload["last_price"],
                        payload["bid"],
                        payload["ask"],
                        payload["close_price"],
                        session_open,
                        cached_open.get("session_date"),
                        change,
                        error_text,
                    )
            if error_text or any(payload.get(k) is not None for k in ("bid", "ask", "last_price", "close_price")):
                fingerprint = quote_value_fingerprint(payload)
                queued_at = self._last_quote_queued.get(key, 0.0)
                refresh = time.monotonic() - queued_at >= 120.0
                if self._last_quote_fp.get(key) != fingerprint or refresh:
                    put_status = self.queue.put(payload)
                    self._last_quote_fp[key] = fingerprint
                    self._last_quote_queued[key] = time.monotonic()
                    if put_status == "overflow_dropped":
                        logger.warning("outbound queue overflow; oldest pending quote dropped")
        if types:
            preferred = [t for t in ("LIVE", "DELAYED", "FROZEN", "DELAYED_FROZEN", "UNAVAILABLE") if t in types]
            self.market_data_type = preferred[0] if preferred else "UNAVAILABLE"
        if self.subs and time.monotonic() - self._last_coverage_log >= 60.0:
            self._last_coverage_log = time.monotonic()
            logger.info(
                "quote coverage priced=%s/%s delayed_ticks=%s market_data_type=%s unpriced=%s",
                priced,
                len(self.subs),
                delayed_n,
                self.market_data_type,
                ",".join(unpriced) if unpriced else "-",
            )
        any_entitlement = any(bool((self.client.ticks.get(sub["req_id"]) or {}).get("entitlement_error")) for sub in self.subs.values())
        if any_entitlement and self.state == "CONNECTED":
            self._set_state("ENTITLEMENT_ERROR")
        elif self.state == "ENTITLEMENT_ERROR" and not any_entitlement:
            self._set_state("CONNECTED")

    def _qualify_and_subscribe(self) -> None:
        assert self.client is not None
        rows = [dict(row) for row in approved_contracts()]
        for row in rows:
            assert_subscription_allowed(row["symbol"], row.get("sec_type") or "STK")
        if len(rows) != EXPECTED_IBKR_LIVE_COUNT:
            raise RuntimeError("refusing to subscribe {0} instruments; expected {1}".format(len(rows), EXPECTED_IBKR_LIVE_COUNT))
        symbols = [row["symbol"] for row in rows]
        logger.info("IBKR live subscription count=%s symbols=%s", len(symbols), ",".join(symbols))
        qualified: list[tuple[dict[str, str], int | None, list[dict[str, Any]]]] = []
        for row in rows:
            time.sleep(0.25)
            qualified.append((row, *self._qualify_row(row)))
        keep_probe = self._probe_market_data_type(qualified[0])
        if self._line_limit_hit:
            logger.error("IBKR line limit already hit; leaving the book at %s lines", len(self.subs))
            return
        self.client.reqMarketDataType(self._md_type)
        probe_key = self._key(qualified[0][0])
        for row, con_id, details in qualified:
            if keep_probe and self._key(row) == probe_key:
                continue
            if not self._subscribe_row(row, con_id, details):
                break
        logger.info("IBKR market data lines open=%s type=%s", len(self.subs), self._md_type)

    def _qualify_row(self, row: dict[str, str]) -> tuple[int | None, list[dict[str, Any]]]:
        assert self.client is not None
        detail_id = self.client.next_req_id()
        self.client.wait_event(detail_id, self.client.contract_details_done)
        self.client.reqContractDetails(detail_id, _contract_from_row(row))
        self.client.contract_details_done[detail_id].wait(8.0)
        details = list(self.client.contract_details.get(detail_id) or [])
        con_id = None
        if details and details[0].get("con_id"):
            con_id = int(details[0]["con_id"])
        if len(details) != 1:
            logger.warning("contract detail count=%s for %s; using conId=%s", len(details), row["symbol"], con_id)
        return con_id, details

    def _subscribe_row(self, row: dict[str, str], con_id: int | None, details: list[dict[str, Any]]) -> bool:
        """Open one streaming line. False means stop; the line limit was hit."""
        assert self.client is not None
        if self._line_limit_hit or any(err.get("error_code") in LINE_LIMIT_ERROR_CODES for err in self.client.errors):
            self._line_limit_hit = True
            logger.error("IBKR market data line limit; not opening %s", row["symbol"])
            return False
        if len(self.subs) >= EXPECTED_IBKR_LIVE_COUNT:
            logger.error("refusing a market data line beyond %s", EXPECTED_IBKR_LIVE_COUNT)
            return False
        key = self._key(row)
        if key in self.subs:
            return True
        contract = _contract_from_row(row)
        if con_id:
            contract.conId = con_id
        req_id = self.client.next_req_id()
        self.client.reqMktData(req_id, contract, "", False, False, [])
        time.sleep(0.08)
        self.subs[key] = {"req_id": req_id, "row": row, "con_id": con_id, "opened_at": time.monotonic()}
        chosen = details[0] if details else {}
        logger.info(
            "subscribed %s conId=%s req=%s name=%s primary=%s",
            row["symbol"],
            con_id,
            req_id,
            chosen.get("long_name"),
            chosen.get("primary_exchange"),
        )
        return True

    def _probe_market_data_type(self, probe: tuple[dict[str, str], int | None, list[dict[str, Any]]]) -> bool:
        """Try one live line. On 2186, cancel it and use delayed for the single full pass.

        Returns True when the probe line is kept. A second pass over the book is never opened.
        """
        assert self.client is not None
        row, con_id, details = probe
        self._md_type = 1
        self.client.reqMarketDataType(1)
        self._subscribe_row(row, con_id, details)
        req_id = self.subs[self._key(row)]["req_id"]
        deadline = time.monotonic() + 2.5
        code = None
        priced = False
        while time.monotonic() < deadline:
            for err in list(self.client.errors):
                if err.get("req_id") != req_id:
                    continue
                if err.get("error_code") in DELAYED_AVAILABLE_CODES or err.get("error_code") in LINE_LIMIT_ERROR_CODES:
                    code = err.get("error_code")
                    break
            ticks = self.client.ticks.get(req_id) or {}
            if any(ticks.get(name) is not None for name in ("bid", "ask", "last", "close")):
                priced = True
                break
            if code is not None:
                break
            time.sleep(0.05)
        if priced:
            logger.info("probe %s accepted live market data", row["symbol"])
            return True
        if code in LINE_LIMIT_ERROR_CODES:
            self._line_limit_hit = True
            logger.error("IBKR line limit on probe %s (code %s); not opening more lines", row["symbol"], code)
            return True
        if code is None:
            logger.warning(
                "probe %s sent no price; cancelling that line and opening each symbol once as DELAYED",
                row["symbol"],
            )
        else:
            logger.warning(
                "probe %s returned %s; cancelling that line and opening each symbol once as DELAYED",
                row["symbol"],
                code,
            )
        try:
            self.client.cancelMktData(req_id)
        except Exception:
            logger.debug("cancel probe %s", row["symbol"], exc_info=True)
        self.subs.pop(self._key(row), None)
        time.sleep(0.5)
        self._md_type = 3
        self._delayed_fallback_done = True
        return False

    def _session_open_path(self):
        return self.cfg.data_dir / "session_opens.json"

    def _load_session_opens(self) -> dict[str, Any]:
        path = self._session_open_path()
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_session_opens(self) -> None:
        path = self._session_open_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.session_opens, default=str) + "\n", encoding="utf-8")

    def _heal_one_silent_quote(self) -> None:
        """Reopen one line that never received a price. Cancel first so the line count does not rise."""
        if self.client is None or self._line_limit_hit:
            return
        now = time.monotonic()
        if now - self._last_heal < 3.0:
            return
        for key, sub in list(self.subs.items()):
            ticks = self.client.ticks.get(sub["req_id"]) or {}
            if any(ticks.get(name) is not None for name in ("bid", "ask", "last", "close")):
                continue
            if ticks.get("entitlement_error"):
                continue
            if now - float(sub.get("opened_at") or now) < 20.0:
                continue
            symbol = str(sub["row"]["symbol"])
            retries = int(sub.get("silent_retries") or 0)
            if retries >= 2:
                if not sub.get("silent_exhausted"):
                    sub["silent_exhausted"] = True
                    logger.warning("no IBKR ticks for %s after retries", symbol)
                continue
            self._last_heal = now
            sub["silent_retries"] = retries + 1
            logger.warning("no ticks for %s; reopening that one line (%s/2)", symbol, retries + 1)
            try:
                self.client.cancelMktData(sub["req_id"])
            except Exception:
                logger.debug("cancel silent %s", key, exc_info=True)
            time.sleep(0.3)
            if any(err.get("error_code") in LINE_LIMIT_ERROR_CODES for err in self.client.errors):
                self._line_limit_hit = True
                logger.error("IBKR line limit while reopening %s; stopped", symbol)
                return
            contract = _contract_from_row(sub["row"])
            if sub.get("con_id"):
                contract.conId = int(sub["con_id"])
            req_id = self.client.next_req_id()
            self.client.reqMktData(req_id, contract, "", False, False, [])
            sub["req_id"] = req_id
            sub["opened_at"] = time.monotonic()
            return

    def _maybe_downgrade_market_data(self) -> None:
        """2186 is recorded on the existing line. Never open a second line for the same symbol."""
        if self._delayed_fallback_done or self.client is None:
            return
        hits = [err for err in self.client.errors if err.get("error_code") in DELAYED_AVAILABLE_CODES]
        if not hits:
            return
        self._delayed_fallback_done = True
        logger.warning(
            "IBKR code %s on an open line; leaving that subscription in place",
            hits[-1].get("error_code"),
        )

    def _refresh_one_open(self) -> None:
        if self.client is None or not self.subs:
            return
        if time.monotonic() - self._last_open_request < 2.5:
            return
        expected = latest_opened_rth_session()
        now = utcnow()
        for sub in self.subs.values():
            symbol = str(sub["row"]["symbol"]).upper()
            if not needs_open_refresh(self.session_opens.get(symbol), expected, now):
                continue
            self._last_open_request = time.monotonic()
            self._fetch_open(symbol, sub)
            return

    def _fetch_open(self, symbol: str, sub: dict[str, Any]) -> None:
        assert self.client is not None
        row = sub["row"]
        contract = _contract_from_row(row)
        if sub.get("con_id"):
            contract.conId = int(sub["con_id"])
        req_id = self.client.next_req_id()
        self.client.mark_historical(req_id)
        try:
            self.client.reqHistoricalData(req_id, contract, "", "2 W", "1 day", "TRADES", 1, 1, False, [])
            self.client.historical_done[req_id].wait(12.0)
        except Exception:
            logger.exception("session open request failed for %s", symbol)
            self.session_opens[symbol] = {
                "session_date": (self.session_opens.get(symbol) or {}).get("session_date"),
                "open": (self.session_opens.get(symbol) or {}).get("open"),
                "fetched_at": utcnow().isoformat(),
                "error": "historical request failed",
            }
            self._save_session_opens()
            return
        raw_bars = list(self.client.historical_bars.get(req_id) or [])
        parsed = []
        for item in raw_bars:
            bar = parse_historical_bar(item, what_to_show="TRADES")
            if bar is not None and bar.open is not None:
                parsed.append((bar.bar_date, float(bar.open)))
        chosen = choose_latest_open(parsed)
        errors = [err for err in self.client.errors if err.get("req_id") == req_id and err.get("kind") != "info"]
        record: dict[str, Any] = {"fetched_at": utcnow().isoformat(), "error": None}
        if chosen is None:
            record["session_date"] = (self.session_opens.get(symbol) or {}).get("session_date")
            record["open"] = (self.session_opens.get(symbol) or {}).get("open")
            if errors:
                record["error"] = "IBKR {0}: {1}".format(errors[-1].get("error_code"), str(errors[-1].get("error_string") or "")[:180])
            else:
                record["error"] = "no regular-session open"
        else:
            record["session_date"] = chosen[0].isoformat()
            record["open"] = chosen[1]
        self.session_opens[symbol] = record
        self._save_session_opens()
        logger.info("session open %s date=%s open=%s", symbol, record.get("session_date"), record.get("open"))

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
        self._maybe_downgrade_market_data()
        self._heal_one_silent_quote()
        self._refresh_one_open()
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
