"""Official ibapi client restricted to read-only market-data and contract lookup."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from ibkr_collector.values import BLOCKED_ECLIENT_METHODS, classify_error, finite_or_none

logger = logging.getLogger("ibkr_collector.client")

MAX_ERRORS = 200
MAX_RAW_TICKS = 100
MAX_REQ_STATE = 4096
TERMINAL_ERROR_KINDS = frozenset({"entitlement", "error", "pacing", "historical", "invalid_contract"})


class OrderMethodBlocked(RuntimeError):
    """Raised if any order, account, position, or execution API is invoked."""


def _block(method_name: str) -> Callable[..., Any]:
    def _blocked(self, *args: Any, **kwargs: Any) -> None:
        raise OrderMethodBlocked("blocked IBKR API method: {0}".format(method_name))

    _blocked.__name__ = method_name
    return _blocked


def _install_blocks(cls: type) -> type:
    for name in BLOCKED_ECLIENT_METHODS:
        setattr(cls, name, _block(name))
    return cls


class _ReadOnlyCallbacks:
    """Shared callback/state mixin; composed with official EWrapper/EClient at runtime."""

    def _init_readonly_state(self) -> None:
        self._lock = threading.Lock()
        self.handshake = threading.Event()
        self.disconnected = threading.Event()
        self.next_valid_id: int | None = None
        self.server_time_unix: int | None = None
        self.server_time_event = threading.Event()
        self.errors: list[dict[str, Any]] = []
        self.contract_details: dict[int, list[dict[str, Any]]] = {}
        self.contract_details_done: dict[int, threading.Event] = {}
        self.symbol_samples: dict[int, list[dict[str, Any]]] = {}
        self.symbol_samples_done: dict[int, threading.Event] = {}
        self.option_params: dict[int, list[dict[str, Any]]] = {}
        self.option_params_done: dict[int, threading.Event] = {}
        self.ticks: dict[int, dict[str, Any]] = {}
        self.market_data_types: dict[int, int] = {}
        self.raw_ticks: list[dict[str, Any]] = []
        self.historical_bars: dict[int, list[dict[str, Any]]] = {}
        self.historical_done: dict[int, threading.Event] = {}
        self.historical_pending: set[int] = set()
        self._req_seq = 1000
        self._generation = 0
        self._req_generation: dict[int, int] = {}
        self._cancelled: set[int] = set()

    def begin_connection_generation(self) -> int:
        """Advance the connection generation and release waiters from the previous socket."""
        with self._lock:
            self._generation += 1
            events = self._all_wait_events_unlocked()
            self.historical_pending.clear()
            generation = self._generation
        for event in events:
            event.set()
        return generation

    def mark_historical(self, req_id: int) -> threading.Event:
        with self._lock:
            self.historical_pending.add(req_id)
            self.historical_bars.setdefault(req_id, [])
            return self._ensure_event_unlocked(req_id, self.historical_done)

    def next_req_id(self) -> int:
        with self._lock:
            self._req_seq += 1
            req_id = self._req_seq
            self._req_generation[req_id] = self._generation
            self._cancelled.discard(req_id)
            self._prune_req_state_unlocked()
            return req_id

    def wait_event(self, req_id: int, store: dict[int, threading.Event]) -> threading.Event:
        with self._lock:
            return self._ensure_event_unlocked(req_id, store)

    def retire_request(self, req_id: int) -> None:
        """Mark a request cancelled so late callbacks cannot mutate a later occupant."""
        with self._lock:
            self._cancelled.add(req_id)
            self.historical_pending.discard(req_id)
            events = [
                self.historical_done.get(req_id),
                self.contract_details_done.get(req_id),
                self.symbol_samples_done.get(req_id),
                self.option_params_done.get(req_id),
            ]
        for event in events:
            if event is not None:
                event.set()

    def _ensure_event_unlocked(self, req_id: int, store: dict[int, threading.Event]) -> threading.Event:
        event = store.get(req_id)
        if event is None:
            event = threading.Event()
            store[req_id] = event
        return event

    def _all_wait_events_unlocked(self) -> list[threading.Event]:
        events: list[threading.Event] = []
        for store in (
            self.historical_done,
            self.contract_details_done,
            self.symbol_samples_done,
            self.option_params_done,
        ):
            events.extend(store.values())
        return events

    def _stale_unlocked(self, req_id: int) -> bool:
        if req_id in self._cancelled:
            return True
        generation = self._req_generation.get(req_id)
        if generation is None:
            return False
        return generation != self._generation

    def _prune_req_state_unlocked(self) -> None:
        if len(self._req_generation) <= MAX_REQ_STATE:
            return
        keep_from = self._req_seq - (MAX_REQ_STATE // 2)
        for store in (
            self.contract_details,
            self.contract_details_done,
            self.symbol_samples,
            self.symbol_samples_done,
            self.option_params,
            self.option_params_done,
            self.ticks,
            self.market_data_types,
            self.historical_bars,
            self.historical_done,
            self._req_generation,
        ):
            for req_id in [key for key in store if key < keep_from]:
                store.pop(req_id, None)
        self.historical_pending = {req_id for req_id in self.historical_pending if req_id >= keep_from}
        self._cancelled = {req_id for req_id in self._cancelled if req_id >= keep_from}

    def nextValidId(self, orderId: int) -> None:
        self.next_valid_id = int(orderId)
        self.handshake.set()

    def connectionClosed(self) -> None:
        self.disconnected.set()
        self.handshake.clear()
        self.begin_connection_generation()

    def connectAck(self) -> None:
        logger.info("tws socket accepted API connectAck")

    def managedAccounts(self, accountsList: str) -> None:
        return

    def currentTime(self, time: int) -> None:
        self.server_time_unix = int(time)
        self.server_time_event.set()

    def error(self, reqId: int, errorTime: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:
        kind = classify_error(int(errorCode))
        rec = {
            "req_id": reqId,
            "error_time": errorTime,
            "error_code": int(errorCode),
            "error_string": str(errorString or ""),
            "kind": kind,
        }
        done_event = None
        with self._lock:
            self.errors.append(rec)
            if len(self.errors) > MAX_ERRORS:
                self.errors = self.errors[-MAX_ERRORS:]
            stale = self._stale_unlocked(reqId)
            if not stale and kind == "entitlement" and reqId is not None:
                bucket = self.ticks.setdefault(reqId, {})
                bucket["entitlement_error"] = True
                bucket["entitlement_code"] = int(errorCode)
            if not stale and reqId in self.historical_pending and kind in TERMINAL_ERROR_KINDS:
                done_event = self._ensure_event_unlocked(reqId, self.historical_done)
                self.historical_pending.discard(reqId)
        if done_event is not None:
            done_event.set()
        if kind == "info":
            logger.info("tws info code=%s req=%s", errorCode, reqId)
        elif kind == "entitlement":
            logger.warning("tws entitlement code=%s req=%s msg=%s", errorCode, reqId, rec["error_string"][:180])
        elif kind == "connectivity":
            logger.warning("tws connectivity code=%s req=%s msg=%s", errorCode, reqId, rec["error_string"][:180])
            if int(errorCode) in {502, 504, 1100, 1300, 507}:
                self.disconnected.set()
        else:
            logger.warning("tws error code=%s req=%s msg=%s", errorCode, reqId, rec["error_string"][:180])

    def contractDetails(self, reqId: int, contractDetails) -> None:
        payload = _contract_details_payload(contractDetails, bond=False)
        with self._lock:
            if self._stale_unlocked(reqId):
                return
            self.contract_details.setdefault(reqId, []).append(payload)

    def bondContractDetails(self, reqId: int, contractDetails) -> None:
        payload = _contract_details_payload(contractDetails, bond=True)
        with self._lock:
            if self._stale_unlocked(reqId):
                return
            self.contract_details.setdefault(reqId, []).append(payload)

    def contractDetailsEnd(self, reqId: int) -> None:
        event = self.wait_event(reqId, self.contract_details_done)
        event.set()

    def symbolSamples(self, reqId: int, contractDescriptions) -> None:
        rows = []
        for desc in contractDescriptions or []:
            contract = getattr(desc, "contract", None)
            deriv = list(getattr(desc, "derivativeSecTypes", None) or [])
            rows.append(
                {
                    "con_id": getattr(contract, "conId", None) if contract is not None else None,
                    "symbol": getattr(contract, "symbol", None) if contract is not None else None,
                    "sec_type": getattr(contract, "secType", None) if contract is not None else None,
                    "primary_exchange": getattr(contract, "primaryExchange", None) if contract is not None else None,
                    "currency": getattr(contract, "currency", None) if contract is not None else None,
                    "description": getattr(contract, "description", None) if contract is not None else None,
                    "derivative_sec_types": deriv,
                }
            )
        with self._lock:
            if self._stale_unlocked(reqId):
                return
            self.symbol_samples[reqId] = rows
        self.wait_event(reqId, self.symbol_samples_done).set()

    def securityDefinitionOptionParameter(self, reqId: int, exchange: str, underlyingConId: int, tradingClass: str, multiplier: str, expirations, strikes) -> None:
        payload = {
            "exchange": exchange,
            "underlying_con_id": underlyingConId,
            "trading_class": tradingClass,
            "multiplier": multiplier,
            "expirations": sorted(str(item) for item in (expirations or [])),
            "strikes": sorted(float(item) for item in (strikes or [])),
        }
        with self._lock:
            if self._stale_unlocked(reqId):
                return
            self.option_params.setdefault(reqId, []).append(payload)

    def securityDefinitionOptionParameterEnd(self, reqId: int) -> None:
        self.wait_event(reqId, self.option_params_done).set()

    def _note_callback(self, reqId: int) -> None:
        from ibkr_collector.values import utcnow

        bucket = self.ticks.setdefault(reqId, {})
        bucket["last_callback_at"] = utcnow().isoformat()

    def marketDataType(self, reqId: int, marketDataType: int) -> None:
        with self._lock:
            if self._stale_unlocked(reqId):
                return
            self.market_data_types[reqId] = int(marketDataType)
            bucket = self.ticks.setdefault(reqId, {})
            bucket["market_data_type"] = int(marketDataType)

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib) -> None:
        from ibkr_collector.values import PRICE_TICKS

        try:
            tick_id = int(tickType)
        except (TypeError, ValueError):
            tick_id = tickType
        with self._lock:
            if self._stale_unlocked(reqId):
                return
            self.raw_ticks.append({"req_id": reqId, "kind": "price", "tick_type": tick_id, "price": finite_or_none(price)})
            if len(self.raw_ticks) > MAX_RAW_TICKS:
                self.raw_ticks = self.raw_ticks[-MAX_RAW_TICKS:]
            self._note_callback(reqId)
        field = PRICE_TICKS.get(tick_id) if isinstance(tick_id, int) else None
        if not field:
            return
        with self._lock:
            if self._stale_unlocked(reqId):
                return
            bucket = self.ticks.setdefault(reqId, {})
            bucket[field] = finite_or_none(price)
            bucket["{0}_callback_at".format(field)] = bucket.get("last_callback_at")
            if isinstance(tick_id, int) and tick_id >= 66:
                bucket["delayed_ticks"] = True

    def tickSize(self, reqId: int, tickType: int, size) -> None:
        from ibkr_collector.values import SIZE_TICKS

        field = SIZE_TICKS.get(int(tickType))
        if not field:
            return
        value = finite_or_none(size)
        with self._lock:
            if self._stale_unlocked(reqId):
                return
            bucket = self.ticks.setdefault(reqId, {})
            bucket[field] = value
            bucket["{0}_callback_at".format(field)] = bucket.get("last_callback_at")
            self._note_callback(reqId)

    def tickString(self, reqId: int, tickType: int, value: str) -> None:
        from ibkr_collector.values import TIMESTAMP_TICKS

        field = TIMESTAMP_TICKS.get(int(tickType))
        if not field:
            return
        with self._lock:
            if self._stale_unlocked(reqId):
                return
            bucket = self.ticks.setdefault(reqId, {})
            bucket[field] = value or None
            self._note_callback(reqId)

    def tickGeneric(self, reqId: int, tickType: int, value: float) -> None:
        from ibkr_collector.values import GENERIC_TICKS

        field = GENERIC_TICKS.get(int(tickType))
        if not field:
            return
        with self._lock:
            if self._stale_unlocked(reqId):
                return
            bucket = self.ticks.setdefault(reqId, {})
            bucket[field] = finite_or_none(value)
            bucket["{0}_callback_at".format(field)] = bucket.get("last_callback_at")
            self._note_callback(reqId)

    def tickOptionComputation(self, reqId: int, tickType: int, tickAttrib: int, impliedVol: float, delta: float, optPrice: float, pvDividend: float, gamma: float, vega: float, theta: float, undPrice: float) -> None:
        from ibkr_collector.values import OPTION_COMPUTATION_TICKS, utcnow

        label = OPTION_COMPUTATION_TICKS.get(int(tickType), "tick_{0}".format(tickType))
        payload = {
            "tick_type": int(tickType),
            "tick_attrib": int(tickAttrib) if tickAttrib is not None else None,
            "implied_vol": finite_or_none(impliedVol),
            "delta": finite_or_none(delta),
            "opt_price": finite_or_none(optPrice),
            "pv_dividend": finite_or_none(pvDividend),
            "gamma": finite_or_none(gamma),
            "vega": finite_or_none(vega),
            "theta": finite_or_none(theta),
            "und_price": finite_or_none(undPrice),
            "callback_at": utcnow().isoformat(),
        }
        with self._lock:
            if self._stale_unlocked(reqId):
                return
            bucket = self.ticks.setdefault(reqId, {})
            bucket.setdefault("option_computations", {})[label] = payload
            self._note_callback(reqId)

    def tickSnapshotEnd(self, reqId: int) -> None:
        with self._lock:
            if self._stale_unlocked(reqId):
                return
            bucket = self.ticks.setdefault(reqId, {})
            bucket["snapshot_end"] = True

    def historicalData(self, reqId: int, bar) -> None:
        payload = {
            "date": getattr(bar, "date", None) or getattr(bar, "time", None),
            "open": finite_or_none(getattr(bar, "open", None)),
            "high": finite_or_none(getattr(bar, "high", None)),
            "low": finite_or_none(getattr(bar, "low", None)),
            "close": finite_or_none(getattr(bar, "close", None)),
            "volume": finite_or_none(getattr(bar, "volume", None)),
        }
        with self._lock:
            if self._stale_unlocked(reqId):
                return
            self.historical_bars.setdefault(reqId, []).append(payload)

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
        with self._lock:
            if self._stale_unlocked(reqId):
                self.historical_pending.discard(reqId)
                event = self.historical_done.get(reqId)
            else:
                self.historical_pending.discard(reqId)
                event = self._ensure_event_unlocked(reqId, self.historical_done)
        if event is not None:
            event.set()

    def cancelMktData(self, reqId: int) -> None:
        self.retire_request(reqId)
        from ibapi.client import EClient

        return EClient.cancelMktData(self, reqId)

    def cancelHistoricalData(self, reqId: int) -> None:
        self.retire_request(reqId)
        from ibapi.client import EClient

        return EClient.cancelHistoricalData(self, reqId)

    def connect(self, host: str, port: int, clientId: int) -> None:
        self.begin_connection_generation()
        from ibapi.client import EClient

        return EClient.connect(self, host, port, clientId)


_CLIENT_CLS: type | None = None


def ReadOnlyTwsClient():
    """Construct the official ibapi client with read-only callbacks and blocked order methods."""
    global _CLIENT_CLS
    if _CLIENT_CLS is None:
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper

        class _Live(_ReadOnlyCallbacks, EWrapper, EClient):
            def __init__(self) -> None:
                EClient.__init__(self, self)
                self._init_readonly_state()

        _install_blocks(_Live)
        _CLIENT_CLS = _Live
    return _CLIENT_CLS()


def _contract_details_payload(contractDetails, *, bond: bool) -> dict[str, Any]:
    contract = getattr(contractDetails, "contract", None)
    payload: dict[str, Any] = {
        "bond": bond,
        "con_id": getattr(contract, "conId", None) if contract is not None else None,
        "symbol": getattr(contract, "symbol", None) if contract is not None else None,
        "local_symbol": getattr(contract, "localSymbol", None) if contract is not None else None,
        "sec_type": getattr(contract, "secType", None) if contract is not None else None,
        "currency": getattr(contract, "currency", None) if contract is not None else None,
        "exchange": getattr(contract, "exchange", None) if contract is not None else None,
        "primary_exchange": getattr(contract, "primaryExchange", None) if contract is not None else None,
        "trading_class": getattr(contract, "tradingClass", None) if contract is not None else None,
        "multiplier": getattr(contract, "multiplier", None) if contract is not None else None,
        "last_trade_date": getattr(contract, "lastTradeDateOrContractMonth", None) if contract is not None else None,
        "strike": getattr(contract, "strike", None) if contract is not None else None,
        "right": getattr(contract, "right", None) if contract is not None else None,
        "min_tick": getattr(contractDetails, "minTick", None),
        "long_name": getattr(contractDetails, "longName", None),
        "market_name": getattr(contractDetails, "marketName", None),
        "valid_exchanges": getattr(contractDetails, "validExchanges", None),
    }
    if bond:
        payload.update(
            {
                "cusip": getattr(contractDetails, "cusip", None) or None,
                "coupon": finite_or_none(getattr(contractDetails, "coupon", None)),
                "maturity": getattr(contractDetails, "maturity", None) or None,
                "issue_date": getattr(contractDetails, "issueDate", None) or None,
                "ratings": getattr(contractDetails, "ratings", None) or None,
                "bond_type": getattr(contractDetails, "bondType", None) or None,
                "coupon_type": getattr(contractDetails, "couponType", None) or None,
                "next_option_date": getattr(contractDetails, "nextOptionDate", None) or None,
                "next_option_type": getattr(contractDetails, "nextOptionType", None) or None,
                "next_option_partial": getattr(contractDetails, "nextOptionPartial", None),
                "notes": getattr(contractDetails, "notes", None) or None,
            }
        )
    return payload
