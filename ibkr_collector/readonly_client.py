"""Official ibapi client restricted to read-only market-data and contract lookup."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from ibkr_collector.values import BLOCKED_ECLIENT_METHODS, classify_error, finite_or_none

logger = logging.getLogger("ibkr_collector.client")


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
        self.ticks: dict[int, dict[str, Any]] = {}
        self.market_data_types: dict[int, int] = {}
        self.raw_ticks: list[dict[str, Any]] = []
        self._req_seq = 1000

    def next_req_id(self) -> int:
        with self._lock:
            self._req_seq += 1
            return self._req_seq

    def wait_event(self, req_id: int, store: dict[int, threading.Event]) -> threading.Event:
        with self._lock:
            event = store.get(req_id)
            if event is None:
                event = threading.Event()
                store[req_id] = event
            return event

    def nextValidId(self, orderId: int) -> None:
        self.next_valid_id = int(orderId)
        self.handshake.set()

    def connectionClosed(self) -> None:
        self.disconnected.set()
        self.handshake.clear()

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
        with self._lock:
            self.errors.append(rec)
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
            self.contract_details.setdefault(reqId, []).append(payload)

    def bondContractDetails(self, reqId: int, contractDetails) -> None:
        payload = _contract_details_payload(contractDetails, bond=True)
        with self._lock:
            self.contract_details.setdefault(reqId, []).append(payload)

    def contractDetailsEnd(self, reqId: int) -> None:
        self.wait_event(reqId, self.contract_details_done).set()

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
            self.symbol_samples[reqId] = rows
        self.wait_event(reqId, self.symbol_samples_done).set()

    def marketDataType(self, reqId: int, marketDataType: int) -> None:
        with self._lock:
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
            self.raw_ticks.append({"req_id": reqId, "kind": "price", "tick_type": tick_id, "price": finite_or_none(price)})
        field = PRICE_TICKS.get(tick_id) if isinstance(tick_id, int) else None
        if not field:
            return
        with self._lock:
            bucket = self.ticks.setdefault(reqId, {})
            bucket[field] = finite_or_none(price)
            if isinstance(tick_id, int) and tick_id >= 66:
                bucket["delayed_ticks"] = True

    def tickSize(self, reqId: int, tickType: int, size) -> None:
        from ibkr_collector.values import SIZE_TICKS

        field = SIZE_TICKS.get(int(tickType))
        if not field:
            return
        value = finite_or_none(size)
        with self._lock:
            bucket = self.ticks.setdefault(reqId, {})
            bucket[field] = value

    def tickString(self, reqId: int, tickType: int, value: str) -> None:
        from ibkr_collector.values import TIMESTAMP_TICKS

        field = TIMESTAMP_TICKS.get(int(tickType))
        if not field:
            return
        with self._lock:
            bucket = self.ticks.setdefault(reqId, {})
            bucket[field] = value or None

    def tickSnapshotEnd(self, reqId: int) -> None:
        with self._lock:
            bucket = self.ticks.setdefault(reqId, {})
            bucket["snapshot_end"] = True


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
