"""Versioned SEC metric formulas. Missing/invalid inputs stay unavailable; no forced ratios."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

SEC_METRIC_VERSION = "sec_metrics_v1"

STATUS_OK = "OK"
STATUS_MISSING = "MISSING_PREREQUISITE"
STATUS_INVALID = "INVALID_DENOMINATOR"
STATUS_AMBIGUOUS = "AMBIGUOUS_SCOPE"
STATUS_UNSUPPORTED = "UNSUPPORTED_ENTITY"


def _d(value: Any) -> Decimal | None:
    if value is None:
        return None
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    return number if number.is_finite() else None


def ratio(numerator: Any, denominator: Any) -> dict[str, Any]:
    num = _d(numerator)
    den = _d(denominator)
    if num is None or den is None:
        return {"value": None, "status": STATUS_MISSING, "reason": "missing_input"}
    if den == 0:
        return {"value": None, "status": STATUS_INVALID, "reason": "zero_denominator"}
    return {"value": num / den, "status": STATUS_OK, "reason": None}


def discrete_quarter_from_ytd(current_ytd: Any, prior_ytd: Any) -> dict[str, Any]:
    """Q2/Q3/Q4 differencing only when both YTD facts share compatible coverage."""
    cur = _d(current_ytd)
    prior = _d(prior_ytd)
    if cur is None or prior is None:
        return {"value": None, "status": STATUS_MISSING, "reason": "missing_ytd_pair"}
    return {"value": cur - prior, "status": STATUS_OK, "reason": None}


def refuse_summed_ytd_mix(parts: list[Any]) -> dict[str, Any]:
    """Never add Q1 + six-month YTD + nine-month YTD + annual."""
    return {"value": None, "status": STATUS_AMBIGUOUS, "reason": "refused_mixed_ytd_sum", "parts": len(parts)}


def fcf(cfo: Any, capex: Any) -> dict[str, Any]:
    cash = _d(cfo)
    cap = _d(capex)
    if cash is None or cap is None:
        return {"value": None, "status": STATUS_MISSING, "reason": "need_cfo_and_capex", "inputs": {"cfo": cash, "capex": cap}}
    return {"value": cash - cap, "status": STATUS_OK, "reason": None, "inputs": {"cfo": cash, "capex": cap}}


def ttm_from_four_quarters(quarters: list[Any]) -> dict[str, Any]:
    values = [_d(item) for item in quarters]
    if len(values) != 4 or any(item is None for item in values):
        return {"value": None, "status": STATUS_MISSING, "reason": "need_four_compatible_quarters"}
    return {"value": sum(values, Decimal(0)), "status": STATUS_OK, "reason": None}


def pe(price: Any, earnings: Any) -> dict[str, Any]:
    earn = _d(earnings)
    if earn is not None and earn <= 0:
        return {"value": None, "status": STATUS_INVALID, "reason": "non_positive_earnings"}
    return ratio(price, earnings)


def market_cap(price: Any, shares: Any, *, price_basis: str, share_basis: str) -> dict[str, Any]:
    if price_basis != share_basis:
        return {"value": None, "status": STATUS_AMBIGUOUS, "reason": "incompatible_price_share_basis", "price_basis": price_basis, "share_basis": share_basis}
    px = _d(price)
    sh = _d(shares)
    if px is None or sh is None:
        return {"value": None, "status": STATUS_MISSING, "reason": "missing_price_or_shares"}
    return {"value": px * sh, "status": STATUS_OK, "reason": None}


METRIC_CATALOG: dict[str, dict[str, Any]] = {
    "FCF": {"fn": "fcf", "prerequisites": ("cfo", "capex"), "units": "currency"},
    "GROSS_MARGIN": {"fn": "ratio", "prerequisites": ("gross_profit", "revenue"), "units": "fraction"},
    "OPERATING_MARGIN": {"fn": "ratio", "prerequisites": ("operating_income", "revenue"), "units": "fraction"},
    "NET_MARGIN": {"fn": "ratio", "prerequisites": ("net_income", "revenue"), "units": "fraction"},
    "REVENUE_GROWTH": {"fn": "ratio", "prerequisites": ("revenue", "prior_revenue"), "units": "fraction"},
    "ROE": {"fn": "ratio", "prerequisites": ("net_income", "equity"), "units": "fraction", "unsupported_entities": ("bank", "insurer", "etf", "reit")},
    "ROIC": {"fn": "ratio", "prerequisites": ("nopat", "invested_capital"), "units": "fraction", "unsupported_entities": ("bank", "insurer", "etf", "reit")},
    "LEVERAGE": {"fn": "ratio", "prerequisites": ("total_debt", "equity"), "units": "fraction"},
    "PE": {"fn": "pe", "prerequisites": ("price", "earnings"), "units": "multiple"},
    "PB": {"fn": "ratio", "prerequisites": ("price", "book_value_per_share"), "units": "multiple"},
    "PS": {"fn": "ratio", "prerequisites": ("market_cap", "revenue"), "units": "multiple"},
    "EV_EBITDA": {"fn": "ratio", "prerequisites": ("enterprise_value", "ebitda"), "units": "multiple", "unsupported_entities": ("bank", "insurer", "etf", "reit")},
    "FCF_YIELD": {"fn": "ratio", "prerequisites": ("fcf", "market_cap"), "units": "fraction"},
    "EARNINGS_YIELD": {"fn": "ratio", "prerequisites": ("earnings", "market_cap"), "units": "fraction"},
}


def metric_status_for_entity(metric_id: str, entity_type: str | None) -> dict[str, Any] | None:
    spec = METRIC_CATALOG.get(metric_id)
    if spec is None:
        return {"value": None, "status": STATUS_UNSUPPORTED, "reason": "unknown_metric"}
    blocked = spec.get("unsupported_entities") or ()
    if entity_type and entity_type.lower() in blocked:
        return {"value": None, "status": STATUS_UNSUPPORTED, "reason": "unsupported_entity_type", "entity_type": entity_type}
    return None
