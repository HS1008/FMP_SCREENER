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
AGG_WEEK_END_FRI = "week_ending_friday"  # Chicago Fed NFCI
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
       transforms=("yoy_pct", "ann3m_pct", "mom_pct"), label="Industrial Production", aggregation=AGG_PERIOD_LEVEL,
       notes="Index 2017=100, monthly, SA."),
    _s("RSAFS", "growth", "consumption", CADENCE_MONTHLY, _MLN, expected_sa="SA", value_kind="level",
       transforms=("yoy_pct", "mom_pct"), label="Retail Sales (adv.)", aggregation=AGG_PERIOD_TOTAL,
       notes="Millions of dollars, monthly total, SA."),
    # Labor
    _s("PAYEMS", "labor", "employment", CADENCE_MONTHLY, _THOUS, expected_sa="SA", value_kind="count",
       transforms=("mom_change", "mom_change_ma3", "yoy_pct"), label="Nonfarm Payrolls", aggregation=AGG_PERIOD_LEVEL,
       notes="Thousands of persons, monthly, SA. Net additions are thousands of persons, not percent. The 3-month average is the mean of three monthly changes."),
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
    _s("TRMMEANCPIM159SFRBCLE", "inflation", "cpi", CADENCE_MONTHLY, _PCT, expected_sa="SA",
       value_kind="percent", label="Cleveland Fed 16% trimmed-mean CPI", aggregation=AGG_PERIOD_LEVEL,
       backfill_years=30,
       notes="Federal Reserve Bank of Cleveland 16% trimmed-mean CPI. Percent change from a year ago, monthly, SA. "
             "Drops the CPI components with the highest and lowest 8% of the one-month price-change distribution. "
             "Not TRMMEANCPIM158SFRBCLE, which is the one-month annualized rate, and not median CPI."),
    _s("PCETRIM12M159SFRBDAL", "inflation", "pce", CADENCE_MONTHLY, _PCT, expected_sa="SA",
       value_kind="percent", label="Dallas Fed trimmed-mean PCE", aggregation=AGG_PERIOD_LEVEL,
       backfill_years=30,
       notes="Federal Reserve Bank of Dallas trimmed-mean PCE. Percent change from a year ago, monthly, SA. "
             "Not PCETRIM1M158SFRBDAL, the one-month annualized rate, and not PCETRIM6M680SFRBDAL, the six-month annualized rate."),
    _s("CPIUFDSL", "inflation", "cpi", CADENCE_MONTHLY, _IDX, expected_sa="SA", value_kind="index",
       transforms=("yoy_pct",), label="CPI food", aggregation=AGG_PERIOD_LEVEL, backfill_years=30,
       notes="CPI-U food. Index 1982-1984=100, monthly, SA. The chart uses the 12-month percent change. "
             "Not a contribution to headline CPI."),
    _s("CPIENGSL", "inflation", "cpi", CADENCE_MONTHLY, _IDX, expected_sa="SA", value_kind="index",
       transforms=("yoy_pct",), label="CPI energy", aggregation=AGG_PERIOD_LEVEL, backfill_years=30,
       notes="CPI-U energy. Index 1982-1984=100, monthly, SA. The chart uses the 12-month percent change. "
             "Not a contribution to headline CPI."),
    _s("CUSR0000SACL1E", "inflation", "cpi", CADENCE_MONTHLY, _IDX, expected_sa="SA", value_kind="index",
       transforms=("yoy_pct",), label="CPI core goods", aggregation=AGG_PERIOD_LEVEL, backfill_years=30,
       notes="CPI-U commodities less food and energy commodities. Index 1982-1984=100, monthly, SA. "
             "Mutually exclusive with food, energy, and services less energy services. "
             "The chart uses the 12-month percent change, not a contribution."),
    _s("CUSR0000SASLE", "inflation", "cpi", CADENCE_MONTHLY, _IDX, expected_sa="SA", value_kind="index",
       transforms=("yoy_pct",), label="CPI core services", aggregation=AGG_PERIOD_LEVEL, backfill_years=30,
       notes="CPI-U services less energy services. Index 1982-1984=100, monthly, SA. Shelter is inside this aggregate. "
             "Mutually exclusive with food, energy, and core goods. The chart uses the 12-month percent change, not a contribution."),
    _s("DDURRG3M086SBEA", "inflation", "pce", CADENCE_MONTHLY, _IDX, expected_sa="SA", value_kind="index",
       transforms=("yoy_pct",), label="PCE durable goods", aggregation=AGG_PERIOD_LEVEL, backfill_years=30,
       notes="BEA chain-type price index for PCE durable goods, account DDURRG. Index 2017=100, monthly, SA. "
             "Mutually exclusive with nondurable goods and services. The chart uses the 12-month percent change, not a contribution."),
    _s("DNDGRG3M086SBEA", "inflation", "pce", CADENCE_MONTHLY, _IDX, expected_sa="SA", value_kind="index",
       transforms=("yoy_pct",), label="PCE nondurable goods", aggregation=AGG_PERIOD_LEVEL, backfill_years=30,
       notes="BEA chain-type price index for PCE nondurable goods, account DNDGRG. Index 2017=100, monthly, SA. "
             "Food and energy goods sit inside this aggregate. Mutually exclusive with durable goods and services. "
             "The chart uses the 12-month percent change, not a contribution."),
    _s("DSERRG3M086SBEA", "inflation", "pce", CADENCE_MONTHLY, _IDX, expected_sa="SA", value_kind="index",
       transforms=("yoy_pct",), label="PCE services", aggregation=AGG_PERIOD_LEVEL, backfill_years=30,
       notes="BEA chain-type price index for PCE services, account DSERRG. Index 2017=100, monthly, SA. "
             "Energy services sit inside this aggregate. Mutually exclusive with durable and nondurable goods. "
             "The chart uses the 12-month percent change, not a contribution."),
    # Policy
    _s("DFF", "policy", "fed_funds", CADENCE_DAILY, _PCT, expected_sa="NSA", value_kind="percent",
       transforms=("level_pct", "chg_bps"), label="Effective Fed Funds"),
    _s("DFEDTARL", "policy", "fed_funds_target", CADENCE_DAILY, _PCT, expected_sa="NSA", value_kind="percent",
       transforms=("level_pct",), label="Fed funds target lower",
       notes="Federal Funds Target Range - Lower Limit (DFEDTARL). FOMC policy state held until changed. "
             "Not a Treasury maturity and not the effective federal funds rate (DFF)."),
    _s("DFEDTARU", "policy", "fed_funds_target", CADENCE_DAILY, _PCT, expected_sa="NSA", value_kind="percent",
       transforms=("level_pct",), label="Fed funds target upper",
       notes="Federal Funds Target Range - Upper Limit (DFEDTARU). FOMC policy state held until changed. "
             "Not a Treasury maturity and not the effective federal funds rate (DFF)."),
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
    _s("TREAST", "liquidity", "fed_balance_sheet", CADENCE_WEEKLY, _MLN, expected_sa="NSA",
       value_kind="balance", label="U.S. Treasury securities held outright (Wednesday level)",
       aggregation=AGG_WED_LEVEL, backfill_years=30,
       notes="H.4.1 Assets: Securities Held Outright: U.S. Treasury Securities: All: Wednesday Level. "
             "Millions of USD, NSA. Not a change series."),
    _s("WSHOMCB", "liquidity", "fed_balance_sheet", CADENCE_WEEKLY, _MLN, expected_sa="NSA",
       value_kind="balance", label="Mortgage-backed securities held outright (Wednesday level)",
       aggregation=AGG_WED_LEVEL, backfill_years=30,
       notes="H.4.1 Assets: Securities Held Outright: Mortgage-Backed Securities: Wednesday Level. "
             "Millions of USD, NSA. Not a change series."),
    _s("WSHOFADSL", "liquidity", "fed_balance_sheet", CADENCE_WEEKLY, _MLN, expected_sa="NSA",
       value_kind="balance", label="Federal agency debt securities held outright (Wednesday level)",
       aggregation=AGG_WED_LEVEL, backfill_years=30,
       notes="H.4.1 Assets: Securities Held Outright: Federal Agency Debt Securities: Wednesday Level. "
             "Millions of USD, NSA. Not a change series."),
    _s("WLCFLPCL", "liquidity", "fed_balance_sheet", CADENCE_WEEKLY, _MLN, expected_sa="NSA",
       value_kind="balance", label="Primary credit (Wednesday level)",
       aggregation=AGG_WED_LEVEL, backfill_years=30,
       notes="H.4.1 Assets: Liquidity and Credit Facilities: Loans: Primary Credit: Wednesday Level. "
             "Millions of USD, NSA. Not a change series."),
    _s("WRBWFRBL", "liquidity", "fed_balance_sheet", CADENCE_WEEKLY, _MLN, expected_sa="NSA",
       value_kind="balance", label="Reserve balances with Federal Reserve Banks (Wednesday level)",
       aggregation=AGG_WED_LEVEL, backfill_years=30,
       notes="H.4.1 Reserve Balances with Federal Reserve Banks: Wednesday Level. Millions of USD, NSA. "
             "Not WRESBAL, which is the week average."),
    _s("WCICL", "liquidity", "fed_balance_sheet", CADENCE_WEEKLY, _MLN, expected_sa="NSA",
       value_kind="balance", label="Currency in circulation (Wednesday level)",
       aggregation=AGG_WED_LEVEL, backfill_years=30,
       notes="H.4.1 Currency in Circulation: Wednesday Level. Millions of USD, NSA. Not a change series."),
    _s("WDTGAL", "liquidity", "fed_balance_sheet", CADENCE_WEEKLY, _MLN, expected_sa="NSA",
       value_kind="balance", label="Treasury General Account (Wednesday level)",
       aggregation=AGG_WED_LEVEL, backfill_years=30,
       notes="H.4.1 U.S. Treasury, General Account: Wednesday Level. Millions of USD, NSA. "
             "Not WTREGEN, which is the week average."),
    _s("WLRRAL", "liquidity", "fed_balance_sheet", CADENCE_WEEKLY, _MLN, expected_sa="NSA",
       value_kind="balance", label="Reverse repurchase agreements (Wednesday level)",
       aggregation=AGG_WED_LEVEL, backfill_years=30,
       notes="H.4.1 Reverse Repurchase Agreements: Wednesday Level. Millions of USD, NSA. "
             "Not RRPONTSYD, which is daily overnight reverse repo in billions."),
    _s("WCPIL", "liquidity", "fed_balance_sheet", CADENCE_WEEKLY, _MLN, expected_sa="NSA",
       value_kind="balance", label="Capital paid in (Wednesday level)",
       aggregation=AGG_WED_LEVEL, backfill_years=30,
       notes="H.4.1 Capital: Capital Paid in: Wednesday Level. Millions of USD, NSA. Not a change series."),
    _s("WCSL", "liquidity", "fed_balance_sheet", CADENCE_WEEKLY, _MLN, expected_sa="NSA",
       value_kind="balance", label="Surplus (Wednesday level)",
       aggregation=AGG_WED_LEVEL, backfill_years=30,
       notes="H.4.1 Capital: Surplus: Wednesday Level. Millions of USD, NSA. Not a change series."),
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
    _s("NFCI", "policy", "financial_conditions", CADENCE_WEEKLY, _IDX, expected_sa="NSA",
       value_kind="index", label="Chicago Fed NFCI", aggregation=AGG_WEEK_END_FRI,
       notes="Chicago Fed National Financial Conditions Index. Weekly, ending Friday, NSA. "
             "Positive = tighter than average. Negative = looser than average. Not a trading signal."),
    _s("CUSR0000SASL2RS", "inflation", "cpi", CADENCE_MONTHLY, _IDX, expected_sa="SA", value_kind="index",
       transforms=("yoy_pct", "ann3m_pct", "ann6m_pct"), label="CPI services less rent of shelter",
       aggregation=AGG_PERIOD_LEVEL,
       notes="CPI: Services Less Rent of Shelter. Index 1982-1984=100, monthly, SA. Not labeled supercore."),
    _s("PERMIT", "growth", "housing", CADENCE_MONTHLY, _THOUS, expected_sa="SAAR", value_kind="count",
       transforms=("yoy_pct",), label="Building permits", aggregation=AGG_PERIOD_TOTAL,
       notes="New privately-owned housing units authorized. Thousands of units, monthly, SAAR."),
    _s("NEWORDER", "growth", "orders", CADENCE_MONTHLY, _MLN, expected_sa="SA", value_kind="level",
       transforms=("yoy_pct", "ann3m_pct", "ann6m_pct"), label="Core capital goods orders",
       aggregation=AGG_PERIOD_TOTAL,
       notes="Manufacturers' new orders: nondefense capital goods excluding aircraft. Millions of dollars, monthly, SA."),
    _s("AWHMAN", "labor", "hours", CADENCE_MONTHLY, ("hours",), expected_sa="SA", value_kind="level",
       label="Manufacturing weekly hours", aggregation=AGG_PERIOD_LEVEL,
       notes="Average weekly hours of production and nonsupervisory employees, manufacturing. Monthly, SA."),
    _s("CMRMT", "growth", "sales", CADENCE_MONTHLY, _MLN, expected_sa="SA", value_kind="level",
       transforms=("yoy_pct", "ann3m_pct"), label="Real manufacturing and trade sales",
       aggregation=AGG_PERIOD_TOTAL,
       notes="Real manufacturing and trade industries sales. Millions of chained dollars, monthly, SA."),
    _s("PCEC96", "growth", "consumption", CADENCE_MONTHLY, _BLN, expected_sa="SAAR", value_kind="level",
       transforms=("yoy_pct", "ann3m_pct"), label="Real personal consumption expenditures",
       aggregation=AGG_PERIOD_TOTAL,
       notes="Real personal consumption expenditures. Billions of chained 2017 dollars, monthly, SAAR. Not nominal PCE."),
    _s("W875RX1", "growth", "income", CADENCE_MONTHLY, _BLN, expected_sa="SAAR", value_kind="level",
       transforms=("yoy_pct", "ann3m_pct"), label="Real personal income ex transfers",
       aggregation=AGG_PERIOD_TOTAL,
       notes="Real personal income excluding current transfer receipts. Billions of chained 2017 dollars, monthly, SAAR."),
    _s("UEMPMED", "labor", "unemployment", CADENCE_MONTHLY, ("weeks",), expected_sa="SA", value_kind="level",
       label="Median unemployment duration", aggregation=AGG_PERIOD_LEVEL,
       notes="Median duration of unemployment. Weeks, monthly, SA."),
    _s("ISRATIO", "growth", "inventories", CADENCE_MONTHLY, ("ratio",), expected_sa="SA", value_kind="level",
       label="Inventory-to-sales ratio", aggregation=AGG_PERIOD_LEVEL,
       notes="Total business inventories to sales ratio. Monthly, SA. Ratio, not a percent."),
    _s("BUSLOANS", "banking", "loans", CADENCE_MONTHLY, _BLN, expected_sa="SA", value_kind="balance",
       transforms=("yoy_pct",), label="C&I loans", aggregation=AGG_PERIOD_LEVEL,
       notes="Commercial and industrial loans, all commercial banks. Billions of USD, monthly, seasonally adjusted. "
             "The current FRED series is monthly SA, not the weekly NSA print."),
    _s("DRBLACBS", "banking", "delinquency", CADENCE_QUARTERLY, _PCT, expected_sa="SA", value_kind="percent",
       label="Business loan delinquency rate", aggregation=AGG_PERIOD_LEVEL,
       notes="Delinquency rate on commercial and industrial loans, all commercial banks. Percent, quarterly, SA. Not interpolated."),
    _s("ULCNFB", "labor", "costs", CADENCE_QUARTERLY, _IDX, expected_sa="SA", value_kind="index",
       transforms=("yoy_pct", "qoq_annualized_pct"), label="Nonfarm business unit labor costs",
       aggregation=AGG_PERIOD_LEVEL,
       notes="Nonfarm business sector: unit labor costs for all workers. Index 2017=100, quarterly, SA."),
    _s("USREC", "recession", "nber", CADENCE_MONTHLY, ("+1 or 0",), expected_sa="NSA", value_kind="level",
       label="NBER recession indicator", aggregation=AGG_PERIOD_LEVEL,
       notes="NBER recession indicator. Monthly, NSA. 1 from the period after the peak through the trough. "
             "Support series for chart shading, not a primary macro chart."),
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
        )
    ],
    _s(
        "BAMLEMCBPIOAS",
        "credit",
        "em_broad",
        CADENCE_DAILY,
        _PCT,
        expected_sa="NSA",
        value_kind="percent",
        export_scope=EXPORT_RESTRICTED,
        attribution=ICE_ATTRIBUTION,
        backfill_years=4,
        transforms=("oas_bps", "chg_bps", "pctile_available"),
        label="EM Corporate OAS",
        notes=(
            "ICE BofA Emerging Markets Corporate Plus Index Option-Adjusted Spread. "
            "Broad emerging-markets corporate OAS. Not the USD-only US Emerging Markets "
            "subset BAMLEMUBCRPIUSOAS. Option-adjusted spread in percent (x100 = bps). "
            "Provider limits history; distribution restricted by ICE terms."
        ),
    ),
    *[
        _s(sid, "credit", bucket, CADENCE_DAILY, _PCT, expected_sa="NSA", value_kind="percent",
           export_scope=EXPORT_RESTRICTED, attribution=ICE_ATTRIBUTION, backfill_years=4,
           transforms=("oas_bps", "chg_bps", "pctile_available"), label=lbl,
           notes="Option-adjusted spread in percent (x100 = bps). Provider limits history; "
                 "distribution restricted by ICE terms.")
        for sid, bucket, lbl in (
            ("BAMLC0A1CAAA", "aaa", "AAA OAS"),
            ("BAMLC0A2CAA", "aa", "AA OAS"),
            ("BAMLC0A3CA", "a", "A OAS"),
            ("BAMLC0A4CBBB", "bbb", "BBB OAS"),
            ("BAMLH0A1HYBB", "bb", "BB OAS"),
            ("BAMLH0A2HYB", "b", "B OAS"),
            ("BAMLH0A3HYC", "ccc_lower", "CCC & lower OAS"),
        )
    ],
    _s("DCOILWTICO", "commodities", "energy", CADENCE_DAILY, ("dollars",), expected_sa="NSA", value_kind="level",
       transforms=("chg_1d", "chg_1w"), label="WTI spot",
       notes="Dollars per barrel, daily, NSA. EIA via FRED."),
    _s("DHHNGSP", "commodities", "energy", CADENCE_DAILY, ("dollars",), expected_sa="NSA", value_kind="level",
       transforms=("chg_1d", "chg_1w"), label="Henry Hub natural gas",
       notes="Dollars per million BTU, daily, NSA. EIA via FRED."),
    _s("PCOPPUSDM", "commodities", "metals", CADENCE_MONTHLY, ("dollars",), expected_sa="NSA", value_kind="level",
       transforms=("mom_pct", "yoy_pct"), label="Global copper price",
       notes="USD per metric ton, monthly. IMF via FRED."),
    _s("U6RATE", "labor", "unemployment", CADENCE_MONTHLY, _PCT, expected_sa="SA", value_kind="percent",
       backfill_years=30, label="U-6 unemployment", aggregation=AGG_PERIOD_LEVEL,
       notes="U-6 labor underutilization, percent, monthly, SA. Same units as U-3."),
    _s("JTSJOL", "labor", "jolts", CADENCE_MONTHLY, _THOUS, expected_sa="SA", value_kind="count",
       backfill_years=30, label="Job openings", aggregation=AGG_PERIOD_LEVEL,
       notes="JOLTS job openings, total nonfarm, level in thousands, monthly, SA."),
    _s("JTSHIL", "labor", "jolts", CADENCE_MONTHLY, _THOUS, expected_sa="SA", value_kind="count",
       backfill_years=30, label="Hires", aggregation=AGG_PERIOD_LEVEL,
       notes="JOLTS hires, total nonfarm, level in thousands, monthly, SA."),
    _s("JTSQUL", "labor", "jolts", CADENCE_MONTHLY, _THOUS, expected_sa="SA", value_kind="count",
       backfill_years=30, label="Quits", aggregation=AGG_PERIOD_LEVEL,
       notes="JOLTS quits, total nonfarm, level in thousands, monthly, SA."),
    _s("UNEMPLOY", "labor", "unemployment", CADENCE_MONTHLY, _THOUS, expected_sa="SA", value_kind="count",
       backfill_years=30, label="Unemployment level", aggregation=AGG_PERIOD_LEVEL,
       notes="Unemployment level, thousands of persons, monthly, SA. Same thousands as JTSJOL."),
    _s("CES0500000003", "labor", "wages", CADENCE_MONTHLY, ("dollars",), expected_sa="SA", value_kind="level",
       transforms=("yoy_pct", "ann3m_pct"), backfill_years=30, label="Average hourly earnings",
       aggregation=AGG_PERIOD_LEVEL,
       notes="Average hourly earnings of all employees, total private, dollars per hour, monthly, SA."),
    _s("MORTGAGE30US", "housing", "rates", CADENCE_WEEKLY, _PCT, expected_sa="NSA", value_kind="percent",
       backfill_years=30, label="30-year mortgage rate", aggregation=AGG_POINT,
       notes="30-year fixed mortgage rate, percent, weekly, NSA. Freddie Mac via FRED."),
    _s("HOUST", "housing", "construction", CADENCE_MONTHLY, _THOUS, expected_sa="SAAR", value_kind="count",
       backfill_years=30, label="Housing starts", aggregation=AGG_PERIOD_TOTAL,
       notes="New privately owned housing units started, thousands of units, monthly, SAAR. Same units as PERMIT."),
    _s("HSN1F", "housing", "demand", CADENCE_MONTHLY, _THOUS, expected_sa="SAAR", value_kind="count",
       backfill_years=30, label="New home sales", aggregation=AGG_PERIOD_TOTAL,
       notes="New one-family houses sold, thousands, monthly, SAAR. Census via FRED."),
    _s("MSACSR", "housing", "supply", CADENCE_MONTHLY, ("months",), expected_sa="SA", value_kind="level",
       backfill_years=30, label="Months' supply of new houses", aggregation=AGG_PERIOD_LEVEL,
       notes="Months' supply of new houses, monthly, SA. Census via FRED."),
    _s("CSUSHPINSA", "housing", "prices", CADENCE_MONTHLY, _IDX, expected_sa="NSA", value_kind="index",
       transforms=("yoy_pct",), backfill_years=30, label="Case-Shiller national home price index",
       aggregation=AGG_PERIOD_LEVEL,
       notes="S&P CoreLogic Case-Shiller U.S. national home price index, January 2000=100, monthly, NSA."),
    _s("DSPIC96", "growth", "income", CADENCE_MONTHLY, _BLN, expected_sa="SAAR", value_kind="level",
       transforms=("yoy_pct",), backfill_years=30, label="Real disposable personal income",
       aggregation=AGG_PERIOD_TOTAL,
       notes="Real disposable personal income, billions of chained 2017 dollars, monthly, SAAR. Not nominal."),
    _s("PSAVERT", "growth", "saving", CADENCE_MONTHLY, _PCT, expected_sa="SAAR", value_kind="percent",
       backfill_years=30, label="Personal saving rate", aggregation=AGG_PERIOD_LEVEL,
       notes="Personal saving as a percent of disposable personal income, monthly, seasonally adjusted annual rate."),
    _s("DRCCLACBS", "growth", "credit", CADENCE_QUARTERLY, _PCT, expected_sa="SA", value_kind="percent",
       backfill_years=30, label="Credit card delinquency rate", aggregation=AGG_PERIOD_LEVEL,
       notes="Delinquency rate on credit card loans, all commercial banks, percent, quarterly, seasonally adjusted. Not forward-filled."),
    _s("MTSDS133FMS", "fiscal", "budget", CADENCE_MONTHLY, _MLN, expected_sa="NSA", value_kind="balance",
       transforms=("sum_12m",), backfill_years=30, label="Federal surplus or deficit",
       aggregation=AGG_PERIOD_TOTAL,
       notes="Monthly Treasury Statement surplus or deficit, millions of dollars, monthly, NSA. Negative is a deficit."),
    _s("MTSR133FMS", "fiscal", "budget", CADENCE_MONTHLY, _MLN, expected_sa="NSA", value_kind="balance",
       transforms=("sum_12m",), backfill_years=30, label="Federal receipts",
       aggregation=AGG_PERIOD_TOTAL,
       notes="Monthly Treasury Statement federal receipts, millions of dollars, monthly, NSA."),
    _s("MTSO133FMS", "fiscal", "budget", CADENCE_MONTHLY, _MLN, expected_sa="NSA", value_kind="balance",
       transforms=("sum_12m",), backfill_years=30, label="Federal outlays",
       aggregation=AGG_PERIOD_TOTAL,
       notes="Monthly Treasury Statement federal outlays, millions of dollars, monthly, NSA."),
    _s("FYGFGDQ188S", "fiscal", "debt", CADENCE_QUARTERLY, _PCT, expected_sa="SA", value_kind="percent",
       backfill_years=30, label="Debt held by the public / GDP", aggregation=AGG_PERIOD_LEVEL,
       notes="Federal debt held by the public as a percent of GDP, quarterly, SA. Not gross federal debt."),
    _s("A091RC1Q027SBEA", "fiscal", "interest", CADENCE_QUARTERLY, _BLN, expected_sa="SAAR", value_kind="level",
       backfill_years=30, label="Federal interest payments", aggregation=AGG_PERIOD_TOTAL,
       notes="BEA federal interest payments, billions of dollars, quarterly, SAAR."),
    _s("FGRECPT", "fiscal", "interest", CADENCE_QUARTERLY, _BLN, expected_sa="SAAR", value_kind="level",
       backfill_years=30, label="Federal government current receipts", aggregation=AGG_PERIOD_TOTAL,
       notes="BEA federal current receipts, billions of dollars, quarterly, SAAR. Matched to interest payments."),
)

CATALOG_BY_ID: dict[str, SeriesSpec] = {spec.series_id: spec for spec in CATALOG}
CREDIT_SERIES: tuple[str, ...] = tuple(s.series_id for s in CATALOG if s.category == "credit")
CREDIT_BROAD_BUCKETS = frozenset({"ig_broad", "hy_broad", "em_broad"})
# Short tile labels. Chart series use these names; catalog labels keep the full OAS names.
CREDIT_BROAD_TILES: tuple[tuple[str, str], ...] = (
    ("BAMLC0A0CM", "IG"),
    ("BAMLH0A0HYM2", "HY"),
    ("BAMLEMCBPIOAS", "EM"),
)
CREDIT_RATING_TILES: tuple[tuple[str, str], ...] = (
    ("BAMLC0A1CAAA", "AAA"),
    ("BAMLC0A2CAA", "AA"),
    ("BAMLC0A3CA", "A"),
    ("BAMLC0A4CBBB", "BBB"),
    ("BAMLH0A1HYBB", "BB"),
    ("BAMLH0A2HYB", "B"),
    ("BAMLH0A3HYC", "CCC & lower"),
)
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
# Real par yields only. No 2Y, 3Y, or 7Y is invented; Treasury XML has a 7Y real field with no FRED twin.
TIPS_TENORS: dict[str, str] = {
    "5Y": "DFII5",
    "10Y": "DFII10",
    "20Y": "DFII20",
    "30Y": "DFII30",
}
# Body minus wings. Order is (2Y, 5Y, 10Y). Stored metric uses the exact formula 2*DGS5 - DGS2 - DGS10.
CURVE_FLIES: dict[str, tuple[str, str, str]] = {
    "2s5s10s": ("DGS2", "DGS5", "DGS10"),
}
SLOPE_10Y2Y_METRIC = "curve.slope_10Y2Y_bps"
FLY_2S5S10S_METRIC = "curve.fly_2s5s10s_bps"
FED_FUNDS_TARGET_LOWER = "DFEDTARL"
FED_FUNDS_TARGET_UPPER = "DFEDTARU"
# H.4.1 Wednesday levels for the Fed balance-sheet table. A host that already
# recorded the broader macro max-backfill marker still ingests this set.
H41_WEDNESDAY_LEVEL_SERIES: tuple[str, ...] = (
    "WALCL",
    "TREAST",
    "WSHOMCB",
    "WSHOFADSL",
    "WLCFLPCL",
    "WRBWFRBL",
    "WCICL",
    "WDTGAL",
    "WLRRAL",
    "WCPIL",
    "WCSL",
)
# Trimmed-mean rates and CPI/PCE component indexes added after the macro max-history
# marker already existed. A host that recorded that marker still ingests this set.
INFLATION_COMPONENT_SERIES: tuple[str, ...] = (
    "TRMMEANCPIM159SFRBCLE",
    "PCETRIM12M159SFRBDAL",
    "CPIUFDSL",
    "CPIENGSL",
    "CUSR0000SACL1E",
    "CUSR0000SASLE",
    "DDURRG3M086SBEA",
    "DNDGRG3M086SBEA",
    "DSERRG3M086SBEA",
)
# Labor, housing, growth, and fiscal series added after the macro max-history marker
# already existed. PAYEMS is included so the new 3-month payroll average is rebuilt.
MACRO_EXPANSION_SERIES: tuple[str, ...] = (
    "U6RATE",
    "JTSJOL",
    "JTSHIL",
    "JTSQUL",
    "UNEMPLOY",
    "CES0500000003",
    "MORTGAGE30US",
    "HOUST",
    "HSN1F",
    "MSACSR",
    "CSUSHPINSA",
    "DSPIC96",
    "PSAVERT",
    "DRCCLACBS",
    "MTSDS133FMS",
    "MTSR133FMS",
    "MTSO133FMS",
    "FYGFGDQ188S",
    "A091RC1Q027SBEA",
    "FGRECPT",
    "PAYEMS",
    "GDPC1",
    "PCEC96",
)
# Explicit max-history path for the Macro dashboard. Incremental refresh keeps catalog backfill_years.
MACRO_MAX_BACKFILL_SERIES: tuple[str, ...] = (
    "DFF",
    "SOFR",
    FED_FUNDS_TARGET_LOWER,
    FED_FUNDS_TARGET_UPPER,
    *H41_WEDNESDAY_LEVEL_SERIES,
    "WRESBAL",
    "WTREGEN",
    "RRPONTSYD",
    "M2SL",
    "NFCI",
    "CPIAUCSL",
    "CPILFESL",
    "PCEPI",
    "PCEPILFE",
    *INFLATION_COMPONENT_SERIES,
    "CUSR0000SASL2RS",
    "T5YIE",
    "T10YIE",
    "T5YIFR",
    "ICSA",
    "PERMIT",
    "NEWORDER",
    "AWHMAN",
    "PAYEMS",
    "INDPRO",
    "CMRMT",
    "PCEC96",
    "W875RX1",
    "UNRATE",
    "UEMPMED",
    "ISRATIO",
    "BUSLOANS",
    "DRBLACBS",
    "ULCNFB",
    "USREC",
    *tuple(
        series_id
        for series_id in MACRO_EXPANSION_SERIES
        if series_id not in {"PAYEMS", "GDPC1", "PCEC96"}
    ),
)
MACRO_COVERAGE_METRICS: tuple[str, ...] = (
    "M2SL.yoy_pct",
    "CPIAUCSL.yoy_pct",
    "CPIAUCSL.ann3m_pct",
    "CPIAUCSL.ann6m_pct",
    "CPILFESL.yoy_pct",
    "CPILFESL.ann3m_pct",
    "CPILFESL.ann6m_pct",
    "PCEPI.yoy_pct",
    "PCEPI.ann3m_pct",
    "PCEPI.ann6m_pct",
    "PCEPILFE.yoy_pct",
    "PCEPILFE.ann3m_pct",
    "PCEPILFE.ann6m_pct",
    "CPIUFDSL.yoy_pct",
    "CPIENGSL.yoy_pct",
    "CUSR0000SACL1E.yoy_pct",
    "CUSR0000SASLE.yoy_pct",
    "DDURRG3M086SBEA.yoy_pct",
    "DNDGRG3M086SBEA.yoy_pct",
    "DSERRG3M086SBEA.yoy_pct",
    "CUSR0000SASL2RS.yoy_pct",
    "CUSR0000SASL2RS.ann3m_pct",
    "CUSR0000SASL2RS.ann6m_pct",
    "ICSA.avg_4w",
    "PERMIT.yoy_pct",
    "NEWORDER.yoy_pct",
    "NEWORDER.ann3m_pct",
    "NEWORDER.ann6m_pct",
    "PAYEMS.mom_change",
    "PAYEMS.mom_change_ma3",
    "CES0500000003.yoy_pct",
    "CES0500000003.ann3m_pct",
    "CSUSHPINSA.yoy_pct",
    "DSPIC96.yoy_pct",
    "MTSDS133FMS.sum_12m",
    "MTSR133FMS.sum_12m",
    "MTSO133FMS.sum_12m",
    "INDPRO.yoy_pct",
    "INDPRO.ann3m_pct",
    "CMRMT.yoy_pct",
    "CMRMT.ann3m_pct",
    "PCEC96.yoy_pct",
    "PCEC96.ann3m_pct",
    "W875RX1.yoy_pct",
    "W875RX1.ann3m_pct",
    "BUSLOANS.yoy_pct",
    "ULCNFB.yoy_pct",
    "ULCNFB.qoq_saar_pct",
)
# Explicit max-history path only. Incremental refresh keeps each series' catalog backfill_years.
RATES_MAX_BACKFILL_SERIES: tuple[str, ...] = (
    "DGS2",
    "DGS5",
    "DGS10",
    "DFII5",
    "DFII10",
    "DFII20",
    "DFII30",
    FED_FUNDS_TARGET_LOWER,
    FED_FUNDS_TARGET_UPPER,
)


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


RETIRED_CBOE_TERMS = (
    "Intentionally retired. CBOE/OpenBB-CBOE is no longer an active data provider. "
    "Yahoo volatility replaced it for active dashboard use. Historical observations are retained. "
    "This is not a platform outage and does not require CBOE credentials."
)
RETIRED_CBOE_ATTRIBUTION = (
    "Retired CBOE/OpenBB-CBOE provider. Not an active integration. "
    "Index names such as the Cboe SKEW Index identify the index, not a CBOE data feed."
)
# Inserted when missing. ON CONFLICT updates only retirement fields so an existing
# production provider/dataset label is preserved.
RETIRED_CBOE_REGISTRY: tuple[dict[str, str], ...] = (
    {"source_id": "CBOE_ALL_ACCESS", "provider": "Cboe (retired)", "dataset": "cboe_all_access"},
    {"source_id": "OPENBB_CBOE", "provider": "OpenBB / Cboe (retired)", "dataset": "openbb_cboe"},
    {"source_id": "OPENBB_CBOE_OPTIONS", "provider": "OpenBB / Cboe (retired)", "dataset": "cboe_delayed_options_chains"},
    {"source_id": "OPENBB_CBOE_VIX", "provider": "OpenBB / Cboe (retired)", "dataset": "cboe_vx_eod_curve"},
)
RETIRED_CBOE_SOURCE_IDS = tuple(row["source_id"] for row in RETIRED_CBOE_REGISTRY)


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
        "dataset": "sector_internals_v1",
        "source_url": "quant-strategies research/market_intelligence (local producer -> hash-verified artifact file)",
        "expected_cadence": "ON_DEMAND",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "attribution": "QuantConnect PIT universe; derived sector aggregates only (no constituents).",
        "terms_notes": "Consumer ingests local artifact files only. QC project activation is a human gate; pre-2025 only until approved. SYNTHETIC_TEST_ONLY artifacts are research-ineligible.",
        "units_metadata": {"pct_above_*": "fraction_0_1", "*_return_*": "simple_return_fraction", "hhi_cap": "sum_of_squared_weights_0_1"},
    },
    {
        "source_id": "IBKR_MARKET_DATA",
        "provider": "Interactive Brokers",
        "dataset": "market_quotes",
        "source_url": "",
        "expected_cadence": "INTRADAY",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "attribution": "Interactive Brokers market data (entitlement dependent).",
        "terms_notes": "Windows-local read-only TWS collector. Server never opens a TWS socket. No orders.",
        "units_metadata": {},
    },
    {
        "source_id": "FINRA_QUERY",
        "provider": "FINRA",
        "dataset": "fixedIncomeMarket_aggregates",
        "source_url": "https://developer.finra.org/docs",
        "expected_cadence": "D",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "attribution": "FINRA TRACE-derived aggregates via the FINRA Query API.",
        "terms_notes": "Public Query API fixed-income aggregates. Individual TRACE prints are a separate product.",
        "units_metadata": {"totalVolume": "source_native", "totalTrades": "count"},
    },
    {
        "source_id": "FINRA_TRACE",
        "provider": "FINRA",
        "dataset": "trace_corporate_trades",
        "source_url": "https://www.finra.org/filing-reporting/trace/documentation",
        "expected_cadence": "INTRADAY",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "attribution": "FINRA TRACE individual transactions (not on Query API).",
        "terms_notes": "TRACE API / TRAQS file download is not part of the FINRA API Platform. Not probed.",
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
        "terms_notes": "Reference data only; requires SEC_USER_AGENT; rate limited. Not a bond quote feed.",
        "units_metadata": {},
    },
    {
        "source_id": "OPENFIGI",
        "provider": "OpenFIGI",
        "dataset": "identifier_mapping",
        "source_url": "https://www.openfigi.com/api",
        "expected_cadence": "ON_DEMAND",
        "usage_scope": EXPORT_ATTRIBUTION_REQUIRED,
        "attribution": "OpenFIGI identifier mapping.",
        "terms_notes": "Maps identifiers (FIGI/CUSIP/ISIN/ticker). Not quotes, yields, ratings, or a complete bond master. Ambiguous matches stay unresolved.",
        "units_metadata": {},
    },
    {
        "source_id": "TREASURY",
        "provider": "U.S. Department of the Treasury",
        "dataset": "daily_treasury_yield_curve_xml",
        "source_url": "https://home.treasury.gov/treasury-daily-interest-rate-xml-feed",
        "expected_cadence": "D",
        "usage_scope": EXPORT_ATTRIBUTION_REQUIRED,
        "attribution": "U.S. Treasury Daily Treasury Par Yield Curve Rates (XML).",
        "terms_notes": "Official daily XML. Distinct from Fiscal Data JSON and from FRED DGS*.",
        "units_metadata": {"yield": "percent"},
    },
    {
        "source_id": "EQUITY_EOD",
        "provider": "Configured equity EOD adapter",
        "dataset": "equity_etf_daily_bars",
        "source_url": "",
        "expected_cadence": "D",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "attribution": "Independent daily bars. Never labeled as FMP.",
        "terms_notes": "Yahoo is optional and entitlement-unverified. IBKR is available behind MI_EQUITY_PROVIDER=ibkr via the Windows collector (ADJUSTED_LAST). Missing access is UNAVAILABLE, not an FMP fallback. IBKR values stay INTERNAL_ONLY until a human confirms remote export rights.",
        "units_metadata": {"price": "adjusted_close"},
    },
    {
        "source_id": "YAHOO_LIVE",
        "provider": "Yahoo Finance (yfinance, unofficial)",
        "dataset": "live_quotes_fallback",
        "source_url": "",
        "expected_cadence": "INTRADAY",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "attribution": "Yahoo Finance via yfinance (unofficial; no SLA).",
        "terms_notes": "Optional live-quote fallback when IBKR quotes are missing or stale (MI_YAHOO_LIVE_FALLBACK=1). Never labeled as IBKR. No redistribution rights claimed. Streamlit never calls yfinance.",
        "units_metadata": {"price": "last_trade_or_regular_market"},
    },
    {
        "source_id": "YAHOO_EOD",
        "provider": "Yahoo Finance (yfinance, unofficial)",
        "dataset": "equity_daily_prior_close_fallback",
        "source_url": "",
        "expected_cadence": "D",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "attribution": "Yahoo Finance via yfinance (unofficial; no SLA).",
        "terms_notes": "Exact-session prior-close fallback when IBKR EQUITY_EOD is missing (MI_YAHOO_EOD_FALLBACK / inherits MI_YAHOO_LIVE_FALLBACK). Separate source so IBKR can supersede. Never mixes into longer-horizon EQUITY_EOD history. Streamlit never calls yfinance.",
        "units_metadata": {"price": "adjusted_close"},
    },
    {
        "source_id": "OPENBB_CBOE_OPTIONS",
        "provider": "OpenBB / Cboe (retired)",
        "dataset": "cboe_delayed_options_chains",
        "source_url": "https://www.cboe.com/delayed_quotes/",
        "expected_cadence": "D",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "enabled": False,
        "access_status": "RETIRED_OPTIONAL",
        "attribution": RETIRED_CBOE_ATTRIBUTION,
        "terms_notes": RETIRED_CBOE_TERMS,
        "units_metadata": {"implied_volatility": "decimal", "greeks": "decimal"},
    },
    {
        "source_id": "OPENBB_CBOE_VIX",
        "provider": "OpenBB / Cboe (retired)",
        "dataset": "cboe_vx_eod_curve",
        "source_url": "https://www.cboe.com/delayed_quotes/",
        "expected_cadence": "D",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "enabled": False,
        "access_status": "RETIRED_OPTIONAL",
        "attribution": RETIRED_CBOE_ATTRIBUTION,
        "terms_notes": RETIRED_CBOE_TERMS,
        "units_metadata": {"price": "index_points", "expiration": "month"},
    },
    {
        "source_id": "IBKR_OPTIONS",
        "provider": "Interactive Brokers TWS",
        "dataset": "ibkr_bounded_option_chains",
        "source_url": "https://interactivebrokers.github.io/tws-api/options.html",
        "expected_cadence": "D",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "enabled": False,
        "access_status": "PROVIDER_SUPPORT_REQUIRED",
        "attribution": "IBKR TWS option quotes via reqSecDefOptParams + bounded reqMktData. Delayed OPRA unless this username has live OPRA. Not Cboe website JSON.",
        "terms_notes": "INTERNAL_ONLY. TWS API client 73 still returns 354 / no NBBO after OPRA L1 and a frozen Type 2 probe. Collection stays off. Storage rights are a separate IBKR_OPTIONS_STORAGE row. Not on the remote AI allowlist.",
        "units_metadata": {"implied_volatility": "decimal", "greeks": "decimal"},
    },
    {
        "source_id": "IBKR_OPTIONS_STORAGE",
        "provider": "Interactive Brokers / OPRA",
        "dataset": "opra_snapshot_rights",
        "source_url": "https://www.interactivebrokers.com/en/pricing/market-data-pricing.php",
        "expected_cadence": "ON_DEMAND",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "enabled": False,
        "access_status": "RIGHTS_PENDING",
        "attribution": "OPRA via IBKR. Display subscription is not treated as archival rights.",
        "terms_notes": "Non-pro OPRA agreement: personal nonbusiness use; do not furnish to any other person. PostgreSQL snapshots need an explicit IBKR/OPRA storage answer.",
        "units_metadata": {},
    },
    {
        "source_id": "MSRB_EMMA",
        "provider": "MSRB EMMA",
        "dataset": "municipal_transactions",
        "source_url": "https://emma.msrb.org/",
        "expected_cadence": "D",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "enabled": False,
        "access_status": "CONFIGURATION_REQUIRED",
        "attribution": "Municipal transaction reporting is not FINRA TRACE.",
        "terms_notes": "No free unauthenticated EMMA API. Human must create an MSRB developer account/API key at https://emma.msrb.org/AboutEMMA/Developers. Do not scrape HTML.",
        "units_metadata": {},
    },
    {
        "source_id": "IBKR_MUNICIPAL_BONDS",
        "provider": "Interactive Brokers TWS",
        "dataset": "ibkr_municipal_discovery",
        "source_url": "",
        "expected_cadence": "ON_DEMAND",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "enabled": False,
        "access_status": "CONFIGURATION_REQUIRED",
        "attribution": "IBKR municipal discovery/quotes if later entitled.",
        "terms_notes": "Cash-muni quotes need an official CUSIP/ISIN (MSRB developer key or issuer prospectus). reqMatchingSymbols name-only rows are insufficient. Calculator remains available. Quote persistence RIGHTS_PENDING. INTERNAL_ONLY.",
        "units_metadata": {},
    },
    {
        "source_id": "IBKR_CORPORATE_BONDS",
        "provider": "Interactive Brokers TWS",
        "dataset": "ibkr_corporate_bond_quotes",
        "source_url": "",
        "expected_cadence": "INTRADAY",
        "usage_scope": EXPORT_INTERNAL_ONLY,
        "enabled": False,
        "access_status": "ENTITLEMENT_REQUIRED",
        "attribution": "Supplementary to FINRA Query aggregates.",
        "terms_notes": "CUSIP/ISIN resolves to conId via TWS reqContractDetails (symbol=CUSIP proven 2026-09-15). Live quotes need bond market-data entitlement; PostgreSQL archival remains RIGHTS_PENDING. FINRA Query aggregates remain the live activity feed. Collection off. INTERNAL_ONLY.",
        "units_metadata": {},
    },
    {
        "source_id": "CFTC_COT",
        "provider": "CFTC",
        "dataset": "commitment_of_traders",
        "source_url": "https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm",
        "expected_cadence": "W",
        "usage_scope": EXPORT_ATTRIBUTION_REQUIRED,
        "enabled": True,
        "access_status": "AVAILABLE",
        "attribution": "CFTC Public Reporting Environment, Legacy Futures-Only Commitments of Traders.",
        "terms_notes": "Legacy futures-only 6dca-aqww stays stored. Dashboard positioning uses TFF gpe5-46if and Disaggregated 72hh-3qpy. Position date is Tuesday. No publication timestamp is returned; Friday is the regular release day.",
        "units_metadata": {},
        "catalog_version": "cross_asset_v1",
    },
    {
        "source_id": "YAHOO_FX",
        "provider": "Yahoo Finance",
        "dataset": "fx_daily",
        "source_url": "https://finance.yahoo.com/",
        "expected_cadence": "D",
        "usage_scope": EXPORT_ATTRIBUTION_REQUIRED,
        "enabled": True,
        "access_status": "AVAILABLE",
        "attribution": "Yahoo Finance daily FX history.",
        "terms_notes": "Yahoo daily FX closes, including DX-Y.NYB. Weekday observations. Not an equity session calendar.",
        "units_metadata": {},
        "catalog_version": "cross_asset_v1",
    },
    {
        "source_id": "YAHOO_FUTURES_PROXY",
        "provider": "Yahoo Finance",
        "dataset": "commodity_futures_proxy",
        "source_url": "https://finance.yahoo.com/",
        "expected_cadence": "D",
        "usage_scope": EXPORT_ATTRIBUTION_REQUIRED,
        "enabled": True,
        "access_status": "AVAILABLE",
        "attribution": "Yahoo Finance futures-proxy daily history.",
        "terms_notes": "Yahoo futures proxies for market monitoring. Not an official continuous settlement or roll history.",
        "units_metadata": {},
        "catalog_version": "cross_asset_v1",
    },
    {
        "source_id": "YAHOO_CRYPTO",
        "provider": "Yahoo Finance",
        "dataset": "crypto_daily",
        "source_url": "https://finance.yahoo.com/",
        "expected_cadence": "D",
        "usage_scope": EXPORT_ATTRIBUTION_REQUIRED,
        "enabled": True,
        "access_status": "AVAILABLE",
        "attribution": "Yahoo Finance crypto daily history.",
        "terms_notes": "Yahoo BTC-USD and ETH-USD daily bars. Observation date is UTC and includes weekends.",
        "units_metadata": {},
        "catalog_version": "cross_asset_v1",
    },
    {
        "source_id": "EIA_ENERGY",
        "provider": "U.S. Energy Information Administration",
        "dataset": "petroleum_and_gas_statistics",
        "source_url": "https://www.eia.gov/",
        "expected_cadence": "W",
        "usage_scope": EXPORT_ATTRIBUTION_REQUIRED,
        "enabled": False,
        "access_status": "CONFIGURATION_REQUIRED",
        "attribution": "U.S. Energy Information Administration Open Data (v2).",
        "terms_notes": "Free API key required: https://www.eia.gov/opendata/. Missing EIA_API_KEY is CONFIGURATION_REQUIRED, not FAILED. FRED WTI/Henry Hub remain price fallbacks.",
        "units_metadata": {},
    },
)
