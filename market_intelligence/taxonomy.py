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
            *SECTOR_PROXIES.values(),
            *(proxy for mapping in INDUSTRY_PROXIES.values() for proxy in mapping.values()),
            *(m for basket in ALL_BASKETS for m in basket.members),
            "XLK",
        }
    )
)


def baskets_for_sector(sector: str) -> tuple[BasketDef, ...]:
    return tuple(b for b in ALL_BASKETS if b.parent_sector == sector)


__all__ = [
    "ALL_BASKETS",
    "BENCHMARK_SPY",
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
    "baskets_for_sector",
]
