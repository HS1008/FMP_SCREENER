"""Normalize IBKR numeric sentinels and label market-data types."""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from typing import Any

UNSET_DOUBLE = float(sys.float_info.max)
UNSET_INTEGER = 2**31 - 1

MARKET_DATA_TYPE_LABELS = {
    1: "LIVE",
    2: "FROZEN",
    3: "DELAYED",
    4: "DELAYED_FROZEN",
}

# Default (live) ticks and their delayed counterparts used for quote snapshots.
PRICE_TICKS = {
    1: "bid",
    2: "ask",
    4: "last",
    9: "close",
    66: "bid",
    67: "ask",
    68: "last",
    75: "close",
}
SIZE_TICKS = {
    0: "bid_size",
    3: "ask_size",
    5: "last_size",
    69: "bid_size",
    70: "ask_size",
    71: "last_size",
}
TIMESTAMP_TICKS = {45: "last_timestamp", 88: "last_timestamp"}
DELAYED_TICK_IDS = frozenset({66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 88})

INFORMATIONAL_ERROR_CODES = frozenset(
    {
        2103,
        2104,
        2105,
        2106,
        2107,
        2108,
        2119,
        2158,
        2174,
        2100,
        2188,  # up-to-the-second historical bars need a streaming subscription; EOD bars may still follow
    }
)
ENTITLEMENT_ERROR_CODES = frozenset({354, 10089, 10167, 10168, 10197, 10225, 2186})
CONNECTIVITY_ERROR_CODES = frozenset({502, 504, 1100, 1300, 2110, 326, 507, 1101, 1102})
PACING_ERROR_CODES = frozenset({420})
HISTORICAL_ERROR_CODES = frozenset({162, 165, 366})
INVALID_CONTRACT_ERROR_CODES = frozenset({200, 321})

BLOCKED_ECLIENT_METHODS = (
    "placeOrder",
    "placeOrderProtoBuf",
    "cancelOrder",
    "reqGlobalCancel",
    "reqOpenOrders",
    "reqAllOpenOrders",
    "reqAutoOpenOrders",
    "reqCompletedOrders",
    "reqExecutions",
    "reqExecutionsProtoBuf",
    "reqAccountUpdates",
    "reqAccountUpdatesMulti",
    "reqAccountSummary",
    "reqPositions",
    "reqPositionsMulti",
    "reqPnL",
    "reqPnLSingle",
    "reqFamilyCodes",
    "exercisePositions",
    "reqHistoricalTicks",
    "reqHeadTimeStamp",
    "reqHistogramData",
    "reqRealTimeBars",
    "reqTickByTickData",
    "reqScannerSubscription",
    "reqFundamentalData",
    "reqNewsBulletins",
    "reqMktDepth",
    "reqMktDepthExchanges",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_unset_number(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value in {UNSET_INTEGER, -1} and value != 0
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return True
        if value == UNSET_DOUBLE or value == -1.0:
            return True
    return False


def finite_or_none(value: Any) -> float | None:
    if is_unset_number(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def market_data_type_label(code: int | None) -> str:
    if code is None:
        return "UNAVAILABLE"
    return MARKET_DATA_TYPE_LABELS.get(int(code), "UNAVAILABLE")


def classify_error(code: int) -> str:
    if code in INFORMATIONAL_ERROR_CODES:
        return "info"
    if code in ENTITLEMENT_ERROR_CODES:
        return "entitlement"
    if code in CONNECTIVITY_ERROR_CODES:
        return "connectivity"
    if code in PACING_ERROR_CODES:
        return "pacing"
    if code in HISTORICAL_ERROR_CODES:
        return "historical"
    if code in INVALID_CONTRACT_ERROR_CODES:
        return "invalid_contract"
    return "error"
