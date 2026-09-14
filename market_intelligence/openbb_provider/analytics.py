"""Pure options / GEX analytics over one immutable normalized chain."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from market_intelligence.openbb_provider.config import (
    ANALYTICS_VERSION,
    ATM_RULE,
    GEX_METHOD_ID,
    GEX_METHOD_VERSION,
    GEX_SIGN_CONVENTION,
    GAMMA_CONVENTION,
    IV_TIME_BASIS,
)
from market_intelligence.openbb_provider.normalize import NormalizedChain, NormalizedContract, year_fraction

TARGET_30D = 30
TARGET_30D_YEARS = Decimal("30") / Decimal("365.25")
DELTA_25 = Decimal("0.25")


def _ok_iv(contract: NormalizedContract) -> bool:
    return (
        contract.iv_decimal is not None
        and contract.iv_decimal > 0
        and contract.quote_quality in {"OK", "ONE_SIDED"}
        and not contract.flags.get("invalid_iv")
        and contract.strike is not None
        and contract.strike > 0
    )


def _log_moneyness(strike: Decimal, spot: Decimal) -> Decimal:
    return Decimal(str(math.log(float(strike / spot))))


def _expiry_groups(chain: NormalizedChain) -> dict[date, list[NormalizedContract]]:
    groups: dict[date, list[NormalizedContract]] = {}
    for contract in chain.contracts:
        if contract.expiration is None or contract.is_adjusted:
            continue
        groups.setdefault(contract.expiration, []).append(contract)
    return groups


def atm_iv_for_expiry(contracts: Iterable[NormalizedContract], spot: Decimal) -> dict[str, Any]:
    valid = [c for c in contracts if _ok_iv(c) and c.option_type in {"call", "put"}]
    if not valid or spot <= 0:
        return {"iv_decimal": None, "status": "UNAVAILABLE", "reason": "no_valid_spot_atm_iv", "rule": ATM_RULE}
    by_strike: dict[Decimal, dict[str, NormalizedContract]] = {}
    for contract in valid:
        by_strike.setdefault(contract.strike, {})[contract.option_type] = contract
    best = None
    best_key = None
    for strike, sides in by_strike.items():
        distance = abs(_log_moneyness(strike, spot))
        both = "call" in sides and "put" in sides
        key = (distance, 0 if both else 1, strike)
        if best_key is None or key < best_key:
            best_key = key
            best = (strike, sides)
    strike, sides = best
    if "call" in sides and "put" in sides:
        iv = (sides["call"].iv_decimal + sides["put"].iv_decimal) / Decimal("2")
        sided = "both"
    elif "call" in sides:
        iv = sides["call"].iv_decimal
        sided = "call_only"
    else:
        iv = sides["put"].iv_decimal
        sided = "put_only"
    return {
        "iv_decimal": iv,
        "iv_percent": iv * Decimal("100"),
        "strike": strike,
        "sided": sided,
        "status": "OK",
        "label": "spot_atm",
        "rule": ATM_RULE,
        "reason": None,
    }


def interpolate_30d_atm(points: list[tuple[Decimal, Decimal]]) -> dict[str, Any]:
    """``points`` are (T_years, iv_decimal) with T > 0. No extrapolation."""
    usable = [(t, iv) for t, iv in points if t > 0 and iv is not None and iv > 0]
    usable.sort(key=lambda item: item[0])
    if len(usable) < 2:
        return {"iv_decimal": None, "status": "UNAVAILABLE", "reason": "need_two_bracketing_expiries", "label": "atm_iv_30d_calendar"}
    below = [item for item in usable if item[0] <= TARGET_30D_YEARS]
    above = [item for item in usable if item[0] >= TARGET_30D_YEARS]
    if not below or not above:
        return {"iv_decimal": None, "status": "UNAVAILABLE", "reason": "no_bracketing_30d", "label": "atm_iv_30d_calendar"}
    t1, s1 = below[-1]
    t2, s2 = above[0]
    if t1 == t2:
        iv = s1
    else:
        w = (TARGET_30D_YEARS - t1) / (t2 - t1)
        total = (s1 * s1 * t1) + w * ((s2 * s2 * t2) - (s1 * s1 * t1))
        if total <= 0:
            return {"iv_decimal": None, "status": "UNAVAILABLE", "reason": "non_positive_total_variance", "label": "atm_iv_30d_calendar"}
        iv = (total / TARGET_30D_YEARS).sqrt()
    return {
        "iv_decimal": iv,
        "iv_percent": iv * Decimal("100"),
        "status": "OK",
        "label": "atm_iv_30d_calendar",
        "time_basis": IV_TIME_BASIS,
        "not_vix": True,
        "t1": str(t1),
        "t2": str(t2),
        "reason": None,
    }


def _delta_iv_points(contracts: Iterable[NormalizedContract], option_type: str) -> list[tuple[Decimal, Decimal]]:
    points = []
    for contract in contracts:
        if contract.option_type != option_type or not _ok_iv(contract) or contract.delta is None:
            continue
        points.append((contract.delta, contract.iv_decimal))
    return points


def _interp_delta(points: list[tuple[Decimal, Decimal]], target: Decimal) -> Decimal | None:
    if not points:
        return None
    ordered = sorted(points, key=lambda item: item[0])
    if any(delta == target for delta, _ in ordered):
        return next(iv for delta, iv in ordered if delta == target)
    below = [item for item in ordered if item[0] <= target]
    above = [item for item in ordered if item[0] >= target]
    if not below or not above:
        return None
    d1, i1 = below[-1]
    d2, i2 = above[0]
    if d1 == d2:
        return i1
    w = (target - d1) / (d2 - d1)
    return i1 + w * (i2 - i1)


def skew_25d(contracts: Iterable[NormalizedContract]) -> dict[str, Any]:
    put_iv = _interp_delta(_delta_iv_points(contracts, "put"), -DELTA_25)
    call_iv = _interp_delta(_delta_iv_points(contracts, "call"), DELTA_25)
    if put_iv is None or call_iv is None:
        return {"value_pp": None, "status": "UNAVAILABLE", "reason": "no_bracketing_25d", "units": "volatility_percentage_points"}
    return {
        "value_pp": (put_iv - call_iv) * Decimal("100"),
        "put_iv_decimal": put_iv,
        "call_iv_decimal": call_iv,
        "status": "OK",
        "units": "volatility_percentage_points",
        "reason": None,
        "convention": "provider_delta_decimal",
    }


def put_call_ratio(contracts: Iterable[NormalizedContract], field: str) -> dict[str, Any]:
    puts = 0
    calls = 0
    used = 0
    for contract in contracts:
        if contract.is_adjusted:
            continue
        value = getattr(contract, field)
        if value is None:
            continue
        used += 1
        if contract.option_type == "put":
            puts += value
        elif contract.option_type == "call":
            calls += value
    if used == 0:
        return {"value": None, "status": "UNAVAILABLE", "reason": "no_{0}".format(field), "puts": puts, "calls": calls}
    if calls == 0:
        return {"value": None, "status": "UNAVAILABLE", "reason": "zero_call_{0}".format(field), "puts": puts, "calls": calls}
    return {"value": Decimal(puts) / Decimal(calls), "status": "OK", "puts": puts, "calls": calls, "reason": None}


def zerodte_share(contracts: Iterable[NormalizedContract], session_date: date, field: str) -> dict[str, Any]:
    num = 0
    den = 0
    for contract in contracts:
        value = getattr(contract, field)
        if value is None or contract.expiration is None:
            continue
        den += value
        if contract.expiration == session_date:
            num += value
    if den == 0:
        return {"value": None, "status": "UNAVAILABLE", "reason": "zero_{0}_denominator".format(field)}
    return {"value": Decimal(num) / Decimal(den), "status": "OK", "numerator": num, "denominator": den, "reason": None}


def expected_move(spot: Decimal | None, iv: Decimal | None, days: int) -> dict[str, Any]:
    if spot is None or spot <= 0 or iv is None or iv <= 0 or days <= 0:
        return {"value": None, "status": "UNAVAILABLE", "reason": "insufficient_horizon_or_iv", "label": "iv_expected_move_proxy"}
    t = year_fraction(days)
    if t <= 0:
        return {"value": None, "status": "UNAVAILABLE", "reason": "non_positive_t", "label": "iv_expected_move_proxy"}
    value = spot * iv * t.sqrt()
    return {
        "value": value,
        "status": "OK",
        "units": "underlying_points",
        "horizon_days": days,
        "time_basis": IV_TIME_BASIS,
        "label": "iv_expected_move_proxy",
        "not_a_probability_interval": True,
        "reason": None,
    }


def unsigned_gex(*, gamma: Decimal, open_interest: int, multiplier: Decimal, spot: Decimal) -> Decimal:
    return gamma * Decimal(open_interest) * multiplier * (spot ** 2) * Decimal("0.01")


def gex_proxy(chain: NormalizedChain) -> dict[str, Any]:
    """OI-derived gamma-exposure proxy. Not observed dealer inventory.

    Unsigned term is ``gamma * OI * multiplier * spot^2 * 0.01``.
    Sign convention ``CALL_PLUS_PUT_MINUS_V1``: calls +, puts −.
    Missing gamma/OI/multiplier are excluded, never filled with zeroes.
    """
    spot = chain.underlying_price
    if spot is None or spot <= 0:
        return {"status": "UNAVAILABLE", "reason": "missing_spot", "dealer_gex": False, "method_id": GEX_METHOD_ID, "method_version": GEX_METHOD_VERSION}
    signed = Decimal("0")
    unsigned = Decimal("0")
    call_gex = Decimal("0")
    put_gex = Decimal("0")
    used_oi = 0
    used_n = 0
    eligible_oi = 0
    excluded = {"unknown_gamma": 0, "unknown_oi": 0, "unknown_multiplier": 0, "adjusted": 0, "invalid_gamma": 0}
    by_strike: dict[str, dict[str, Any]] = {}
    zerodte_signed = Decimal("0")
    zerodte_unsigned = Decimal("0")
    for contract in chain.contracts:
        if contract.open_interest is not None:
            eligible_oi += contract.open_interest
        if contract.is_adjusted:
            excluded["adjusted"] += 1
            continue
        if contract.gamma is None:
            excluded["unknown_gamma"] += 1
            continue
        if contract.flags.get("invalid_long_gamma"):
            excluded["invalid_gamma"] += 1
            continue
        if contract.open_interest is None:
            excluded["unknown_oi"] += 1
            continue
        if contract.multiplier is None:
            excluded["unknown_multiplier"] += 1
            continue
        mag = unsigned_gex(gamma=contract.gamma, open_interest=contract.open_interest, multiplier=contract.multiplier, spot=spot)
        sign = Decimal("1") if contract.option_type == "call" else Decimal("-1")
        signed_i = sign * mag
        unsigned += mag
        signed += signed_i
        used_oi += contract.open_interest
        used_n += 1
        if contract.option_type == "call":
            call_gex += mag
        else:
            put_gex += mag
        key = str(contract.strike)
        bucket = by_strike.setdefault(key, {"strike": contract.strike, "unsigned": Decimal("0"), "signed": Decimal("0")})
        bucket["unsigned"] += mag
        bucket["signed"] += signed_i
        if contract.expiration == chain.session_date:
            zerodte_unsigned += mag
            zerodte_signed += signed_i
    if used_n == 0:
        return {
            "status": "UNAVAILABLE",
            "reason": "no_eligible_gex_contracts",
            "exclusions": excluded,
            "dealer_gex": False,
            "method_id": GEX_METHOD_ID,
            "method_version": GEX_METHOD_VERSION,
            "sign_convention": GEX_SIGN_CONVENTION,
        }
    ranked = sorted(by_strike.values(), key=lambda row: row["unsigned"], reverse=True)
    top = ranked[0] if ranked else None
    return {
        "status": "OK",
        "gross_unsigned": unsigned,
        "signed_net": signed,
        "call_unsigned": call_gex,
        "put_unsigned": put_gex,
        "units": "delta_notional_per_1pct",
        "dealer_gex": False,
        "label": "gex_proxy",
        "sign_convention": GEX_SIGN_CONVENTION,
        "gamma_convention": GAMMA_CONVENTION,
        "method_id": GEX_METHOD_ID,
        "method_version": GEX_METHOD_VERSION,
        "covered_oi_fraction": (Decimal(used_oi) / Decimal(eligible_oi)) if eligible_oi else None,
        "covered_contract_count": used_n,
        "exclusions": excluded,
        "largest_gamma_concentration": {
            "strike": top["strike"] if top else None,
            "unsigned": top["unsigned"] if top else None,
            "note": "concentration under CALL_PLUS_PUT_MINUS_V1; not proven support/resistance",
        } if top else None,
        "zerodte": {"unsigned": zerodte_unsigned, "signed": zerodte_signed},
        "strike_concentrations": [
            {"strike": row["strike"], "unsigned": row["unsigned"], "signed": row["signed"]} for row in ranked[:8]
        ],
        "gamma_flip": "not_implemented_v1",
        "reason": None,
    }


def compute_options_metrics(chain: NormalizedChain) -> dict[str, Any]:
    spot = chain.underlying_price
    groups = _expiry_groups(chain)
    term = []
    for expiration, contracts in sorted(groups.items()):
        dte = (expiration - chain.session_date).days
        atm = atm_iv_for_expiry(contracts, spot) if spot else {"iv_decimal": None, "status": "UNAVAILABLE", "reason": "missing_spot"}
        skew = skew_25d(contracts)
        t = year_fraction(dte) if dte is not None else None
        term.append(
            {
                "expiration": expiration.isoformat(),
                "dte_session": dte,
                "t_years": t,
                "atm": atm,
                "skew_25d": skew,
                "put_call_oi": put_call_ratio(contracts, "open_interest"),
                "put_call_volume": put_call_ratio(contracts, "volume"),
            }
        )
    atm_points = [(row["t_years"], row["atm"].get("iv_decimal")) for row in term if row.get("t_years") and row["atm"].get("status") == "OK"]
    iv30 = interpolate_30d_atm([(t, iv) for t, iv in atm_points if t is not None])
    nearest_ok = next((row for row in term if row["atm"].get("status") == "OK"), None)
    move = expected_move(spot, nearest_ok["atm"]["iv_decimal"] if nearest_ok else None, nearest_ok["dte_session"] if nearest_ok else 0)
    return {
        "method_version": ANALYTICS_VERSION,
        "underlying": chain.underlying,
        "session_date": chain.session_date.isoformat(),
        "observation_time_utc": chain.observation_time_utc.isoformat() if chain.observation_time_utc else None,
        "observation_precision": chain.observation_precision,
        "spot": spot,
        "delay_label": "Cboe delayed quotes",
        "eod_label": None,
        "input_content_hash": chain.content_hash,
        "atm_iv_30d": iv30,
        "term_structure": term,
        "put_call_oi": put_call_ratio(chain.contracts, "open_interest"),
        "put_call_volume": put_call_ratio(chain.contracts, "volume"),
        "zerodte_oi_share": zerodte_share(chain.contracts, chain.session_date, "open_interest"),
        "zerodte_volume_share": zerodte_share(chain.contracts, chain.session_date, "volume"),
        "expected_move_nearest_atm": move,
        "gex_proxy": gex_proxy(chain),
        "unsupported_v1": ["gamma_flip", "iv_rank", "iv_percentile", "realized_vol_substitute", "dealer_inventory"],
        "export_scope": "INTERNAL_ONLY",
        "iv_30d": {"iv_decimal": iv30.get("iv_decimal"), "iv_percent": iv30.get("iv_percent"), "status": iv30.get("status"), "reason": iv30.get("reason")},
        "selected_skew_25d": {
            "skew_25d_vol_points": (nearest_ok or {}).get("skew_25d", {}).get("value_pp") if nearest_ok else None,
            "status": (nearest_ok or {}).get("skew_25d", {}).get("status") if nearest_ok else "UNAVAILABLE",
        },
        "put_call": {
            "oi_put_call": put_call_ratio(chain.contracts, "open_interest").get("value"),
            "volume_put_call": put_call_ratio(chain.contracts, "volume").get("value"),
        },
        "atm_term_structure": term,
        "expected_move": move,
        "zero_dte": {
            "oi_share": zerodte_share(chain.contracts, chain.session_date, "open_interest").get("value"),
            "volume_share": zerodte_share(chain.contracts, chain.session_date, "volume").get("value"),
        },
        "gex": gex_proxy(chain),
        "concentrations": (gex_proxy(chain).get("strike_concentrations") or []),
    }
