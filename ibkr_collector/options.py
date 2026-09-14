"""Bounded, read-only IBKR option-chain collector. No orders. No production timer.

Uses reqSecDefOptParams for strike/expiry discovery, then a capped number of
reqMktData subscriptions. Default market-data type is delayed (3). Recurring
host storage is not enabled from this module.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Mapping

from ibkr_collector import DEFAULT_OPTIONS_CLIENT_ID, DEFAULT_TWS_HOST, DEFAULT_TWS_PORT
from ibkr_collector.diagnostic import probe_socket
from ibkr_collector.readonly_client import ReadOnlyTwsClient
from ibkr_collector.values import market_data_type_label, utcnow

logger = logging.getLogger("ibkr_collector.options")

DEFAULT_SYMBOLS = ("SPY", "QQQ", "IWM")
GENERIC_TICKS = "100,101,106"
DELAYED_GENERIC_TICKS = "101,106"
MAX_EXPIRATIONS = 6
ATM_STRIKES_EACH_SIDE = 5
MAX_CONCURRENT_LINES = 20
QUOTE_WAIT_SEC = 12.0
FILL_FIELDS = (
    "bid",
    "ask",
    "last",
    "volume",
    "open_interest",
    "implied_volatility",
    "delta",
    "gamma",
    "theta",
    "vega",
)
HANDSHAKE_WAIT_SEC = 20.0
CONTRACT_WAIT_SEC = 10.0
PARAMS_WAIT_SEC = 15.0
MARKET_DATA_TYPE_DELAYED = 3
EXPORT_SCOPE = "INTERNAL_ONLY"
SOURCE_ID = "IBKR_OPTIONS"
PRIMARY_EXCHANGE = {"SPY": "ARCA", "QQQ": "NASDAQ", "IWM": "ARCA"}


@dataclass(frozen=True)
class OptionSpec:
    symbol: str
    expiration: str
    strike: float
    right: str
    exchange: str = "SMART"
    trading_class: str = ""
    multiplier: str = "100"
    currency: str = "USD"


@dataclass
class BoundedChainResult:
    symbol: str
    collected_at: str
    underlying: dict[str, Any]
    param_exchanges: list[dict[str, Any]]
    selected: list[dict[str, Any]]
    quotes: list[dict[str, Any]]
    entitlement_errors: list[dict[str, Any]]
    line_limit_errors: list[dict[str, Any]]
    market_data_types: list[str]
    quality: dict[str, Any] = field(default_factory=dict)


def parse_expiration(raw: str) -> date | None:
    text = str(raw or "").strip()
    if len(text) >= 8 and text[:8].isdigit():
        try:
            return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
        except ValueError:
            return None
    if len(text) >= 10:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def select_param_row(rows: list[Mapping[str, Any]], symbol: str) -> Mapping[str, Any] | None:
    if not rows:
        return None
    symbol = symbol.upper()
    smart = [row for row in rows if str(row.get("exchange") or "").upper() == "SMART"]
    pool = smart or list(rows)
    matching = [row for row in pool if str(row.get("trading_class") or "").upper() == symbol]
    ranked = matching or pool
    return max(ranked, key=lambda row: len(row.get("expirations") or []) + len(row.get("strikes") or []))


def select_bounded_specs(
    *,
    symbol: str,
    param: Mapping[str, Any],
    spot: float | None,
    as_of: date,
    max_expirations: int = MAX_EXPIRATIONS,
    atm_strikes_each_side: int = ATM_STRIKES_EACH_SIDE,
) -> list[OptionSpec]:
    expirations: list[tuple[date, str]] = []
    for raw in param.get("expirations") or []:
        parsed = parse_expiration(str(raw))
        if parsed is None or parsed < as_of:
            continue
        expirations.append((parsed, str(raw)))
    expirations.sort(key=lambda item: item[0])
    chosen_exp = expirations[: max(1, int(max_expirations))]
    strikes = sorted(float(item) for item in (param.get("strikes") or []))
    if not chosen_exp or not strikes:
        return []
    if spot is None:
        mid = len(strikes) // 2
        lo = max(0, mid - atm_strikes_each_side)
        hi = min(len(strikes), mid + atm_strikes_each_side + 1)
        chosen_strikes = strikes[lo:hi]
    else:
        ordered = sorted(strikes, key=lambda strike: abs(strike - spot))
        keep = max(1, atm_strikes_each_side * 2 + 1)
        chosen_strikes = sorted(ordered[:keep])
    trading_class = str(param.get("trading_class") or symbol)
    multiplier = str(param.get("multiplier") or "100")
    specs: list[OptionSpec] = []
    for _exp_date, exp_text in chosen_exp:
        for strike in chosen_strikes:
            for right in ("C", "P"):
                specs.append(
                    OptionSpec(
                        symbol=symbol.upper(),
                        expiration=exp_text,
                        strike=strike,
                        right=right,
                        trading_class=trading_class,
                        multiplier=multiplier,
                    )
                )
    return specs


def _stock_contract(symbol: str):
    from ibapi.contract import Contract

    contract = Contract()
    contract.symbol = symbol
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"
    primary = PRIMARY_EXCHANGE.get(symbol.upper())
    if primary:
        contract.primaryExchange = primary
    return contract


def _option_contract(spec: OptionSpec):
    from ibapi.contract import Contract

    contract = Contract()
    contract.symbol = spec.symbol
    contract.secType = "OPT"
    contract.exchange = spec.exchange
    contract.currency = spec.currency
    contract.lastTradeDateOrContractMonth = spec.expiration
    contract.strike = float(spec.strike)
    contract.right = spec.right
    contract.multiplier = spec.multiplier
    if spec.trading_class:
        contract.tradingClass = spec.trading_class
    return contract


def _option_contract_from_details(spec: OptionSpec, payload: Mapping[str, Any]):
    contract = _option_contract(spec)
    con_id = payload.get("con_id")
    if con_id is not None:
        contract.conId = int(con_id)
    local = payload.get("local_symbol")
    if local:
        contract.localSymbol = str(local)
    exchange = payload.get("exchange")
    if exchange:
        contract.exchange = str(exchange)
    return contract


def _spot_from_ticks(ticks: Mapping[str, Any]) -> float | None:
    for key in ("last", "bid", "ask", "close"):
        if ticks.get(key) is not None:
            return float(ticks[key])
    return None


def _generic_ticks_for(md_code: int | None) -> str:
    if md_code == 1:
        return GENERIC_TICKS
    return DELAYED_GENERIC_TICKS


def _errors_since(client: Any, origin: int) -> list[dict[str, Any]]:
    return list(client.errors[origin:])


def _quote_from_ticks(spec: OptionSpec, ticks: Mapping[str, Any], md_code: int | None) -> dict[str, Any]:
    oi = ticks.get("open_interest")
    if oi is None:
        if spec.right == "C":
            oi = ticks.get("call_open_interest")
        elif spec.right == "P":
            oi = ticks.get("put_open_interest")
    volume = ticks.get("volume")
    if volume is None:
        volume = ticks.get("call_volume") if spec.right == "C" else ticks.get("put_volume")
    md_label = market_data_type_label(md_code)
    if md_code is None and ticks.get("delayed_ticks"):
        md_label = "DELAYED"
    local_symbol = str(ticks.get("local_symbol") or "")
    return {
        "symbol": spec.symbol,
        "expiration": spec.expiration,
        "strike": spec.strike,
        "right": spec.right,
        "trading_class": spec.trading_class,
        "multiplier": spec.multiplier,
        "bid": ticks.get("bid"),
        "ask": ticks.get("ask"),
        "last": ticks.get("last"),
        "close": ticks.get("close"),
        "bid_size": ticks.get("bid_size"),
        "ask_size": ticks.get("ask_size"),
        "volume": volume,
        "open_interest": oi,
        "implied_volatility": ticks.get("implied_volatility"),
        "delta": ticks.get("delta"),
        "gamma": ticks.get("gamma"),
        "theta": ticks.get("theta"),
        "vega": ticks.get("vega"),
        "theoretical_price": ticks.get("theoretical_price"),
        "underlying_price": ticks.get("underlying_price"),
        "last_timestamp": ticks.get("last_timestamp"),
        "market_data_type": md_label,
        "market_data_type_code": md_code,
        "entitlement_error": bool(ticks.get("entitlement_error")),
        "local_symbol": local_symbol,
        "export_scope": EXPORT_SCOPE,
        "source_id": SOURCE_ID,
    }


def collect_bounded_chain(
    client: Any,
    symbol: str,
    *,
    as_of: date | None = None,
    spot: float | None = None,
    max_expirations: int = MAX_EXPIRATIONS,
    atm_strikes_each_side: int = ATM_STRIKES_EACH_SIDE,
    max_concurrent: int = MAX_CONCURRENT_LINES,
    quote_wait_sec: float = QUOTE_WAIT_SEC,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] | None = None,
) -> BoundedChainResult:
    """Drive an already-connected read-only TWS client. Caller owns connect/disconnect."""
    now = (clock or utcnow)()
    session_day = as_of or now.date()
    symbol = symbol.upper()
    error_origin = len(client.errors)
    client.reqMarketDataType(MARKET_DATA_TYPE_DELAYED)

    stock_req = client.next_req_id()
    client.wait_event(stock_req, client.contract_details_done)
    client.reqContractDetails(stock_req, _stock_contract(symbol))
    client.contract_details_done[stock_req].wait(CONTRACT_WAIT_SEC)
    details = list(client.contract_details.get(stock_req) or [])
    con_id = None
    if details:
        con_id = details[0].get("con_id")
    if not con_id:
        new_errors = _errors_since(client, error_origin)
        return BoundedChainResult(
            symbol=symbol,
            collected_at=now.isoformat(),
            underlying={"details": details[:1]},
            param_exchanges=[],
            selected=[],
            quotes=[],
            entitlement_errors=[e for e in new_errors if e.get("kind") == "entitlement"],
            line_limit_errors=[e for e in new_errors if e.get("kind") == "line_limit"],
            market_data_types=[],
            quality={"status": "NO_UNDERLYING_CONID"},
        )

    under_req = client.next_req_id()
    under_contract = _stock_contract(symbol)
    under_contract.conId = int(con_id)
    client.reqMktData(under_req, under_contract, "", False, False, [])
    deadline = time.monotonic() + quote_wait_sec
    while time.monotonic() < deadline:
        ticks = dict(client.ticks.get(under_req) or {})
        if any(ticks.get(key) is not None for key in ("bid", "ask", "last", "close")):
            break
        sleeper(0.2)
    under_ticks = dict(client.ticks.get(under_req) or {})
    try:
        client.cancelMktData(under_req)
    except Exception:
        logger.debug("cancel underlying mkt data failed", exc_info=True)
    if spot is None:
        spot = _spot_from_ticks(under_ticks)
    under_md = client.market_data_types.get(under_req, under_ticks.get("market_data_type"))
    option_generic = _generic_ticks_for(under_md if isinstance(under_md, int) else None)

    param_req = client.next_req_id()
    client.wait_event(param_req, client.opt_params_done)
    client.reqSecDefOptParams(param_req, symbol, "", "STK", int(con_id))
    client.opt_params_done[param_req].wait(PARAMS_WAIT_SEC)
    param_rows = list(client.opt_params.get(param_req) or [])
    param = select_param_row(param_rows, symbol)
    specs = select_bounded_specs(
        symbol=symbol,
        param=param or {},
        spot=spot,
        as_of=session_day,
        max_expirations=max_expirations,
        atm_strikes_each_side=atm_strikes_each_side,
    )

    qualified: list[tuple[OptionSpec, dict[str, Any]]] = []
    max_lines = max(1, int(max_concurrent))
    for offset in range(0, len(specs), max_lines):
        batch = specs[offset : offset + max_lines]
        detail_map: dict[int, OptionSpec] = {}
        for spec in batch:
            det_req = client.next_req_id()
            detail_map[det_req] = spec
            client.wait_event(det_req, client.contract_details_done)
            client.reqContractDetails(det_req, _option_contract(spec))
        sleeper(min(CONTRACT_WAIT_SEC, quote_wait_sec))
        for det_req, spec in detail_map.items():
            client.contract_details_done.get(det_req, threading.Event()).wait(0.01)
            rows = list(client.contract_details.get(det_req) or [])
            if rows and rows[0].get("con_id"):
                qualified.append((spec, dict(rows[0])))

    quotes: list[dict[str, Any]] = []
    md_labels: list[str] = []
    observed_tick_keys: set[str] = set()
    for offset in range(0, len(qualified), max_lines):
        batch = qualified[offset : offset + max_lines]
        req_map: dict[int, tuple[OptionSpec, dict[str, Any]]] = {}
        for spec, payload in batch:
            req_id = client.next_req_id()
            req_map[req_id] = (spec, payload)
            client.reqMktData(req_id, _option_contract_from_details(spec, payload), option_generic, False, False, [])
        sleeper(quote_wait_sec)
        for req_id, (spec, payload) in req_map.items():
            ticks = dict(client.ticks.get(req_id) or {})
            observed_tick_keys.update(key for key in ticks if key != "last_callback_at")
            if not ticks.get("local_symbol"):
                ticks["local_symbol"] = payload.get("local_symbol")
            md_code = client.market_data_types.get(req_id, ticks.get("market_data_type"))
            quote = _quote_from_ticks(spec, ticks, md_code if isinstance(md_code, int) else None)
            quotes.append(quote)
            md_labels.append(quote["market_data_type"])
            try:
                client.cancelMktData(req_id)
            except Exception:
                logger.debug("cancel option mkt data failed req=%s", req_id, exc_info=True)
        sleeper(0.25)

    new_errors = _errors_since(client, error_origin)
    entitlement = [e for e in new_errors if e.get("kind") == "entitlement"]
    line_limit = [e for e in new_errors if e.get("kind") == "line_limit"]
    invalid = [e for e in new_errors if e.get("kind") == "invalid_contract"]
    quoted = [row for row in quotes if any(row.get(k) is not None for k in ("bid", "ask", "last", "implied_volatility"))]
    error_code_counts: dict[str, int] = {}
    for err in new_errors:
        if err.get("kind") == "info":
            continue
        key = str(err.get("error_code"))
        error_code_counts[key] = error_code_counts.get(key, 0) + 1
    field_fills = {name: sum(1 for row in quotes if row.get(name) is not None) for name in FILL_FIELDS}
    selected_param = None
    if param:
        selected_param = {
            "exchange": param.get("exchange"),
            "trading_class": param.get("trading_class"),
            "expirations": len(param.get("expirations") or []),
            "strikes": len(param.get("strikes") or []),
        }
    return BoundedChainResult(
        symbol=symbol,
        collected_at=now.isoformat(),
        underlying={
            "con_id": con_id,
            "spot": spot,
            "ticks": {k: under_ticks.get(k) for k in ("bid", "ask", "last", "close", "option_implied_volatility")},
            "market_data_type": market_data_type_label(under_md if isinstance(under_md, int) else None),
        },
        param_exchanges=[{"exchange": r.get("exchange"), "trading_class": r.get("trading_class"), "expirations": len(r.get("expirations") or []), "strikes": len(r.get("strikes") or [])} for r in param_rows],
        selected=[spec.__dict__ for spec in specs],
        quotes=quotes,
        entitlement_errors=entitlement,
        line_limit_errors=line_limit,
        market_data_types=sorted({label for label in md_labels if label}),
        quality={
            "status": "OK" if quoted else "EMPTY_QUOTES",
            "specs": len(specs),
            "qualified": len(qualified),
            "quoted": len(quoted),
            "max_concurrent": max_lines,
            "generic_ticks": option_generic,
            "export_scope": EXPORT_SCOPE,
            "field_fills": field_fills,
            "error_code_counts": error_code_counts,
            "invalid_contract_count": len(invalid),
            "selected_param": selected_param,
            "tick_field_keys": sorted(observed_tick_keys),
        },
    )


def connect_options_session(
    *,
    host: str = DEFAULT_TWS_HOST,
    port: int = DEFAULT_TWS_PORT,
    client_id: int = DEFAULT_OPTIONS_CLIENT_ID,
    timeout_sec: float = HANDSHAKE_WAIT_SEC,
) -> tuple[Any | None, str | None]:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return None, "refused_non_localhost_tws"
    probe = probe_socket(host, port)
    if not probe["ok"]:
        return None, "tws_socket_unavailable"
    client = ReadOnlyTwsClient()
    reader = threading.Thread(target=client.run, name="ibkr-options-reader", daemon=True)
    client.connect(host, port, client_id)
    reader.start()
    if not client.handshake.wait(timeout_sec):
        try:
            client.disconnect()
        except Exception:
            pass
        return None, "handshake_timeout"
    return client, None


def result_summary(result: BoundedChainResult) -> dict[str, Any]:
    """Counts and entitlement only. Does not dump the full chain."""
    return {
        "symbol": result.symbol,
        "collected_at": result.collected_at,
        "underlying_spot": (result.underlying or {}).get("spot"),
        "underlying_market_data_type": (result.underlying or {}).get("market_data_type"),
        "param_exchange_count": len(result.param_exchanges),
        "selected_param": (result.quality or {}).get("selected_param"),
        "selected_count": len(result.selected),
        "qualified_count": (result.quality or {}).get("qualified"),
        "quoted_count": result.quality.get("quoted"),
        "market_data_types": result.market_data_types,
        "entitlement_error_count": len(result.entitlement_errors),
        "line_limit_error_count": len(result.line_limit_errors),
        "field_fills": (result.quality or {}).get("field_fills") or {},
        "error_code_counts": (result.quality or {}).get("error_code_counts") or {},
        "export_scope": EXPORT_SCOPE,
        "source_id": SOURCE_ID,
        "quality": result.quality,
    }
