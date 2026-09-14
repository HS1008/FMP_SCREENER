"""Official ibapi client restricted to read-only market-data and contract lookup."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from ibkr_collector.values import BLOCKED_ECLIENT_METHODS, classify_error, finite_or_none

logger = logging.getLogger("ibkr_collector.client")

MAX_ERRORS = 200
MAX_RAW_TICKS = 500


class OrderMethodBlocked(RuntimeError):
    """Raised if any order, account, position, or execution API is invoked."""


OPTION_COMPUTATION_RANK = (13, 83, 12, 82, 11, 81, 10, 80)


def _preferred_option_computation(ranked: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    for tick_id in OPTION_COMPUTATION_RANK:
        row = ranked.get(tick_id)
        if row and (row.get("implied_vol") is not None or row.get("delta") is not None):
            return row
    return None


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
        self.historical_bars: dict[int, list[dict[str, Any]]] = {}
        self.historical_done: dict[int, threading.Event] = {}
        self.historical_pending: set[int] = set()
        self.opt_params: dict[int, list[dict[str, Any]]] = {}
        self.opt_params_done: dict[int, threading.Event] = {}
        self._req_seq = 1000
        self.managed_accounts: list[str] = []

    def mark_historical(self, req_id: int) -> threading.Event:
        with self._lock:
            self.historical_pending.add(req_id)
            self.historical_bars.setdefault(req_id, [])
        return self.wait_event(req_id, self.historical_done)

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
        parts = [part.strip() for part in str(accountsList or "").split(",") if part.strip()]
        self.managed_accounts = parts

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
            if len(self.errors) > MAX_ERRORS:
                self.errors = self.errors[-MAX_ERRORS:]
            if kind == "entitlement" and reqId is not None:
                bucket = self.ticks.setdefault(reqId, {})
                bucket["entitlement_error"] = True
                bucket["entitlement_code"] = int(errorCode)
            if reqId in self.historical_pending and kind in {"entitlement", "error", "pacing", "historical", "invalid_contract"}:
                self.wait_event(reqId, self.historical_done).set()
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

    def _count_tick(self, reqId: int, kind: str, tick_id: int) -> None:
        bucket = self.ticks.setdefault(reqId, {})
        counts = bucket.setdefault("tick_type_counts", {})
        key = "{0}:{1}".format(kind, tick_id)
        counts[key] = int(counts.get(key) or 0) + 1

    def _note_callback(self, reqId: int) -> None:
        from ibkr_collector.values import utcnow

        bucket = self.ticks.setdefault(reqId, {})
        bucket["last_callback_at"] = utcnow().isoformat()

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
            if len(self.raw_ticks) > MAX_RAW_TICKS:
                self.raw_ticks = self.raw_ticks[-MAX_RAW_TICKS:]
            if isinstance(tick_id, int):
                self._count_tick(reqId, "price", tick_id)
            self._note_callback(reqId)
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
        value = finite_or_none(size)
        with self._lock:
            self._count_tick(reqId, "size", int(tickType))
            if not field:
                self._note_callback(reqId)
                return
            bucket = self.ticks.setdefault(reqId, {})
            bucket[field] = value
            self._note_callback(reqId)

    def tickString(self, reqId: int, tickType: int, value: str) -> None:
        from ibkr_collector.values import TIMESTAMP_TICKS

        field = TIMESTAMP_TICKS.get(int(tickType))
        with self._lock:
            self._count_tick(reqId, "string", int(tickType))
            if not field:
                self._note_callback(reqId)
                return
            bucket = self.ticks.setdefault(reqId, {})
            bucket[field] = value or None
            self._note_callback(reqId)

    def tickGeneric(self, reqId: int, tickType: int, value: float) -> None:
        from ibkr_collector.values import GENERIC_TICKS

        field = GENERIC_TICKS.get(int(tickType))
        with self._lock:
            self._count_tick(reqId, "generic", int(tickType))
            if not field:
                self._note_callback(reqId)
                return
            bucket = self.ticks.setdefault(reqId, {})
            bucket[field] = finite_or_none(value)
            self._note_callback(reqId)

    def tickOptionComputation(
        self,
        reqId: int,
        tickType: int,
        tickAttrib: float | int | None = None,
        impliedVol: float | None = None,
        delta: float | None = None,
        optPrice: float | None = None,
        pvDividend: float | None = None,
        gamma: float | None = None,
        vega: float | None = None,
        theta: float | None = None,
        undPrice: float | None = None,
    ) -> None:
        """Accept current (tickAttrib) and older 10-argument ibapi signatures."""
        tick_id = int(tickType)
        # Older ibapi: (reqId, tickType, impliedVol, delta, optPrice, pvDividend, gamma, vega, theta, undPrice)
        if undPrice is None and theta is not None and vega is not None:
            undPrice = theta
            theta = vega
            vega = gamma
            gamma = pvDividend
            pvDividend = optPrice
            optPrice = delta
            delta = impliedVol
            impliedVol = tickAttrib
        greeks = {
            "implied_vol": finite_or_none(impliedVol),
            "delta": finite_or_none(delta),
            "gamma": finite_or_none(gamma),
            "vega": finite_or_none(vega),
            "theta": finite_or_none(theta),
            "opt_price": finite_or_none(optPrice),
            "und_price": finite_or_none(undPrice),
            "tick_type": tick_id,
        }
        with self._lock:
            bucket = self.ticks.setdefault(reqId, {})
            self._count_tick(reqId, "option_computation", tick_id)
            ranked = bucket.setdefault("option_computations", {})
            ranked[tick_id] = greeks
            preferred = _preferred_option_computation(ranked)
            if preferred:
                bucket["implied_volatility"] = preferred.get("implied_vol")
                bucket["delta"] = preferred.get("delta")
                bucket["gamma"] = preferred.get("gamma")
                bucket["vega"] = preferred.get("vega")
                bucket["theta"] = preferred.get("theta")
                bucket["theoretical_price"] = preferred.get("opt_price")
                if preferred.get("und_price") is not None:
                    bucket["underlying_price"] = preferred.get("und_price")
            if tick_id >= 66:
                bucket["delayed_ticks"] = True
            self._note_callback(reqId)

    def securityDefinitionOptionParameter(
        self,
        reqId: int,
        exchange: str,
        underlyingConId: int,
        tradingClass: str,
        multiplier: str,
        expirations,
        strikes,
    ) -> None:
        row = {
            "exchange": exchange,
            "underlying_con_id": int(underlyingConId) if underlyingConId is not None else None,
            "trading_class": tradingClass,
            "multiplier": multiplier,
            "expirations": sorted(str(item) for item in (expirations or [])),
            "strikes": sorted(float(item) for item in (strikes or [])),
        }
        with self._lock:
            self.opt_params.setdefault(reqId, []).append(row)

    def securityDefinitionOptionParameterEnd(self, reqId: int) -> None:
        self.wait_event(reqId, self.opt_params_done).set()

    def tickSnapshotEnd(self, reqId: int) -> None:
        with self._lock:
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
            self.historical_bars.setdefault(reqId, []).append(payload)

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
        with self._lock:
            self.historical_pending.discard(reqId)
        self.wait_event(reqId, self.historical_done).set()


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
