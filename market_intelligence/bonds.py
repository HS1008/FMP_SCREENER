"""Local bond analytics (bounded domain): fixed-coupon bullet bonds only.

Inputs are explicit security terms from a source (never inferred from ticker strings) plus a
clean price per 100. Outputs: accrued interest, dirty price, street-convention yield to
maturity, Macaulay/modified duration, convexity, G-spread vs an interpolated par Treasury
curve, and Z-spread vs a bootstrapped semiannual zero curve. OAS is UNSUPPORTED (no option
model); callable/putable bonds get YTM only; floating/step coupons are UNSUPPORTED.

Conventions: coupon schedule rolled backward from maturity; day counts 30/360 (US), ACT/ACT
(ICMA), ACT/360, ACT/365F; yields compounded at the coupon frequency; prices per 100.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Mapping

ANALYTICS_VERSION = "bond_analytics_v1"
SUPPORT_FULL = "FULL"
SUPPORT_YTM_ONLY_CALLABLE = "YTM_ONLY_CALLABLE"
SUPPORT_UNSUPPORTED_FLOATING = "UNSUPPORTED_FLOATING"
SUPPORT_UNSUPPORTED_TERMS = "UNSUPPORTED_TERMS"
OAS_STATUS = "UNSUPPORTED_NO_OPTION_MODEL"
DAY_COUNTS = ("30/360", "ACT/ACT", "ACT/360", "ACT/365F")
TREASURY_TENORS_YEARS: dict[str, float] = {"DGS1MO": 1 / 12, "DGS3MO": 0.25, "DGS6MO": 0.5, "DGS1": 1.0, "DGS2": 2.0, "DGS3": 3.0, "DGS5": 5.0, "DGS7": 7.0, "DGS10": 10.0, "DGS20": 20.0, "DGS30": 30.0}


class BondAnalyticsError(ValueError):
    """Terms are inconsistent or the computation cannot be performed honestly."""


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

    def validate(self) -> None:
        if self.coupon_frequency not in (1, 2, 4, 12):
            raise BondAnalyticsError("coupon_frequency must be 1, 2, 4 or 12 (got {0})".format(self.coupon_frequency))
        if self.day_count not in DAY_COUNTS:
            raise BondAnalyticsError("unsupported day_count {0}; supported {1}".format(self.day_count, DAY_COUNTS))
        if self.redemption <= 0:
            raise BondAnalyticsError("redemption must be positive")
        if self.coupon_type == "FIXED" and self.coupon_rate_pct is None:
            raise BondAnalyticsError("fixed coupon requires coupon_rate_pct")
        if self.coupon_type == "ZERO" and (self.coupon_rate_pct or 0) != 0:
            raise BondAnalyticsError("zero coupon bond cannot carry a coupon rate")
        if self.issue_date is not None and self.issue_date >= self.maturity_date:
            raise BondAnalyticsError("issue_date must precede maturity_date")


def add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    last_day = (date(year + (month // 12), month % 12 + 1, 1) - timedelta(days=1)).day if month != 12 else 31
    return date(year, month, min(d.day, last_day))


def coupon_dates(terms: BondTerms, settlement: date) -> list[date]:
    """All coupon dates strictly after settlement, rolled backward from maturity."""
    if terms.coupon_type == "ZERO":
        return [terms.maturity_date]
    step = 12 // terms.coupon_frequency
    dates = [terms.maturity_date]
    current = terms.maturity_date
    while True:
        previous = add_months(terms.maturity_date, -step * len(dates))
        if previous <= settlement:
            break
        if terms.issue_date is not None and previous <= terms.issue_date:
            break
        dates.append(previous)
        current = previous
        if len(dates) > 12 * 100:
            raise BondAnalyticsError("coupon schedule too long")
    _ = current
    return sorted(dates)


def previous_coupon_date(terms: BondTerms, settlement: date) -> date:
    step = 12 // terms.coupon_frequency
    n = 0
    while True:
        d = add_months(terms.maturity_date, -step * n)
        if d <= settlement:
            return d
        n += 1
        if n > 12 * 100:
            raise BondAnalyticsError("cannot locate previous coupon date")


def _days_30_360(start: date, end: date) -> int:
    d1, d2 = min(start.day, 30), end.day
    if d1 == 30 and d2 == 31:
        d2 = 30
    return 360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1)


def accrual_fraction(terms: BondTerms, start: date, settlement: date, end: date) -> float:
    """Fraction of the coupon period accrued at settlement, by day count."""
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
    coupon = terms.coupon_rate_pct / terms.coupon_frequency
    start = previous_coupon_date(terms, settlement)
    end = add_months(start, 12 // terms.coupon_frequency)
    return coupon * accrual_fraction(terms, start, settlement, end)


def cashflows(terms: BondTerms, settlement: date) -> list[tuple[float, float]]:
    """(time in years from settlement measured in coupon periods / frequency, amount per 100)."""
    dates = coupon_dates(terms, settlement)
    coupon = (terms.coupon_rate_pct or 0.0) / terms.coupon_frequency
    start = previous_coupon_date(terms, settlement) if terms.coupon_type != "ZERO" else None
    flows: list[tuple[float, float]] = []
    if terms.coupon_type == "ZERO":
        t = (terms.maturity_date - settlement).days / 365.0
        return [(t, terms.redemption)]
    first_end = dates[0]
    w = 1.0 - accrual_fraction(terms, start, settlement, first_end)  # fraction of first period remaining
    for i, d in enumerate(dates):
        periods = w + i
        amount = coupon + (terms.redemption if d == terms.maturity_date else 0.0)
        flows.append((periods / terms.coupon_frequency, amount))
    return flows


def price_from_yield(terms: BondTerms, settlement: date, ytm_pct: float) -> float:
    """Dirty price per 100 for a yield compounded at the coupon frequency."""
    f = terms.coupon_frequency if terms.coupon_type != "ZERO" else 2
    y = ytm_pct / 100.0 / f
    return sum(amount / (1 + y) ** (t * f) for t, amount in cashflows(terms, settlement))


def yield_from_price(terms: BondTerms, settlement: date, dirty_price: float, *, tol: float = 1e-10) -> float:
    if dirty_price <= 0:
        raise BondAnalyticsError("dirty price must be positive")
    lo, hi = -50.0, 200.0
    f_lo = price_from_yield(terms, settlement, lo) - dirty_price
    for _ in range(300):
        mid = (lo + hi) / 2
        f_mid = price_from_yield(terms, settlement, mid) - dirty_price
        if abs(f_mid) < tol or (hi - lo) < 1e-12:
            return mid
        if (f_lo > 0) == (f_mid > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return (lo + hi) / 2


def durations(terms: BondTerms, settlement: date, ytm_pct: float, dirty_price: float) -> tuple[float, float, float]:
    """Macaulay (years), modified (years), convexity (years^2) from discounted cashflows."""
    f = terms.coupon_frequency if terms.coupon_type != "ZERO" else 2
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

def interpolate_par_yield(curve: Mapping[str, float | None], years: float) -> float | None:
    """Linear interpolation of a par Treasury curve keyed by FRED series id; None outside the range."""
    points = sorted((TREASURY_TENORS_YEARS[k], float(v)) for k, v in curve.items() if k in TREASURY_TENORS_YEARS and v is not None)
    if len(points) < 2 or years < points[0][0] or years > points[-1][0]:
        return None
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= years <= x1:
            return y0 + (y1 - y0) * (years - x0) / (x1 - x0) if x1 != x0 else y0
    return None


def bootstrap_zero_curve(curve: Mapping[str, float | None], *, max_years: float = 30.0) -> list[tuple[float, float]]:
    """Semiannual bootstrap from linearly interpolated par yields -> [(t_years, zero_rate_pct_semiannual)]."""
    points = sorted((TREASURY_TENORS_YEARS[k], float(v)) for k, v in curve.items() if k in TREASURY_TENORS_YEARS and v is not None)
    if len(points) < 2:
        raise BondAnalyticsError("treasury curve needs at least two tenors to bootstrap")
    zeros: list[tuple[float, float]] = []
    dfs: list[float] = []
    n = int(round(max_years * 2))
    for i in range(1, n + 1):
        t = i / 2.0
        # Every semiannual period must carry a discount factor, so tenors outside the quoted
        # range use flat extrapolation of the nearest quoted par yield.
        par = interpolate_par_yield(curve, t)
        if par is None:
            par = points[0][1] if t < points[0][0] else points[-1][1]
        c = par / 100.0 / 2.0
        pv_coupons = sum(c * df for df in dfs)
        df = (1.0 - pv_coupons) / (1.0 + c)
        if df <= 0:
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
    return zeros[-1][1]


def z_spread_bps(terms: BondTerms, settlement: date, dirty_price: float, zeros: list[tuple[float, float]]) -> float:
    flows = cashflows(terms, settlement)

    def pv(spread: float) -> float:
        total = 0.0
        for t, amount in flows:
            r = _zero_rate_at(zeros, t) / 100.0 + spread
            total += amount / (1 + r / 2.0) ** (2.0 * t)
        return total

    lo, hi = -0.5, 1.0
    f_lo = pv(lo) - dirty_price
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = pv(mid) - dirty_price
        if abs(f_mid) < 1e-10:
            break
        if (f_lo > 0) == (f_mid > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return mid * 10000.0


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


def settlement_from(as_of: date, settlement_days: int) -> date:
    d = as_of
    added = 0
    while added < settlement_days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def analyze_bond(terms: BondTerms, *, as_of: date, clean_price: float, price_kind: str = "MID", treasury_curve: Mapping[str, float | None] | None = None) -> BondAnalytics:
    terms.validate()
    settlement = settlement_from(as_of, terms.settlement_days)
    if settlement >= terms.maturity_date:
        raise BondAnalyticsError("settlement {0} is on/after maturity {1}".format(settlement, terms.maturity_date))
    detail: dict[str, Any] = {"day_count": terms.day_count, "coupon_frequency": terms.coupon_frequency, "yield_convention": "compounded at coupon frequency (street)", "price_per": 100}
    if terms.coupon_type not in ("FIXED", "ZERO"):
        return BondAnalytics(terms.bond_id, as_of, settlement, price_kind, clean_price, None, None, None, None, None, None, None, None, None, SUPPORT_UNSUPPORTED_FLOATING, detail=dict(detail, reason="coupon_type {0} requires a floating-rate model".format(terms.coupon_type)))
    accrued = accrued_interest(terms, settlement)
    dirty = clean_price + accrued
    ytm = yield_from_price(terms, settlement, dirty)
    if terms.callable or terms.putable:
        return BondAnalytics(terms.bond_id, as_of, settlement, price_kind, clean_price, dirty, accrued, ytm, None, None, None, None, None, None, SUPPORT_YTM_ONLY_CALLABLE, detail=dict(detail, reason="embedded option: duration/convexity/spreads need an option model"))
    mac, mod, conv = durations(terms, settlement, ytm, dirty)
    years = (terms.maturity_date - settlement).days / 365.25
    g_spread = None
    z_spread = None
    if treasury_curve:
        par = interpolate_par_yield(treasury_curve, years)
        if par is not None:
            g_spread = (ytm - par) * 100.0
            detail["g_spread_benchmark_pct"] = par
            detail["g_spread_method"] = "linear interpolation of FRED par curve at {0:.2f}y".format(years)
        try:
            zeros = bootstrap_zero_curve(treasury_curve, max_years=max(1.0, math.ceil(years)))
            z_spread = z_spread_bps(terms, settlement, dirty, zeros)
            detail["z_spread_method"] = "semiannual bootstrap of interpolated par curve"
        except BondAnalyticsError as exc:
            detail["z_spread_error"] = str(exc)
    else:
        detail["spreads"] = "no treasury curve supplied"
    return BondAnalytics(terms.bond_id, as_of, settlement, price_kind, clean_price, dirty, accrued, ytm, mac, mod, conv, g_spread, z_spread, None, SUPPORT_FULL, detail=detail)


def terms_from_row(row: Mapping[str, Any]) -> BondTerms:
    """Build terms from a mi_bond_securities row; refuses ticker-inferred or missing terms."""
    if not row.get("maturity_date") or row.get("coupon_frequency") is None:
        raise BondAnalyticsError("bond {0}: terms incomplete (maturity_date/coupon_frequency); terms_status {1}".format(row.get("bond_id"), row.get("terms_status")))
    if str(row.get("terms_status") or "UNVERIFIED") not in {"VERIFIED", "SOURCE_PROVIDED"}:
        raise BondAnalyticsError("bond {0}: terms_status {1} is not source-verified".format(row.get("bond_id"), row.get("terms_status")))
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

    def as_dict(self) -> dict[str, Any]:
        return {"as_of": self.as_of, "curve_date": self.curve_date, "curve_points": self.curve_points, "computed": self.computed, "skipped": list(self.skipped), "support": dict(self.support), "analytics_version": ANALYTICS_VERSION}


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
    """Analyse every bond with source-verified terms and a quote on ``as_of``; skip (and report) the rest."""
    from sqlalchemy import text

    curve, curve_date = treasury_curve_from_store(conn, as_of)
    report = BondBatchReport(as_of=as_of, curve_date=curve_date, curve_points=len(curve))
    rows = conn.execute(
        text(
            """
            SELECT s.*, q.bid_price, q.ask_price, q.last_price, q.quote_ts
            FROM mi_bond_securities s
            LEFT JOIN LATERAL (
                SELECT bid_price, ask_price, last_price, quote_ts FROM mi_bond_quotes
                WHERE bond_id = s.bond_id AND quote_ts::date <= :as_of AND quote_ts::date >= :as_of - 7
                ORDER BY quote_ts DESC LIMIT 1
            ) q ON TRUE
            ORDER BY s.bond_id
            """
        ),
        {"as_of": as_of},
    ).mappings().all()
    results: list[BondAnalytics] = []
    for row in rows:
        bond_id = str(row["bond_id"])
        if row["quote_ts"] is None:
            report.skipped.append({"bond_id": bond_id, "reason": "no quote within 7 days of as_of"})
            continue
        clean = _clean_price(row, price_kind)
        if clean is None:
            report.skipped.append({"bond_id": bond_id, "reason": "quote lacks {0} price".format(price_kind)})
            continue
        try:
            terms = terms_from_row(row)
            result = analyze_bond(terms, as_of=as_of, clean_price=clean, price_kind=price_kind, treasury_curve=curve or None)
        except BondAnalyticsError as exc:
            report.skipped.append({"bond_id": bond_id, "reason": str(exc)})
            continue
        result.detail["quote_ts"] = row["quote_ts"].isoformat()
        result.detail["curve_date"] = curve_date.isoformat() if curve_date else None
        results.append(result)
        report.support[result.support_status] = report.support.get(result.support_status, 0) + 1
    report.computed = upsert_bond_analytics(conn, results)
    return report


def _clean_price(row: Mapping[str, Any], price_kind: str) -> float | None:
    bid, ask, last = row.get("bid_price"), row.get("ask_price"), row.get("last_price")
    if price_kind == "MID":
        if bid is not None and ask is not None:
            return (float(bid) + float(ask)) / 2.0
        return float(last) if last is not None else None
    if price_kind == "BID":
        return float(bid) if bid is not None else None
    if price_kind == "ASK":
        return float(ask) if ask is not None else None
    if price_kind == "LAST":
        return float(last) if last is not None else None
    raise BondAnalyticsError("unknown price_kind {0}".format(price_kind))


__all__ = [
    "ANALYTICS_VERSION",
    "BondBatchReport",
    "compute_stored_bond_analytics",
    "treasury_curve_from_store",
    "BondAnalytics",
    "BondAnalyticsError",
    "BondTerms",
    "OAS_STATUS",
    "SUPPORT_FULL",
    "SUPPORT_UNSUPPORTED_FLOATING",
    "SUPPORT_YTM_ONLY_CALLABLE",
    "accrued_interest",
    "analyze_bond",
    "bootstrap_zero_curve",
    "cashflows",
    "coupon_dates",
    "durations",
    "interpolate_par_yield",
    "price_from_yield",
    "terms_from_row",
    "upsert_bond_analytics",
    "yield_from_price",
    "z_spread_bps",
]
