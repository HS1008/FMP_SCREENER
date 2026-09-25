"""Versioned option exposure magnitudes. Never labelled as measured dealer GEX."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

GEX_METHOD_ID = "OPTION_EXPOSURE_MAGNITUDE"
GEX_METHOD_VERSION = "exposure_magnitude_v1"
SIGN_PROXY_METHOD = "CALL_POS_PUT_NEG_PROXY_V1"


def _d(value: Any) -> Decimal | None:
    if value is None:
        return None
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    return number if number.is_finite() else None


def exposure_magnitude(*, gamma: Any, open_interest: Any, multiplier: Any, spot: Any) -> dict[str, Any]:
    """abs(gamma) * OI * multiplier * spot^2 * 0.01 for a 1% spot move.

    Zero OI is a valid zero. Missing OI is not treated as zero. Volume is never substituted.
    """
    g = _d(gamma)
    oi = _d(open_interest)
    mult = _d(multiplier)
    px = _d(spot)
    if any(item is None for item in (g, oi, mult, px)):
        return {"value": None, "status": "INCOMPLETE", "reason": "missing_gamma_oi_multiplier_or_spot", "method_id": GEX_METHOD_ID, "method_version": GEX_METHOD_VERSION}
    if px < 0:
        return {"value": None, "status": "UNAVAILABLE", "reason": "negative_spot", "method_id": GEX_METHOD_ID, "method_version": GEX_METHOD_VERSION}
    value = abs(g) * oi * mult * (px ** 2) * Decimal("0.01")
    return {
        "value": value,
        "status": "OK",
        "units": "delta_notional_per_1pct",
        "method_id": GEX_METHOD_ID,
        "method_version": GEX_METHOD_VERSION,
        "label": "option_exposure_magnitude",
        "dealer_gex": False,
    }


def signed_proxy(*, call_magnitude: Any, put_magnitude: Any) -> dict[str, Any]:
    """Assumption-based call-positive / put-negative proxy. Not measured dealer positioning."""
    call = _d(call_magnitude)
    put = _d(put_magnitude)
    if call is None or put is None:
        return {"value": None, "status": "INCOMPLETE", "reason": "missing_call_or_put_magnitude", "method_id": SIGN_PROXY_METHOD}
    return {
        "value": call - put,
        "status": "OK",
        "method_id": SIGN_PROXY_METHOD,
        "label": "call_positive_put_negative_proxy",
        "dealer_gex": False,
        "assumption": "calls contribute +magnitude, puts contribute -magnitude; ownership unknown",
    }
