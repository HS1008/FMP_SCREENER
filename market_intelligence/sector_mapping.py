"""Versioned canonical sector mapping. Provider labels are retained alongside canonical names.

AI (and other ETF baskets such as semiconductors) are *themes*, not canonical sectors.
Unknown labels are quarantined (``canonical=None``) rather than silently assigned.
"""

from __future__ import annotations

from dataclasses import dataclass

CLASSIFICATION_VERSION = "canonical_sectors_v1"

CANONICAL_SECTORS: tuple[str, ...] = (
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Health Care",
    "Industrials",
    "Materials",
    "Real Estate",
    "Technology",
    "Utilities",
)

# FMP legacy label -> canonical label
FMP_LEGACY_TO_CANONICAL: dict[str, str] = {
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Financial Services": "Financials",
    "Basic Materials": "Materials",
    "Healthcare": "Health Care",
    "Communication Services": "Communication Services",
    "Energy": "Energy",
    "Industrials": "Industrials",
    "Real Estate": "Real Estate",
    "Technology": "Technology",
    "Utilities": "Utilities",
}

THEMES: dict[str, dict[str, str]] = {
    "AI": {"theme_key": "THEME:AI", "label": "Artificial Intelligence", "proxy": "AIQ"},
    "Semiconductors": {"theme_key": "THEME:SEMIS", "label": "Semiconductors", "proxy": "SOXX"},
}

SECTOR_ETF_PROXY: dict[str, str] = {
    "Materials": "XLB",
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Real Estate": "XLRE",
    "Technology": "XLK",
    "Utilities": "XLU",
}


@dataclass(frozen=True)
class SectorResolution:
    provider_label: str
    entity_kind: str  # SECTOR | THEME | UNKNOWN
    sector_key: str
    canonical_sector: str | None
    classification_version: str = CLASSIFICATION_VERSION

    @property
    def quarantined(self) -> bool:
        return self.entity_kind == "UNKNOWN"


def slug_to_label(slug: str) -> str:
    return str(slug).replace("_", " ").replace("-", "/").strip()


def resolve_provider_sector(label: str) -> SectorResolution:
    raw = str(label).strip()
    canonical = FMP_LEGACY_TO_CANONICAL.get(raw)
    if canonical is not None:
        return SectorResolution(raw, "SECTOR", canonical, canonical)
    if raw in CANONICAL_SECTORS:
        return SectorResolution(raw, "SECTOR", raw, raw)
    theme = THEMES.get(raw)
    if theme is not None:
        return SectorResolution(raw, "THEME", theme["theme_key"], None)
    return SectorResolution(raw, "UNKNOWN", "UNKNOWN:{0}".format(raw or "<blank>"), None)


__all__ = [
    "CANONICAL_SECTORS",
    "CLASSIFICATION_VERSION",
    "FMP_LEGACY_TO_CANONICAL",
    "SECTOR_ETF_PROXY",
    "THEMES",
    "SectorResolution",
    "resolve_provider_sector",
    "slug_to_label",
]
