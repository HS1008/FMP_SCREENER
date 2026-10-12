"""Canonical dashboard quote universe.

Two universes share this module:

* ``APPROVED_EQUITY_ETF_SYMBOLS`` / ``approved_contracts()`` is the IBKR
  streaming allowlist. It is capped by IBKR's 100 market-data lines and the
  collector refuses any drift, so it stays at the index, sector, subsector, and
  regional ETFs plus ``IBKR_STOCK_SYMBOLS``.
* ``DASHBOARD_EQUITY_SYMBOLS`` is the Yahoo quote and daily-history universe:
  the allowlist plus every ``STOCK_GROUPS`` member. Yahoo has no line limit, so
  the thematic stock map on US Equities can be wider than the IBKR book.

Display groups may repeat a ticker (NVDA, TSLA, SPCX). One contract per symbol.
VIX is included. Yahoo quotes use ``yahoo_symbol``; VIX is ``^VIX``.
The eight Power names are part of both universes. The Global Markets regional
ETFs (VEA, VGK, EWJ, VWO, MCHI, INDA, EWZ) are included so their 1D follows the
same stored-quote policy as the US indexes.
"""

from __future__ import annotations

from typing import Any, Mapping

from market_intelligence.taxonomy import GLOBAL_MARKET_ETFS

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

# Screenshot order and labels. The third field is the display group, not a GICS rename.
SUBSECTOR_ETFS: tuple[tuple[str, str, str], ...] = (
    ("SMH", "Semiconductors", "Tech"),
    ("IGV", "Software", "Tech"),
    ("CIBR", "Cybersecurity", "Tech"),
    ("KRE", "Regional banks", "Financials"),
    ("KIE", "Insurance", "Financials"),
    ("KCE", "Capital Markets", "Financials"),
    ("XBI", "Biotechnology", "Health Care"),
    ("XPH", "Pharmaceuticals", "Health Care"),
    ("IHI", "Medical Devices", "Health Care"),
    ("XHS", "Health Care Services/Providers", "Health Care"),
    ("XRT", "Retail", "Consumer Discretionary"),
    ("XHB", "Homebuilders / housing ecosystem", "Consumer Discretionary"),
    ("XTL", "Telecom", "Communication Services"),
    ("SOCL", "Social Media / internet platforms", "Communication Services"),
    ("ITA", "Aerospace & Defense", "Industrials"),
    ("IYT", "Transportation", "Industrials"),
    ("PAVE", "Infrastructure/engineering", "Industrials"),
    ("PBJ", "Food & Beverage", "Consumer Staples"),
    ("XOP", "Exploration & Production", "Energy"),
    ("OIH", "Oil Services", "Energy"),
    ("AMLP", "Midstream / pipelines", "Energy"),
    ("FCG", "Natural Gas producers", "Energy"),
    ("XME", "Metals & Mining", "Materials"),
    ("COPX", "Copper Miners", "Materials"),
    ("GDX", "Gold Miners", "Materials"),
    ("REZ", "Residential REITs", "Real Estate"),
    ("SRVR", "Digital infrastructure/data centers", "Real Estate"),
    ("REM", "Mortgage REITs", "Real Estate"),
    ("GRID", "grid/transmission infrastructure", "Utilities"),
)

# Stocks the IBKR collector streams. Frozen at the 47 names that fit the 100-line
# book with the ETFs and VIX; new thematic names are Yahoo-only (see STOCK_GROUPS).
IBKR_STOCK_SYMBOLS: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "SPCX",
    "AMD", "AVGO",
    "MU", "SNDK",
    "ANET",
    "GLW", "COHR", "LITE",
    "CAT",
    "WULF", "IREN", "APLD", "CRWV", "NBIS", "NUAI", "HUT", "CIFR",
    "SMCI", "VRT", "ALAB", "CRDO",
    "PLTR", "SNOW", "DDOG", "NET",
    "RKLB", "ASTS",
    "IONQ",
    "RIVN",
    "AFRM", "SOFI",
    "CEG", "VST", "TLN",
    "GEV", "ETN", "PWR",
    "CCJ",
    "BE",
)  # fmt: skip

# Thematic eras shown as headings on US Equities. Each era lists its group names in
# display order; every name must exist in STOCK_GROUPS.
STOCK_GROUP_ERAS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "2026 – 2027 · AI & Power Buildout",
        "Chips. Data centers. Infrastructure. Power.",
        (
            "Mag 8",
            "Hyperscalers",
            "Semiconductors",
            "EDA / Chip Design",
            "Foundries",
            "Memory",
            "Networking/Routers",
            "Optical/Connectivity",
            "Interconnects",
            "Edge AI / Devices",
            "AI Data Centers / Neocloud",
            "Data Centers",
            "Cooling",
            "Servers",
            "Storage",
            "GPU Cloud",
            "Digital Infrastructure",
            "AI Hardware",
            "Cloud Software",
            "High-Beta Software",
            "Cybersecurity",
            "Construction",
            "Power — Grid Infrastructure",
            "Electrical Equipment",
            "Power — IPP",
            "Utilities",
            "Energy Storage",
        ),
    ),
    (
        "2027 – 2029 · Energy & Resources",
        "Power. Fuel. Minerals. The backbone.",
        (
            "Natural Gas / Turbines",
            "Power — Distributed / On-Site Power",
            "Power — Nuclear",
            "Uranium / Nuclear Fuel",
            "Copper",
            "Lithium",
            "Critical Minerals",
            "Rare Earths",
            "Real Assets (ETFs)",
            "Energy (ETFs)",
            "Infrastructure (ETFs)",
        ),
    ),
    (
        "2028 – 2030+ · Physical AI",
        "Robotics. Autonomy. Real-world AI.",
        (
            "AI Applications",
            "Robotics",
            "Industrial Automation",
            "Autonomy",
            "EV",
            "Drones",
            "Defense",
            "New Aerospace",
            "Quantum",
            "Fintech",
            "AI Healthcare",
            "Biotech (ETFs)",
            "Emerging Tech (ETFs)",
            "AI Infrastructure (ETFs)",
            "Economy / Risk (ETFs)",
            "Other Key Names",
        ),
    ),
)

# Group name, member tickers. Repeats across groups are display-only. The first
# members of each Power group are the original eight Power names, in order.
# Not tracked from the source map because Yahoo has no US daily bars for them:
# PSTG (delisted), PLL (merged into Elevra), REE (delisted), ABB (no US listing;
# ABBNY is OTC), IVN and LUN (TSX only), TMRC (OTC, no daily bars). "NG" is the
# natural-gas futures instrument id in cross_asset_universe, so the NovaGold
# ticker cannot be a stock symbol here.
STOCK_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Mag 8", ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "SPCX")),
    ("Hyperscalers", ("AMZN", "MSFT", "GOOGL", "META", "ORCL", "IBM")),
    ("Semiconductors", ("AMD", "AVGO", "NVDA", "MRVL", "INTC", "ARM", "QCOM")),
    ("EDA / Chip Design", ("SNPS", "CDNS")),
    ("Foundries", ("TSM", "ASML")),
    ("Memory", ("MU", "SNDK", "WDC", "SKHY")),
    ("Networking/Routers", ("ANET", "CSCO", "CLS", "AAOI", "NVTS", "LITE", "CRDO", "GLW")),
    ("Optical/Connectivity", ("GLW", "COHR", "LITE", "AAOI", "NVTS")),
    ("Interconnects", ("APH", "CIEN", "FN", "MTSI")),
    ("Edge AI / Devices", ("QCOM", "ARM", "NTNX")),
    ("AI Data Centers / Neocloud", ("WULF", "IREN", "APLD", "CRWV", "NBIS", "NUAI", "HUT", "CIFR", "CORZ")),
    ("Data Centers", ("VRT", "SMCI", "IREN", "DLR", "EQIX", "CIFR", "APLD", "CORZ")),
    ("Cooling", ("VRT", "AAON")),
    ("Servers", ("DELL", "SMCI", "HPE", "NTAP")),
    ("Storage", ("WDC", "NTAP", "SNDK")),
    ("GPU Cloud", ("CRWV", "NBIS", "DOCN", "MSFT", "AMZN")),
    ("Digital Infrastructure", ("EQIX", "DLR", "AMZN", "ORCL", "IBM", "NTNX", "AKAM")),
    ("AI Hardware", ("SMCI", "VRT", "ALAB", "CRDO")),
    ("Cloud Software", ("NET", "FSLY")),
    ("High-Beta Software", ("PLTR", "SNOW", "DDOG", "NET")),
    ("Cybersecurity", ("CRWD", "PANW")),
    ("Construction", ("CAT", "FIX", "EME", "WCC")),
    ("Power — Grid Infrastructure", ("GEV", "ETN", "PWR", "HUBB", "EME", "WCC", "VRT")),
    ("Electrical Equipment", ("ETN", "POWL", "NVT", "TE", "CMI")),
    ("Power — IPP", ("CEG", "VST", "TLN", "NEE", "NRG", "DLR")),
    ("Utilities", ("AEP", "ETR", "NEE", "DUK", "SO", "EXC")),
    ("Energy Storage", ("EOSE", "FLNC", "BE")),
    ("Natural Gas / Turbines", ("GEV", "EQT", "BE", "CMI", "KMI")),
    ("Power — Distributed / On-Site Power", ("BE", "CMI", "GNRC", "VST", "BWXT")),
    ("Power — Nuclear", ("CCJ", "OKLO", "SMR", "UUUU", "BWXT", "CEG")),
    ("Uranium / Nuclear Fuel", ("CCJ", "LEU", "UUUU", "DNN", "NXE")),
    ("Copper", ("FCX", "SCCO", "TECK")),
    ("Lithium", ("ALB", "SQM", "LAC")),
    ("Critical Minerals", ("LAC", "CRML", "USAU")),
    ("Rare Earths", ("MP", "USAR", "UAMY")),
    ("Real Assets (ETFs)", ("GLD", "SLV", "COPX", "REMX")),
    ("Energy (ETFs)", ("XLE", "IWM", "XLF", "JETS")),
    ("Infrastructure (ETFs)", ("PAVE", "XLU")),
    ("AI Applications", ("PLTR", "NOW", "META", "AMZN", "MSFT", "GOOGL", "ORCL", "CRM", "SNOW")),
    ("Robotics", ("TSLA", "SYM", "ROK", "TER", "ISRG", "PATH")),
    ("Industrial Automation", ("ROK", "ETN", "IR", "HON", "CAT", "DE")),
    ("Autonomy", ("GOOGL", "MBLY", "TSLA", "AMZN", "UBER", "LIDR")),
    ("EV", ("RIVN", "TSLA")),
    ("Drones", ("AVAV", "RCAT", "ACHR", "JOBY")),
    ("Defense", ("KTOS", "NOC", "LMT", "GD", "RTX", "BA")),
    ("New Aerospace", ("RKLB", "ASTS", "SPCX", "LUNR", "RDW", "PL", "BKSY")),
    ("Quantum", ("IONQ", "QBTS", "RGTI", "QUBT")),
    ("Fintech", ("AFRM", "SOFI")),
    ("AI Healthcare", ("TEM", "RXRX", "ISRG", "TDOC", "HIMS", "VERX")),
    ("Biotech (ETFs)", ("XBI", "IBB", "ARKG")),
    ("Emerging Tech (ETFs)", ("QTUM", "BOTZ", "ARKX", "NLR")),
    ("AI Infrastructure (ETFs)", ("SMH", "DRAM", "EUV", "IGV", "CIBR", "WGMI", "DTCR")),
    ("Economy / Risk (ETFs)", ("IWM", "XLF", "JETS", "XLE")),
    ("Other Key Names", ("ASTS", "VZ", "T")),
)


def stock_groups_by_era() -> tuple[tuple[str, str, tuple[tuple[str, tuple[str, ...]], ...]], ...]:
    """``(era title, era caption, ((group, members), ...))`` in display order.

    Groups missing from every era are appended to the last era so a new group
    is never silently hidden.
    """
    members_by_name = dict(STOCK_GROUPS)
    placed: set[str] = set()
    eras: list[tuple[str, str, tuple[tuple[str, tuple[str, ...]], ...]]] = []
    for title, caption, names in STOCK_GROUP_ERAS:
        groups = tuple((name, members_by_name[name]) for name in names if name in members_by_name)
        placed.update(name for name, _members in groups)
        eras.append((title, caption, groups))
    leftovers = tuple((name, members) for name, members in STOCK_GROUPS if name not in placed)
    if leftovers and eras:
        title, caption, groups = eras[-1]
        eras[-1] = (title, caption, groups + leftovers)
    return tuple(eras)


YAHOO_SYMBOL_OVERRIDES: dict[str, str] = {"VIX": "^VIX"}


def subsector_groups() -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Screenshot groups in list order. A ticker is collected once even if displayed twice."""
    grouped: list[tuple[str, list[tuple[str, str]]]] = []
    by_name: dict[str, list[tuple[str, str]]] = {}
    for symbol, label, group in SUBSECTOR_ETFS:
        rows = by_name.get(group)
        if rows is None:
            rows = []
            by_name[group] = rows
            grouped.append((group, rows))
        rows.append((symbol, label))
    return tuple((group, tuple(rows)) for group, rows in grouped)


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
    for symbol in IBKR_STOCK_SYMBOLS:
        _add(ordered, seen, symbol)
    # Global Markets regional ETFs: SPY is already present; seven new tickers.
    for symbol, _label in GLOBAL_MARKET_ETFS:
        _add(ordered, seen, symbol)
    return tuple(ordered)


def _dashboard_symbols() -> tuple[str, ...]:
    """IBKR allowlist order first, then Yahoo-only thematic names in STOCK_GROUPS order."""
    ordered = list(_equity_symbols())
    seen = set(ordered)
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

# Yahoo quote + YAHOO_PRICE_DAILY universe (no IBKR line limit). Superset of the allowlist.
DASHBOARD_EQUITY_SYMBOLS: tuple[str, ...] = _dashboard_symbols()
YAHOO_ONLY_STOCK_SYMBOLS: tuple[str, ...] = tuple(symbol for symbol in DASHBOARD_EQUITY_SYMBOLS if symbol not in set(APPROVED_EQUITY_ETF_SYMBOLS))


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
    """Tracked dashboard symbols only, in universe order. TLT and other old rows are omitted."""
    chosen = quotes_by_symbol(quotes)
    order = list(DASHBOARD_EQUITY_SYMBOLS) + sorted(APPROVED_EXTRA_INDEXES)
    return [chosen[symbol] for symbol in order if symbol in chosen]


STOCK_RETURN_HORIZONS: tuple[str, ...] = ("1D", "1W", "1M", "3M", "6M", "1Y")


def stock_horizon_values(
    open_to_current: float | None,
    stored_returns: Mapping[str, Any] | None,
) -> list[float | None]:
    """1D is the since-open fraction. Longer windows are the shared price-return map."""
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
                    "quote": quote or None,
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
