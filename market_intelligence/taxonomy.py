"""Current-context sector / industry / custom-subgroup definitions.

These are display mappings, not official taxonomies and not point-in-time
classifications. Do not replay today's baskets into trusted backtests.
SMH and XSD are ETF comparisons, not mutually exclusive subindustries.

Sector keys use the repository's canonical labels (``sector_mapping.CANONICAL_SECTORS``,
``canonical_sectors_v1``): the XLK sector is ``"Technology"`` so independent EOD rows and
legacy rows describe the same sector. The GICS name "Information Technology" is accepted
as an input alias by ``sector_mapping.FMP_LEGACY_TO_CANONICAL``.
"""

from __future__ import annotations

from dataclasses import dataclass

from market_intelligence.sector_mapping import CANONICAL_SECTORS, canonical_sector_name

TAXONOMY_VERSION = "sector_hierarchy_current_v1"
KIND_SECTOR_PROXY = "SECTOR_ETF_PROXY"
KIND_INDUSTRY_PROXY = "INDUSTRY_ETF_PROXY"
KIND_CUSTOM_BASKET = "CUSTOM_EQUAL_DOLLAR_BASKET"
KIND_ETF_COMPARISON = "ETF_COMPARISON"
KIND_THEME = "CROSS_SECTOR_THEME"

SECTOR_PROXIES: dict[str, str] = {
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Technology": "XLK",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}

BENCHMARK_SPY = "SPY"
# Equal-weight S&P 500 ETF for dashboard comparison charts only (not Stage 2 research).
EQUAL_WEIGHT_SPX = "RSP"

# Investable ETF proxies for the Markets pages. These are current monitoring
# symbols, not a point-in-time research universe, and they are not added to
# UNIVERSE_SYMBOLS (that tuple drives IBKR equity coverage).
US_INDEX_ETFS: tuple[tuple[str, str], ...] = (
    ("RSP", "Equal-Weight S&P 500"),
    ("SPY", "S&P 500"),
    ("QQQ", "Nasdaq-100"),
    ("IWM", "Russell 2000"),
    ("DIA", "Dow Jones Industrial Average"),
)
US_PERFORMANCE_ETFS: tuple[tuple[str, str], ...] = US_INDEX_ETFS


def index_etf_label(symbol: str, name: str) -> str:
    """Performance pill and chart label: ticker beside the full index name."""
    return "{0} · {1}".format(symbol, name)


US_HEATMAP_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ", "IWM", "DIA", "RSP")
US_DRAWDOWN_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ", "IWM", "RSP", "DIA")
US_SNAPSHOT_SYMBOLS: tuple[str, ...] = tuple(symbol for symbol, _label in US_INDEX_ETFS)
US_LEADERSHIP: tuple[tuple[str, str, str], ...] = (
    (
        "RSP",
        "RSP / SPY",
        "Numerator RSP (Equal-Weight S&P 500), denominator SPY (S&P 500). Rising: equal-weight S&P 500 is outperforming cap-weighted S&P 500, usually broader participation within large-cap equities and less dependence on the largest stocks. Falling: cap-weighted SPY is outperforming equal-weight RSP, usually increasing concentration in larger constituents.",
    ),
    (
        "IWM",
        "IWM / SPY",
        "Numerator IWM (Russell 2000), denominator SPY (S&P 500). Rising: small caps are outperforming large caps. Falling: large caps are outperforming small caps.",
    ),
    (
        "QQQ",
        "QQQ / SPY",
        "Numerator QQQ (Nasdaq-100), denominator SPY (S&P 500). Rising: Nasdaq-100 / growth-heavy mega-cap exposure is outperforming the broader S&P 500. Falling: the broader S&P 500 is outperforming QQQ.",
    ),
    (
        "DIA",
        "DIA / SPY",
        "Numerator DIA (Dow 30), denominator SPY (S&P 500). Rising: Dow 30 / mature blue-chip exposure is outperforming the broader S&P 500. Falling: the broader S&P 500 is outperforming DIA. This compares the price-weighted Dow 30 with the cap-weighted S&P 500.",
    ),
)
GLOBAL_MARKET_ETFS: tuple[tuple[str, str], ...] = (
    ("SPY", "United States"),
    ("VEA", "Developed ex-US"),
    ("VGK", "Europe"),
    ("EWJ", "Japan"),
    ("VWO", "Emerging Markets"),
    ("MCHI", "China"),
    ("INDA", "India"),
    ("EWZ", "Brazil"),
)
GLOBAL_DEFAULT_SELECTED: tuple[str, ...] = ("SPY", "VEA", "VGK", "EWJ", "VWO")
GLOBAL_CORE_ETFS: tuple[tuple[str, str], ...] = (
    ("SPY", "United States"),
    ("VEA", "Developed ex-US"),
    ("VWO", "Emerging Markets"),
)
GLOBAL_LEADERSHIP: tuple[tuple[str, str, str], ...] = (
    (
        "VEA",
        "Developed ex-US vs US",
        "Developed ex-US leadership relative to the S&P 500. Rising means VEA outperformed SPY.",
    ),
    (
        "VWO",
        "Emerging Markets vs US",
        "Emerging-markets leadership relative to the S&P 500. Rising means VWO outperformed SPY.",
    ),
)
GLOBAL_SNAPSHOT_SYMBOLS: tuple[str, ...] = ("SPY", "VEA", "VWO", "EWJ")


def _ordered_symbols(*groups: tuple[str, ...] | tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
    ordered: list[str] = []
    for group in groups:
        for item in group:
            symbol = item if isinstance(item, str) else item[0]
            if symbol not in ordered:
                ordered.append(symbol)
    return tuple(ordered)


US_MARKET_SYMBOLS: tuple[str, ...] = _ordered_symbols(
    US_PERFORMANCE_ETFS,
    US_HEATMAP_SYMBOLS,
    US_DRAWDOWN_SYMBOLS,
    US_SNAPSHOT_SYMBOLS,
)
GLOBAL_MARKET_SYMBOLS: tuple[str, ...] = _ordered_symbols(GLOBAL_MARKET_ETFS)
MARKET_MONITOR_SYMBOLS: tuple[str, ...] = _ordered_symbols(US_MARKET_SYMBOLS, GLOBAL_MARKET_SYMBOLS)

# Industry ETF comparisons we actually have as listed ETFs. Empty means explicit unavailable.
INDUSTRY_PROXIES: dict[str, dict[str, str]] = {
    "Technology": {
        "Semiconductors ETF (SMH, comparison)": "SMH",
        "Equal-weight Semiconductors ETF (XSD, comparison)": "XSD",
    },
    "Financials": {"Regional Banks ETF (KRE, comparison)": "KRE"},
    "Health Care": {"Biotech ETF (XBI, comparison)": "XBI"},
    "Energy": {"E&P ETF (XOP, comparison)": "XOP"},
    "Consumer Discretionary": {"Retail ETF (XRT, comparison)": "XRT"},
    "Industrials": {},
    "Consumer Staples": {},
    "Communication Services": {},
    "Materials": {},
    "Real Estate": {},
    "Utilities": {},
}


@dataclass(frozen=True)
class BasketDef:
    key: str
    label: str
    parent_sector: str
    parent_industry: str | None
    members: tuple[str, ...]
    kind: str
    notes: str = ""


SEMI_BASKETS: tuple[BasketDef, ...] = (
    BasketDef("AI_COMPUTE_GPUS", "AI Compute / GPUs", "Technology", "Semiconductors", ("NVDA", "AMD"), KIND_CUSTOM_BASKET, "Curated current-context names, not an official subindustry."),
    BasketDef("SEMI_EQUIPMENT", "Semiconductor Equipment", "Technology", "Semiconductors", ("ASML", "AMAT", "LRCX", "KLAC"), KIND_CUSTOM_BASKET),
    BasketDef("MEMORY", "Memory", "Technology", "Semiconductors", ("MU",), KIND_CUSTOM_BASKET),
    BasketDef("NETWORKING", "Networking / Connectivity", "Technology", "Semiconductors", ("AVGO", "MRVL"), KIND_CUSTOM_BASKET),
    BasketDef("ANALOG_INDUSTRIAL", "Analog / Industrial", "Technology", "Semiconductors", ("TXN", "ADI", "ON"), KIND_CUSTOM_BASKET),
    BasketDef("FOUNDRY", "Foundry / Manufacturing", "Technology", "Semiconductors", ("TSM",), KIND_CUSTOM_BASKET),
    BasketDef("MOBILE_CONSUMER", "Mobile / Consumer Chips", "Technology", "Semiconductors", ("QCOM",), KIND_CUSTOM_BASKET),
)

SEMI_ETF_COMPARISONS: tuple[BasketDef, ...] = (
    BasketDef("SMH_ETF", "SMH (mega-cap semi ETF comparison)", "Technology", "Semiconductors", ("SMH",), KIND_ETF_COMPARISON, "ETF comparison, not a mutually exclusive subindustry."),
    BasketDef("XSD_ETF", "XSD (equal-weight semi ETF comparison)", "Technology", "Semiconductors", ("XSD",), KIND_ETF_COMPARISON, "ETF comparison, not a mutually exclusive subindustry."),
)

TECH_THEME_BASKETS: tuple[BasketDef, ...] = (
    BasketDef("CYBERSECURITY", "Cybersecurity", "Technology", "Software", ("CRWD", "PANW", "ZS", "FTNT"), KIND_CUSTOM_BASKET),
    BasketDef("ENTERPRISE_SOFTWARE", "Enterprise Software", "Technology", "Software", ("MSFT", "CRM", "NOW", "ORCL"), KIND_CUSTOM_BASKET),
    BasketDef("CLOUD_DATA", "Cloud / Data Infrastructure", "Technology", None, ("AMZN", "MSFT", "GOOGL"), KIND_THEME, "Cross-sector names; not an official industry."),
    BasketDef("DATACENTER_POWER", "Data Center Power & Cooling", "Industrials", None, ("VRT", "ETN", "NVT"), KIND_THEME),
    BasketDef("INTERNET_PLATFORMS", "Internet Platforms", "Communication Services", None, ("META", "GOOGL"), KIND_THEME),
)

INDUSTRY_ETF_COMPARISONS: tuple[BasketDef, ...] = (
    BasketDef("KRE_ETF", "KRE (regional banks ETF comparison)", "Financials", None, ("KRE",), KIND_ETF_COMPARISON, "ETF comparison, not an official industry taxonomy."),
    BasketDef("XBI_ETF", "XBI (biotech ETF comparison)", "Health Care", None, ("XBI",), KIND_ETF_COMPARISON, "ETF comparison, not an official industry taxonomy."),
    BasketDef("XOP_ETF", "XOP (E&P ETF comparison)", "Energy", None, ("XOP",), KIND_ETF_COMPARISON, "ETF comparison, not an official industry taxonomy."),
    BasketDef("XRT_ETF", "XRT (retail ETF comparison)", "Consumer Discretionary", None, ("XRT",), KIND_ETF_COMPARISON, "ETF comparison, not an official industry taxonomy."),
)

ALL_BASKETS: tuple[BasketDef, ...] = SEMI_BASKETS + SEMI_ETF_COMPARISONS + TECH_THEME_BASKETS + INDUSTRY_ETF_COMPARISONS

UNIVERSE_SYMBOLS: tuple[str, ...] = tuple(
    sorted(
        {
            BENCHMARK_SPY,
            EQUAL_WEIGHT_SPX,
            *SECTOR_PROXIES.values(),
            *(proxy for mapping in INDUSTRY_PROXIES.values() for proxy in mapping.values()),
            *(m for basket in ALL_BASKETS for m in basket.members),
            "XLK",
        }
    )
)


def baskets_for_sector(sector: str) -> tuple[BasketDef, ...]:
    return tuple(b for b in ALL_BASKETS if b.parent_sector == sector)


def stock_subsector_baskets() -> tuple[BasketDef, ...]:
    """Curated single-sector stock baskets.

    ETF comparisons and cross-sector themes are not subsectors. Themes stay
    available from ``cross_sector_themes`` so the page can name what it omits.
    """
    return tuple(basket for basket in ALL_BASKETS if basket.kind == KIND_CUSTOM_BASKET)


def cross_sector_themes() -> tuple[BasketDef, ...]:
    """Named themes that cross the parent-sector line. Not industry coverage."""
    return tuple(basket for basket in ALL_BASKETS if basket.kind == KIND_THEME)


# Display names for the curated monitor universe. Unknown tickers stay ticker-only.
COMPANY_LABELS: dict[str, str] = {
    "ADI": "Analog Devices Inc.",
    "AMAT": "Applied Materials Inc.",
    "AMD": "Advanced Micro Devices Inc.",
    "AMZN": "Amazon.com Inc.",
    "ASML": "ASML Holding",
    "AVGO": "Broadcom Inc.",
    "CRM": "Salesforce Inc.",
    "CRWD": "CrowdStrike Holdings Inc.",
    "DIA": "Dow Jones Industrial Average",
    "ETN": "Eaton Corp.",
    "FTNT": "Fortinet Inc.",
    "GOOGL": "Alphabet Inc.",
    "IWM": "Russell 2000",
    "KLAC": "KLA Corp.",
    "KRE": "SPDR S&P Regional Banking ETF",
    "LRCX": "Lam Research Corp.",
    "META": "Meta Platforms Inc.",
    "MRVL": "Marvell Technology Inc.",
    "MSFT": "Microsoft Corp.",
    "MU": "Micron Technology Inc.",
    "NOW": "ServiceNow Inc.",
    "NVDA": "NVIDIA Corp.",
    "NVT": "nVent Electric plc",
    "ON": "ON Semiconductor Corp.",
    "ORCL": "Oracle Corp.",
    "PANW": "Palo Alto Networks Inc.",
    "QCOM": "Qualcomm Inc.",
    "QQQ": "Invesco QQQ",
    "RSP": "Invesco S&P 500 Equal Weight",
    "SMH": "VanEck Semiconductor ETF",
    "SPY": "SPDR S&P 500",
    "TSM": "Taiwan Semiconductor",
    "TXN": "Texas Instruments Inc.",
    "VRT": "Vertiv Holdings",
    "XBI": "SPDR S&P Biotech ETF",
    "XLB": "Materials Select Sector SPDR",
    "XLC": "Communication Services Select Sector SPDR",
    "XLE": "Energy Select Sector SPDR",
    "XLF": "Financial Select Sector SPDR",
    "XLI": "Industrial Select Sector SPDR",
    "XLK": "Technology Select Sector SPDR",
    "XLP": "Consumer Staples Select Sector SPDR",
    "XLRE": "Real Estate Select Sector SPDR",
    "XLU": "Utilities Select Sector SPDR",
    "XLV": "Health Care Select Sector SPDR",
    "XLY": "Consumer Discretionary Select Sector SPDR",
    "XOP": "SPDR S&P Oil & Gas Exploration & Production ETF",
    "XRT": "SPDR S&P Retail ETF",
    "XSD": "SPDR S&P Semiconductor ETF",
    "ZS": "Zscaler Inc.",
}

NO_SUBSECTOR_CLASSIFICATION = "No subsector classification available for this sector"


def constituent_company_name(symbol: str) -> str:
    """Readable company name when one is curated. Otherwise the ticker itself."""
    ticker = str(symbol or "").upper()
    return COMPANY_LABELS.get(ticker) or ticker


def constituent_label(symbol: str, company: str | None = None) -> str:
    """External selector label. The ticker stays the stable internal value."""
    ticker = str(symbol or "").upper()
    supplied = str(company or "").strip()
    if not supplied or supplied.upper() == ticker:
        name = constituent_company_name(ticker)
    else:
        name = supplied
    if not name or name.upper() == ticker:
        return ticker
    return "{0} — {1}".format(ticker, name)


def canonical_basket_sector(parent_sector: str) -> str | None:
    """Normalize a basket parent onto a canonical sector. Themes stay ungrouped."""
    return canonical_sector_name(parent_sector)


def subsector_coverage() -> dict[str, dict[str, object]]:
    """One status for every canonical sector. Technology is not a special renderer."""
    grouped: dict[str, list[BasketDef]] = {name: [] for name in CANONICAL_SECTORS}
    for basket in stock_subsector_baskets():
        sector = canonical_basket_sector(basket.parent_sector)
        if sector is None or sector not in grouped:
            continue
        grouped[sector].append(basket)
    coverage: dict[str, dict[str, object]] = {}
    for name in CANONICAL_SECTORS:
        baskets = tuple(grouped[name])
        if baskets:
            coverage[name] = {"status": "available", "baskets": baskets, "reason": None}
        else:
            coverage[name] = {
                "status": "unavailable",
                "baskets": (),
                "reason": NO_SUBSECTOR_CLASSIFICATION,
            }
    return coverage


__all__ = [
    "ALL_BASKETS",
    "BENCHMARK_SPY",
    "EQUAL_WEIGHT_SPX",
    "GLOBAL_CORE_ETFS",
    "GLOBAL_DEFAULT_SELECTED",
    "GLOBAL_LEADERSHIP",
    "GLOBAL_MARKET_ETFS",
    "GLOBAL_MARKET_SYMBOLS",
    "GLOBAL_SNAPSHOT_SYMBOLS",
    "MARKET_MONITOR_SYMBOLS",
    "US_DRAWDOWN_SYMBOLS",
    "US_HEATMAP_SYMBOLS",
    "US_INDEX_ETFS",
    "US_LEADERSHIP",
    "US_MARKET_SYMBOLS",
    "US_PERFORMANCE_ETFS",
    "US_SNAPSHOT_SYMBOLS",
    "BasketDef",
    "INDUSTRY_ETF_COMPARISONS",
    "INDUSTRY_PROXIES",
    "KIND_CUSTOM_BASKET",
    "KIND_ETF_COMPARISON",
    "KIND_INDUSTRY_PROXY",
    "KIND_SECTOR_PROXY",
    "KIND_THEME",
    "SECTOR_PROXIES",
    "SEMI_BASKETS",
    "TAXONOMY_VERSION",
    "UNIVERSE_SYMBOLS",
    "COMPANY_LABELS",
    "NO_SUBSECTOR_CLASSIFICATION",
    "baskets_for_sector",
    "canonical_basket_sector",
    "constituent_company_name",
    "index_etf_label",
    "constituent_label",
    "cross_sector_themes",
    "stock_subsector_baskets",
    "subsector_coverage",
]
