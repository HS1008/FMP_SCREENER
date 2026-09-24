"""Deterministic Cboe volatility metrics. No network and no database.

Constructions are not interchangeable:

- VIX index term structure uses Cboe volatility-index levels (VIX9D, VIX, VIX3M, VIX6M, VIX1Y).
  It is not a VIX futures curve and is never labeled contango or backwardation.
- 25-delta SPX skew is put IV minus call IV on the SPX root only, at the expiry closest to 30 calendar days.
- IV minus realized vol is either SPX_IV30_MINUS_SPX_RV20 (LiveVol ``iv30``, the documented 30-day average
  implied volatility, not ATM and not VIX) or VIX_MINUS_SPX_RV20 when ``iv30`` is absent.
- RV20 is the sample standard deviation of 20 daily SPX close-to-close log returns, times sqrt(252), in vol points.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from market_intelligence.cboe_client import NY

METHOD_VIX = "cboe_vix_index_v1"
METHOD_TERM = "vix_index_term_structure_v1"
METHOD_SKEW = "spx_25d_skew_v1"
METHOD_IV30_SPREAD = "spx_iv30_minus_spx_rv20_v1"
METHOD_VIX_SPREAD = "vix_minus_spx_rv20_v1"
METHOD_RV = "spx_rv20_sample_std_v1"

TARGET_SKEW_DAYS = 30
TARGET_ABS_DELTA = 0.25
RV_WINDOW = 20
TERM_ORDER = (
    ("VIX9D", "9D", "VIX_9D"),
    ("VIX", "1M", "VIX_1M"),
    ("VIX3M", "3M", "VIX_3M"),
    ("VIX6M", "6M", "VIX_6M"),
    ("VIX1Y", "1Y", "VIX_1Y"),
)
INDEX_SYMBOLS = ("VIX9D", "VIX", "VIX3M", "VIX6M", "VIX1Y", "SPX")
SKEW_UNDERLYING = "SPX"
SKEW_ROOT = "SPX"


def to_vol_points(value: Any) -> float | None:
    """LiveVol index ``iv30`` is in vol points (22.08). Option ``iv`` / ``mid_iv`` are decimals (0.27)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    if number == 0:
        return 0.0
    if abs(number) <= 3:
        return number * 100.0
    return number


def quote_level(row: Mapping[str, Any]) -> float | None:
    for key in ("underlying_last_trade_price", "underlying_close", "underlying_mid", "implied_underlying_mid"):
        level = _positive_number(row.get(key))
        if level is not None:
            return level
    return None


def index_snapshots(rows: Any, *, quote_date: date) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError("underlying quotes payload must be a list")
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        level = quote_level(row)
        prev = _positive_number(row.get("underlying_prev_day_close"))
        iv30 = to_vol_points(row.get("iv30"))
        if iv30 is not None and iv30 <= 0:
            iv30 = None
        out[symbol] = {
            "symbol": symbol,
            "level": level,
            "prev_close": prev,
            "iv30": iv30,
            "observation_ts": combine_timestamp(quote_date, row.get("timestamp")),
            "provider_timestamp": row.get("timestamp"),
            "as_of": quote_date.isoformat(),
        }
    return out


def term_structure(snapshots: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    points = []
    for symbol, tenor, metric_id in TERM_ORDER:
        snap = snapshots.get(symbol) or {}
        level = snap.get("level")
        if level is None:
            continue
        points.append(
            {
                "symbol": symbol,
                "tenor": tenor,
                "metric_id": metric_id,
                "level": level,
                "prev_close": snap.get("prev_close"),
                "observation_ts": snap.get("observation_ts"),
            }
        )
    slope = None
    state = None
    if len(points) >= 2:
        slope = float(points[-1]["level"]) - float(points[0]["level"])
        if slope > 0.5:
            state = "upward_sloping"
        elif slope < -0.5:
            state = "downward_sloping"
        else:
            state = "flat"
    return {
        "construction": "vix_index_tenor",
        "label": "VIX index term structure",
        "points": points,
        "front_to_back_slope": slope,
        "curve_state": state,
        "method": METHOD_TERM,
    }


def choose_expiry(expiries: list[date], as_of: date, *, target_days: int = TARGET_SKEW_DAYS) -> date | None:
    future = [item for item in expiries if item > as_of]
    if not future:
        return None
    return min(future, key=lambda item: (abs((item - as_of).days - target_days), -(item - as_of).days))


def provider_iv(contract: Mapping[str, Any]) -> float | None:
    raw = contract.get("mid_iv")
    if raw is None:
        raw = contract.get("iv")
    points = to_vol_points(raw)
    if points is None or points <= 0:
        return None
    return points


def quote_is_usable(contract: Mapping[str, Any]) -> bool:
    """Positive provider IV, or a two-sided NBBO when IV is absent. Missing IV is not zero."""
    if provider_iv(contract) is not None:
        return True
    bid, ask = contract.get("option_bid"), contract.get("option_ask")
    if bid is None or ask is None:
        return False
    try:
        bid_n, ask_n = float(bid), float(ask)
    except (TypeError, ValueError):
        return False
    return bid_n > 0 and ask_n > bid_n


def select_25_delta(contracts: list[Mapping[str, Any]], *, side: str) -> dict[str, Any] | None:
    side = side.upper()
    eligible: list[Mapping[str, Any]] = []
    for contract in contracts:
        if str(contract.get("root") or SKEW_ROOT).upper() != SKEW_ROOT:
            continue
        if str(contract.get("option_type") or "").upper() != side:
            continue
        delta = contract.get("delta")
        if delta is None or isinstance(delta, bool):
            continue
        try:
            delta_n = float(delta)
        except (TypeError, ValueError):
            continue
        if math.isnan(delta_n) or math.isinf(delta_n):
            continue
        if side == "P" and delta_n >= 0:
            continue
        if side == "C" and delta_n <= 0:
            continue
        if provider_iv(contract) is None or not quote_is_usable(contract):
            continue
        eligible.append(contract)
    if not eligible:
        return None

    def sort_key(contract: Mapping[str, Any]) -> tuple:
        distance = abs(abs(float(contract["delta"])) - TARGET_ABS_DELTA)
        oi = contract.get("open_interest")
        try:
            oi_n = float(oi) if oi is not None else -1.0
        except (TypeError, ValueError):
            oi_n = -1.0
        return (distance, -oi_n, str(contract.get("option") or ""))

    chosen = min(eligible, key=sort_key)
    return dict(chosen)


def skew_from_chain(payload: Any, *, as_of: date) -> dict[str, Any]:
    contracts, envelope = _option_contracts(payload)
    expiries = []
    for contract in contracts:
        parsed = _as_date(contract.get("expiry"))
        if parsed is not None and str(contract.get("root") or "").upper() == SKEW_ROOT:
            expiries.append(parsed)
    expiry = choose_expiry(sorted(set(expiries)), as_of)
    base = {
        "method": METHOD_SKEW,
        "underlying": SKEW_UNDERLYING,
        "root": SKEW_ROOT,
        "target_calendar_days": TARGET_SKEW_DAYS,
        "target_abs_delta": TARGET_ABS_DELTA,
        "status": "INCOMPLETE",
        "put_iv": None,
        "call_iv": None,
        "skew": None,
        "expiry": None,
        "dte": None,
        "reason": None,
    }
    if expiry is None:
        base["reason"] = "no_spx_expiry_near_30d"
        return base
    selected_rows = [row for row in contracts if _as_date(row.get("expiry")) == expiry and str(row.get("root") or "").upper() == SKEW_ROOT]
    put = select_25_delta(selected_rows, side="P")
    call = select_25_delta(selected_rows, side="C")
    base["expiry"] = expiry.isoformat()
    base["dte"] = (expiry - as_of).days
    base["underlying_level"] = quote_level(envelope) if isinstance(envelope, Mapping) else None
    base["observation_ts"] = combine_timestamp(as_of, envelope.get("timestamp") if isinstance(envelope, Mapping) else None)
    if put is None or call is None:
        base["reason"] = "missing_put" if put is None else "missing_call"
        if put is None and call is None:
            base["reason"] = "missing_put_and_call"
        base["put"] = _contract_evidence(put) if put else None
        base["call"] = _contract_evidence(call) if call else None
        return base
    put_iv = provider_iv(put)
    call_iv = provider_iv(call)
    if put_iv is None or call_iv is None:
        base["reason"] = "missing_iv"
        return base
    base.update(
        {
            "status": "OK",
            "reason": None,
            "put_iv": put_iv,
            "call_iv": call_iv,
            "skew": put_iv - call_iv,
            "put": _contract_evidence(put),
            "call": _contract_evidence(call),
        }
    )
    return base


def realized_vol_20(closes: list[tuple[date, float]]) -> dict[str, Any]:
    ordered = sorted((item for item in closes if item[1] is not None and item[1] > 0), key=lambda item: item[0])
    detail = {"method": METHOD_RV, "window": RV_WINDOW, "annualization": 252, "std": "sample", "underlying": "SPX", "return": "close_to_close_log"}
    if len(ordered) < RV_WINDOW + 1:
        return {"value": None, "status": "INCOMPLETE", "reason": "fewer_than_21_spx_closes", **detail, "observations": len(ordered)}
    window = ordered[-(RV_WINDOW + 1) :]
    returns = []
    for prev, cur in zip(window, window[1:]):
        if prev[1] <= 0 or cur[1] <= 0:
            return {"value": None, "status": "INCOMPLETE", "reason": "non_positive_close", **detail}
        returns.append(math.log(cur[1] / prev[1]))
    if len(returns) != RV_WINDOW:
        return {"value": None, "status": "INCOMPLETE", "reason": "incomplete_return_window", **detail}
    value = statistics.stdev(returns) * math.sqrt(252) * 100.0
    return {
        "value": value,
        "status": "OK",
        "reason": None,
        "window_start": window[0][0].isoformat(),
        "window_end": window[-1][0].isoformat(),
        **detail,
    }


def iv_rv_spread(*, iv30: float | None, vix: float | None, rv: float | None) -> dict[str, Any]:
    if rv is None:
        return {"metric_id": None, "value": None, "iv": None, "rv": None, "status": "INCOMPLETE", "reason": "missing_rv", "method": None}
    if iv30 is not None and iv30 > 0:
        return {
            "metric_id": "SPX_IV30_MINUS_SPX_RV20",
            "iv_metric": "SPX_IV30",
            "value": iv30 - rv,
            "iv": iv30,
            "rv": rv,
            "status": "OK",
            "reason": None,
            "method": METHOD_IV30_SPREAD,
            "construction": "livevol_iv30_minus_spx_rv20",
        }
    if vix is not None and vix > 0:
        return {
            "metric_id": "VIX_MINUS_SPX_RV20",
            "iv_metric": "VIX_SPOT",
            "value": vix - rv,
            "iv": vix,
            "rv": rv,
            "status": "OK",
            "reason": None,
            "method": METHOD_VIX_SPREAD,
            "construction": "vix_minus_spx_rv20",
        }
    return {"metric_id": None, "value": None, "iv": None, "rv": rv, "status": "INCOMPLETE", "reason": "missing_iv", "method": None}


def combine_timestamp(quote_date: date, raw: Any) -> str | None:
    if raw is None or raw == "":
        return None
    text = str(raw).strip()
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%H:%M:%S.%f", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    if parsed.year == 1900:
        parsed = datetime.combine(quote_date, parsed.time())
    ny = parsed.replace(tzinfo=NY)
    return ny.astimezone(timezone.utc).isoformat()


def strike_band(spot: float | None) -> tuple[float | None, float | None]:
    if spot is None or spot <= 0:
        return None, None
    return round(spot * 0.75, 2), round(spot * 1.15, 2)


def expiry_window(as_of: date) -> tuple[date, date]:
    from datetime import timedelta

    return as_of + timedelta(days=16), as_of + timedelta(days=50)


def _option_contracts(payload: Any) -> tuple[list[dict[str, Any]], Mapping[str, Any]]:
    if isinstance(payload, dict):
        options = payload.get("options")
        if not isinstance(options, list):
            raise ValueError("option quote payload missing options list")
        return [row for row in options if isinstance(row, dict)], payload
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)], {}
    raise ValueError("option quote payload was not an object or list")


def _contract_evidence(contract: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if contract is None:
        return None
    return {
        "osi": contract.get("option"),
        "root": contract.get("root"),
        "expiry": contract.get("expiry"),
        "strike": contract.get("strike"),
        "option_type": contract.get("option_type"),
        "delta": contract.get("delta"),
        "iv": provider_iv(contract),
        "mid_iv": contract.get("mid_iv"),
        "bid": contract.get("option_bid"),
        "ask": contract.get("option_ask"),
        "open_interest": contract.get("open_interest"),
    }


def _positive_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number) or number <= 0:
        return None
    return number


def _as_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None

