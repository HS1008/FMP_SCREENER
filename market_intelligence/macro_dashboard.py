"""Macro dashboard grouping, display scales, and recession intervals.

Catalog categories stay canonical. These groups are only the Macro page layout.
Nothing here calls FRED, fills missing observations, or builds a composite score.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from market_intelligence.catalog import CATALOG_BY_ID, FRED_ATTRIBUTION, MACRO_MAX_BACKFILL_SERIES
from market_intelligence.fed_balance_sheet import BALANCE_SHEET_SERIES
from market_intelligence.components.market_chart import observation_day
from market_intelligence.history_range import filter_history_rows, union_history_bounds

VINTAGE_NOTE = (
    "Historical macro series use the latest stored FRED vintage unless explicitly identified otherwise."
)
LEADING_NOTE = "Curated leading indicators; not the Conference Board Leading Economic Index."
COINCIDENT_NOTE = "Curated coincident indicators; not a proprietary coincident index."
NFCI_NOTE = (
    "Positive values = tighter-than-average financial conditions. "
    "Negative values = looser-than-average financial conditions."
)
BREAKEVEN_NOTE = (
    "Market-implied inflation compensation, including risk and liquidity premiums. "
    "Not a survey expectation or a pure forecast."
)
CPI_COMPARE_NOTE = (
    "Year-over-year inflation. Headline and core are 12-month changes in the seasonally adjusted CPI indexes. "
    "Trimmed mean is the Cleveland Fed 16% trimmed-mean CPI, already published as a percent change from a year ago."
)
PCE_COMPARE_NOTE = (
    "Year-over-year inflation. Headline and core are 12-month changes in the seasonally adjusted PCE price indexes. "
    "Trimmed mean is the Dallas Fed trimmed-mean PCE, already published as a percent change from a year ago."
)
CPI_DECOMP_NOTE = (
    "Year-over-year percent change in each CPI index. Food, energy, core goods, and core services are mutually exclusive. "
    "These are component inflation rates, not contributions to headline CPI, and they do not sum to the headline rate."
)
PCE_DECOMP_NOTE = (
    "Year-over-year percent change in each PCE price index. Durable goods, nondurable goods, and services are mutually exclusive. "
    "Food and energy sit inside those categories. These are component inflation rates, not contributions, and they do not sum to headline PCE."
)

POLICY_RATES_DEFAULT_START = date(2000, 1, 1)

GROUP_ORDER: tuple[str, ...] = ("fed", "inflation", "labor", "housing", "growth", "fiscal", "leading", "coincident", "lagging")
GROUP_LABELS: dict[str, str] = {
    "fed": "Fed",
    "inflation": "Inflation",
    "labor": "Labor",
    "housing": "Housing",
    "growth": "Growth & Consumer",
    "fiscal": "Fiscal",
    "leading": "Leading indicators",
    "coincident": "Coincident indicators",
    "lagging": "Lagging indicators",
}
SHARED_2000_GROUPS = frozenset({"inflation", "labor", "housing", "growth", "fiscal"})

# One conversion from provider-native stored units. Not applied on top of level_display.
DISPLAY_SCALE: dict[str, dict[str, Any]] = {
    "WALCL": {"divisor": 1_000_000.0, "units": "USD tn", "source_units": "millions_usd"},
    "M2SL": {"divisor": 1_000.0, "units": "USD tn", "source_units": "billions_usd"},
    "WRESBAL": {"divisor": 1_000.0, "units": "USD bn", "source_units": "millions_usd"},
    "WTREGEN": {"divisor": 1_000.0, "units": "USD bn", "source_units": "millions_usd"},
    "RRPONTSYD": {"divisor": 1.0, "units": "USD bn", "source_units": "billions_usd"},
    # Monthly Treasury Statement millions. A negative divisor flips a deficit to a positive magnitude.
    "MTSDS133FMS.sum_12m": {"divisor": -1_000_000.0, "units": "USD tn", "source_units": "millions_usd"},
    "MTSR133FMS.sum_12m": {"divisor": 1_000_000.0, "units": "USD tn", "source_units": "millions_usd"},
    "MTSO133FMS.sum_12m": {"divisor": 1_000_000.0, "units": "USD tn", "source_units": "millions_usd"},
    "TREAS_GROSS_BILL": {"divisor": 1_000_000_000_000.0, "units": "USD tn", "source_units": "dollars"},
    "TREAS_GROSS_NOTE": {"divisor": 1_000_000_000_000.0, "units": "USD tn", "source_units": "dollars"},
    "TREAS_GROSS_BOND": {"divisor": 1_000_000_000_000.0, "units": "USD tn", "source_units": "dollars"},
}

CHART_TITLES: dict[str, tuple[str, ...]] = {
    "fed": (
        "Fed Policy Rates",
        "Federal Reserve Balance Sheet",
        "M2 Money Supply",
        "Financial Conditions",
    ),
    "inflation": (
        "Headline, Core, and Trimmed Mean CPI",
        "Headline, Core, and Trimmed Mean PCE",
        "CPI Decomposition",
        "PCE Decomposition",
        "Market Inflation Expectations",
    ),
    "labor": (
        "Unemployment Rate",
        "Nonfarm Payrolls",
        "Jobless Claims",
        "JOLTS Openings, Hires, and Quits",
        "Job Openings per Unemployed Worker",
        "Average Hourly Earnings",
    ),
    "housing": (
        "30-Year Mortgage Rate",
        "Building Permits and Housing Starts",
        "New Home Sales",
        "Months' Supply of New Houses",
        "Case-Shiller Home Prices",
        "NAHB Housing Market Index",
    ),
    "growth": (
        "Real GDP Growth",
        "Industrial Production",
        "ISM Manufacturing and Services",
        "ISM New Orders",
        "Real Personal Consumption Expenditures",
        "Real Disposable Personal Income",
        "Personal Saving Rate",
        "Credit Card Delinquency Rate",
    ),
    "fiscal": (
        "Rolling 12-Month Federal Deficit",
        "Federal Receipts and Outlays",
        "Debt Held by the Public / GDP",
        "Federal Interest Expense",
        "Treasury Issuance",
    ),
    "leading": (
        "Initial Jobless Claims",
        "Building Permits",
        "Core Capital Goods Orders",
        "Manufacturing Weekly Hours",
    ),
    "coincident": (
        "Nonfarm Payroll Growth",
        "Industrial Production",
        "Real Manufacturing & Trade Sales",
        "Real Consumer Spending",
        "Real Personal Income ex Transfers",
    ),
    "lagging": (
        "Unemployment Rate",
        "Median Duration of Unemployment",
        "Inventory-to-Sales Ratio",
        "C&I Loan Growth",
        "Business Loan Delinquency Rate",
        "Unit Labor Cost Growth",
    ),
}

# source is a series id or ``SERIES.metric`` id. scale marks raw observations that
# need DISPLAY_SCALE. kind is lines, policy, or toggle.
CHARTS: dict[str, tuple[dict[str, Any], ...]] = {
    "fed": (
        {
            "title": "Fed Policy Rates",
            "kind": "policy",
            "unit": "Percent",
            "format": "percent",
            "series": (
                ("DFEDTARL", "Target lower"),
                ("DFEDTARU", "Target upper"),
                ("DFF", "Effective Fed Funds"),
                ("SOFR", "SOFR"),
            ),
        },
        {
            "title": "Federal Reserve Balance Sheet",
            "kind": "balance_sheet",
            "unit": "Millions of USD",
            "series": tuple((series_id, label) for series_id, label, _section in BALANCE_SHEET_SERIES),
        },
        {
            "title": "M2 Money Supply",
            "kind": "toggle",
            "unit_by_mode": {"Level": "USD tn", "YoY %": "Percent"},
            "format_by_mode": {"Level": "number", "YoY %": "percent"},
            "modes": ("Level", "YoY %"),
            "default": "Level",
            "series_by_mode": {
                "Level": (("M2SL", "M2", True),),
                "YoY %": (("M2SL.yoy_pct", "M2 YoY", False),),
            },
        },
        {
            "title": "Financial Conditions",
            "kind": "lines",
            "unit": "Index",
            "format": "index",
            "reference": 0,
            "caption": NFCI_NOTE,
            "series": (("NFCI", "NFCI", False),),
        },
    ),
    "inflation": (
        {
            "title": "Headline, Core, and Trimmed Mean CPI",
            "kind": "multi",
            "unit": "Percent",
            "format": "percent",
            "caption": CPI_COMPARE_NOTE,
            "series": (
                ("CPIAUCSL.yoy_pct", "Headline", False),
                ("CPILFESL.yoy_pct", "Core", False),
                ("TRMMEANCPIM159SFRBCLE", "Trimmed mean", False),
            ),
        },
        {
            "title": "Headline, Core, and Trimmed Mean PCE",
            "kind": "multi",
            "unit": "Percent",
            "format": "percent",
            "caption": PCE_COMPARE_NOTE,
            "series": (
                ("PCEPI.yoy_pct", "Headline", False),
                ("PCEPILFE.yoy_pct", "Core", False),
                ("PCETRIM12M159SFRBDAL", "Trimmed mean", False),
            ),
        },
        {
            "title": "CPI Decomposition",
            "kind": "multi",
            "unit": "Percent",
            "format": "percent",
            "caption": CPI_DECOMP_NOTE,
            "series": (
                ("CPIUFDSL.yoy_pct", "Food", False),
                ("CPIENGSL.yoy_pct", "Energy", False),
                ("CUSR0000SACL1E.yoy_pct", "Core goods", False),
                ("CUSR0000SASLE.yoy_pct", "Core services", False),
            ),
        },
        {
            "title": "PCE Decomposition",
            "kind": "multi",
            "unit": "Percent",
            "format": "percent",
            "caption": PCE_DECOMP_NOTE,
            "series": (
                ("DDURRG3M086SBEA.yoy_pct", "Durable goods", False),
                ("DNDGRG3M086SBEA.yoy_pct", "Nondurable goods", False),
                ("DSERRG3M086SBEA.yoy_pct", "Services", False),
            ),
        },
        {
            "title": "Market Inflation Expectations",
            "kind": "lines",
            "unit": "Percent",
            "format": "percent",
            "caption": BREAKEVEN_NOTE,
            "series": (
                ("T5YIE", "5Y Breakeven", False),
                ("T10YIE", "10Y Breakeven", False),
                ("T5YIFR", "5Y5Y Forward", False),
            ),
        },
    ),
    "labor": (
        {
            "title": "Unemployment Rate",
            "kind": "multi",
            "unit": "Percent",
            "format": "percent",
            "caption": "U-3 is the official unemployment rate. U-6 adds marginal attachment and involuntary part-time work. Both are monthly and seasonally adjusted.",
            "series": (
                ("UNRATE", "U-3", False),
                ("U6RATE", "U-6", False),
            ),
        },
        {
            "title": "Nonfarm Payrolls",
            "kind": "multi",
            "unit": "Thousands of jobs",
            "format": "thousands",
            "caption": "Monthly change in total nonfarm payroll employment, and the mean of the latest three monthly changes. Not the employment level.",
            "styles": {"PAYEMS.mom_change": "histogram"},
            "series": (
                ("PAYEMS.mom_change", "Monthly change", False),
                ("PAYEMS.mom_change_ma3", "3-month average", False),
            ),
        },
        {
            "title": "Jobless Claims",
            "kind": "multi",
            "unit": "Persons, weekly, seasonally adjusted",
            "format": "claims",
            "caption": "Initial claims use the right axis. Continuing claims use the left axis. The 4-week average of initial claims is optional.",
            "default_off": ("ICSA.avg_4w",),
            "axes": {"CCSA": "left"},
            "series": (
                ("ICSA", "Initial claims", False),
                ("CCSA", "Continuing claims", False),
                ("ICSA.avg_4w", "Initial claims, 4-week average", False),
            ),
        },
        {
            "title": "JOLTS Openings, Hires, and Quits",
            "kind": "multi",
            "unit": "Thousands of persons, monthly, seasonally adjusted",
            "format": "thousands",
            "caption": "Total nonfarm job openings, hires, and quits. Layoffs and separations are not included.",
            "series": (
                ("JTSJOL", "Job openings", False),
                ("JTSHIL", "Hires", False),
                ("JTSQUL", "Quits", False),
            ),
        },
        {
            "title": "Job Openings per Unemployed Worker",
            "kind": "lines",
            "unit": "Openings per unemployed worker",
            "format": "ratio",
            "reference": 1.0,
            "caption": "Total nonfarm job openings divided by the unemployment level on the same month. Both inputs are thousands of persons. 1.0 means one opening per unemployed worker. A missing input stays missing.",
            "series": (("labor_tightness", "Openings / unemployed", False),),
            "derived": {
                "labor_tightness": {"op": "ratio", "numerator": "JTSJOL", "denominator": "UNEMPLOY", "scale": 1.0},
            },
        },
        {
            "title": "Average Hourly Earnings",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "caption": "Average hourly earnings of all private employees. YoY is the 12-month percent change. 3-month annualized is 100·((X_t/X_{t−3})^4 − 1).",
            "modes": ("YoY", "3M annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("CES0500000003.yoy_pct", "AHE YoY", False),),
                "3M annualized": (("CES0500000003.ann3m_pct", "AHE 3M annualized", False),),
            },
        },
    ),
    "housing": (
        {
            "title": "30-Year Mortgage Rate",
            "kind": "lines",
            "unit": "Percent",
            "format": "percent",
            "caption": "Freddie Mac 30-year fixed mortgage rate, weekly, not seasonally adjusted.",
            "series": (("MORTGAGE30US", "30-year fixed", False),),
        },
        {
            "title": "Building Permits and Housing Starts",
            "kind": "multi",
            "unit": "Thousands of units, SAAR",
            "format": "thousands",
            "caption": "New privately owned units. Both series are monthly seasonally adjusted annual rates.",
            "series": (
                ("PERMIT", "Building permits", False),
                ("HOUST", "Housing starts", False),
            ),
        },
        {
            "title": "New Home Sales",
            "kind": "lines",
            "unit": "Thousands, SAAR",
            "format": "thousands",
            "caption": "New one-family houses sold. Existing-home sales are not included.",
            "series": (("HSN1F", "New home sales", False),),
        },
        {
            "title": "Months' Supply of New Houses",
            "kind": "lines",
            "unit": "Months",
            "format": "number",
            "caption": "Months' supply of new houses, seasonally adjusted.",
            "series": (("MSACSR", "Months' supply", False),),
        },
        {
            "title": "Case-Shiller Home Prices",
            "kind": "toggle",
            "unit_by_mode": {"YoY %": "Percent", "Index": "Index, January 2000 = 100"},
            "format_by_mode": {"YoY %": "percent", "Index": "index"},
            "caption": "S&P CoreLogic Case-Shiller U.S. national index, not seasonally adjusted. YoY uses the 12-month percent change of the index.",
            "modes": ("YoY %", "Index"),
            "default": "YoY %",
            "series_by_mode": {
                "YoY %": (("CSUSHPINSA.yoy_pct", "Case-Shiller YoY", False),),
                "Index": (("CSUSHPINSA", "Case-Shiller index", False),),
            },
        },
        {
            "title": "NAHB Housing Market Index",
            "kind": "unavailable",
            "caption": "The NAHB/Wells Fargo Housing Market Index is not shown. FRED no longer carries this proprietary series, and NAHB does not publish a free structured historical API.",
        },
    ),
    "growth": (
        {
            "title": "Real GDP Growth",
            "heading": "Economic Growth",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "caption": "Real GDP, billions of chained 2017 dollars. QoQ annualized is 100·((X_t/X_{t−1 quarter})^4 − 1). It is not the level and not the year-over-year rate.",
            "modes": ("QoQ annualized", "YoY"),
            "default": "QoQ annualized",
            "series_by_mode": {
                "QoQ annualized": (("GDPC1.qoq_saar_pct", "Real GDP QoQ annualized", False),),
                "YoY": (("GDPC1.yoy_pct", "Real GDP YoY", False),),
            },
        },
        {
            "title": "Industrial Production",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "caption": "Industrial production index, 2017=100, seasonally adjusted. YoY is the 12-month percent change.",
            "modes": ("YoY", "3M annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("INDPRO.yoy_pct", "Industrial production YoY", False),),
                "3M annualized": (("INDPRO.ann3m_pct", "Industrial production 3M annualized", False),),
            },
        },
        {
            "title": "ISM Manufacturing and Services",
            "heading": "Business Cycle",
            "kind": "unavailable",
            "caption": "ISM manufacturing and services PMIs are not shown. ISM withdrew redistribution, and FRED removed the series in 2016. No free structured replacement is used.",
        },
        {
            "title": "ISM New Orders",
            "kind": "unavailable",
            "caption": "The ISM manufacturing new orders index is not shown. Census durable-goods orders are a different series and are not substituted here.",
        },
        {
            "title": "Real Personal Consumption Expenditures",
            "heading": "Consumer",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "caption": "Real personal consumption expenditures, chained 2017 dollars, seasonally adjusted annual rate. Not nominal PCE.",
            "modes": ("YoY", "3M annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("PCEC96.yoy_pct", "Real PCE YoY", False),),
                "3M annualized": (("PCEC96.ann3m_pct", "Real PCE 3M annualized", False),),
            },
        },
        {
            "title": "Real Disposable Personal Income",
            "kind": "lines",
            "unit": "Percent",
            "format": "percent",
            "caption": "Year-over-year percent change in real disposable personal income, chained 2017 dollars.",
            "series": (("DSPIC96.yoy_pct", "Real disposable income YoY", False),),
        },
        {
            "title": "Personal Saving Rate",
            "kind": "lines",
            "unit": "Percent of disposable income",
            "format": "percent",
            "caption": "Personal saving as a percent of disposable personal income, monthly, seasonally adjusted.",
            "series": (("PSAVERT", "Personal saving rate", False),),
        },
        {
            "title": "Credit Card Delinquency Rate",
            "kind": "lines",
            "unit": "Percent, quarterly",
            "format": "percent",
            "caption": "Delinquency rate on credit card loans at all commercial banks. Quarterly observations stay on their quarter dates and are not filled forward.",
            "series": (("DRCCLACBS", "Credit card delinquency", False),),
        },
    ),
    "fiscal": (
        {
            "title": "Rolling 12-Month Federal Deficit",
            "heading": "Budget",
            "kind": "lines",
            "unit": "USD tn",
            "format": "number",
            "caption": "Trailing 12 monthly Treasury balances, sign flipped. A positive value is a deficit. A negative value is a 12-month surplus. Not a fiscal-year sum.",
            "series": (("MTSDS133FMS.sum_12m", "12-month deficit", True),),
        },
        {
            "title": "Federal Receipts and Outlays",
            "kind": "multi",
            "unit": "USD tn",
            "format": "number",
            "caption": "Trailing 12-month sums of monthly Treasury receipts and outlays. Same window as the deficit.",
            "series": (
                ("MTSR133FMS.sum_12m", "Receipts", True),
                ("MTSO133FMS.sum_12m", "Outlays", True),
            ),
        },
        {
            "title": "Debt Held by the Public / GDP",
            "heading": "Debt",
            "kind": "lines",
            "unit": "Percent of GDP",
            "format": "percent",
            "caption": "Federal debt held by the public as a percent of GDP. This is not gross federal debt.",
            "series": (("FYGFGDQ188S", "Debt held by the public / GDP", False),),
        },
        {
            "title": "Federal Interest Expense",
            "heading": "Debt Service",
            "kind": "toggle",
            "unit_by_mode": {"Interest expense": "USD bn, quarterly SAAR", "Interest / receipts": "Percent of receipts"},
            "format_by_mode": {"Interest expense": "number", "Interest / receipts": "percent"},
            "caption": "BEA federal interest payments and current receipts are both quarterly SAAR billions. The ratio is 100 times interest divided by receipts in the same quarter. It is not a monthly Treasury interest figure divided by one month of receipts.",
            "modes": ("Interest expense", "Interest / receipts"),
            "default": "Interest expense",
            "series_by_mode": {
                "Interest expense": (("A091RC1Q027SBEA", "Interest expense", False),),
                "Interest / receipts": (("interest_burden", "Interest / receipts", False),),
            },
            "derived": {
                "interest_burden": {"op": "ratio", "numerator": "A091RC1Q027SBEA", "denominator": "FGRECPT", "scale": 100.0},
            },
        },
        {
            "title": "Treasury Issuance",
            "heading": "Treasury Financing",
            "kind": "multi",
            "unit": "USD tn, gross accepted",
            "format": "number",
            "caption": "Gross accepted auction amounts by calendar month of the auction date. Bills, notes, and bonds only. TIPS, floating-rate notes, and cash-management bills are excluded. This is not net issuance.",
            "series": (
                ("TREAS_GROSS_BILL", "Bills", True),
                ("TREAS_GROSS_NOTE", "Notes", True),
                ("TREAS_GROSS_BOND", "Bonds", True),
            ),
        },
    ),
    "leading": (
        {
            "title": "Initial Jobless Claims",
            "kind": "lines",
            "unit": "Claims",
            "format": "claims",
            "series": (
                ("ICSA", "Weekly claims", False),
                ("ICSA.avg_4w", "4-week average", False),
            ),
        },
        {
            "title": "Building Permits",
            "kind": "toggle",
            "unit_by_mode": {"Level": "SAAR, thousands", "YoY %": "Percent"},
            "format_by_mode": {"Level": "thousands", "YoY %": "percent"},
            "modes": ("Level", "YoY %"),
            "default": "Level",
            "series_by_mode": {
                "Level": (("PERMIT", "Building permits", False),),
                "YoY %": (("PERMIT.yoy_pct", "Building permits YoY", False),),
            },
        },
        {
            "title": "Core Capital Goods Orders",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "modes": ("YoY", "3M annualized", "6M annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("NEWORDER.yoy_pct", "Core capital goods YoY", False),),
                "3M annualized": (("NEWORDER.ann3m_pct", "Core capital goods 3M ann.", False),),
                "6M annualized": (("NEWORDER.ann6m_pct", "Core capital goods 6M ann.", False),),
            },
        },
        {
            "title": "Manufacturing Weekly Hours",
            "kind": "lines",
            "unit": "Hours",
            "format": "hours",
            "series": (("AWHMAN", "Manufacturing hours", False),),
        },
    ),
    "coincident": (
        {
            "title": "Nonfarm Payroll Growth",
            "kind": "lines",
            "unit": "Thousands of persons",
            "format": "thousands",
            "series": (("PAYEMS.mom_change", "Monthly payroll change", False),),
        },
        {
            "title": "Industrial Production",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "modes": ("YoY", "3M annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("INDPRO.yoy_pct", "Industrial production YoY", False),),
                "3M annualized": (("INDPRO.ann3m_pct", "Industrial production 3M ann.", False),),
            },
        },
        {
            "title": "Real Manufacturing & Trade Sales",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "modes": ("YoY", "3M annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("CMRMT.yoy_pct", "Real sales YoY", False),),
                "3M annualized": (("CMRMT.ann3m_pct", "Real sales 3M ann.", False),),
            },
        },
        {
            "title": "Real Consumer Spending",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "modes": ("YoY", "3M annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("PCEC96.yoy_pct", "Real consumption YoY", False),),
                "3M annualized": (("PCEC96.ann3m_pct", "Real consumption 3M ann.", False),),
            },
        },
        {
            "title": "Real Personal Income ex Transfers",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "modes": ("YoY", "3M annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("W875RX1.yoy_pct", "Real income ex transfers YoY", False),),
                "3M annualized": (("W875RX1.ann3m_pct", "Real income ex transfers 3M ann.", False),),
            },
        },
    ),
    "lagging": (
        {
            "title": "Unemployment Rate",
            "kind": "lines",
            "unit": "Percent",
            "format": "percent",
            "series": (("UNRATE", "Unemployment rate", False),),
        },
        {
            "title": "Median Duration of Unemployment",
            "kind": "lines",
            "unit": "Weeks",
            "format": "weeks",
            "series": (("UEMPMED", "Median duration", False),),
        },
        {
            "title": "Inventory-to-Sales Ratio",
            "kind": "lines",
            "unit": "Ratio",
            "format": "ratio",
            "series": (("ISRATIO", "Inventory / sales", False),),
        },
        {
            "title": "C&I Loan Growth",
            "kind": "lines",
            "unit": "Percent",
            "format": "percent",
            "series": (("BUSLOANS.yoy_pct", "C&I loans YoY", False),),
        },
        {
            "title": "Business Loan Delinquency Rate",
            "kind": "lines",
            "unit": "Percent",
            "format": "percent",
            "series": (("DRBLACBS", "Business loan delinquency", False),),
        },
        {
            "title": "Unit Labor Cost Growth",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "modes": ("YoY", "QoQ annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("ULCNFB.yoy_pct", "Unit labor costs YoY", False),),
                "QoQ annualized": (("ULCNFB.qoq_saar_pct", "Unit labor costs QoQ ann.", False),),
            },
        },
    ),
}


def scale_level(series_id: str, value: Any) -> float | None:
    """Convert one stored raw observation into the chart unit. Missing stays missing."""
    spec = DISPLAY_SCALE[series_id]
    if value is None:
        return None
    number = float(value)
    return number / float(spec["divisor"])


def recession_intervals(rows: Sequence[Mapping[str, Any]], *, date_key: str = "as_of") -> list[dict[str, str]]:
    """Contiguous USREC == 1 observations become one inclusive interval.

    A stored value other than 1 ends the interval. Missing rows are skipped and
    do not become a recession. Dates are not extended before the first 1 or
    after the last 1.
    """
    points: list[tuple[date, float]] = []
    for row in rows:
        day = observation_day(row.get(date_key) if date_key in row else row.get("observation_date"))
        raw = row.get("value")
        if day is None or raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        points.append((day, value))
    points.sort(key=lambda item: item[0])
    intervals: list[dict[str, str]] = []
    start: date | None = None
    end: date | None = None
    for day, value in points:
        if value == 1.0:
            if start is None:
                start = day
            end = day
            continue
        if start is not None and end is not None:
            intervals.append({"start": start.isoformat(), "end": end.isoformat()})
        start = None
        end = None
    if start is not None and end is not None:
        intervals.append({"start": start.isoformat(), "end": end.isoformat()})
    return intervals


def derived_ids(group: str) -> set[str]:
    found: set[str] = set()
    for chart in CHARTS[group]:
        found.update((chart.get("derived") or {}).keys())
    return found


def group_source_ids(group: str) -> list[str]:
    """Series and metric ids the group charts read, plus USREC shading."""
    found: list[str] = []
    seen: set[str] = set()
    computed = derived_ids(group)

    def add(source_id: str) -> None:
        if source_id in computed or source_id in seen:
            return
        seen.add(source_id)
        found.append(source_id)

    for chart in CHARTS[group]:
        for item in chart.get("series") or ():
            add(item[0])
        for mode_rows in (chart.get("series_by_mode") or {}).values():
            for item in mode_rows:
                add(item[0])
        for spec in (chart.get("derived") or {}).values():
            add(str(spec["numerator"]))
            add(str(spec["denominator"]))
        members = chart.get("members") or {}
        windows = chart.get("windows") or {}
        for series_id in members.values():
            for metric in windows.values():
                add("{0}.{1}".format(series_id, metric))
    add("USREC")
    return found


def aligned_ratio(
    numerator: Sequence[Mapping[str, Any]],
    denominator: Sequence[Mapping[str, Any]],
    *,
    scale: float = 1.0,
) -> list[dict[str, Any]]:
    """Divide matching observation dates. A missing input or a zero denominator is omitted."""
    bottoms: dict[date, float] = {}
    for row in denominator:
        day = observation_day(row.get("as_of"))
        raw = row.get("value")
        if day is None or raw is None:
            continue
        bottoms[day] = float(raw)
    points: list[dict[str, Any]] = []
    for row in numerator:
        day = observation_day(row.get("as_of"))
        raw = row.get("value")
        if day is None or raw is None or day not in bottoms:
            continue
        bottom = bottoms[day]
        if bottom == 0:
            continue
        points.append({"as_of": day, "value": float(scale) * float(raw) / bottom})
    points.sort(key=lambda item: item["as_of"])
    return points


def materialize_derived(group: str, histories: dict[str, list[dict[str, Any]]]) -> None:
    """Fill chart-only ratios from series already loaded for the group."""
    for chart in CHARTS[group]:
        for key, spec in (chart.get("derived") or {}).items():
            if spec.get("op") != "ratio":
                continue
            histories[key] = aligned_ratio(
                histories.get(str(spec["numerator"])) or [],
                histories.get(str(spec["denominator"])) or [],
                scale=float(spec.get("scale") or 1.0),
            )


def selected_lines(
    chart: Mapping[str, Any],
    *,
    mode: str | None = None,
    series_mode: str | None = None,
    window_mode: str | None = None,
    chosen: Sequence[str] | None = None,
) -> list[tuple[str, str, bool]]:
    """``(source_id, label, scale)`` for the active toggle. One unit family only.

    ``chosen`` filters a multiselect chart. ``None`` keeps every configured series.
    An empty selection stays empty.
    """
    kind = chart["kind"]
    if kind in {"lines", "multi"}:
        rows = [(item[0], item[1], bool(item[2])) for item in chart["series"]]
        if kind == "multi" and chosen is not None:
            wanted = set(chosen)
            return [row for row in rows if row[0] in wanted]
        return rows
    if kind == "toggle":
        chosen = mode or chart["default"]
        rows = chart["series_by_mode"][chosen]
        return [(item[0], item[1], bool(item[2])) for item in rows]
    if kind == "momentum":
        series_choice = series_mode or chart["default_series"]
        window_choice = window_mode or chart["default_window"]
        member_names = list(chart["members"]) if series_choice == "Both" else [series_choice]
        window_names = list(chart["windows"]) if window_choice == "Both" else [window_choice]
        lines: list[tuple[str, str, bool]] = []
        for member_name in member_names:
            series_id = chart["members"][member_name]
            for window_name in window_names:
                metric = chart["windows"][window_name]
                lines.append(("{0}.{1}".format(series_id, metric), "{0} {1}".format(member_name, window_name), False))
        return lines
    return []


def chart_unit(chart: Mapping[str, Any], *, mode: str | None = None) -> str:
    if chart.get("unit_by_mode"):
        return str(chart["unit_by_mode"][mode or chart["default"]])
    return str(chart.get("unit") or "")


def chart_format(chart: Mapping[str, Any], *, mode: str | None = None) -> str:
    if chart.get("format_by_mode"):
        return str(chart["format_by_mode"][mode or chart["default"]])
    return str(chart.get("format") or "number")


def history_window_bounds(
    groups: Sequence[Sequence[Mapping[str, Any]]],
) -> tuple[date | None, date | None]:
    """Union of real observations. A later-starting series does not move the start."""
    return union_history_bounds(groups, date_key="as_of")


def rows_in_range(
    rows: Sequence[Mapping[str, Any]],
    *,
    start: date | None,
    end: date | None,
) -> list[dict[str, Any]]:
    """Inclusive filter. From after To yields an empty list and changes nothing."""
    if start is None or end is None or start > end:
        return []
    return filter_history_rows(rows, start=start, end=end, date_key="as_of")


def prepare_line_points(
    rows: Sequence[Mapping[str, Any]],
    *,
    start: date,
    end: date,
    scale_series: str | None = None,
) -> list[dict[str, Any]]:
    """Chart points inside the window. Missing values are omitted, never zero."""
    points: list[dict[str, Any]] = []
    for row in rows_in_range(rows, start=start, end=end):
        raw = row.get("value")
        if raw is None:
            continue
        value = scale_level(scale_series, raw) if scale_series else float(raw)
        if value is None:
            continue
        points.append({"as_of": row.get("as_of"), "value": value})
    return points


def policy_rate_frame(
    lower: Sequence[Mapping[str, Any]],
    upper: Sequence[Mapping[str, Any]],
    effective: Sequence[Mapping[str, Any]],
    sofr: Sequence[Mapping[str, Any]],
    bands: Sequence[Mapping[str, str]],
    *,
    start: date,
    end: date,
) -> dict[str, Any]:
    """Aligned policy series. The span is upper minus lower on dates where both exist.

    No target is copied onto a date that lacks that print. DFF and SOFR stay on
    their own observation dates.
    """

    def lookup(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
        out: dict[str, float] = {}
        for row in rows_in_range(rows, start=start, end=end):
            day = observation_day(row.get("as_of"))
            if day is None or row.get("value") is None:
                continue
            out[day.isoformat()] = float(row["value"])
        return out

    lower_map = lookup(lower)
    upper_map = lookup(upper)
    dff_map = lookup(effective)
    sofr_map = lookup(sofr)
    days = sorted(set(lower_map) | set(upper_map) | set(dff_map) | set(sofr_map))

    def column(values: Mapping[str, float]) -> list[list[Any]]:
        return [[day, values[day]] if day in values else [day, None] for day in days]

    span: list[list[Any]] = []
    for day in days:
        if day in lower_map and day in upper_map:
            span.append([day, upper_map[day] - lower_map[day]])
        else:
            span.append([day, None])
    clipped: list[dict[str, str]] = []
    if days:
        first, last = days[0], days[-1]
        for band in bands:
            band_start = str(band.get("start") or "")
            band_end = str(band.get("end") or "")
            if not band_start or not band_end or band_end < first or band_start > last:
                continue
            clipped.append({"start": max(band_start, first), "end": min(band_end, last)})
    return {
        "categories": days,
        "lower": column(lower_map),
        "upper": column(upper_map),
        "span": span,
        "effective": column(dff_map),
        "sofr": column(sofr_map),
        "bands": clipped,
    }


def methodology_lines(group: str) -> list[str]:
    """Series identity, units, frequency, and the shared vintage sentence."""
    lines = [VINTAGE_NOTE, "FRED is the retrieval layer. Stored PostgreSQL observations are the chart source."]
    if group == "leading":
        lines.append(LEADING_NOTE)
    if group == "coincident":
        lines.append(COINCIDENT_NOTE)
    if group == "labor":
        lines.append(
            "Payroll changes are monthly differences in thousands of jobs. "
            "The 3-month average is the mean of those three differences. "
            "Openings per unemployed worker uses JTSJOL divided by UNEMPLOY on the same month."
        )
    if group == "housing":
        lines.append(
            "Permits and starts are both thousands of units at a seasonally adjusted annual rate. "
            "Case-Shiller YoY is the 12-month percent change of the not-seasonally-adjusted national index. "
            "The NAHB Housing Market Index is omitted because no free structured history is available."
        )
    if group == "growth":
        lines.append(
            "Real GDP QoQ annualized is 100·((X_t/X_{t−1 quarter})^4 − 1) of the chained-dollar level. "
            "Real PCE and real disposable income are chained 2017 dollars, not nominal. "
            "ISM indexes are omitted because ISM withdrew redistribution. "
            "Credit-card delinquency stays on its quarterly dates."
        )
    if group == "fiscal":
        lines.append(
            "The deficit chart is the trailing 12 monthly Treasury balances with the sign flipped, in trillions. "
            "A positive value is a deficit. Receipts and outlays use that same 12-month sum. "
            "Interest / receipts is the same-quarter ratio of two BEA SAAR billion series. "
            "Treasury issuance is gross accepted auction dollars for bills, notes, and bonds, excluding TIPS, FRNs, and cash-management bills."
        )
    if group == "inflation":
        lines.append(
            "Trimmed-mean CPI and PCE are the published 12-month percent changes. "
            "They are not transformed again, and they are not median inflation or one-month annualized rates. "
            "Decomposition charts show component year-over-year inflation rates. "
            "They are not stacked, and they are not contributions to headline inflation."
        )
        lines.append(BREAKEVEN_NOTE)
    lines.append(
        "YoY = 100·(X_t/X_{t−12} − 1). "
        "3M annualized = 100·((X_t/X_{t−3})^4 − 1). "
        "6M annualized = 100·((X_t/X_{t−6})^2 − 1). "
        "Quarterly annualized = 100·((X_t/X_{t−1})^4 − 1). "
        "Lags use the calendar date. A missing lag stays missing."
    )
    lines.append(
        "C&I loan growth is the monthly calendar YoY of BUSLOANS. "
        "Payroll growth is the monthly difference in thousands of persons. "
        "Claims 4-week average is the mean of the last four stored weekly prints."
    )
    lines.append(
        "The balance-sheet table shows H.4.1 Wednesday levels in millions of USD. "
        "Reserve balances use WRBWFRBL, the TGA uses WDTGAL, and reverse repo uses WLRRAL. "
        "Those are not the week-average series WRESBAL and WTREGEN, and not daily RRPONTSYD. "
        "M2 billions ÷ 1,000 = trillions on the M2 chart. Raw rows are not rewritten."
    )
    seen: set[str] = set()
    for source_id in group_source_ids(group):
        series_id = source_id.split(".", 1)[0]
        if series_id in seen:
            continue
        seen.add(series_id)
        spec = CATALOG_BY_ID.get(series_id)
        if spec is None:
            continue
        units = ", ".join(spec.expected_units_contains)
        lines.append(
            "{0} ({1}): {2}; frequency {3}; seasonal adjustment {4}. {5}".format(
                spec.label or series_id,
                series_id,
                units,
                spec.expected_frequency,
                spec.expected_sa or "n/a",
                spec.notes,
            )
        )
    lines.append(FRED_ATTRIBUTION)
    if "USREC" not in seen:
        lines.append("USREC shades NBER recession months. It is not plotted as its own series.")
    return lines


def macro_series_ids() -> tuple[str, ...]:
    return MACRO_MAX_BACKFILL_SERIES
