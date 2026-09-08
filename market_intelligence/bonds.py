"""Local bond analytics (bounded domain): fixed-coupon bullet bonds only.

Inputs are explicit security terms from a source (never inferred from ticker strings) plus a
clean price per 100. Outputs: accrued interest, dirty price, street-convention yield to
maturity, Macaulay/modified duration, convexity, G-spread vs an interpolated par Treasury
curve, and Z-spread vs a bootstrapped semiannual zero curve. OAS is UNSUPPORTED (no option
model); callable/putable bonds get YTM only; floating/step coupons are UNSUPPORTED.

Conventions (``bond_analytics_v2``):
    * coupon schedule rolled backward from maturity (unadjusted dates; payment-date business
      day adjustment is ignored, as in street yield conventions); optional end-of-month roll;
    * day counts 30/360 (US), ACT/ACT (ICMA), ACT/360, ACT/365F; yields compounded at the coupon
      frequency; prices per 100; zero-coupon yields are semiannual bond-equivalent;
    * an explicit ``first_coupon_date`` is honoured: it must lie on the regular schedule (odd
      first coupon, short or long stub from ``issue_date``); irregular schedules are
      UNSUPPORTED_TERMS. When settlement falls inside the first coupon period and the terms do
      not pin the first coupon date, the schedule is UNSUPPORTED_TERMS rather than guessed;
    * ACT/ACT stub periods are UNSUPPORTED_TERMS (ICMA notional-period rules are not implemented).

Numerical policy (engineering tolerances, not economic thresholds):
    * yields are solved by bisection on a *verified* bracket inside ``YIELD_DOMAIN_PCT``; a
      price whose implied yield lies outside that domain is ``UNSUPPORTED_PRICE_DOMAIN`` and no
      yield/duration/spread is published;
    * every solved yield/spread is re-priced and must match the input dirty price within
      ``REPRICE_TOL`` per 100, otherwise ``NUMERICAL_FAILURE`` and NULL values;
    * Z-spread requires the quoted par curve to span the bond's maturity with at least
      ``MIN_CURVE_TENORS`` tenors; anything else is ``CURVE_COVERAGE_INSUFFICIENT`` (NULL).
      Flat short-end extrapolation below the shortest quoted tenor is disclosed.

Cross-checks live in ``tests/test_bond_analytics.py``: Excel ``PRICE``/``YIELD``/``DURATION``/
``MDURATION`` documented examples, closed-form par/annuity/zero cases, and finite-difference
duration/convexity, so validation is not a round trip through this implementation alone.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

ANALYTICS_VERSION = "bond_analytics_v2"
SUPPORT_FULL = "FULL"
SUPPORT_PARTIAL = "PARTIAL"  # yield/duration ok, some spread unavailable (see detail statuses)
SUPPORT_YTM_ONLY_CALLABLE = "YTM_ONLY_CALLABLE"
SUPPORT_UNSUPPORTED_FLOATING = "UNSUPPORTED_FLOATING"
SUPPORT_UNSUPPORTED_TERMS = "UNSUPPORTED_TERMS"
SUPPORT_UNSUPPORTED_PRICE_DOMAIN = "UNSUPPORTED_PRICE_DOMAIN"
SUPPORT_NUMERICAL_FAILURE = "NUMERICAL_FAILURE"
OAS_STATUS = "UNSUPPORTED_NO_OPTION_MODEL"
STATUS_OK = "OK"
STATUS_NO_CURVE = "NO_CURVE"
STATUS_CURVE_COVERAGE = "CURVE_COVERAGE_INSUFFICIENT"
STATUS_OUT_OF_DOMAIN = "OUT_OF_DOMAIN"
STATUS_NO_CONVERGENCE = "NO_CONVERGENCE"
STATUS_NOT_COMPUTED = "NOT_COMPUTED"
DAY_COUNTS = ("30/360", "ACT/ACT", "ACT/360", "ACT/365F")
TREASURY_TENORS_YEARS: dict[str, float] = {"DGS1MO": 1 / 12, "DGS3MO": 0.25, "DGS6MO": 0.5, "DGS1": 1.0, "DGS2": 2.0, "DGS3": 3.0, "DGS5": 5.0, "DGS7": 7.0, "DGS10": 10.0, "DGS20": 20.0, "DGS30": 30.0}

YIELD_DOMAIN_PCT = (-10.0, 150.0)
Z_SPREAD_DOMAIN = (-0.05, 0.50)  # decimal per annum: -500 bp .. +5000 bp
PRICE_TOL = 1e-9  # root-finder stop tolerance on |price(y) - target|
REPRICE_TOL = 1e-6  # independent re-pricing check, per 100
MAX_ITER = 400
MIN_CURVE_TENORS = 4
MAX_SCHEDULE_PERIODS = 12 * 100


class BondAnalyticsError(ValueError):
    """Terms are inconsistent or the computation cannot be performed honestly."""


class OutOfDomain(BondAnalyticsError):
    """The root lies outside the supported domain (no bracket)."""


class NoConvergence(BondAnalyticsError):
    """The solver stopped without re-pricing the input within tolerance."""


@dataclass(frozen=True)
class BondTerms:
    bond_id: str
    coupon_rate_pct: float | None
    coupon_frequency: int
    maturity_date: date
    coupon_type: str = "FIXED"
    day_count: str = "30/360"
    redemption: float = 100.0
    issue_date: date | None = None
    callable: bool = False
    putable: bool = False
    settlement_days: int = 1
    first_coupon_date: date | None = None
    end_of_month: bool = False

    def validate(self) -> None:
        if self.coupon_frequency not in (1, 2, 4, 12):
            raise BondAnalyticsError("coupon_frequency must be 1, 2, 4 or 12 (got {0})".format(self.coupon_frequency))
        if self.day_count not in DAY_COUNTS:
            raise BondAnalyticsError("unsupported day_count {0}; supported {1}".format(self.day_count, DAY_COUNTS))
        if not _finite(self.redemption) or self.redemption <= 0:
            raise BondAnalyticsError("redemption must be a positive finite number")
        if self.coupon_type == "FIXED":
            if self.coupon_rate_pct is None:
                raise BondAnalyticsError("fixed coupon requires coupon_rate_pct")
            if not _finite(self.coupon_rate_pct) or self.coupon_rate_pct < 0:
                raise BondAnalyticsError("coupon_rate_pct must be finite and non-negative")
        if self.coupon_type == "ZERO" and (self.coupon_rate_pct or 0) != 0:
            raise BondAnalyticsError("zero coupon bond cannot carry a coupon rate")
        if self.issue_date is not None and self.issue_date >= self.maturity_date:
            raise BondAnalyticsError("issue_date must precede maturity_date")
        if self.first_coupon_date is not None:
            if self.coupon_type == "ZERO":
                raise BondAnalyticsError("zero coupon bond cannot carry a first_coupon_date")
            if self.issue_date is None:
                raise BondAnalyticsError("first_coupon_date requires issue_date (stub length is undefined without it)")
            if not (self.issue_date < self.first_coupon_date <= self.maturity_date):
                raise BondAnalyticsError("first_coupon_date must lie after issue_date and on/before maturity_date")
        if self.settlement_days < 0:
            raise BondAnalyticsError("settlement_days must be non-negative")


def _finite(x: Any) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _month_end(year: int, month: int) -> int:
    nxt = date(year + (month // 12), month % 12 + 1, 1)
    return (nxt - timedelta(days=1)).day


def add_months(d: date, months: int, *, end_of_month: bool = False) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    last_day = _month_end(year, month)
    if end_of_month:
        return date(year, month, last_day)
    return date(year, month, min(d.day, last_day))


def _is_month_end(d: date) -> bool:
    return d.day == _month_end(d.year, d.month)


def regular_dates(terms: BondTerms, floor: date) -> list[date]:
    """Regular schedule rolled backward from maturity: every regular date strictly after ``floor``.

    ``floor`` is the issue date when known (so the earliest element is the first regular coupon
    date after issue), otherwise the settlement date.
    """
    step = 12 // terms.coupon_frequency
    eom = terms.end_of_month and _is_month_end(terms.maturity_date)
    dates = [terms.maturity_date]
    while True:
        previous = add_months(terms.maturity_date, -step * len(dates), end_of_month=eom)
        if previous <= floor:
            break
        dates.append(previous)
        if len(dates) > MAX_SCHEDULE_PERIODS:
            raise BondAnalyticsError("coupon schedule too long")
    return sorted(dates)


def on_regular_schedule(terms: BondTerms, d: date) -> bool:
    """True when ``d`` is a regular coupon date rolled back from maturity."""
    step = 12 // terms.coupon_frequency
    eom = terms.end_of_month and _is_month_end(terms.maturity_date)
    months = (terms.maturity_date.year - d.year) * 12 + (terms.maturity_date.month - d.month)
    k = round(months / step)
    return k >= 0 and add_months(terms.maturity_date, -step * k, end_of_month=eom) == d


@dataclass(frozen=True)
class Schedule:
    """Payment dates after settlement plus the accrual period containing settlement."""

    payment_dates: list[date]
    period_start: date  # accrual start for the current period (issue_date in a stub)
    period_end: date  # next payment date
    notional_start: date  # regular period start used for time measurement / day-count basis
    stub: str  # "NONE" | "SHORT_FIRST" | "LONG_FIRST"
    first_coupon_amount: float  # per 100, for the first payment date after settlement


def _period_days(terms: BondTerms, start: date, end: date) -> float:
    if terms.day_count == "30/360":
        return float(_days_30_360(start, end))
    return float((end - start).days)


def build_schedule(terms: BondTerms, settlement: date) -> Schedule:
    """Coupon schedule honouring issue_date / first_coupon_date; raises on unsupported shapes."""
    if terms.coupon_type == "ZERO":
        return Schedule([terms.maturity_date], settlement, terms.maturity_date, settlement, "NONE", terms.redemption)
    coupon = (terms.coupon_rate_pct or 0.0) / terms.coupon_frequency
    step = 12 // terms.coupon_frequency
    eom = terms.end_of_month and _is_month_end(terms.maturity_date)
    floor = terms.issue_date if terms.issue_date is not None else settlement
    regular = regular_dates(terms, floor)
    first_coupon: date | None = None
    if terms.first_coupon_date is not None:
        if not on_regular_schedule(terms, terms.first_coupon_date):
            raise BondAnalyticsError("first_coupon_date {0} is not on the regular schedule rolled back from maturity; irregular schedules are unsupported".format(terms.first_coupon_date))
        first_coupon = terms.first_coupon_date
        # Regular dates between issue and the explicit first coupon are not paid (long first coupon).
        payment_candidates = [d for d in regular if d >= first_coupon]
    else:
        if terms.issue_date is not None:
            first_coupon = regular[0]
        payment_candidates = list(regular)
    payments = [d for d in payment_candidates if d > settlement]
    if not payments:
        raise BondAnalyticsError("no cash flows after settlement {0}".format(settlement))
    next_pay = payments[0]
    notional_start = add_months(next_pay, -step, end_of_month=eom)
    if first_coupon is not None and next_pay == first_coupon and notional_start != terms.issue_date:
        # Settlement inside an odd first period: accrue from issue_date, prorate the first coupon.
        if terms.first_coupon_date is None:
            raise BondAnalyticsError("settlement {0} falls inside the first coupon period but first_coupon_date is not specified; refusing to guess the stub".format(settlement))
        if terms.day_count == "ACT/ACT":
            raise BondAnalyticsError("ACT/ACT (ICMA) stub periods are unsupported")
        assert terms.issue_date is not None
        stub = "SHORT_FIRST" if terms.issue_date > notional_start else "LONG_FIRST"
        regular_days = _period_days(terms, notional_start, next_pay)
        stub_days = _period_days(terms, terms.issue_date, next_pay)
        if terms.day_count in ("ACT/360", "ACT/365F"):
            first_amount = (terms.coupon_rate_pct or 0.0) * stub_days / _basis(terms)
        else:
            first_amount = coupon * stub_days / regular_days
        return Schedule(payments, terms.issue_date, next_pay, notional_start, stub, first_amount)
    if terms.day_count in ("ACT/360", "ACT/365F"):
        first_amount = (terms.coupon_rate_pct or 0.0) * (next_pay - notional_start).days / _basis(terms)
    else:
        first_amount = coupon
    return Schedule(payments, notional_start, next_pay, notional_start, "NONE", first_amount)


def _basis(terms: BondTerms) -> float:
    return 360.0 if terms.day_count == "ACT/360" else 365.0


def coupon_dates(terms: BondTerms, settlement: date) -> list[date]:
    """All payment dates strictly after settlement (honours first_coupon_date)."""
    return list(build_schedule(terms, settlement).payment_dates)


def previous_coupon_date(terms: BondTerms, settlement: date) -> date:
    """Accrual start of the period containing settlement (issue_date inside a stub)."""
    return build_schedule(terms, settlement).period_start


def _days_30_360(start: date, end: date) -> int:
    d1, d2 = min(start.day, 30), end.day
    if d1 == 30 and d2 == 31:
        d2 = 30
    return 360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1)


def accrual_fraction(terms: BondTerms, start: date, settlement: date, end: date) -> float:
    """Fraction of the coupon *amount* accrued at settlement, by day count."""
    if settlement <= start:
        return 0.0
    if terms.day_count == "30/360":
        return _days_30_360(start, settlement) / _days_30_360(start, end)
    if terms.day_count == "ACT/ACT":
        return (settlement - start).days / (end - start).days
    if terms.day_count == "ACT/360":
        return (settlement - start).days / 360.0 * terms.coupon_frequency
    return (settlement - start).days / 365.0 * terms.coupon_frequency  # ACT/365F


def accrued_interest(terms: BondTerms, settlement: date) -> float:
    if terms.coupon_type == "ZERO" or not terms.coupon_rate_pct:
        return 0.0
    sched = build_schedule(terms, settlement)
    if terms.day_count in ("ACT/360", "ACT/365F"):
        basis = 360.0 if terms.day_count == "ACT/360" else 365.0
        return terms.coupon_rate_pct * max(0, (settlement - sched.period_start).days) / basis
    if sched.stub == "NONE":
        return (terms.coupon_rate_pct / terms.coupon_frequency) * accrual_fraction(terms, sched.period_start, settlement, sched.period_end)
    # Stub (30/360 only): accrue the prorated first coupon linearly over the stub.
    stub_days = _period_days(terms, sched.period_start, sched.period_end)
    return sched.first_coupon_amount * (_period_days(terms, sched.period_start, settlement) / stub_days if stub_days > 0 else 0.0)


def cashflows(terms: BondTerms, settlement: date) -> list[tuple[float, float]]:
    """(time in years from settlement measured in coupon periods / frequency, amount per 100)."""
    sched = build_schedule(terms, settlement)
    if terms.coupon_type == "ZERO":
        t = (terms.maturity_date - settlement).days / 365.0
        return [(t, terms.redemption)]
    coupon = (terms.coupon_rate_pct or 0.0) / terms.coupon_frequency
    # Fraction of a regular period from settlement to the next payment (may exceed 1 in a long stub).
    regular_days = _period_days(terms, sched.notional_start, sched.period_end)
    w = _period_days(terms, settlement, sched.period_end) / regular_days if regular_days > 0 else 0.0
    flows: list[tuple[float, float]] = []
    for i, d in enumerate(sched.payment_dates):
        periods = w + i
        if i == 0:
            amount = sched.first_coupon_amount
        elif terms.day_count in ("ACT/360", "ACT/365F"):
            # Money-market day counts pay rate x actual days / basis each period.
            amount = (terms.coupon_rate_pct or 0.0) * (d - sched.payment_dates[i - 1]).days / _basis(terms)
        else:
            amount = coupon
        amount += terms.redemption if d == terms.maturity_date else 0.0
        flows.append((periods / terms.coupon_frequency, amount))
    return flows


def _compounding(terms: BondTerms) -> int:
    return terms.coupon_frequency if terms.coupon_type != "ZERO" else 2


def price_from_yield(terms: BondTerms, settlement: date, ytm_pct: float, flows: list[tuple[float, float]] | None = None) -> float:
    """Dirty price per 100 for a yield compounded at the coupon frequency."""
    f = _compounding(terms)
    y = ytm_pct / 100.0 / f
    if y <= -1.0:
        raise OutOfDomain("periodic yield {0} <= -100%".format(y))
    return sum(amount / (1 + y) ** (t * f) for t, amount in (flows if flows is not None else cashflows(terms, settlement)))


def _bisect(fn, lo: float, hi: float, *, tol: float, what: str) -> float:
    """Root of a monotone decreasing ``fn`` on a verified bracket [lo, hi]; raises without a sign change."""
    f_lo, f_hi = fn(lo), fn(hi)
    if not (_finite(f_lo) and _finite(f_hi)):
        raise NoConvergence("{0}: non-finite objective at the domain bounds".format(what))
    if abs(f_lo) <= tol:
        return lo
    if abs(f_hi) <= tol:
        return hi
    if (f_lo > 0) == (f_hi > 0):
        raise OutOfDomain("{0}: no root inside the supported domain [{1}, {2}] (f_lo={3:.6g}, f_hi={4:.6g})".format(what, lo, hi, f_lo, f_hi))
    for _ in range(MAX_ITER):
        mid = (lo + hi) / 2
        f_mid = fn(mid)
        if not _finite(f_mid):
            raise NoConvergence("{0}: non-finite objective at {1}".format(what, mid))
        if abs(f_mid) <= tol or (hi - lo) < 1e-14:
            return mid
        if (f_lo > 0) == (f_mid > 0):
            lo, f_lo = mid, f_mid
        else:
            hi, f_hi = mid, f_mid
    raise NoConvergence("{0}: bisection did not converge in {1} iterations".format(what, MAX_ITER))


def yield_from_price(terms: BondTerms, settlement: date, dirty_price: float, *, tol: float = PRICE_TOL) -> float:
    """Street yield (pct) re-pricing ``dirty_price`` within REPRICE_TOL; OutOfDomain/NoConvergence otherwise."""
    if not _finite(dirty_price) or dirty_price <= 0:
        raise BondAnalyticsError("dirty price must be a positive finite number (got {0!r})".format(dirty_price))
    flows = cashflows(terms, settlement)
    lo, hi = YIELD_DOMAIN_PCT
    y = _bisect(lambda ypct: price_from_yield(terms, settlement, ypct, flows) - dirty_price, lo, hi, tol=tol, what="yield")
    repriced = price_from_yield(terms, settlement, y, flows)
    if abs(repriced - dirty_price) > REPRICE_TOL:
        raise NoConvergence("yield {0:.8f}% re-prices to {1:.8f}, input {2:.8f}".format(y, repriced, dirty_price))
    return y


def durations(terms: BondTerms, settlement: date, ytm_pct: float, dirty_price: float) -> tuple[float, float, float]:
    """Macaulay (years), modified (years), convexity (years^2) from discounted cashflows."""
    f = _compounding(terms)
    y = ytm_pct / 100.0 / f
    mac = 0.0
    conv = 0.0
    for t, amount in cashflows(terms, settlement):
        n = t * f
        pv = amount / (1 + y) ** n
        mac += t * pv
        conv += n * (n + 1) * pv
    mac /= dirty_price
    modified = mac / (1 + y)
    convexity = conv / (dirty_price * (1 + y) ** 2) / f**2
    return mac, modified, convexity


# ---- curves -----------------------------------------------------------------------------------------

def _curve_points(curve: Mapping[str, float | None]) -> list[tuple[float, float]]:
    return sorted((TREASURY_TENORS_YEARS[k], float(v)) for k, v in curve.items() if k in TREASURY_TENORS_YEARS and v is not None and _finite(v))


def interpolate_par_yield(curve: Mapping[str, float | None], years: float) -> float | None:
    """Linear interpolation of a par Treasury curve keyed by FRED series id; None outside the range."""
    points = _curve_points(curve)
    if len(points) < 2 or years < points[0][0] or years > points[-1][0]:
        return None
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= years <= x1:
            return y0 + (y1 - y0) * (years - x0) / (x1 - x0) if x1 != x0 else y0
    return None


HORIZON_TOLERANCE_YEARS = 0.02  # day-count measurement of the last flow may exceed the tenor by ~a week


def curve_coverage(curve: Mapping[str, float | None], years: float) -> dict[str, Any]:
    """Coverage verdict for a bond whose last cash flow is at ``years``: span, tenor count, extrapolation."""
    points = _curve_points(curve)
    tenors = [k for k in TREASURY_TENORS_YEARS if curve.get(k) is not None and _finite(curve.get(k))]
    if not points:
        return {"status": STATUS_NO_CURVE, "tenors": tenors, "tenor_count": 0}
    short_extrapolated = points[0][0] > 0.5  # bootstrap needs a 6m point; below the shortest tenor it is flat
    out = {"tenors": tenors, "tenor_count": len(points), "min_tenor_years": points[0][0], "max_tenor_years": points[-1][0], "short_end_flat_extrapolation": short_extrapolated, "horizon_years": years}
    if len(points) < MIN_CURVE_TENORS:
        out["status"] = STATUS_CURVE_COVERAGE
        out["reason"] = "only {0} quoted tenors (< {1})".format(len(points), MIN_CURVE_TENORS)
    elif years > points[-1][0] + HORIZON_TOLERANCE_YEARS:
        out["status"] = STATUS_CURVE_COVERAGE
        out["reason"] = "maturity {0:.2f}y beyond longest quoted tenor {1:.2f}y (no extrapolation)".format(years, points[-1][0])
    else:
        out["status"] = STATUS_OK
        out["long_end_within_tolerance"] = years > points[-1][0]
    return out


def bootstrap_zero_curve(curve: Mapping[str, float | None], *, max_years: float = 30.0) -> list[tuple[float, float]]:
    """Semiannual bootstrap from linearly interpolated par yields -> [(t_years, zero_rate_pct_semiannual)].

    Tenors below the shortest quoted point use flat extrapolation (disclosed by ``curve_coverage``);
    ``max_years`` must not exceed the longest quoted tenor (callers enforce via ``curve_coverage``).
    """
    points = _curve_points(curve)
    if len(points) < 2:
        raise BondAnalyticsError("treasury curve needs at least two tenors to bootstrap")
    zeros: list[tuple[float, float]] = []
    dfs: list[float] = []
    n = int(math.ceil(max_years * 2 - 1e-9))
    for i in range(1, n + 1):
        t = i / 2.0
        par = interpolate_par_yield(curve, t)
        if par is None:
            if t > points[-1][0] + 0.5:
                raise BondAnalyticsError("bootstrap beyond the longest quoted tenor {0}y is not supported".format(points[-1][0]))
            # Below the shortest tenor, or the single semiannual node straddling the longest tenor
            # (allowed only within HORIZON_TOLERANCE_YEARS by curve_coverage): flat par.
            par = points[0][1] if t < points[0][0] else points[-1][1]
        c = par / 100.0 / 2.0
        pv_coupons = sum(c * df for df in dfs)
        df = (1.0 - pv_coupons) / (1.0 + c)
        if not _finite(df) or df <= 0:
            raise BondAnalyticsError("bootstrap produced a non-positive discount factor at {0}y".format(t))
        dfs.append(df)
        zero = 2.0 * (df ** (-1.0 / (2.0 * t)) - 1.0) * 100.0
        zeros.append((t, zero))
    return zeros


def _zero_rate_at(zeros: list[tuple[float, float]], t: float) -> float:
    if not zeros:
        raise BondAnalyticsError("empty zero curve")
    if t <= zeros[0][0]:
        return zeros[0][1]
    for (t0, z0), (t1, z1) in zip(zeros, zeros[1:]):
        if t0 <= t <= t1:
            return z0 + (z1 - z0) * (t - t0) / (t1 - t0)
    raise BondAnalyticsError("cash flow at {0:.3f}y lies beyond the bootstrapped curve".format(t))


def z_spread_bps(terms: BondTerms, settlement: date, dirty_price: float, zeros: list[tuple[float, float]]) -> float:
    """Constant spread (bp) over the semiannual zero curve re-pricing the bond; verified bracket + re-price."""
    flows = cashflows(terms, settlement)

    def pv(spread: float) -> float:
        total = 0.0
        for t, amount in flows:
            r = _zero_rate_at(zeros, t) / 100.0 + spread
            total += amount / (1 + r / 2.0) ** (2.0 * t)
        return total

    lo, hi = Z_SPREAD_DOMAIN
    spread = _bisect(lambda s: pv(s) - dirty_price, lo, hi, tol=PRICE_TOL, what="z-spread")
    if abs(pv(spread) - dirty_price) > REPRICE_TOL:
        raise NoConvergence("z-spread {0:.6f} re-prices to {1:.8f}, input {2:.8f}".format(spread, pv(spread), dirty_price))
    return spread * 10000.0


# ---- top level ----------------------------------------------------------------------------------------

@dataclass
class BondAnalytics:
    bond_id: str
    as_of: date
    settlement_date: date
    price_kind: str
    clean_price: float | None
    dirty_price: float | None
    accrued_interest: float | None
    ytm: float | None
    macaulay_duration: float | None
    modified_duration: float | None
    convexity: float | None
    g_spread_bps: float | None
    z_spread_bps: float | None
    oas_bps: None
    support_status: str
    analytics_version: str = ANALYTICS_VERSION
    detail: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        row = {k: getattr(self, k) for k in ("bond_id", "as_of", "settlement_date", "price_kind", "clean_price", "dirty_price", "accrued_interest", "ytm", "macaulay_duration", "modified_duration", "convexity", "g_spread_bps", "z_spread_bps", "oas_bps", "analytics_version", "support_status")}
        row["detail_json"] = dict(self.detail, oas_status=OAS_STATUS)
        return row


def settlement_from(as_of: date, settlement_days: int, *, holidays: Iterable[date] = ()) -> date:
    """T+n settlement skipping weekends and any supplied holidays (none are built in)."""
    skip = set(holidays)
    d = as_of
    added = 0
    while added < settlement_days:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in skip:
            added += 1
    while settlement_days == 0 and (d.weekday() >= 5 or d in skip):
        d += timedelta(days=1)
    return d


def _empty(terms: BondTerms, as_of: date, settlement: date, price_kind: str, clean: float | None, status: str, detail: dict[str, Any], *, dirty: float | None = None, accrued: float | None = None, ytm: float | None = None) -> BondAnalytics:
    return BondAnalytics(terms.bond_id, as_of, settlement, price_kind, clean, dirty, accrued, ytm, None, None, None, None, None, None, status, detail=detail)


def analyze_bond(
    terms: BondTerms,
    *,
    as_of: date,
    clean_price: float,
    price_kind: str = "MID",
    treasury_curve: Mapping[str, float | None] | None = None,
    holidays: Iterable[date] = (),
) -> BondAnalytics:
    """Full analytics for one bond; never returns a numeric value that failed verification."""
    terms.validate()
    holidays = tuple(holidays)
    settlement = settlement_from(as_of, terms.settlement_days, holidays=holidays)
    if settlement >= terms.maturity_date:
        raise BondAnalyticsError("settlement {0} is on/after maturity {1}".format(settlement, terms.maturity_date))
    detail: dict[str, Any] = {
        "day_count": terms.day_count,
        "coupon_frequency": terms.coupon_frequency,
        "yield_convention": "compounded at coupon frequency (street); zero coupons semiannual bond-equivalent",
        "price_per": 100,
        "settlement_calendar": "WEEKENDS_ONLY" if not holidays else "WEEKENDS_PLUS_{0}_HOLIDAYS".format(len(holidays)),
        "payment_date_adjustment": "IGNORED_STREET_CONVENTION",
        "yield_domain_pct": list(YIELD_DOMAIN_PCT),
        "z_spread_domain_bps": [Z_SPREAD_DOMAIN[0] * 1e4, Z_SPREAD_DOMAIN[1] * 1e4],
        "reprice_tolerance": REPRICE_TOL,
        "statuses": {"ytm": STATUS_NOT_COMPUTED, "durations": STATUS_NOT_COMPUTED, "g_spread": STATUS_NOT_COMPUTED, "z_spread": STATUS_NOT_COMPUTED},
    }
    if not _finite(clean_price):
        return _empty(terms, as_of, settlement, price_kind, None, SUPPORT_UNSUPPORTED_PRICE_DOMAIN, dict(detail, reason="clean price is not a finite number"))
    if terms.coupon_type not in ("FIXED", "ZERO"):
        return _empty(terms, as_of, settlement, price_kind, clean_price, SUPPORT_UNSUPPORTED_FLOATING, dict(detail, reason="coupon_type {0} requires a floating-rate model".format(terms.coupon_type)))
    try:
        sched = build_schedule(terms, settlement)
    except BondAnalyticsError as exc:
        return _empty(terms, as_of, settlement, price_kind, clean_price, SUPPORT_UNSUPPORTED_TERMS, dict(detail, reason=str(exc)))
    detail["schedule"] = {"stub": sched.stub, "period_start": sched.period_start.isoformat(), "next_payment": sched.period_end.isoformat(), "payments_remaining": len(sched.payment_dates), "end_of_month": bool(terms.end_of_month)}
    accrued = accrued_interest(terms, settlement)
    dirty = clean_price + accrued
    if dirty <= 0:
        return _empty(terms, as_of, settlement, price_kind, clean_price, SUPPORT_UNSUPPORTED_PRICE_DOMAIN, dict(detail, reason="dirty price {0:.6f} is not positive".format(dirty)), accrued=accrued)
    try:
        ytm = yield_from_price(terms, settlement, dirty)
    except OutOfDomain as exc:
        detail["statuses"]["ytm"] = STATUS_OUT_OF_DOMAIN
        return _empty(terms, as_of, settlement, price_kind, clean_price, SUPPORT_UNSUPPORTED_PRICE_DOMAIN, dict(detail, reason=str(exc)), dirty=dirty, accrued=accrued)
    except NoConvergence as exc:
        detail["statuses"]["ytm"] = STATUS_NO_CONVERGENCE
        return _empty(terms, as_of, settlement, price_kind, clean_price, SUPPORT_NUMERICAL_FAILURE, dict(detail, reason=str(exc)), dirty=dirty, accrued=accrued)
    detail["statuses"]["ytm"] = STATUS_OK
    detail["repriced_dirty"] = price_from_yield(terms, settlement, ytm)
    if terms.callable or terms.putable:
        detail["reason"] = "embedded option: duration/convexity/spreads need an option model"
        return _empty(terms, as_of, settlement, price_kind, clean_price, SUPPORT_YTM_ONLY_CALLABLE, detail, dirty=dirty, accrued=accrued, ytm=ytm)
    mac, mod, conv = durations(terms, settlement, ytm, dirty)
    detail["statuses"]["durations"] = STATUS_OK
    years = (terms.maturity_date - settlement).days / 365.25
    horizon = max(years, max(t for t, _ in cashflows(terms, settlement)))
    g_spread = None
    z_spread = None
    if not treasury_curve:
        detail["statuses"]["g_spread"] = STATUS_NO_CURVE
        detail["statuses"]["z_spread"] = STATUS_NO_CURVE
        detail["spreads"] = "no treasury curve supplied"
    else:
        coverage = curve_coverage(treasury_curve, horizon)
        detail["curve_coverage"] = coverage
        par = interpolate_par_yield(treasury_curve, years)
        if par is not None:
            g_spread = (ytm - par) * 100.0
            detail["g_spread_benchmark_pct"] = par
            detail["g_spread_method"] = "linear interpolation of FRED par curve at {0:.2f}y".format(years)
            detail["statuses"]["g_spread"] = STATUS_OK
        else:
            detail["statuses"]["g_spread"] = STATUS_CURVE_COVERAGE
        if coverage["status"] != STATUS_OK:
            detail["statuses"]["z_spread"] = coverage["status"]
            detail["z_spread_error"] = coverage.get("reason")
        else:
            try:
                zeros = bootstrap_zero_curve(treasury_curve, max_years=max(0.5, horizon))
                z_spread = z_spread_bps(terms, settlement, dirty, zeros)
                detail["z_spread_method"] = "semiannual bootstrap of linearly interpolated par curve; constant spread re-prices the dirty price"
                detail["statuses"]["z_spread"] = STATUS_OK
            except OutOfDomain as exc:
                detail["statuses"]["z_spread"] = STATUS_OUT_OF_DOMAIN
                detail["z_spread_error"] = str(exc)
            except BondAnalyticsError as exc:
                detail["statuses"]["z_spread"] = STATUS_NO_CONVERGENCE if isinstance(exc, NoConvergence) else STATUS_CURVE_COVERAGE
                detail["z_spread_error"] = str(exc)
    support = SUPPORT_FULL if (g_spread is not None and z_spread is not None) else SUPPORT_PARTIAL
    return BondAnalytics(terms.bond_id, as_of, settlement, price_kind, clean_price, dirty, accrued, ytm, mac, mod, conv, g_spread, z_spread, None, support, detail=detail)


def terms_from_row(row: Mapping[str, Any]) -> BondTerms:
    """Build terms from a mi_bond_securities row; refuses ticker-inferred or missing terms."""
    if not row.get("maturity_date") or row.get("coupon_frequency") is None:
        raise BondAnalyticsError("bond {0}: terms incomplete (maturity_date/coupon_frequency); terms_status {1}".format(row.get("bond_id"), row.get("terms_status")))
    if str(row.get("terms_status") or "UNVERIFIED") not in {"VERIFIED", "SOURCE_PROVIDED"}:
        raise BondAnalyticsError("bond {0}: terms_status {1} is not source-verified".format(row.get("bond_id"), row.get("terms_status")))
    bdc = str(row.get("business_day_convention") or "").upper().replace(" ", "_")
    return BondTerms(
        bond_id=str(row["bond_id"]),
        coupon_rate_pct=float(row["coupon_rate"]) if row.get("coupon_rate") is not None else None,
        coupon_frequency=int(row["coupon_frequency"]),
        maturity_date=row["maturity_date"],
        coupon_type=str(row.get("coupon_type") or "FIXED"),
        day_count=str(row.get("day_count") or "30/360"),
        redemption=float(row.get("redemption") or 100.0),
        issue_date=row.get("issue_date"),
        callable=bool(row.get("callable")),
        putable=bool(row.get("putable")),
        settlement_days=int(row.get("settlement_days") or 1),
        first_coupon_date=row.get("first_coupon_date"),
        end_of_month=bdc in {"END_OF_MONTH", "EOM"},
    )


def upsert_bond_analytics(conn, rows: Iterable[BondAnalytics]) -> int:
    from sqlalchemy import text

    from market_intelligence.nulls import strict_dumps

    count = 0
    for item in rows:
        row = item.as_row()
        row["detail_json"] = strict_dumps(row["detail_json"])
        conn.execute(
            text(
                """
                INSERT INTO mi_bond_analytics (bond_id, as_of, settlement_date, price_kind, clean_price, dirty_price, accrued_interest, ytm,
                    macaulay_duration, modified_duration, convexity, g_spread_bps, z_spread_bps, oas_bps, analytics_version, support_status, detail_json)
                VALUES (:bond_id, :as_of, :settlement_date, :price_kind, :clean_price, :dirty_price, :accrued_interest, :ytm,
                    :macaulay_duration, :modified_duration, :convexity, :g_spread_bps, :z_spread_bps, :oas_bps, :analytics_version, :support_status, CAST(:detail_json AS JSONB))
                ON CONFLICT (bond_id, as_of, price_kind, analytics_version) DO UPDATE SET
                    settlement_date = EXCLUDED.settlement_date, clean_price = EXCLUDED.clean_price, dirty_price = EXCLUDED.dirty_price,
                    accrued_interest = EXCLUDED.accrued_interest, ytm = EXCLUDED.ytm, macaulay_duration = EXCLUDED.macaulay_duration,
                    modified_duration = EXCLUDED.modified_duration, convexity = EXCLUDED.convexity, g_spread_bps = EXCLUDED.g_spread_bps,
                    z_spread_bps = EXCLUDED.z_spread_bps, oas_bps = EXCLUDED.oas_bps, support_status = EXCLUDED.support_status,
                    detail_json = EXCLUDED.detail_json, computed_at = NOW()
                """
            ),
            row,
        )
        count += 1
    return count


@dataclass
class BondBatchReport:
    as_of: date
    curve_date: date | None
    curve_points: int
    computed: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)
    support: dict[str, int] = field(default_factory=dict)
    curve_tenors: list[str] = field(default_factory=list)
    curve_missing_tenors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "curve_date": self.curve_date,
            "curve_points": self.curve_points,
            "curve_tenors": list(self.curve_tenors),
            "curve_missing_tenors": list(self.curve_missing_tenors),
            "computed": self.computed,
            "skipped": list(self.skipped),
            "support": dict(self.support),
            "analytics_version": ANALYTICS_VERSION,
        }


QUOTE_MAX_AGE_DAYS = 7


def treasury_curve_from_store(conn, as_of: date) -> tuple[dict[str, float], date | None]:
    """Latest current FRED par yields on or before ``as_of`` (one common observation date)."""
    from market_intelligence.store import current_observations

    latest_by_series: dict[str, tuple[date, float]] = {}
    for series_id in TREASURY_TENORS_YEARS:
        obs = current_observations(conn, series_id, start=as_of - timedelta(days=14), end=as_of)
        dated = [(d, float(v)) for d, v in obs.items() if v is not None]
        if dated:
            latest_by_series[series_id] = max(dated)
    if not latest_by_series:
        return {}, None
    curve_date = max(d for d, _ in latest_by_series.values())
    curve = {sid: v for sid, (d, v) in latest_by_series.items() if d == curve_date}
    return curve, curve_date


def compute_stored_bond_analytics(conn, *, as_of: date, price_kind: str = "MID") -> BondBatchReport:
    """Analyse every bond with source-verified terms and a quote on ``as_of``; skip (and report) the rest.

    The stored ``price_kind`` is the kind actually used: a MID request served from ``last_price``
    because bid/ask are missing is stored as LAST, never labelled MID.
    """
    from sqlalchemy import text

    curve, curve_date = treasury_curve_from_store(conn, as_of)
    report = BondBatchReport(as_of=as_of, curve_date=curve_date, curve_points=len(curve), curve_tenors=[k for k in TREASURY_TENORS_YEARS if k in curve], curve_missing_tenors=[k for k in TREASURY_TENORS_YEARS if k not in curve])
    rows = conn.execute(
        text(
            """
            SELECT s.*, q.bid_price, q.ask_price, q.last_price, q.quote_ts
            FROM mi_bond_securities s
            LEFT JOIN LATERAL (
                SELECT bid_price, ask_price, last_price, quote_ts FROM mi_bond_quotes
                WHERE bond_id = s.bond_id AND quote_ts::date <= :as_of AND quote_ts::date >= :as_of - :max_age
                ORDER BY quote_ts DESC LIMIT 1
            ) q ON TRUE
            ORDER BY s.bond_id
            """
        ),
        {"as_of": as_of, "max_age": QUOTE_MAX_AGE_DAYS},
    ).mappings().all()
    results: list[BondAnalytics] = []
    for row in rows:
        bond_id = str(row["bond_id"])
        if row["quote_ts"] is None:
            report.skipped.append({"bond_id": bond_id, "reason": "no quote within {0} days of as_of".format(QUOTE_MAX_AGE_DAYS)})
            continue
        clean, actual_kind = _clean_price(row, price_kind)
        if clean is None:
            report.skipped.append({"bond_id": bond_id, "reason": "quote lacks {0} price".format(price_kind)})
            continue
        try:
            terms = terms_from_row(row)
            result = analyze_bond(terms, as_of=as_of, clean_price=clean, price_kind=actual_kind, treasury_curve=curve or None)
        except BondAnalyticsError as exc:
            report.skipped.append({"bond_id": bond_id, "reason": str(exc)})
            continue
        quote_date = row["quote_ts"].date()
        result.detail["quote_ts"] = row["quote_ts"].isoformat()
        result.detail["quote_age_days"] = (as_of - quote_date).days
        result.detail["quote_status"] = "SAME_DAY" if quote_date == as_of else "STALE_{0}D".format((as_of - quote_date).days)
        result.detail["requested_price_kind"] = price_kind
        result.detail["price_kind_fallback"] = actual_kind != price_kind
        result.detail["curve_date"] = curve_date.isoformat() if curve_date else None
        result.detail["curve_age_days"] = (as_of - curve_date).days if curve_date else None
        results.append(result)
        report.support[result.support_status] = report.support.get(result.support_status, 0) + 1
    report.computed = upsert_bond_analytics(conn, results)
    return report


def _clean_price(row: Mapping[str, Any], price_kind: str) -> tuple[float | None, str]:
    """(clean price, price kind actually used). MID falls back to LAST only with that label."""
    bid, ask, last = row.get("bid_price"), row.get("ask_price"), row.get("last_price")
    if price_kind == "MID":
        if bid is not None and ask is not None:
            return (float(bid) + float(ask)) / 2.0, "MID"
        return (float(last), "LAST") if last is not None else (None, "MID")
    if price_kind == "BID":
        return (float(bid), "BID") if bid is not None else (None, "BID")
    if price_kind == "ASK":
        return (float(ask), "ASK") if ask is not None else (None, "ASK")
    if price_kind == "LAST":
        return (float(last), "LAST") if last is not None else (None, "LAST")
    raise BondAnalyticsError("unknown price_kind {0}".format(price_kind))


__all__ = [
    "ANALYTICS_VERSION",
    "BondAnalytics",
    "BondAnalyticsError",
    "BondBatchReport",
    "BondTerms",
    "MIN_CURVE_TENORS",
    "NoConvergence",
    "OAS_STATUS",
    "OutOfDomain",
    "REPRICE_TOL",
    "SUPPORT_FULL",
    "SUPPORT_NUMERICAL_FAILURE",
    "SUPPORT_PARTIAL",
    "SUPPORT_UNSUPPORTED_FLOATING",
    "SUPPORT_UNSUPPORTED_PRICE_DOMAIN",
    "SUPPORT_UNSUPPORTED_TERMS",
    "SUPPORT_YTM_ONLY_CALLABLE",
    "Schedule",
    "YIELD_DOMAIN_PCT",
    "Z_SPREAD_DOMAIN",
    "accrued_interest",
    "analyze_bond",
    "bootstrap_zero_curve",
    "build_schedule",
    "cashflows",
    "compute_stored_bond_analytics",
    "coupon_dates",
    "curve_coverage",
    "durations",
    "interpolate_par_yield",
    "previous_coupon_date",
    "price_from_yield",
    "on_regular_schedule",
    "regular_dates",
    "settlement_from",
    "terms_from_row",
    "treasury_curve_from_store",
    "upsert_bond_analytics",
    "yield_from_price",
    "z_spread_bps",
]
