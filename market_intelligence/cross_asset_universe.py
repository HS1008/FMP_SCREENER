"""Verified Yahoo symbols for FOREX, commodity futures proxies, and crypto.

Symbols were checked against Yahoo history on 2026-09-28. Charts use close,
not an adjusted series. Futures symbols are provider-maintained rolling
proxies, not an official continuous settlement history.
"""

from __future__ import annotations

from dataclasses import dataclass


YAHOO_FX_SOURCE = "YAHOO_FX"
YAHOO_FUTURES_SOURCE = "YAHOO_FUTURES_PROXY"
YAHOO_CRYPTO_SOURCE = "YAHOO_CRYPTO"
YAHOO_FX_DATASET = "fx_daily"
YAHOO_FUTURES_DATASET = "commodity_futures_proxy"
YAHOO_CRYPTO_DATASET = "crypto_daily"

FX_WINDOWS = (
    ("1D", 1),
    ("1W", 5),
    ("1M", 21),
    ("3M", 63),
    ("6M", 126),
    ("1Y", 252),
)
COMMODITY_WINDOWS = FX_WINDOWS
CRYPTO_WINDOWS = (
    ("1D", 1),
    ("7D", 7),
    ("1M", 30),
    ("3M", 90),
    ("1Y", 365),
)


@dataclass(frozen=True)
class YahooInstrument:
    instrument_id: str
    yahoo_symbol: str
    display_name: str
    asset_class: str
    source_id: str
    dataset: str
    group: str
    currency: str = "USD"
    base_currency: str | None = None
    quote_currency: str | None = None
    invert_vs_usd: bool = False


def _fx(
    instrument_id: str,
    yahoo_symbol: str,
    display_name: str,
    *,
    base: str,
    quote: str,
    invert_vs_usd: bool,
) -> YahooInstrument:
    return YahooInstrument(
        instrument_id,
        yahoo_symbol,
        display_name,
        "FX",
        YAHOO_FX_SOURCE,
        YAHOO_FX_DATASET,
        "forex",
        currency=quote,
        base_currency=base,
        quote_currency=quote,
        invert_vs_usd=invert_vs_usd,
    )


def _future(instrument_id: str, yahoo_symbol: str, display_name: str, group: str) -> YahooInstrument:
    return YahooInstrument(
        instrument_id,
        yahoo_symbol,
        display_name,
        "FUTURES_PROXY",
        YAHOO_FUTURES_SOURCE,
        YAHOO_FUTURES_DATASET,
        group,
    )


FX_INSTRUMENTS: tuple[YahooInstrument, ...] = (
    YahooInstrument("DXY", "DX-Y.NYB", "US Dollar Index", "INDEX", YAHOO_FX_SOURCE, YAHOO_FX_DATASET, "forex"),
    _fx("EURUSD", "EURUSD=X", "EUR/USD", base="EUR", quote="USD", invert_vs_usd=False),
    _fx("GBPUSD", "GBPUSD=X", "GBP/USD", base="GBP", quote="USD", invert_vs_usd=False),
    _fx("USDJPY", "USDJPY=X", "USD/JPY", base="USD", quote="JPY", invert_vs_usd=True),
    _fx("AUDUSD", "AUDUSD=X", "AUD/USD", base="AUD", quote="USD", invert_vs_usd=False),
    _fx("USDCAD", "USDCAD=X", "USD/CAD", base="USD", quote="CAD", invert_vs_usd=True),
    _fx("USDCHF", "USDCHF=X", "USD/CHF", base="USD", quote="CHF", invert_vs_usd=True),
)

COMMODITY_INSTRUMENTS: tuple[YahooInstrument, ...] = (
    _future("CL", "CL=F", "WTI crude", "energy"),
    _future("BZ", "BZ=F", "Brent crude", "energy"),
    _future("NG", "NG=F", "Natural gas", "energy"),
    _future("GC", "GC=F", "Gold", "metals"),
    _future("SI", "SI=F", "Silver", "metals"),
    _future("HG", "HG=F", "Copper", "metals"),
    _future("ZC", "ZC=F", "Corn", "agriculture"),
    _future("ZW", "ZW=F", "Wheat", "agriculture"),
    _future("ZS_F", "ZS=F", "Soybeans", "agriculture"),
    YahooInstrument("VIX", "^VIX", "VIX spot", "INDEX", YAHOO_FUTURES_SOURCE, YAHOO_FUTURES_DATASET, "volatility"),
)

CRYPTO_INSTRUMENTS: tuple[YahooInstrument, ...] = (
    YahooInstrument("BTC", "BTC-USD", "Bitcoin", "CRYPTO", YAHOO_CRYPTO_SOURCE, YAHOO_CRYPTO_DATASET, "crypto", base_currency="BTC", quote_currency="USD"),
    YahooInstrument("ETH", "ETH-USD", "Ethereum", "CRYPTO", YAHOO_CRYPTO_SOURCE, YAHOO_CRYPTO_DATASET, "crypto", base_currency="ETH", quote_currency="USD"),
)

YAHOO_CROSS_ASSET: tuple[YahooInstrument, ...] = FX_INSTRUMENTS + COMMODITY_INSTRUMENTS + CRYPTO_INSTRUMENTS
INSTRUMENT_BY_ID = {row.instrument_id: row for row in YAHOO_CROSS_ASSET}
INSTRUMENT_BY_SYMBOL = {row.yahoo_symbol: row for row in YAHOO_CROSS_ASSET}

CURRENCY_VS_USD: tuple[tuple[str, str], ...] = (
    ("EURUSD", "EUR"),
    ("GBPUSD", "GBP"),
    ("USDJPY", "JPY"),
    ("AUDUSD", "AUD"),
    ("USDCAD", "CAD"),
    ("USDCHF", "CHF"),
)

FRED_COMMODITY_SERIES = (
    ("DCOILWTICO", "WTI spot (FRED/EIA)"),
    ("DHHNGSP", "Henry Hub spot (FRED/EIA)"),
    ("PCOPPUSDM", "Global copper (FRED/IMF, monthly)"),
)

EIA_FUNDAMENTALS = (
    {
        "alias": "crude_stocks",
        "route": "petroleum/stoc/wstk/data",
        "series_id": "WCESTUS1",
        "label": "U.S. Commercial Crude Inventories",
        "frequency": "weekly",
    },
    {
        "alias": "cushing_crude_stocks",
        "route": "petroleum/stoc/wstk/data",
        "series_id": "WCESTCUS1",
        "label": "Cushing Crude Inventories",
        "frequency": "weekly",
    },
    {
        "alias": "crude_production",
        "route": "petroleum/sum/sndw/data",
        "series_id": "WCRFPUS2",
        "label": "U.S. Crude Oil Production",
        "frequency": "weekly",
    },
    {
        "alias": "working_gas_storage",
        "route": "natural-gas/stor/wkly/data",
        "series_id": "NW2_EPG0_SWO_R48_BCF",
        "label": "U.S. Natural Gas Storage",
        "frequency": "weekly",
    },
)
