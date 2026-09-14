"""Tax-aware bond relative-value math. No provider I/O. No personalized tax advice.

Stacked ordinary rates are used as-is. State/local tax is not assumed to be federally
deductible (SALT cap). Missing rates stay missing rather than becoming zero unless the
caller passed an explicit 0. Yield is not total return.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ASSET_MUNI = "muni"
ASSET_TREASURY = "treasury"
ASSET_CORPORATE = "corporate"
ASSET_CLASSES = (ASSET_MUNI, ASSET_TREASURY, ASSET_CORPORATE)
METHOD_VERSION = "bond_tax_v1"


class BondTaxError(ValueError):
    """Inputs are inconsistent or a rate is out of domain."""


@dataclass(frozen=True)
class TaxAssumptions:
    federal_rate: float | None = None
    state_rate: float | None = None
    local_rate: float | None = None
    niit_rate: float | None = None
    combined_override: float | None = None
    muni_federal_exempt: bool = True
    muni_state_exempt: bool | None = None
    muni_local_exempt: bool | None = None
    muni_in_state: bool = False
    muni_amt_or_pab: bool = False
    residence_state: str | None = None
    issuer_state: str | None = None

    def warnings(self) -> list[str]:
        notes = [
            "Estimates only. Not tax, legal, or investment advice.",
            "Yield is not guaranteed total return. Callable bonds may realize YTW rather than YTM.",
            "Capital gains are not treated as tax-exempt coupon interest.",
            "State/local tax is not assumed federally deductible.",
        ]
        if self.combined_override is not None:
            notes.append("Combined ordinary rate override is in use; component rates are ignored for stacked ordinary tax.")
        if self.muni_amt_or_pab:
            notes.append("AMT / private-activity flag is set: federal exemption is not assumed.")
        if self.muni_state_exempt is None:
            notes.append("Muni state exemption is inferred from in-state / residence match when not set.")
        if any(rate is None for rate in (self.federal_rate, self.state_rate, self.local_rate, self.niit_rate)) and self.combined_override is None:
            notes.append("One or more tax-rate inputs are missing. Affected after-tax figures stay unavailable.")
        return notes


@dataclass(frozen=True)
class BondTaxInputs:
    asset_class: str
    yield_pct: float | None
    ytm_pct: float | None = None
    ytw_pct: float | None = None
    callable: bool = False
    duration: float | None = None
    dv01: float | None = None
    maturity_years: float | None = None
    coupon_pct: float | None = None
    price: float | None = None
    rating: str | None = None
    state: str | None = None
    tax_status: str | None = None
    label: str | None = None

    def conservative_yield(self) -> float | None:
        if self.callable and self.ytw_pct is not None:
            return self.ytw_pct
        if self.yield_pct is not None:
            return self.yield_pct
        if self.ytw_pct is not None:
            return self.ytw_pct
        return self.ytm_pct


@dataclass(frozen=True)
class BondTaxResult:
    asset_class: str
    pretax_yield_pct: float | None
    yield_basis: str
    applicable_tax_rate: float | None
    after_tax_yield_pct: float | None
    taxable_equivalent_yield_pct: float | None
    break_even_treasury_yield_pct: float | None
    break_even_corporate_yield_pct: float | None
    assumptions: list[str] = field(default_factory=list)
    missing_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_class": self.asset_class,
            "pretax_yield_pct": self.pretax_yield_pct,
            "yield_basis": self.yield_basis,
            "applicable_tax_rate": self.applicable_tax_rate,
            "after_tax_yield_pct": self.after_tax_yield_pct,
            "taxable_equivalent_yield_pct": self.taxable_equivalent_yield_pct,
            "break_even_treasury_yield_pct": self.break_even_treasury_yield_pct,
            "break_even_corporate_yield_pct": self.break_even_corporate_yield_pct,
            "assumptions": list(self.assumptions),
            "missing_reason": self.missing_reason,
            "method_version": METHOD_VERSION,
        }


def _require_rate(value: float | None, name: str) -> float:
    if value is None:
        raise BondTaxError("{0} is required".format(name))
    if value < 0 or value >= 1:
        raise BondTaxError("{0} must be in [0, 1)".format(name))
    return value


def _optional_rate(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    return _require_rate(value, name)


def infer_muni_state_exempt(assumptions: TaxAssumptions) -> bool:
    if assumptions.muni_state_exempt is not None:
        return bool(assumptions.muni_state_exempt)
    if assumptions.muni_in_state:
        return True
    residence = (assumptions.residence_state or "").strip().upper()
    issuer = (assumptions.issuer_state or "").strip().upper()
    return bool(residence and issuer and residence == issuer)


def infer_muni_local_exempt(assumptions: TaxAssumptions) -> bool:
    if assumptions.muni_local_exempt is not None:
        return bool(assumptions.muni_local_exempt)
    return infer_muni_state_exempt(assumptions)


def ordinary_stacked_rate(assumptions: TaxAssumptions) -> float | None:
    if assumptions.combined_override is not None:
        return _require_rate(assumptions.combined_override, "combined_override")
    parts = [
        _optional_rate(assumptions.federal_rate, "federal_rate"),
        _optional_rate(assumptions.state_rate, "state_rate"),
        _optional_rate(assumptions.local_rate, "local_rate"),
        _optional_rate(assumptions.niit_rate, "niit_rate"),
    ]
    if any(part is None for part in parts):
        return None
    total = sum(parts)  # type: ignore[arg-type]
    if total < 0 or total >= 1:
        raise BondTaxError("stacked ordinary tax rate must be in [0, 1)")
    return total


def applicable_interest_tax_rate(asset_class: str, assumptions: TaxAssumptions) -> float | None:
    klass = str(asset_class or "").lower()
    if klass not in ASSET_CLASSES:
        raise BondTaxError("asset_class must be muni, treasury, or corporate")
    if assumptions.combined_override is not None and klass == ASSET_CORPORATE:
        return _require_rate(assumptions.combined_override, "combined_override")
    federal = _optional_rate(assumptions.federal_rate, "federal_rate")
    state = _optional_rate(assumptions.state_rate, "state_rate")
    local = _optional_rate(assumptions.local_rate, "local_rate")
    niit = _optional_rate(assumptions.niit_rate, "niit_rate")
    if klass == ASSET_TREASURY:
        if federal is None or niit is None:
            return None
        total = federal + niit
    elif klass == ASSET_CORPORATE:
        if any(part is None for part in (federal, state, local, niit)):
            return None
        total = federal + state + local + niit  # type: ignore[operator]
    else:
        federal_exempt = bool(assumptions.muni_federal_exempt) and not assumptions.muni_amt_or_pab
        state_exempt = infer_muni_state_exempt(assumptions)
        local_exempt = infer_muni_local_exempt(assumptions)
        needed = []
        if not federal_exempt:
            needed.extend([federal, niit])
        if not state_exempt:
            needed.append(state)
        if not local_exempt:
            needed.append(local)
        if any(part is None for part in needed):
            return None
        total = 0.0
        if not federal_exempt:
            total += float(federal) + float(niit)
        if not state_exempt:
            total += float(state)
        if not local_exempt:
            total += float(local)
    if total < 0 or total >= 1:
        raise BondTaxError("applicable interest tax rate must be in [0, 1)")
    return total


def after_tax_yield_pct(pretax_yield_pct: float | None, tax_rate: float | None) -> float | None:
    if pretax_yield_pct is None or tax_rate is None:
        return None
    return pretax_yield_pct * (1.0 - tax_rate)


def taxable_equivalent_yield_pct(after_tax_yield: float | None, taxable_rate: float | None) -> float | None:
    if after_tax_yield is None or taxable_rate is None:
        return None
    if taxable_rate >= 1:
        raise BondTaxError("taxable comparison rate must be < 1")
    return after_tax_yield / (1.0 - taxable_rate)


def analyze_bond_tax(bond: BondTaxInputs, assumptions: TaxAssumptions) -> BondTaxResult:
    pretax = bond.conservative_yield()
    basis = "ytw" if bond.callable and bond.ytw_pct is not None else "stated_yield"
    if pretax is None:
        return BondTaxResult(
            asset_class=bond.asset_class,
            pretax_yield_pct=None,
            yield_basis=basis,
            applicable_tax_rate=None,
            after_tax_yield_pct=None,
            taxable_equivalent_yield_pct=None,
            break_even_treasury_yield_pct=None,
            break_even_corporate_yield_pct=None,
            assumptions=assumptions.warnings(),
            missing_reason="missing_yield",
        )
    tax_rate = applicable_interest_tax_rate(bond.asset_class, assumptions)
    after_tax = after_tax_yield_pct(pretax, tax_rate)
    treasury_rate = applicable_interest_tax_rate(ASSET_TREASURY, assumptions)
    corporate_rate = applicable_interest_tax_rate(ASSET_CORPORATE, assumptions)
    return BondTaxResult(
        asset_class=bond.asset_class,
        pretax_yield_pct=pretax,
        yield_basis=basis,
        applicable_tax_rate=tax_rate,
        after_tax_yield_pct=after_tax,
        taxable_equivalent_yield_pct=taxable_equivalent_yield_pct(after_tax, corporate_rate),
        break_even_treasury_yield_pct=taxable_equivalent_yield_pct(after_tax, treasury_rate),
        break_even_corporate_yield_pct=taxable_equivalent_yield_pct(after_tax, corporate_rate),
        assumptions=assumptions.warnings(),
        missing_reason=None if tax_rate is not None else "missing_tax_inputs",
    )


def income_on_amount(amount: float | None, pretax_yield_pct: float | None, tax_rate: float | None) -> dict[str, float | None]:
    if amount is None or amount < 0 or pretax_yield_pct is None:
        return {
            "investment_amount": amount,
            "gross_interest": None,
            "estimated_tax": None,
            "after_tax_income": None,
        }
    gross = amount * pretax_yield_pct / 100.0
    if tax_rate is None:
        return {
            "investment_amount": amount,
            "gross_interest": gross,
            "estimated_tax": None,
            "after_tax_income": None,
        }
    tax = gross * tax_rate
    return {
        "investment_amount": amount,
        "gross_interest": gross,
        "estimated_tax": tax,
        "after_tax_income": gross - tax,
    }


def interest_tax_components(asset_class: str, assumptions: TaxAssumptions) -> dict[str, float | None]:
    """Component ordinary rates that apply to this asset. Known exemptions are 0, not missing."""
    klass = str(asset_class or "").lower()
    if klass not in ASSET_CLASSES:
        raise BondTaxError("asset_class must be muni, treasury, or corporate")
    federal = _optional_rate(assumptions.federal_rate, "federal_rate")
    state = _optional_rate(assumptions.state_rate, "state_rate")
    local = _optional_rate(assumptions.local_rate, "local_rate")
    niit = _optional_rate(assumptions.niit_rate, "niit_rate")
    if assumptions.combined_override is not None and klass == ASSET_CORPORATE:
        return {
            "federal": _require_rate(assumptions.combined_override, "combined_override"),
            "state": 0.0,
            "local": 0.0,
            "niit": 0.0,
        }
    if klass == ASSET_TREASURY:
        return {"federal": federal, "state": 0.0, "local": 0.0, "niit": niit}
    if klass == ASSET_CORPORATE:
        return {"federal": federal, "state": state, "local": local, "niit": niit}
    federal_exempt = bool(assumptions.muni_federal_exempt) and not assumptions.muni_amt_or_pab
    state_exempt = infer_muni_state_exempt(assumptions)
    local_exempt = infer_muni_local_exempt(assumptions)
    return {
        "federal": 0.0 if federal_exempt else federal,
        "state": 0.0 if state_exempt else state,
        "local": 0.0 if local_exempt else local,
        "niit": 0.0 if federal_exempt else niit,
    }


def income_breakdown(
    amount: float | None,
    pretax_yield_pct: float | None,
    assumptions: TaxAssumptions,
    asset_class: str,
) -> dict[str, float | None]:
    base = income_on_amount(amount, pretax_yield_pct, applicable_interest_tax_rate(asset_class, assumptions))
    if base["gross_interest"] is None:
        return {
            **base,
            "federal_tax": None,
            "state_local_tax": None,
            "niit_tax": None,
        }
    parts = interest_tax_components(asset_class, assumptions)
    gross = float(base["gross_interest"])
    federal = None if parts["federal"] is None else gross * parts["federal"]
    niit = None if parts["niit"] is None else gross * parts["niit"]
    state = None if parts["state"] is None else gross * parts["state"]
    local = None if parts["local"] is None else gross * parts["local"]
    state_local = None if state is None or local is None else state + local
    return {
        **base,
        "federal_tax": federal,
        "state_local_tax": state_local,
        "niit_tax": niit,
    }


def after_tax_spread_pct(left: BondTaxResult, right: BondTaxResult) -> float | None:
    if left.after_tax_yield_pct is None or right.after_tax_yield_pct is None:
        return None
    return left.after_tax_yield_pct - right.after_tax_yield_pct


def after_tax_yield_per_duration(result: BondTaxResult, duration: float | None) -> float | None:
    if result.after_tax_yield_pct is None or duration is None or duration <= 0:
        return None
    return result.after_tax_yield_pct / duration


def compare_three(
    muni: BondTaxInputs,
    treasury: BondTaxInputs,
    corporate: BondTaxInputs,
    assumptions: TaxAssumptions,
    *,
    investment_amount: float | None = None,
) -> dict[str, Any]:
    rows = {
        ASSET_MUNI: analyze_bond_tax(muni, assumptions),
        ASSET_TREASURY: analyze_bond_tax(treasury, assumptions),
        ASSET_CORPORATE: analyze_bond_tax(corporate, assumptions),
    }
    income = {
        key: income_breakdown(investment_amount, row.pretax_yield_pct, assumptions, key)
        for key, row in rows.items()
    }
    return {
        "method_version": METHOD_VERSION,
        "disclaimer": assumptions.warnings(),
        "results": {key: row.as_dict() for key, row in rows.items()},
        "spreads": {
            "muni_minus_treasury_pretax": _diff(rows[ASSET_MUNI].pretax_yield_pct, rows[ASSET_TREASURY].pretax_yield_pct),
            "muni_minus_corporate_pretax": _diff(rows[ASSET_MUNI].pretax_yield_pct, rows[ASSET_CORPORATE].pretax_yield_pct),
            "corporate_minus_treasury_pretax": _diff(rows[ASSET_CORPORATE].pretax_yield_pct, rows[ASSET_TREASURY].pretax_yield_pct),
            "muni_minus_treasury_after_tax": after_tax_spread_pct(rows[ASSET_MUNI], rows[ASSET_TREASURY]),
            "muni_minus_corporate_after_tax": after_tax_spread_pct(rows[ASSET_MUNI], rows[ASSET_CORPORATE]),
            "corporate_minus_treasury_after_tax": after_tax_spread_pct(rows[ASSET_CORPORATE], rows[ASSET_TREASURY]),
        },
        "risk_context": {
            "muni_after_tax_per_duration": after_tax_yield_per_duration(rows[ASSET_MUNI], muni.duration),
            "treasury_after_tax_per_duration": after_tax_yield_per_duration(rows[ASSET_TREASURY], treasury.duration),
            "corporate_after_tax_per_duration": after_tax_yield_per_duration(rows[ASSET_CORPORATE], corporate.duration),
        },
        "income": income,
        "inputs": {
            "muni": muni.__dict__,
            "treasury": treasury.__dict__,
            "corporate": corporate.__dict__,
        },
    }


def _diff(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def muni_treasury_ratio(muni_yield_pct: float | None, treasury_yield_pct: float | None) -> float | None:
    if muni_yield_pct is None or treasury_yield_pct is None or treasury_yield_pct == 0:
        return None
    return muni_yield_pct / treasury_yield_pct


AssetClass = Literal["muni", "treasury", "corporate"]
