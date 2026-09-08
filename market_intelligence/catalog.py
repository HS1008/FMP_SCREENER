"""Versioned FRED series catalog with expected metadata, cadence, and export scope.

Provider metadata is validated against the catalog on every ingestion. Anything other than
``VALIDATED`` (units, frequency, seasonal adjustment, series identity, or missing metadata)
is a *publication gate*: observations retrieved under failed validation are retained in the
quarantine table for diagnosis but are never promoted to current observations or metrics.
Catalog units describe provider-native raw values; any display conversion is an explicit,
versioned transform (``display_divisor``) that keeps the original units in provenance.

``fred_catalog_v2`` corrects WTREGEN / WRESBAL (millions of USD, week averages ending
Wednesday; v1 wrongly said billions / Wednesday level) and adds per-series aggregation
semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

CATALOG_VERSION = "fred_catalog_v2"
FRED_SOURCE_ID = "FRED"
FRED_SERIES_URL = "https://fred.stlouisfed.org/series/{series_id}"
FRED_API_CONTRACT_URL = "https://fred.stlouisfed.org/docs/api/fred/series_observations.html"
FRED_ATTRIBUTION = (
    "This product uses the FRED\u00ae API but is not endorsed or certified by the "
    "Federal Reserve Bank of St. Louis."
)
ICE_ATTRIBUTION = (
    "Ice Data Indices, LLC, retrieved from FRED, Federal Reserve Bank of St. Louis. "
    "Third-party redistribution restricted by the source terms; history availability "
    "is limited by the provider."
)

# Export scopes
EXPORT_ATTRIBUTION_REQUIRED = "ATTRIBUTION_REQUIRED"
EXPORT_RESTRICTED = "RESTRICTED_REDISTRIBUTION"
EXPORT_INTERNAL_ONLY = "INTERNAL_ONLY"

# Cadence codes (FRED frequency_short): D, W, BW, M, Q, SA, A
CADENCE_DAILY = "D"
CADENCE_WEEKLY = "W"
CADENCE_MONTHLY = "M"
CADENCE_QUARTERLY = "Q"

# Aggregation semantics (how one observation relates to its period).
AGG_POINT = "point_observation"  # daily/period-end reading
AGG_PERIOD_AVERAGE = "period_average"  # monthly average of daily data etc.
AGG_WEEK_AVG_WED = "week_average_ending_wednesday"  # H.4.1 week averages (WTREGEN, WRESBAL)
AGG_WED_LEVEL = "wednesday_level"  # H.4.1 Wednesday level (WALCL)
AGG_WEEK_END_SAT = "week_ending_saturday"  # DOL claims
AGG_PERIOD_TOTAL = "period_total"  # flows over the period (retail sales, GDP SAAR)
AGG_PERIOD_LEVEL = "period_level"  # end/average level for the month (M2, payrolls, indexes)

# Metadata publication statuses.
META_VALIDATED = "VALIDATED"
META_MISMATCH = "MISMATCH"
META_UNAVAILABLE = "UNAVAILABLE"
META_IDENTITY_MISMATCH = "IDENTITY_MISMATCH"

# Fields the provider must supply before a series may be published.
REQUIRED_METADATA_FIELDS = ("id", "units", "frequency_short")


@dataclass(frozen=True)
class SeriesSpec:
    series_id: str
    category: str
    subcategory: str
    expected_frequency: str
    expected_units_contains: tuple[str, ...]
    expected_sa: str | None = None  # 'SA', 'NSA', or None to skip the check
    value_kind: str = "level"  # level | percent | index | count | balance
    export_scope: str = EXPORT_ATTRIBUTION_REQUIRED
    attribution: str = FRED_ATTRIBUTION
    notes: str = ""
    transforms: tuple[str, ...] = field(default_factory=tuple)
    backfill_years: int = 12
    label: str = ""
    aggregation: str = AGG_POINT
    # Explicit, versioned display conversion. Raw values keep provider units; when set, an
    # extra ``level_display`` metric = raw / display_divisor with ``display_units`` is emitted.
    display_divisor: float | None = None
    display_units: str | None = None

    @property
    def source_url(self) -> str:
        return FRED_SERIES_URL.format(series_id=self.series_id)

    @property
    def internal_series_id(self) -> str:
        return self.series_id

    @property
    def raw_units(self) -> str:
        """Provider-native units label used for stored levels (e.g. ``millions_usd``)."""
        if self.value_kind == "percent":
            return "pct"
        if self.value_kind == "index":
            return "index"
        if self.value_kind == "count":
            return self.expected_units_contains[0] if self.expected_units_contains else "count"
        if self.value_kind == "balance":
            return "{0}_usd".format(self.expected_units_contains[0]) if self.expected_units_contains else "usd"
        return self.expected_units_contains[0] if self.expected_units_contains else "level"


def _s(series_id: str, category: str, subcategory: str, freq: str, units: Iterable[str], **kw) -> SeriesSpec:
    return SeriesSpec(
        series_id=series_id,
        category=category,
        subcategory=subcategory,
        expected_frequency=freq,
        expected_units_contains=tuple(units),
        **kw,
    )


_PCT = ("percent",)
_IDX = ("index",)
_BLN = ("billions",)
_MLN = ("millions",)
_THOUS = ("thousands",)
_NUM = ("number",)

CATALOG: tuple[SeriesSpec, ...] = (
    # Growth
    _s("GDPC1", "growth", "output", CADENCE_QUARTERLY, _BLN, expected_sa="SAAR", value_kind="level",
       transforms=("qoq_annualized_pct", "yoy_pct"), label="Real GDP (chained 2017 $)",
       aggregation=AGG_PERIOD_TOTAL,
       notes="Billions of chained 2017 dollars, quarterly, seasonally adjusted annual rate. Growth uses quarterly conventions."),
    _s("INDPRO", "growth", "production", CADENCE_MONTHLY, _IDX, expected_sa="SA", value_kind="index",
       transforms=("yoy_pct", "mom_pct"), label="Industrial Production", aggregation=AGG_PERIOD_LEVEL,
       notes="Index 2017=100, monthly, SA."),
    _s("RSAFS", "growth", "consumption", CADENCE_MONTHLY, _MLN, expected_sa="SA", value_kind="level",
       transforms=("yoy_pct", "mom_pct"), label="Retail Sales (adv.)", aggregation=AGG_PERIOD_TOTAL,
       notes="Millions of dollars, monthly total, SA."),
    # Labor
    _s("PAYEMS", "labor", "employment", CADENCE_MONTHLY, _THOUS, expected_sa="SA", value_kind="count",
       transforms=("mom_change", "yoy_pct"), label="Nonfarm Payrolls", aggregation=AGG_PERIOD_LEVEL,
       notes="Thousands of persons, monthly, SA. Net additions are thousands of persons, not percent."),
    _s("UNRATE", "labor", "unemployment", CADENCE_MONTHLY, _PCT, expected_sa="SA", value_kind="percent",
       transforms=("mom_change_pp", "yoy_change_pp"), label="Unemployment Rate", aggregation=AGG_PERIOD_LEVEL,
       notes="Percent, monthly, SA. Changes are percentage points."),
    _s("ICSA", "labor", "claims", CADENCE_WEEKLY, _NUM, expected_sa="SA", value_kind="count",
       transforms=("wow_change", "avg_4w"), label="Initial Claims", aggregation=AGG_WEEK_END_SAT,
       notes="Number of claims, weekly (week ending Saturday), SA."),
    _s("CCSA", "labor", "claims", CADENCE_WEEKLY, _NUM, expected_sa="SA", value_kind="count",
       transforms=("wow_change", "avg_4w"), label="Continued Claims", aggregation=AGG_WEEK_END_SAT,
       notes="Number of claims, weekly (week ending Saturday), SA."),
    # Inflation
    _s("CPIAUCSL", "inflation", "cpi", CADENCE_MONTHLY, _IDX, expected_sa="SA", value_kind="index",
       transforms=("yoy_pct", "ann3m_pct", "ann6m_pct"), label="CPI (all items)", aggregation=AGG_PERIOD_LEVEL,
       notes="Index 1982-1984=100, monthly, SA."),
    _s("CPILFESL", "inflation", "cpi", CADENCE_MONTHLY, _IDX, expected_sa="SA", value_kind="index",
       transforms=("yoy_pct", "ann3m_pct", "ann6m_pct"), label="Core CPI", aggregation=AGG_PERIOD_LEVEL,
       notes="Index 1982-1984=100, monthly, SA."),
    _s("PCEPI", "inflation", "pce", CADENCE_MONTHLY, _IDX, expected_sa="SA", value_kind="index",
       transforms=("yoy_pct", "ann3m_pct", "ann6m_pct"), label="PCE Price Index", aggregation=AGG_PERIOD_LEVEL,
       notes="Index 2017=100, monthly, SA."),
    _s("PCEPILFE", "inflation", "pce", CADENCE_MONTHLY, _IDX, expected_sa="SA", value_kind="index",
       transforms=("yoy_pct", "ann3m_pct", "ann6m_pct"), label="Core PCE Price Index", aggregation=AGG_PERIOD_LEVEL,
       notes="Index 2017=100, monthly, SA."),
    # Policy
    _s("DFF", "policy", "fed_funds", CADENCE_DAILY, _PCT, expected_sa="NSA", value_kind="percent",
       transforms=("level_pct", "chg_bps"), label="Effective Fed Funds"),
    _s("SOFR", "policy", "sofr", CADENCE_DAILY, _PCT, expected_sa="NSA", value_kind="percent",
       transforms=("level_pct", "chg_bps"), label="SOFR"),
    # Nominal curve
    *[
        _s(sid, "rates", "nominal_curve", CADENCE_DAILY, _PCT, expected_sa="NSA", value_kind="percent",
           transforms=("level_pct", "chg_bps"), label=lbl)
        for sid, lbl in (
            ("DGS3MO", "3M Treasury"), ("DGS6MO", "6M Treasury"), ("DGS1", "1Y Treasury"),
            ("DGS2", "2Y Treasury"), ("DGS3", "3Y Treasury"), ("DGS5", "5Y Treasury"),
            ("DGS7", "7Y Treasury"), ("DGS10", "10Y Treasury"), ("DGS20", "20Y Treasury"),
            ("DGS30", "30Y Treasury"),
        )
    ],
    # Real yields
    *[
        _s(sid, "rates", "real_yield", CADENCE_DAILY, _PCT, expected_sa="NSA", value_kind="percent",
           transforms=("level_pct", "chg_bps"), label=lbl)
        for sid, lbl in (
            ("DFII5", "5Y TIPS real yield"), ("DFII10", "10Y TIPS real yield"),
            ("DFII20", "20Y TIPS real yield"), ("DFII30", "30Y TIPS real yield"),
        )
    ],
    # Inflation compensation (market-implied breakevens, NOT survey expectations)
    *[
        _s(sid, "rates", "inflation_compensation", CADENCE_DAILY, _PCT, expected_sa="NSA",
           value_kind="percent", transforms=("level_pct", "chg_bps"), label=lbl,
           notes="Market-implied inflation compensation (breakeven); not a survey expectation.")
        for sid, lbl in (
            ("T5YIE", "5Y breakeven inflation"), ("T10YIE", "10Y breakeven inflation"),
            ("T5YIFR", "5Y5Y forward inflation compensation"),
        )
    ],
    # Liquidity (balances differ in dating and scale; no composite score is built)
    _s("WALCL", "liquidity", "fed_balance_sheet", CADENCE_WEEKLY, _MLN, expected_sa="NSA",
       value_kind="balance", transforms=("wow_change", "chg_4w"), label="Fed total assets (Wednesday level)",
       aggregation=AGG_WED_LEVEL, display_divisor=1000.0, display_units="billions_usd",
       notes="Millions of USD (provider units), weekly Wednesday level, NSA. Display in billions is an explicit /1000 conversion."),
    _s("RRPONTSYD", "liquidity", "reverse_repo", CADENCE_DAILY, _BLN, expected_sa="NSA",
       value_kind="balance", transforms=("chg_1d", "chg_1w"), label="ON RRP (Treasury) usage",
       aggregation=AGG_POINT, notes="Billions of USD, daily, NSA."),
    _s("WTREGEN", "liquidity", "treasury_general_account", CADENCE_WEEKLY, _MLN, expected_sa="NSA",
       value_kind="balance", transforms=("wow_change", "chg_4w"), label="Treasury General Account (week average)",
       aggregation=AGG_WEEK_AVG_WED, display_divisor=1000.0, display_units="billions_usd",
       notes="Millions of USD (provider units), weekly average of daily figures for the week ending Wednesday, NSA. "
             "Not a Wednesday level. Display in billions is an explicit /1000 conversion."),
    _s("WRESBAL", "liquidity", "reserve_balances", CADENCE_WEEKLY, _MLN, expected_sa="NSA",
       value_kind="balance", transforms=("wow_change", "chg_4w"), label="Reserve balances (week average)",
       aggregation=AGG_WEEK_AVG_WED, display_divisor=1000.0, display_units="billions_usd",
       notes="Millions of USD (provider units), weekly average of daily figures for the week ending Wednesday, NSA. "
             "Display in billions is an explicit /1000 conversion."),
    _s("M2SL", "liquidity", "money_supply", CADENCE_MONTHLY, _BLN, expected_sa="SA",
       value_kind="balance", transforms=("yoy_pct", "mom_pct"), label="M2 money stock",
       aggregation=AGG_PERIOD_LEVEL, notes="Billions of USD, monthly, SA."),
    # Credit (ICE BofA OAS via FRED; restricted redistribution, limited history)
    *[
        _s(sid, "credit", bucket, CADENCE_DAILY, _PCT, expected_sa="NSA", value_kind="percent",
           export_scope=EXPORT_RESTRICTED, attribution=ICE_ATTRIBUTION, backfill_years=4,
           transforms=("oas_bps", "chg_bps", "pctile_available"), label=lbl,
           notes="Option-adjusted spread in percent (x100 = bps). Provider limits history; "
                 "distribution restricted by ICE terms.")
        for sid, bucket, lbl in (
            ("BAMLC0A0CM", "ig_broad", "US Corporate IG OAS"),
            ("BAMLH0A0HYM2", "hy_broad", "US High Yield OAS"),
            ("BAMLC0A1CAAA", "aaa", "AAA OAS"),
            ("BAMLC0A2CAA", "aa", "AA OAS"),
            ("BAMLC0A3CA", "a", "A OAS"),
            ("BAMLC0A4CBBB", "bbb", "BBB OAS"),
            ("BAMLH0A1HYBB", "bb", "BB OAS"),
            ("BAMLH0A2HYB", "b", "B OAS"),
            ("BAMLH0A3HYC", "ccc_lower", "CCC & lower OAS"),
        )
    ],
)

CATALOG_BY_ID: dict[str, SeriesSpec] = {spec.series_id: spec for spec in CATALOG}
CREDIT_SERIES: tuple[str, ...] = tuple(s.series_id for s in CATALOG if s.category == "credit")
CURVE_TENORS: dict[str, str] = {
    "3M": "DGS3MO", "6M": "DGS6MO", "1Y": "DGS1", "2Y": "DGS2", "3Y": "DGS3", "5Y": "DGS5",
    "7Y": "DGS7", "10Y": "DGS10", "20Y": "DGS20", "30Y": "DGS30",
}
CURVE_SLOPES: dict[str, tuple[str, str]] = {
    "10Y2Y": ("DGS10", "DGS2"),
    "30Y2Y": ("DGS30", "DGS2"),
    "30Y5Y": ("DGS30", "DGS5"),
    "10Y3M": ("DGS10", "DGS3MO"),
}


def catalog_series(category: str | None = None) -> list[SeriesSpec]:
    if category is None:
        return list(CATALOG)
    return [spec for spec in CATALOG if spec.category == category]


def validate_metadata(spec: SeriesSpec, meta: dict | None) -> tuple[str, list[dict]]:
    """Compare provider metadata against the catalog; return ``(status, mismatches)``.

    Only ``VALIDATED`` allows publication. Missing metadata or missing required fields is
    ``UNAVAILABLE`` (never silently validated); a provider ``id`` that is not the requested
    series is ``IDENTITY_MISMATCH``; units / frequency / seasonal-adjustment differences
    are ``MISMATCH``. Every finding is returned so operators can see all of them at once.
    """
    if not isinstance(meta, dict) or not meta:
        return META_UNAVAILABLE, [{"field": "metadata", "expected": "provider series metadata", "actual": None}]
    mismatches: list[dict] = []
    missing = [f for f in REQUIRED_METADATA_FIELDS if meta.get(f) in (None, "")]
    if missing:
        mismatches.append({"field": "required_fields", "expected": list(REQUIRED_METADATA_FIELDS), "missing": missing})
    provider_id = str(meta.get("id") or "").strip().upper()
    identity_ok = provider_id == spec.series_id.upper()
    if provider_id and not identity_ok:
        mismatches.append({"field": "id", "expected": spec.series_id, "actual": meta.get("id")})
    units = str(meta.get("units") or "").lower()
    if spec.expected_units_contains and not any(tok in units for tok in spec.expected_units_contains):
        mismatches.append({"field": "units", "expected_contains": list(spec.expected_units_contains), "actual": meta.get("units")})
    freq = str(meta.get("frequency_short") or "").upper()
    if freq and freq != spec.expected_frequency:
        mismatches.append({"field": "frequency_short", "expected": spec.expected_frequency, "actual": meta.get("frequency_short")})
    if spec.expected_sa:
        sa = str(meta.get("seasonal_adjustment_short") or "").upper()
        if not sa:
            mismatches.append({"field": "seasonal_adjustment_short", "expected": spec.expected_sa, "actual": None})
        elif sa != spec.expected_sa:
            mismatches.append({"field": "seasonal_adjustment_short", "expected": spec.expected_sa, "actual": meta.get("seasonal_adjustment_short")})
    if missing:
        return META_UNAVAILABLE, mismatches
    if provider_id and not identity_ok:
        return META_IDENTITY_MISMATCH, mismatches
    return (META_VALIDATED if not mismatches else META_MISMATCH), mismatches


def publishable(metadata_status: str) -> bool:
    return metadata_status == META_VALIDATED


SOURCE_REGISTRY_DEFAULTS: tuple[dict, ...] = (
    {
        "source_id": FRED_SOURCE_ID,
        "provider": "Federal Reserve Bank of St. Louis",
        "dataset": "fred_series_observations",
        "source_url": FRED_API_CONTRACT_URL,
        "expected_cadence": "MIXED",
        "usage_scope": EXPORT_ATTRIBUTION_REQUIRED,
        "attribution": FRED_ATTRIBUTION,
        "terms_notes": "FRED API terms; ICE BofA series carry additional redistribution restrictions.",
        "units_metadata": {"per_series": True},
    },
    {
        "source_id": "FMP_LEGACY",
        "provider": "Financial Modeling Prep (legacy nightly bundles)",
        "dataset": "precomputed_sector_bundles",
        "source_url": "outputs/precomputed",
        "expected_cadence": "D",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "attribution": "Financial Modeling Prep (FMP) via nightly_refresh bundles.",
        "terms_notes": "Legacy provider during migration; bridge reads saved bundles only.",
        "units_metadata": {"returns": "fraction", "rs": "relative_price_ratio_change"},
    },
    {
        "source_id": "QC_MARKET_INTELLIGENCE",
        "provider": "QuantConnect (MarketIntelligenceResearch)",
        "dataset": "sector_diagnostics_v1",
        "source_url": "quant-strategies research/market_intelligence",
        "expected_cadence": "ON_DEMAND",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "attribution": "QuantConnect PIT universe; derived aggregates only.",
        "terms_notes": "Current-data activation is a human gate; pre-2025 only until approved.",
        "units_metadata": {"returns": "fraction"},
    },
    {
        "source_id": "IBKR_MARKET_DATA",
        "provider": "Interactive Brokers",
        "dataset": "market_quotes",
        "source_url": "",
        "expected_cadence": "INTRADAY",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "attribution": "Interactive Brokers market data (entitlement dependent).",
        "terms_notes": "Disabled. Interface + mocks only; no orders.",
        "units_metadata": {},
    },
    {
        "source_id": "FINRA_TRACE",
        "provider": "FINRA",
        "dataset": "trace_corporate_trades",
        "source_url": "https://www.finra.org/finra-data",
        "expected_cadence": "INTRADAY",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "attribution": "FINRA TRACE (entitlement dependent).",
        "terms_notes": "Disabled. Access status scaffolding only.",
        "units_metadata": {"price_per": "100"},
    },
    {
        "source_id": "SEC_EDGAR",
        "provider": "U.S. Securities and Exchange Commission",
        "dataset": "edgar_reference",
        "source_url": "https://www.sec.gov/edgar",
        "expected_cadence": "ON_DEMAND",
        "usage_scope": EXPORT_ATTRIBUTION_REQUIRED,
        "attribution": "SEC EDGAR public filings.",
        "terms_notes": "Reference data only; requires SEC_USER_AGENT; rate limited.",
        "units_metadata": {},
    },
)
