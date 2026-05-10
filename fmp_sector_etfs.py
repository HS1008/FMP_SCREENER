"""
SPDR Select Sector ETF proxies aligned to FMP sector names (GICS-style).

Used only for comparison; FMP sector performance remains the API aggregate.
"""

from __future__ import annotations

# State Street SPDR sector ETFs (US-listed)
SECTOR_ETF_PROXY: dict[str, str] = {
    "Basic Materials": "XLB",
    "Communication Services": "XLC",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Energy": "XLE",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Real Estate": "XLRE",
    "Technology": "XLK",
    "Utilities": "XLU",
}


def etf_to_sector() -> dict[str, str]:
    return {etf: sector for sector, etf in SECTOR_ETF_PROXY.items()}
