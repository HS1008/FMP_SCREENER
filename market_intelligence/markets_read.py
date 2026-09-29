"""PostgreSQL reads for the Markets pages.

Dashboard comparisons read ``MARKET_MONITOR_EOD`` only: one Yahoo history for
every market-monitor ETF. Canonical IBKR rows stay on ``EQUITY_EOD`` and are
not concatenated into these series. Adjusted close is the only price used.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from sqlalchemy import bindparam, text

from market_intelligence.markets_analytics import bars_to_series, build_aligned_us_panel, session_window_returns
from market_intelligence.taxonomy import (
    BENCHMARK_SPY,
    GLOBAL_MARKET_SYMBOLS,
    MARKET_MONITOR_SYMBOLS,
    SECTOR_PROXIES,
    US_MARKET_SYMBOLS,
    stock_subsector_baskets,
)

MARKET_MONITOR_SOURCE_ID = "MARKET_MONITOR_EOD"
EQUITY_EOD_SOURCE_ID = "EQUITY_EOD"
_PROVIDER_PREFERENCE = ("IBKR", "YAHOO", "FIXTURE")
_MONITOR_PROVIDER_PREFERENCE = ("YAHOO", "FIXTURE")


def choose_provider(providers: set[str]) -> str | None:
    """Single canonical EQUITY_EOD provider. Never a splice."""
    return _prefer(providers, _PROVIDER_PREFERENCE)


def choose_monitor_provider(providers: set[str]) -> str | None:
    """Single market-monitor provider. Yahoo wins; IBKR is not preferred."""
    return _prefer(providers, _MONITOR_PROVIDER_PREFERENCE)


def _prefer(providers: set[str], order: tuple[str, ...]) -> str | None:
    present = {provider for provider in providers if provider}
    if not present:
        return None
    for name in order:
        if name in present:
            return name
    return sorted(present)[0]


def _iso(day: date | None) -> str | None:
    if day is None:
        return None
    return day.isoformat()


def load_monitor_history(conn, symbols: Sequence[str]) -> dict[str, Any]:
    """Adjusted market-monitor closes and session returns for ``symbols`` only.

    Reads ``mi_v_market_monitor_closes``. That view requires provider YAHOO on
    ``MARKET_MONITOR_EOD`` and excludes every other provider, so a short IBKR
    ``EQUITY_EOD`` history cannot truncate the chart.
    The dashboard role can select the view and cannot select ``mi_market_bars``.
    """
    wanted = [str(symbol) for symbol in symbols]
    grouped: dict[str, dict[str, list[tuple[date, float, str | None]]]] = {symbol: {} for symbol in wanted}
    if wanted:
        rows = conn.execute(
            text(
                """
                SELECT symbol, bar_date, adj_close_price, provider, adjustment_basis, source_id
                FROM mi_v_market_monitor_closes
                WHERE adj_close_price IS NOT NULL
                  AND symbol IN :syms
                ORDER BY symbol, bar_date
                """
            ).bindparams(bindparam("syms", expanding=True)),
            {"syms": wanted},
        ).all()
        for instrument_id, bar_date, price, provider, basis, _source in rows:
            symbol = str(instrument_id)
            day = bar_date if isinstance(bar_date, date) else date.fromisoformat(str(bar_date)[:10])
            provider_name = str(provider) if provider else ""
            grouped.setdefault(symbol, {}).setdefault(provider_name, []).append((day, float(price), str(basis) if basis else None))
    bars: dict[str, list[dict[str, Any]]] = {}
    meta: dict[str, dict[str, Any]] = {}
    returns: dict[str, dict[str, float | None]] = {}
    latest_price: dict[str, float | None] = {}
    earliest: date | None = None
    latest: date | None = None
    for symbol in wanted:
        by_provider = grouped.get(symbol) or {}
        provider = choose_monitor_provider(set(by_provider))
        chosen = by_provider.get(provider or "", [])
        chosen.sort(key=lambda item: item[0])
        points = [{"date": day.isoformat(), "value": price} for day, price, _basis in chosen]
        bases = sorted({basis for _day, _price, basis in chosen if basis})
        first = chosen[0][0] if chosen else None
        last = chosen[-1][0] if chosen else None
        if first is not None and (earliest is None or first < earliest):
            earliest = first
        if last is not None and (latest is None or last > latest):
            latest = last
        series = bars_to_series(points)
        bars[symbol] = points
        meta[symbol] = {
            "provider": provider,
            "providers_present": sorted(name for name in by_provider if name),
            "adjustment_basis": bases[-1] if len(bases) == 1 else None,
            "adjustment_bases": bases,
            "source_id": MARKET_MONITOR_SOURCE_ID,
            "rows": len(points),
            "earliest": _iso(first),
            "latest": _iso(last),
        }
        returns[symbol] = session_window_returns(series, last) if last is not None else {label: None for label in ("1D", "1W", "1M", "3M", "6M", "1Y")}
        latest_price[symbol] = chosen[-1][1] if chosen else None
    return {
        "bars": bars,
        "meta": meta,
        "returns": returns,
        "latest_price": latest_price,
        "bounds": {"earliest": _iso(earliest), "latest": _iso(latest)},
    }


def us_markets_history(conn) -> dict[str, Any]:
    return load_monitor_history(conn, US_MARKET_SYMBOLS)


def load_equity_eod_closes(conn, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Adjusted EQUITY_EOD closes for ``symbols`` in one query.

    IBKR is preferred when both providers exist. Yahoo is the fallback, not a splice.
    Duplicate dates keep the last row after sorting. Null adjusted closes are omitted.
    More than one adjustment basis on the chosen provider rejects the series.
    """
    wanted = [str(symbol) for symbol in symbols]
    loaded: dict[str, dict[str, Any]] = {
        symbol: {"bars": [], "prices": {}, "adjustment_basis": None, "provider": None, "rejection": "missing"}
        for symbol in wanted
    }
    if not wanted:
        return loaded
    rows = conn.execute(
        text(
            """
            SELECT symbol, bar_date, adj_close_price, provider, adjustment_basis
            FROM mi_v_equity_daily_closes
            WHERE source_id = 'EQUITY_EOD'
              AND adj_close_price IS NOT NULL
              AND symbol IN :syms
            ORDER BY symbol, bar_date
            """
        ).bindparams(bindparam("syms", expanding=True)),
        {"syms": wanted},
    ).all()
    grouped: dict[str, dict[str, dict[date, tuple[float, str | None]]]] = {symbol: {} for symbol in wanted}
    for instrument_id, bar_date, price, provider, basis in rows:
        symbol = str(instrument_id)
        day = bar_date if isinstance(bar_date, date) else date.fromisoformat(str(bar_date)[:10])
        provider_name = str(provider) if provider else ""
        basis_name = str(basis).strip() if basis else None
        grouped.setdefault(symbol, {}).setdefault(provider_name, {})[day] = (float(price), basis_name)
    for symbol in wanted:
        by_provider = grouped.get(symbol) or {}
        provider = choose_provider(set(by_provider))
        series = by_provider.get(provider or "", {})
        bases = {basis for _price, basis in series.values() if basis}
        if not series:
            rejection = "missing"
            basis = None
        elif len(bases) != 1:
            rejection = "mixed_adjustment_basis" if bases else "missing_adjustment_basis"
            basis = None
        else:
            rejection = None
            basis = next(iter(bases))
        prices = {day: price for day, (price, _basis) in series.items()} if rejection is None else {}
        loaded[symbol] = {
            "bars": [{"date": day.isoformat(), "value": prices[day]} for day in sorted(prices)],
            "prices": prices,
            "adjustment_basis": basis,
            "provider": provider,
            "rejection": rejection,
        }
    return loaded


def aligned_us_equity_returns(conn) -> dict[str, Any]:
    """One EQUITY_EOD load for SPY, sector ETFs, and curated basket members."""
    symbols: list[str] = [BENCHMARK_SPY]
    for etf in SECTOR_PROXIES.values():
        if etf not in symbols:
            symbols.append(etf)
    for basket in stock_subsector_baskets():
        for symbol in basket.members:
            if symbol not in symbols:
                symbols.append(symbol)
    closes = load_equity_eod_closes(conn, symbols)
    panel = build_aligned_us_panel(closes)
    endpoint = panel.get("endpoint")
    if isinstance(endpoint, date):
        panel["endpoint"] = endpoint.isoformat()
    windows: dict[str, Any] = {}
    for label, bounds in (panel.get("windows") or {}).items():
        if not bounds:
            windows[label] = None
            continue
        windows[label] = {"start": bounds["start"].isoformat(), "end": bounds["end"].isoformat()}
    panel["windows"] = windows
    panel["symbols"] = symbols
    return panel


def global_markets_history(conn) -> dict[str, Any]:
    return load_monitor_history(conn, GLOBAL_MARKET_SYMBOLS)


def _source_coverage(conn, symbols: Sequence[str], source_id: str) -> dict[str, Any]:
    rows = conn.execute(
        text(
            """
            SELECT instrument_id, source_id, COALESCE(provider, ''), COALESCE(adjustment_basis, ''),
                   MIN(bar_date), MAX(bar_date), COUNT(*)
            FROM mi_market_bars
            WHERE source_id = :source
              AND bar_interval = '1D'
              AND instrument_id IN :syms
            GROUP BY instrument_id, source_id, COALESCE(provider, ''), COALESCE(adjustment_basis, '')
            ORDER BY instrument_id, source_id, 3
            """
        ).bindparams(bindparam("syms", expanding=True)),
        {"syms": list(symbols), "source": source_id},
    ).all()
    coverage = [
        {
            "symbol": str(symbol),
            "source_id": str(stored_source),
            "provider": str(provider),
            "adjustment_basis": str(basis),
            "earliest": day.isoformat() if isinstance(day, date) else str(day)[:10],
            "latest": last.isoformat() if isinstance(last, date) else str(last)[:10],
            "rows": int(count),
        }
        for symbol, stored_source, provider, basis, day, last, count in rows
    ]
    duplicate_dates = int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM (
                    SELECT instrument_id, bar_date
                    FROM mi_market_bars
                    WHERE source_id = :source
                      AND bar_interval = '1D'
                      AND instrument_id IN :syms
                    GROUP BY instrument_id, bar_date
                    HAVING COUNT(*) > 1
                ) duplicated
                """
            ).bindparams(bindparam("syms", expanding=True)),
            {"syms": list(symbols), "source": source_id},
        ).scalar()
        or 0
    )
    return {"rows": coverage, "duplicate_dates": duplicate_dates}


def market_monitor_coverage(conn, symbols: Sequence[str] | None = None) -> dict[str, Any]:
    """Dashboard Yahoo spans, plus untouched canonical EQUITY_EOD spans."""
    wanted = list(symbols or MARKET_MONITOR_SYMBOLS)
    dashboard = _source_coverage(conn, wanted, MARKET_MONITOR_SOURCE_ID)
    canonical = _source_coverage(conn, wanted, EQUITY_EOD_SOURCE_ID)
    return {
        "rows": dashboard["rows"],
        "duplicate_dates": dashboard["duplicate_dates"],
        "canonical_rows": canonical["rows"],
        "canonical_duplicate_dates": canonical["duplicate_dates"],
        "symbols": wanted,
    }


def stored_equity_providers(conn, symbols: Sequence[str]) -> dict[str, set[str]]:
    wanted = [str(symbol) for symbol in symbols]
    found = {symbol: set() for symbol in wanted}
    if not wanted:
        return found
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT instrument_id, provider
            FROM mi_market_bars
            WHERE source_id = 'EQUITY_EOD'
              AND bar_interval = '1D'
              AND instrument_id IN :syms
            """
        ).bindparams(bindparam("syms", expanding=True)),
        {"syms": wanted},
    ).all()
    for instrument_id, provider in rows:
        if provider:
            found.setdefault(str(instrument_id), set()).add(str(provider))
    return found


def yahoo_backfill_symbols(provider_by_symbol: Mapping[str, set[str]]) -> tuple[list[str], list[dict[str, Any]]]:
    """Yahoo may fill a symbol with no EQUITY_EOD provider, or Yahoo-only history.

    A symbol that already has any other provider is skipped so Yahoo rows are
    not written over IBKR (or any other) history.
    """
    eligible: list[str] = []
    skipped: list[dict[str, Any]] = []
    for symbol, providers in provider_by_symbol.items():
        others = sorted(provider for provider in providers if provider and provider != "YAHOO")
        if others:
            skipped.append({"symbol": symbol, "providers": others})
        else:
            eligible.append(symbol)
    return eligible, skipped


__all__ = [
    "EQUITY_EOD_SOURCE_ID",
    "MARKET_MONITOR_SOURCE_ID",
    "choose_monitor_provider",
    "choose_provider",
    "global_markets_history",
    "load_monitor_history",
    "market_monitor_coverage",
    "stored_equity_providers",
    "load_equity_eod_closes",
    "aligned_us_equity_returns",
    "us_markets_history",
    "yahoo_backfill_symbols",
]
