"""Versioned futures curve, FX, and calendar-spread helpers. Known-answer fixtures first."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

CURVE_METHOD = "FUTURES_FRONT_DEFERRED_V1"
FX_METHOD = "FX_MID_V1"
SPREAD_METHOD = "CALENDAR_SPREAD_PRICE_V1"


def _d(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def select_front_deferred(contracts: Sequence[Mapping[str, Any]], *, as_of: date) -> dict[str, Any]:
    dated = []
    for item in contracts:
        expiry = item.get("expiry")
        if isinstance(expiry, str):
            try:
                expiry = date.fromisoformat(expiry[:10])
            except ValueError:
                continue
        if not isinstance(expiry, date):
            continue
        dated.append({**dict(item), "expiry": expiry})
    dated.sort(key=lambda row: row["expiry"])
    live = [row for row in dated if row["expiry"] >= as_of]
    if len(live) < 1:
        return {"status": "UNAVAILABLE", "reason": "no_dated_contract_on_or_after_as_of", "method_id": CURVE_METHOD, "front": None, "deferred": None}
    front = live[0]
    deferred = live[1] if len(live) > 1 else None
    return {
        "status": "OK" if deferred else "INCOMPLETE",
        "reason": None if deferred else "no_deferred_expiry",
        "method_id": CURVE_METHOD,
        "front": {"con_id": front.get("con_id"), "local_symbol": front.get("local_symbol"), "expiry": front["expiry"].isoformat()},
        "deferred": None if deferred is None else {"con_id": deferred.get("con_id"), "local_symbol": deferred.get("local_symbol"), "expiry": deferred["expiry"].isoformat()},
        "note": "A continuous chart is a derived display with roll metadata, never a replacement contract identity.",
    }


def calendar_spread(*, front_price: Any, deferred_price: Any, front_expiry: date | None, deferred_expiry: date | None, as_of: date | None) -> dict[str, Any]:
    front = _d(front_price)
    deferred = _d(deferred_price)
    if front is None or deferred is None:
        return {"status": "INCOMPLETE", "reason": "missing_leg_price", "method_id": SPREAD_METHOD}
    if front <= 0 or deferred <= 0:
        return {
            "status": "UNAVAILABLE",
            "reason": "non_positive_price",
            "method_id": SPREAD_METHOD,
            "absolute_spread": deferred - front,
            "ratio": None,
            "note": "Zero/negative prices need unavailable ratio status; the absolute spread is stored separately.",
        }
    days_front = (front_expiry - as_of).days if front_expiry and as_of else None
    days_deferred = (deferred_expiry - as_of).days if deferred_expiry and as_of else None
    return {
        "status": "OK",
        "method_id": SPREAD_METHOD,
        "units": "price_points_deferred_minus_front",
        "absolute_spread": deferred - front,
        "price_ratio": deferred / front,
        "days_to_front_expiry": days_front,
        "days_to_deferred_expiry": days_deferred,
        "note": "Treasury futures price spreads are not yield-curve slopes or DV01-neutral relative value.",
    }


def fx_mid(*, pair: str, bid: Any, ask: Any, last: Any = None, base: str, quote: str) -> dict[str, Any]:
    bid_n = _d(bid)
    ask_n = _d(ask)
    last_n = _d(last)
    if bid_n is None or ask_n is None or bid_n <= 0 or ask_n <= 0:
        return {"status": "INCOMPLETE", "reason": "need_positive_bid_and_ask", "method_id": FX_METHOD, "pair": pair, "base": base, "quote": quote, "last_used": False}
    mid = (bid_n + ask_n) / Decimal(2)
    return {
        "status": "OK",
        "method_id": FX_METHOD,
        "pair": pair,
        "base": base,
        "quote": quote,
        "mid": mid,
        "last": last_n,
        "last_used": False,
        "note": "FX is not a consolidated equity tape; last is stored only when defined and is not substituted for mid.",
    }
