"""Strict validation for IBKR ingest payloads. Extra fields and order/account keys fail closed."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from market_intelligence.calendars import CAL_NYSE, last_completed_session

ALLOWED_QUOTE_KEYS = frozenset(
    {
        "record_id",
        "instrument_id",
        "symbol",
        "sec_type",
        "con_id",
        "currency",
        "exchange",
        "primary_exchange",
        "display_name",
        "quote_ts",
        "source_ts",
        "retrieved_at",
        "bid",
        "ask",
        "last_price",
        "mid",
        "bid_size",
        "ask_size",
        "last_size",
        "close_price",
        "market_data_type",
        "delay_status",
        "quote_status",
        "provenance",
        "last_callback_at",
    }
)
ALLOWED_HEARTBEAT_KEYS = frozenset(
    {
        "collector_id",
        "reported_state",
        "last_socket_ok_at",
        "last_api_handshake_at",
        "last_tws_connect_at",
        "last_quote_at",
        "last_callback_at",
        "market_data_type",
        "client_id",
        "watchlist",
        "details",
        "last_delivery_error_redacted",
    }
)
FORBIDDEN_SUBSTRINGS = ("order", "position", "execution", "account", "password", "balance", "pnl", "sql")
ALLOWED_SEC_TYPES = frozenset({"STK", "ETF", "BOND", "FUND", "IND"})
ALLOWED_MD = frozenset({"LIVE", "DELAYED", "FROZEN", "DELAYED_FROZEN", "UNAVAILABLE"})
ALLOWED_STATES = frozenset(
    {
        "WAITING_FOR_TWS",
        "SOCKET_OPEN_HANDSHAKE_PENDING",
        "API_AUTHENTICATED",
        "CONNECTED",
        "DISCONNECTED",
        "ENTITLEMENT_ERROR",
        "DELIVERY_FAILURE",
        "COLLECTOR_OFFLINE",
    }
)
MAX_BATCH = 100
MAX_BAR_BATCH = 400
MAX_WATCHLIST = 32
ALLOWED_EQUITY_BAR_KEYS = frozenset(
    {
        "symbol",
        "con_id",
        "sec_type",
        "exchange",
        "primary_exchange",
        "currency",
        "bar_date",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
        "adjustment_basis",
        "what_to_show",
        "provider_symbol",
    }
)
ALLOWED_EQUITY_BATCH_KEYS = frozenset(
    {
        "collector_id",
        "provider",
        "source_id",
        "what_to_show",
        "adjustment_basis",
        "bars",
        "coverage",
        "batch_id",
        "chunk_index",
        "chunk_count",
        "request_mode",
        "finalize",
    }
)
ALLOWED_ADJUSTMENT_BASIS = frozenset({"IBKR_ADJUSTED_LAST"})
ALLOWED_WHAT_TO_SHOW = frozenset({"ADJUSTED_LAST"})
ALLOWED_REQUEST_MODES = frozenset({"smoke", "full", "backfill", "incremental"})
IBKR_PROVIDER = "IBKR"
REQUIRED_IBKR_SOURCE_ID = "EQUITY_EOD"
ALLOWED_FINALIZE_KEYS = frozenset(
    {
        "collector_id",
        "batch_id",
        "coverage",
        "request_mode",
        "provider",
        "source_id",
        "what_to_show",
        "adjustment_basis",
    }
)


class PayloadError(ValueError):
    pass


def _forbid_keys(keys: list[str]) -> None:
    for key in keys:
        lowered = key.lower()
        if any(part in lowered for part in FORBIDDEN_SUBSTRINGS):
            raise PayloadError("forbidden field {0!r}".format(key))


def _iso(value: Any, *, optional: bool = False) -> str | None:
    if value is None:
        if optional:
            return None
        raise PayloadError("timestamp required")
    text = str(value)
    datetime.fromisoformat(text.replace("Z", "+00:00"))
    return text


def _number(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        raise PayloadError("boolean is not a quote field")
    if isinstance(value, (int, float)):
        return value
    raise PayloadError("numeric fields must be numbers or null")


def validate_quote(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PayloadError("quote must be an object")
    _forbid_keys(list(raw))
    extra = set(raw) - ALLOWED_QUOTE_KEYS
    if extra:
        raise PayloadError("unknown quote fields: {0}".format(", ".join(sorted(extra))))
    record_id = str(raw.get("record_id") or "")
    if not record_id or len(record_id) > 128:
        raise PayloadError("record_id required")
    symbol = str(raw.get("symbol") or "")
    if not symbol or len(symbol) > 32:
        raise PayloadError("symbol required")
    sec_type = str(raw.get("sec_type") or "STK").upper()
    if sec_type not in ALLOWED_SEC_TYPES:
        raise PayloadError("unsupported sec_type")
    mdt = str(raw.get("market_data_type") or "UNAVAILABLE")
    if mdt not in ALLOWED_MD:
        raise PayloadError("unsupported market_data_type")
    con_id = raw.get("con_id")
    if con_id is not None:
        if not isinstance(con_id, int) or con_id < 0:
            raise PayloadError("con_id must be a non-negative integer or null")
    provenance = raw.get("provenance") or {}
    if not isinstance(provenance, dict):
        raise PayloadError("provenance must be an object")
    _forbid_keys(list(provenance))
    return {
        "record_id": record_id,
        "instrument_id": raw.get("instrument_id"),
        "symbol": symbol,
        "sec_type": sec_type,
        "con_id": con_id,
        "currency": raw.get("currency") or None,
        "exchange": raw.get("exchange") or None,
        "primary_exchange": raw.get("primary_exchange") or None,
        "display_name": raw.get("display_name") or None,
        "quote_ts": _iso(raw.get("quote_ts")),
        "source_ts": _iso(raw.get("source_ts"), optional=True),
        "retrieved_at": _iso(raw.get("retrieved_at"), optional=True),
        "bid": _number(raw.get("bid")),
        "ask": _number(raw.get("ask")),
        "last_price": _number(raw.get("last_price")),
        "mid": _number(raw.get("mid")),
        "bid_size": _number(raw.get("bid_size")),
        "ask_size": _number(raw.get("ask_size")),
        "last_size": _number(raw.get("last_size")),
        "close_price": _number(raw.get("close_price")),
        "market_data_type": mdt,
        "delay_status": raw.get("delay_status") or mdt,
        "quote_status": raw.get("quote_status") or "OK",
        "provenance": provenance,
        "last_callback_at": _iso(raw.get("last_callback_at"), optional=True),
    }


def validate_quote_batch(body: Any) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(body, dict):
        raise PayloadError("body must be an object")
    extra = set(body) - {"collector_id", "quotes"}
    if extra:
        raise PayloadError("unknown batch fields: {0}".format(", ".join(sorted(extra))))
    collector_id = str(body.get("collector_id") or "")
    if not collector_id or len(collector_id) > 64:
        raise PayloadError("collector_id required")
    quotes = body.get("quotes")
    if not isinstance(quotes, list) or not quotes:
        raise PayloadError("quotes must be a non-empty list")
    if len(quotes) > MAX_BATCH:
        raise PayloadError("batch exceeds {0} quotes".format(MAX_BATCH))
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in quotes:
        try:
            accepted.append(validate_quote(row))
        except PayloadError as exc:
            record_id = ""
            if isinstance(row, dict):
                record_id = str(row.get("record_id") or "")
            rejected.append({"record_id": record_id, "outcome": "rejected", "reason": str(exc)[:200]})
    if not accepted and rejected:
        # Entire batch is invalid records; still a 200 at the HTTP layer once the caller
        # uses the tolerant path. Keep a structured result.
        return collector_id, [], rejected
    if not accepted:
        raise PayloadError("quotes must be a non-empty list")
    return collector_id, accepted, rejected


def validate_heartbeat(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise PayloadError("body must be an object")
    _forbid_keys(list(body))
    extra = set(body) - ALLOWED_HEARTBEAT_KEYS
    if extra:
        raise PayloadError("unknown heartbeat fields: {0}".format(", ".join(sorted(extra))))
    collector_id = str(body.get("collector_id") or "")
    if not collector_id or len(collector_id) > 64:
        raise PayloadError("collector_id required")
    state = str(body.get("reported_state") or "")
    if state not in ALLOWED_STATES:
        raise PayloadError("unsupported reported_state")
    watchlist = body.get("watchlist") or []
    if not isinstance(watchlist, list) or len(watchlist) > MAX_WATCHLIST:
        raise PayloadError("watchlist must be a short list of symbols")
    clean_watch = []
    for item in watchlist:
        if isinstance(item, str):
            clean_watch.append(item[:32])
        elif isinstance(item, dict) and item.get("symbol"):
            clean_watch.append(str(item["symbol"])[:32])
        else:
            raise PayloadError("watchlist entries must be symbols")
    details = body.get("details") or {}
    if not isinstance(details, dict):
        raise PayloadError("details must be an object")
    _forbid_keys(list(details))
    client_id = body.get("client_id")
    if client_id is not None and (not isinstance(client_id, int) or client_id <= 0):
        raise PayloadError("client_id must be a positive integer")
    return {
        "collector_id": collector_id,
        "reported_state": state,
        "last_socket_ok_at": _iso(body.get("last_socket_ok_at"), optional=True),
        "last_api_handshake_at": _iso(body.get("last_api_handshake_at"), optional=True),
        "last_tws_connect_at": _iso(body.get("last_tws_connect_at"), optional=True),
        "last_quote_at": _iso(body.get("last_quote_at"), optional=True),
        "last_callback_at": _iso(body.get("last_callback_at"), optional=True),
        "market_data_type": body.get("market_data_type"),
        "client_id": client_id,
        "watchlist": clean_watch,
        "details": details,
        "last_delivery_error_redacted": (str(body["last_delivery_error_redacted"])[:400] if body.get("last_delivery_error_redacted") else None),
    }


def _positive_price(value: Any, field: str) -> float:
    number = _number(value)
    if number is None:
        raise PayloadError("{0} required".format(field))
    if not isinstance(number, (int, float)) or isinstance(number, bool) or not math.isfinite(float(number)):
        raise PayloadError("{0} must be finite".format(field))
    if float(number) <= 0:
        raise PayloadError("{0} must be > 0".format(field))
    return float(number)


def _optional_nonnegative(value: Any, field: str) -> float | None:
    if value is None:
        return None
    number = _number(value)
    if number is None:
        return None
    if not math.isfinite(float(number)) or float(number) < 0:
        raise PayloadError("{0} must be finite and >= 0".format(field))
    return float(number)


def _parse_bar_date(raw: Any) -> date:
    text = str(raw or "").strip()
    if not text:
        raise PayloadError("bar_date must be YYYY-MM-DD")
    try:
        if len(text) >= 10 and text[4] == "-":
            return date.fromisoformat(text[:10])
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise PayloadError("bar_date must be YYYY-MM-DD") from exc


def validate_equity_bar(raw: Any, *, completed_session: date | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PayloadError("bar must be an object")
    _forbid_keys(list(raw))
    extra = set(raw) - ALLOWED_EQUITY_BAR_KEYS
    if extra:
        raise PayloadError("unknown bar fields: {0}".format(", ".join(sorted(extra))))
    symbol = str(raw.get("symbol") or "")
    if not symbol or len(symbol) > 32:
        raise PayloadError("symbol required")
    sec_type = str(raw.get("sec_type") or "STK").upper()
    if sec_type not in ALLOWED_SEC_TYPES:
        raise PayloadError("unsupported sec_type")
    if "adjustment_basis" not in raw:
        raise PayloadError("adjustment_basis required")
    basis = str(raw.get("adjustment_basis") or "")
    if basis not in ALLOWED_ADJUSTMENT_BASIS:
        raise PayloadError("unsupported adjustment_basis")
    if "what_to_show" not in raw:
        raise PayloadError("what_to_show required")
    what_to_show = str(raw.get("what_to_show") or "")
    if what_to_show not in ALLOWED_WHAT_TO_SHOW:
        raise PayloadError("unsupported what_to_show")
    con_id = raw.get("con_id")
    if not isinstance(con_id, int) or isinstance(con_id, bool) or con_id <= 0:
        raise PayloadError("con_id must be a positive integer")
    bar_date = _parse_bar_date(raw.get("bar_date"))
    completed = completed_session or last_completed_session(datetime.now().astimezone(), CAL_NYSE)
    if bar_date > completed:
        raise PayloadError("future or incomplete session bar")
    close = _positive_price(raw.get("close"), "close")
    if "adj_close" not in raw:
        raise PayloadError("adj_close required")
    adj = _positive_price(raw.get("adj_close"), "adj_close")
    if adj != close:
        raise PayloadError("adj_close must equal ADJUSTED_LAST close")
    open_px = _number(raw.get("open"))
    high_px = _number(raw.get("high"))
    low_px = _number(raw.get("low"))
    for label, value in (("open", open_px), ("high", high_px), ("low", low_px)):
        if value is None:
            continue
        if not math.isfinite(float(value)) or float(value) <= 0:
            raise PayloadError("{0} must be finite and > 0".format(label))
    if None not in (open_px, high_px, low_px):
        open_f, high_f, low_f = float(open_px), float(high_px), float(low_px)
        if low_f > high_f:
            raise PayloadError("low cannot exceed high")
        if min(open_f, close) < low_f or max(open_f, close) > high_f:
            raise PayloadError("OHLC sanity failed")
    currency = str(raw.get("currency") or "USD").upper()
    if currency != "USD":
        raise PayloadError("currency must be USD")
    exchange = str(raw.get("exchange") or "").strip()
    primary = str(raw.get("primary_exchange") or "").strip()
    if not exchange or not primary:
        raise PayloadError("exchange and primary_exchange required")
    return {
        "symbol": symbol,
        "con_id": con_id,
        "sec_type": sec_type,
        "exchange": exchange,
        "primary_exchange": primary,
        "currency": currency,
        "bar_date": bar_date.isoformat(),
        "open": float(open_px) if open_px is not None else None,
        "high": float(high_px) if high_px is not None else None,
        "low": float(low_px) if low_px is not None else None,
        "close": close,
        "adj_close": adj,
        "volume": _optional_nonnegative(raw.get("volume"), "volume"),
        "adjustment_basis": basis,
        "what_to_show": what_to_show,
        "provider_symbol": raw.get("provider_symbol") or symbol,
    }


@dataclass
class EquityBarIngestRequest:
    collector_id: str
    records: list[dict[str, Any]]
    coverage: dict[str, Any]
    batch_id: str
    chunk_index: int
    chunk_count: int
    request_mode: str
    provider: str
    source_id: str
    what_to_show: str
    adjustment_basis: str
    auto_finalize: bool
    duplicate_keys: list[str] = field(default_factory=list)


def _require_ibkr_provenance(body: dict[str, Any]) -> tuple[str, str, str, str]:
    provider = str(body.get("provider") or "")
    source_id = str(body.get("source_id") or "")
    what_to_show = str(body.get("what_to_show") or "")
    basis = str(body.get("adjustment_basis") or "")
    if provider != IBKR_PROVIDER:
        raise PayloadError("provider must be IBKR")
    if source_id != REQUIRED_IBKR_SOURCE_ID:
        raise PayloadError("source_id must be EQUITY_EOD")
    if what_to_show not in ALLOWED_WHAT_TO_SHOW:
        raise PayloadError("what_to_show must be ADJUSTED_LAST")
    if basis not in ALLOWED_ADJUSTMENT_BASIS:
        raise PayloadError("adjustment_basis must be IBKR_ADJUSTED_LAST")
    return provider, source_id, what_to_show, basis


def _validate_coverage(coverage: Any) -> dict[str, Any]:
    if coverage is None:
        return {}
    if not isinstance(coverage, dict):
        raise PayloadError("coverage must be an object")
    _forbid_keys(list(coverage))
    return coverage


def _batch_id(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(text))
    except ValueError as exc:
        raise PayloadError("batch_id must be a UUID") from exc


def validate_equity_bar_batch(body: Any, *, completed_session: date | None = None) -> EquityBarIngestRequest:
    if not isinstance(body, dict):
        raise PayloadError("body must be an object")
    _forbid_keys(list(body))
    extra = set(body) - ALLOWED_EQUITY_BATCH_KEYS
    if extra:
        raise PayloadError("unknown batch fields: {0}".format(", ".join(sorted(extra))))
    collector_id = str(body.get("collector_id") or "")
    if not collector_id or len(collector_id) > 64:
        raise PayloadError("collector_id required")
    provider, source_id, what_to_show, basis = _require_ibkr_provenance(body)
    bars = body.get("bars")
    if not isinstance(bars, list) or not bars:
        raise PayloadError("bars must be a non-empty list")
    if len(bars) > MAX_BAR_BATCH:
        raise PayloadError("batch exceeds {0} bars".format(MAX_BAR_BATCH))
    records = [validate_equity_bar(row, completed_session=completed_session) for row in bars]
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in records:
        key = (row["symbol"], row["bar_date"])
        prior = seen.get(key)
        if prior is not None and prior["adj_close"] != row["adj_close"]:
            raise PayloadError("duplicate contradictory row for {0} {1}".format(row["symbol"], row["bar_date"]))
        if row["what_to_show"] != what_to_show:
            raise PayloadError("per-row what_to_show must match batch")
        if row["adjustment_basis"] != basis:
            raise PayloadError("per-row adjustment_basis must match batch")
        seen[key] = row
    request_mode = str(body.get("request_mode") or "incremental")
    if request_mode not in ALLOWED_REQUEST_MODES:
        raise PayloadError("unsupported request_mode")
    supplied_batch = bool(str(body.get("batch_id") or "").strip())
    chunk_count_raw = body.get("chunk_count")
    chunk_index_raw = body.get("chunk_index")
    if chunk_count_raw is None and chunk_index_raw is None and not supplied_batch:
        chunk_index, chunk_count, auto_finalize = 1, 1, True
    else:
        if not isinstance(chunk_index_raw, int) or isinstance(chunk_index_raw, bool) or chunk_index_raw < 1:
            raise PayloadError("chunk_index must be a positive integer")
        if not isinstance(chunk_count_raw, int) or isinstance(chunk_count_raw, bool) or chunk_count_raw < 1:
            raise PayloadError("chunk_count must be a positive integer")
        if chunk_index_raw > chunk_count_raw:
            raise PayloadError("chunk_index cannot exceed chunk_count")
        chunk_index, chunk_count = chunk_index_raw, chunk_count_raw
        if "finalize" in body:
            auto_finalize = bool(body.get("finalize"))
        else:
            auto_finalize = chunk_count == 1
    return EquityBarIngestRequest(
        collector_id=collector_id,
        records=records,
        coverage=_validate_coverage(body.get("coverage")),
        batch_id=_batch_id(body.get("batch_id")),
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        request_mode=request_mode,
        provider=provider,
        source_id=source_id,
        what_to_show=what_to_show,
        adjustment_basis=basis,
        auto_finalize=auto_finalize,
    )


def validate_equity_finalize(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise PayloadError("body must be an object")
    _forbid_keys(list(body))
    extra = set(body) - ALLOWED_FINALIZE_KEYS
    if extra:
        raise PayloadError("unknown finalize fields: {0}".format(", ".join(sorted(extra))))
    collector_id = str(body.get("collector_id") or "")
    if not collector_id or len(collector_id) > 64:
        raise PayloadError("collector_id required")
    batch_id = str(body.get("batch_id") or "").strip()
    try:
        uuid.UUID(batch_id)
    except ValueError as exc:
        raise PayloadError("batch_id must be a UUID") from exc
    provider, source_id, what_to_show, basis = _require_ibkr_provenance(body)
    request_mode = str(body.get("request_mode") or "incremental")
    if request_mode not in ALLOWED_REQUEST_MODES:
        raise PayloadError("unsupported request_mode")
    return {
        "collector_id": collector_id,
        "batch_id": str(uuid.UUID(batch_id)),
        "coverage": _validate_coverage(body.get("coverage")),
        "request_mode": request_mode,
        "provider": provider,
        "source_id": source_id,
        "what_to_show": what_to_show,
        "adjustment_basis": basis,
    }
