"""Analytical bond-ladder aggregation. No orders. Missing weights stay missing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from market_intelligence.bond_tax import BondTaxInputs, TaxAssumptions, analyze_bond_tax

METHOD_VERSION = "bond_ladder_v1"


@dataclass(frozen=True)
class LadderBond:
    label: str
    asset_class: str
    maturity_year: int
    principal: float
    coupon_pct: float | None
    yield_pct: float | None
    ytw_pct: float | None = None
    duration: float | None = None
    rating: str | None = None
    state: str | None = None
    issuer: str | None = None
    callable: bool = False

    def conservative_yield(self) -> float | None:
        if self.callable and self.ytw_pct is not None:
            return self.ytw_pct
        return self.yield_pct if self.yield_pct is not None else self.ytw_pct


def theoretical_rungs(
    *,
    total_investment: float,
    start_year: int,
    end_year: int,
    interval_years: int,
    yield_pct: float | None,
    asset_class: str,
    duration: float | None = None,
) -> list[LadderBond]:
    if total_investment <= 0:
        raise ValueError("total_investment must be positive")
    if interval_years <= 0:
        raise ValueError("interval_years must be positive")
    if end_year < start_year:
        raise ValueError("end_year must be >= start_year")
    years = list(range(int(start_year), int(end_year) + 1, int(interval_years)))
    if not years:
        return []
    principal = total_investment / len(years)
    return [
        LadderBond(
            label="{0} {1}Y rung".format(asset_class, year),
            asset_class=asset_class,
            maturity_year=int(year),
            principal=principal,
            coupon_pct=yield_pct,
            yield_pct=yield_pct,
            duration=duration,
        )
        for year in years
    ]


def _weighted(values: Iterable[tuple[float, float | None]]) -> float | None:
    numer = 0.0
    denom = 0.0
    saw_missing = False
    for weight, value in values:
        if value is None:
            saw_missing = True
            continue
        numer += weight * value
        denom += weight
    if saw_missing and denom == 0:
        return None
    if denom <= 0:
        return None
    return numer / denom


def aggregate_ladder(bonds: list[LadderBond], assumptions: TaxAssumptions | None = None) -> dict[str, Any]:
    total = sum(bond.principal for bond in bonds)
    schedule: dict[int, float] = {}
    for bond in bonds:
        schedule[bond.maturity_year] = schedule.get(bond.maturity_year, 0.0) + bond.principal
    pretax_yields = [(bond.principal, bond.conservative_yield()) for bond in bonds]
    durations = [(bond.principal, bond.duration) for bond in bonds]
    coupons = [(bond.principal, bond.coupon_pct) for bond in bonds]
    tax_rows = []
    if assumptions is not None:
        for bond in bonds:
            tax_rows.append(
                analyze_bond_tax(
                    BondTaxInputs(
                        asset_class=bond.asset_class,
                        yield_pct=bond.conservative_yield(),
                        ytw_pct=bond.ytw_pct,
                        callable=bond.callable,
                        duration=bond.duration,
                        rating=bond.rating,
                        state=bond.state,
                        label=bond.label,
                    ),
                    assumptions,
                )
            )
    after_tax = [(bond.principal, row.after_tax_yield_pct) for bond, row in zip(bonds, tax_rows)] if tax_rows else []
    ratings: dict[str, float] = {}
    issuers: dict[str, float] = {}
    states: dict[str, float] = {}
    call_principal = 0.0
    for bond in bonds:
        if bond.rating:
            ratings[bond.rating] = ratings.get(bond.rating, 0.0) + bond.principal
        if bond.issuer:
            issuers[bond.issuer] = issuers.get(bond.issuer, 0.0) + bond.principal
        if bond.state:
            states[bond.state] = states.get(bond.state, 0.0) + bond.principal
        if bond.callable:
            call_principal += bond.principal
    coupon_income = None
    if all(bond.coupon_pct is not None for bond in bonds):
        coupon_income = sum(bond.principal * float(bond.coupon_pct) / 100.0 for bond in bonds)
    after_tax_income = None
    if tax_rows and all(row.after_tax_yield_pct is not None for row in tax_rows):
        after_tax_income = sum(bond.principal * float(row.after_tax_yield_pct) / 100.0 for bond, row in zip(bonds, tax_rows))
    years = [bond.maturity_year for bond in bonds]
    return {
        "method_version": METHOD_VERSION,
        "analytical_only": True,
        "places_orders": False,
        "bond_count": len(bonds),
        "total_principal": total if bonds else None,
        "maturity_schedule": [{"year": year, "principal": schedule[year]} for year in sorted(schedule)],
        "weighted_average_yield_pct": _weighted(pretax_yields),
        "weighted_average_after_tax_yield_pct": _weighted(after_tax) if after_tax else None,
        "weighted_average_coupon_pct": _weighted(coupons),
        "weighted_average_maturity": (sum(years) / len(years)) if years else None,
        "weighted_duration": _weighted(durations),
        "estimated_coupon_income": coupon_income,
        "estimated_after_tax_income": after_tax_income,
        "rating_distribution": ratings,
        "issuer_concentration": issuers,
        "state_concentration": states,
        "call_exposure_principal": call_principal,
        "call_exposure_share": (call_principal / total) if total else None,
        "missing_yields": sum(1 for bond in bonds if bond.conservative_yield() is None),
        "missing_durations": sum(1 for bond in bonds if bond.duration is None),
    }
