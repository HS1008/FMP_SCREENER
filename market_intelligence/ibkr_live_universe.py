"""Canonical dashboard quote universe.

Display groups may repeat a ticker (NVDA, TSLA, SPCX). One contract per symbol.
VIX is included. Yahoo quotes use ``yahoo_symbol``; VIX is ``^VIX``.
The eight Power names are part of this universe.
"""

from __future__ import annotations

from typing import Any, Mapping

APPROVED_EXTRA_INDEXES = frozenset({"VIX"})

INDEX_ETFS: tuple[tuple[str, str], ...] = (
    ("SPY", "S&P 500"),
    ("QQQ", "Nasdaq-100"),
    ("RSP", "Equal-Weight S&P 500"),
    ("IWM", "Russell 2000"),
    ("DIA", "Dow Jones Industrial Average"),
)

SECTOR_ETFS: tuple[tuple[str, str], ...] = (
    ("XLK", "Tech"),
    ("XLF", "Financials"),
    ("XLV", "Health Care"),
    ("XLY", "Consumer Discretionary"),
    ("XLC", "Communication Services"),
    ("XLI", "Industrials"),
    ("XLP", "Consumer Staples"),
    ("XLE", "Energy"),
    ("XLB", "Materials"),
    ("XLRE", "Real Estate"),
    ("XLU", "Utilities"),
)

SUBSECTOR_ETFS: tuple[tuple[str, str, str], ...] = (
    ("SMH", "Semiconductors", "Technology"),
    ("IGV", "Software", "Technology"),
    ("CIBR", "Cybersecurity", "Technology"),
    ("KRE", "Regional banks", "Financials"),
    ("KIE", "Insurance", "Financials"),
    ("KCE", "Capital Markets", "Financials"),
    ("XBI", "Biotechnology", "Health Care"),
    ("XPH", "Pharmaceuticals", "Health Care"),
    ("IHI", "Medical Devices", "Health Care"),
    ("XHS", "Health Care Services/Providers", "Health Care"),
    ("XRT", "Retail", "Consumer Discretionary"),
    ("XHB", "Homebuilders", "Consumer Discretionary"),
    ("XTL", "Telecom", "Communication Services"),
    ("SOCL", "Social Media", "Communication Services"),
    ("ITA", "Aerospace & Defense", "Industrials"),
    ("IYT", "Transportation", "Industrials"),
    ("PAVE", "Infrastructure", "Industrials"),
    ("PBJ", "Food & Beverage", "Consumer Staples"),
    ("XOP", "Exploration & Production", "Energy"),
    ("OIH", "Oil Services", "Energy"),
    ("AMLP", "Midstream", "Energy"),
    ("FCG", "Natural Gas producers", "Energy"),
    ("XME", "Metals & Mining", "Materials"),
    ("COPX", "Copper Miners", "Materials"),
    ("GDX", "Gold Miners", "Materials"),
    ("REZ", "Residential REITs", "Real Estate"),
    ("SRVR", "Digital infrastructure", "Real Estate"),
    ("REM", "Mortgage REITs", "Real Estate"),
    ("GRID", "Grid infrastructure", "Utilities"),
)

# Group name, member tickers. Repeats across groups are display-only.
STOCK_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Mag 8", ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "SPCX")),
    ("Semiconductors", ("AMD", "AVGO", "NVDA")),
    ("Memory", ("MU", "SNDK")),
    ("Networking/Routers", ("ANET",)),
    ("Optical/Connectivity", ("GLW", "COHR", "LITE")),
    ("Construction", ("CAT",)),
    ("AI Data Centers / Neocloud", ("WULF", "IREN", "APLD", "CRWV", "NBIS", "NUAI", "HUT", "CIFR")),
    ("AI Hardware", ("SMCI", "VRT", "ALAB", "CRDO")),
    ("High-Beta Software", ("PLTR", "SNOW", "DDOG", "NET")),
    ("New Aerospace", ("RKLB", "ASTS", "SPCX")),
    ("Quantum", ("IONQ",)),
    ("EV", ("RIVN", "TSLA")),
    ("Fintech", ("AFRM", "SOFI")),
    ("Power — IPP", ("CEG", "VST", "TLN")),
    ("Power — Grid Infrastructure", ("GEV", "ETN", "PWR")),
    ("Power — Nuclear", ("CCJ",)),
    ("Power — Distributed / On-Site Power", ("BE",)),
)


YAHOO_SYMBOL_OVERRIDES: dict[str, str] = {"VIX": "^VIX"}


def yahoo_symbol(symbol: str) -> str:
    """Yahoo ticker for a display symbol. Share classes use a hyphen (BRK.B -> BRK-B)."""
    ticker = str(symbol or "").upper().strip()
    if ticker in YAHOO_SYMBOL_OVERRIDES:
        return YAHOO_SYMBOL_OVERRIDES[ticker]
    if "." in ticker:
        return ticker.replace(".", "-")
    return ticker


_ARCA = frozenset(
    {
        "SPY",
        "RSP",
        "IWM",
        "DIA",
        "XLK",
        "XLF",
        "XLV",
        "XLY",
        "XLC",
        "XLI",
        "XLP",
        "XLE",
        "XLB",
        "XLRE",
        "XLU",
        "KRE",
        "XBI",
        "XOP",
        "XRT",
    }
)
_NASDAQ_PRIMARY = frozenset({"QQQ", "SMH"})


def _equity_symbols() -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for symbol, _label in INDEX_ETFS + SECTOR_ETFS:
        _add(ordered, seen, symbol)
    for symbol, _label, _parent in SUBSECTOR_ETFS:
        _add(ordered, seen, symbol)
    for _group, members in STOCK_GROUPS:
        for symbol in members:
            _add(ordered, seen, symbol)
    return tuple(ordered)


def _add(ordered: list[str], seen: set[str], symbol: str) -> None:
    ticker = str(symbol or "").upper().strip()
    if not ticker or ticker in seen:
        return
    seen.add(ticker)
    ordered.append(ticker)


APPROVED_EQUITY_ETF_SYMBOLS: tuple[str, ...] = _equity_symbols()
APPROVED_EQUITY_ETF_COUNT = len(APPROVED_EQUITY_ETF_SYMBOLS)
EXPECTED_IBKR_LIVE_COUNT = APPROVED_EQUITY_ETF_COUNT + len(APPROVED_EXTRA_INDEXES)


def _contract(symbol: str, *, sec_type: str, exchange: str, primary: str | None = None) -> dict[str, str]:
    row = {
        "symbol": symbol,
        "sec_type": sec_type,
        "exchange": exchange,
        "currency": "USD",
    }
    if primary:
        row["primary_exchange"] = primary
    return row


def _equity_contract(symbol: str) -> dict[str, str]:
    primary = None
    if symbol in _ARCA:
        primary = "ARCA"
    elif symbol in _NASDAQ_PRIMARY:
        primary = "NASDAQ"
    return _contract(symbol, sec_type="STK", exchange="SMART", primary=primary)


def approved_contracts() -> tuple[dict[str, str], ...]:
    """One contract per approved instrument. Display duplicates are already removed."""
    rows = [_equity_contract(symbol) for symbol in APPROVED_EQUITY_ETF_SYMBOLS]
    rows.append(_contract("VIX", sec_type="IND", exchange="CBOE"))
    _require_counts(rows)
    return tuple(rows)


def _require_counts(rows: list[dict[str, str]]) -> None:
    symbols = [row["symbol"] for row in rows]
    if len(symbols) != len(set(symbols)):
        raise RuntimeError("IBKR live allowlist has duplicate symbols")
    equities = [row for row in rows if row["symbol"] not in APPROVED_EXTRA_INDEXES]
    if len(equities) != APPROVED_EQUITY_ETF_COUNT:
        raise RuntimeError("equity/ETF allowlist count drifted")
    if len(rows) != EXPECTED_IBKR_LIVE_COUNT:
        raise RuntimeError("IBKR live allowlist count drifted")
    if {row["symbol"] for row in rows if row["sec_type"] == "IND"} != set(APPROVED_EXTRA_INDEXES):
        raise RuntimeError("unexpected extra index on the IBKR live allowlist")


_APPROVED_KEYS = frozenset((row["symbol"], row["sec_type"]) for row in approved_contracts())


class UnapprovedSubscription(ValueError):
    """Raised when code tries to stream an instrument outside the allowlist."""


def assert_subscription_allowed(symbol: str, sec_type: str = "STK") -> None:
    key = (str(symbol or "").upper().strip(), str(sec_type or "STK").upper().strip())
    if key not in _APPROVED_KEYS:
        raise UnapprovedSubscription("{0} {1} is not an approved IBKR live instrument".format(key[0], key[1]))


def rejected_watchlist_symbols(rows: list[Any]) -> list[str]:
    """Symbols present in an operator watchlist that the collector will not subscribe."""
    allowed = {symbol for symbol, _sec in _APPROVED_KEYS}
    rejected: list[str] = []
    for row in rows:
        if isinstance(row, str):
            symbol = row
        elif isinstance(row, Mapping):
            symbol = str(row.get("symbol") or "")
        else:
            continue
        ticker = symbol.upper().strip()
        if ticker and ticker not in allowed and ticker not in rejected:
            rejected.append(ticker)
    return rejected


def unique_stock_symbols() -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for _group, members in STOCK_GROUPS:
        for symbol in members:
            _add(ordered, seen, symbol)
    return tuple(ordered)


def _retrieved_stamp(row: Mapping[str, Any]) -> str:
    return str(row.get("retrieved_at") or row.get("quote_ts") or "")


def _row_has_price(row: Mapping[str, Any]) -> bool:
    provenance = row.get("provenance") or {}
    if not isinstance(provenance, Mapping):
        provenance = {}
    return _num(provenance.get("current_price")) is not None or _num(row.get("last_price")) is not None


def _prefer_quote(candidate: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """A priced row wins. Two priced rows, or two empty rows, keep the newer stamp."""
    candidate_priced = _row_has_price(candidate)
    current_priced = _row_has_price(current)
    if candidate_priced != current_priced:
        return candidate_priced
    return _retrieved_stamp(candidate) >= _retrieved_stamp(current)


def quotes_by_symbol(quotes: list[Mapping[str, Any]] | None) -> dict[str, Mapping[str, Any]]:
    """One stored row per ticker. Repeated instrument ids collapse here."""
    chosen: dict[str, Mapping[str, Any]] = {}
    for row in quotes or []:
        symbol = quote_symbol(row)
        if not symbol:
            continue
        current = chosen.get(symbol)
        if current is None or _prefer_quote(row, current):
            chosen[symbol] = row
    return chosen


def display_quote_rows(quotes: list[Mapping[str, Any]] | None) -> list[Mapping[str, Any]]:
    """Approved symbols only, in allowlist order. TLT and other old rows are omitted."""
    chosen = quotes_by_symbol(quotes)
    order = list(APPROVED_EQUITY_ETF_SYMBOLS) + sorted(APPROVED_EXTRA_INDEXES)
    return [chosen[symbol] for symbol in order if symbol in chosen]


STOCK_RETURN_HORIZONS: tuple[str, ...] = ("1D", "1W", "1M", "3M", "6M", "1Y")


def stock_horizon_values(
    open_to_current: float | None,
    stored_returns: Mapping[str, Any] | None,
) -> list[float | None]:
    """1D is the IBKR open-to-current fraction. Longer windows are stored session returns."""
    stored = stored_returns or {}
    values: list[float | None] = []
    for label in STOCK_RETURN_HORIZONS:
        if label == "1D":
            values.append(open_to_current)
        else:
            values.append(_num(stored.get(label)))
    return values


def quote_symbol(row: Mapping[str, Any]) -> str:
    provenance = row.get("provenance") or {}
    if isinstance(provenance, str):
        provenance = {}
    for candidate in (
        row.get("display_name"),
        provenance.get("symbol") if isinstance(provenance, Mapping) else None,
        row.get("symbol"),
        row.get("instrument_id"),
    ):
        text = str(candidate or "").upper().strip()
        if text and not text.startswith("IBKR:"):
            return text
    return ""


def stock_heatmap_rows(quotes: list[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """One display row per group membership. Repeated tickers share one quote record."""
    by_symbol = quotes_by_symbol(quotes)
    display: list[dict[str, Any]] = []
    for group, members in STOCK_GROUPS:
        for symbol in members:
            quote = by_symbol.get(symbol) or {}
            provenance = quote.get("provenance") or {}
            if not isinstance(provenance, Mapping):
                provenance = {}
            price = _num(provenance.get("current_price"))
            if price is None:
                price = _num(quote.get("last_price"))
            change = _num(provenance.get("open_to_current"))
            field = str(provenance.get("current_price_field") or "")
            status = str(quote.get("market_data_type") or quote.get("quote_status") or "UNAVAILABLE")
            session = str(provenance.get("session_date") or "")
            error = str(provenance.get("quote_error") or "")
            note_parts = [part for part in (field, status, session, error) if part]
            display.append(
                {
                    "group": group,
                    "symbol": symbol,
                    "price": price,
                    "open_to_current": change,
                    "quote_ts": quote.get("quote_ts"),
                    "note": " · ".join(note_parts),
                }
            )
    return display


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number
