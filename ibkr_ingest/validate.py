"""Strict validation for IBKR ingest payloads. Extra fields and order/account keys fail closed."""

from __future__ import annotations

from datetime import datetime
from typing import Any

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
    }
)
MAX_BATCH = 100
MAX_WATCHLIST = 32


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
    }


def validate_quote_batch(body: Any) -> tuple[str, list[dict[str, Any]]]:
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
    return collector_id, [validate_quote(row) for row in quotes]


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
        "market_data_type": body.get("market_data_type"),
        "client_id": client_id,
        "watchlist": clean_watch,
        "details": details,
        "last_delivery_error_redacted": (str(body["last_delivery_error_redacted"])[:400] if body.get("last_delivery_error_redacted") else None),
    }
