"""Pure helpers for the US Equities individual-stock board.

No Streamlit, no database connection, and no provider calls. The page decides
which groups are open and passes only those symbols to the readers. Historical
return and risk preparation is cached by the stored bars; the clock-dependent
1D cell is computed by the page and is never cached here.
"""

from __future__ import annotations

import hashlib
from statistics import median
from typing import Any, Mapping, Sequence

from market_intelligence.ibkr_live_universe import STOCK_RETURN_HORIZONS
from market_intelligence.price_returns import completed_price_horizons
from market_intelligence.risk_metrics import RISK_COLUMN_LABELS, stock_risk_metrics
from market_intelligence.taxonomy import constituent_company_name

QUERY_KEY = "us_stock_query"
ERA_KEY = "us_stock_era"
GROUP_FILTER_KEY = "us_stock_group_filter"
SORT_KEY = "us_stock_sort"
SORT_DESC_KEY = "us_stock_sort_desc"
COLUMNS_KEY = "us_stock_columns"
LAYOUT_KEY = "us_stock_layout"
OPEN_MAP_KEY = "us_stock_group_open_map"
FORCED_OPEN_KEY = "us_stock_search_forced_groups"

SORT_CATALOG = "Catalog order"
SORT_TICKER = "Ticker"
LAYOUT_GROUPED = "Grouped"
LAYOUT_ALL = "All tracked stocks"
COLUMN_ALL = "All"
COLUMN_RETURN = "Return"
COLUMN_RISK = "Risk"
FILTER_ALL = "All"

COLUMN_VIEWS: tuple[str, ...] = (COLUMN_ALL, COLUMN_RETURN, COLUMN_RISK)
LAYOUTS: tuple[str, ...] = (LAYOUT_GROUPED, LAYOUT_ALL)

_HISTORY_CACHE: dict[str, Any] = {}
_RISK_CACHE: dict[str, Any] = {}
_CACHE_MAX = 512


def section_state_key(anchor: str) -> str:
    """Stable expander key for a page section. The anchor, not the label, is the identity."""
    return "us_section_" + "".join(ch.lower() if ch.isalnum() else "_" for ch in anchor)


def group_state_key(group: str) -> str:
    """Stable expander key for a stock group. Median, coverage, and timestamps are not part of it."""
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(group or ""))
    while "__" in slug:
        slug = slug.replace("__", "_")
    return "us_stock_group_open_" + slug.strip("_")


def sort_choices() -> tuple[str, ...]:
    return (SORT_CATALOG, SORT_TICKER, *STOCK_RETURN_HORIZONS, *RISK_COLUMN_LABELS)


def normalize_query(query: str | None) -> str:
    return " ".join(str(query or "").lower().split())


def company_name(symbol: str) -> str:
    """Curated company name. An unknown ticker contributes no extra search text."""
    ticker = str(symbol or "").upper()
    name = constituent_company_name(ticker)
    if not name or name.upper() == ticker:
        return ""
    return name


def _bar_token(symbol: str, bars: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha1()
    digest.update(str(symbol or "").upper().encode())
    for row in bars:
        digest.update(
            repr(
                (
                    str(row.get("bar_date")),
                    row.get("close"),
                    row.get("adj_close"),
                    row.get("basis"),
                    row.get("quality"),
                )
            ).encode()
        )
    return digest.hexdigest()


def _remember(cache: dict[str, Any], token: str, value: Any) -> Any:
    if token not in cache and len(cache) >= _CACHE_MAX:
        cache.pop(next(iter(cache)))
    cache[token] = value
    return value


def cached_horizons(symbol: str, bars: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Completed close-to-close horizons. A corrected stored bar misses the cache."""
    token = _bar_token(symbol, bars)
    found = _HISTORY_CACHE.get(token)
    if found is None:
        found = _remember(_HISTORY_CACHE, token, completed_price_horizons(bars))
    return found


def cached_risk(symbol: str, bars: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Volatility and Sharpe from stored bars. Not used for a closed group."""
    token = _bar_token(symbol, bars)
    found = _RISK_CACHE.get(token)
    if found is None:
        found = _remember(_RISK_CACHE, token, stock_risk_metrics(bars))
    return found


def clear_preparation_cache() -> None:
    _HISTORY_CACHE.clear()
    _RISK_CACHE.clear()


def dedupe_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        key = str(symbol or "").upper().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return tuple(ordered)


def group_summary_label(
    group: str,
    *,
    count: int,
    available: Sequence[float],
    pending: int = 0,
    stale: int = 0,
) -> str:
    """Closed-group summary. Coverage stays visible when only one quote has a 1D."""
    noun = "name" if count == 1 else "names"
    usable = [float(value) for value in available if value is not None]
    label = "{0} · {1} {2} · 1D available {3}/{1}".format(group, count, noun, len(usable))
    if usable:
        label += " · median 1D {0:+.2f}%".format(float(median(usable)) * 100.0)
    if pending:
        label += " · pending {0}".format(int(pending))
    if stale:
        label += " · stale {0}".format(int(stale))
    return label


def member_matches(query: str | None, *, group: str, era: str, symbol: str, company: str = "") -> bool:
    """True when the query hits the ticker, group, era, or a curated company name."""
    needle = normalize_query(query)
    if not needle:
        return True
    haystack = " ".join((group, era, symbol, company or company_name(symbol))).lower()
    return needle in haystack


def group_has_match(query: str | None, *, group: str, era: str, symbols: Sequence[str]) -> bool:
    needle = normalize_query(query)
    if not needle:
        return True
    if needle in group.lower() or needle in era.lower():
        return True
    return any(member_matches(needle, group=group, era=era, symbol=symbol) for symbol in symbols)


def filter_members(
    rows: Sequence[Mapping[str, Any]],
    query: str | None,
    *,
    group: str,
    era: str,
) -> list[Mapping[str, Any]]:
    """Rows to show. A group-name or era match keeps every member; a ticker match keeps that ticker."""
    needle = normalize_query(query)
    if not needle or needle in group.lower() or needle in era.lower():
        return list(rows)
    return [row for row in rows if member_matches(needle, group=group, era=era, symbol=str(row.get("symbol") or ""))]


def sort_rows(
    rows: Sequence[Mapping[str, Any]],
    values_by_symbol: Mapping[str, Mapping[str, float | None]],
    *,
    sort_by: str,
    descending: bool,
) -> list[Mapping[str, Any]]:
    """Sort heatmap rows. Missing values stay last. Ties break by ticker ascending."""
    if sort_by == SORT_CATALOG:
        return list(rows)
    if sort_by == SORT_TICKER:
        return sorted(rows, key=lambda row: str(row.get("symbol") or "").upper(), reverse=bool(descending))

    def _key(row: Mapping[str, Any]) -> tuple[int, float, str]:
        symbol = str(row.get("symbol") or "").upper()
        value = (values_by_symbol.get(symbol) or {}).get(sort_by)
        if value is None:
            return (1, 0.0, symbol)
        number = float(value)
        directed = -number if descending else number
        return (0, directed, symbol)

    return sorted(rows, key=_key)


def slice_columns(
    view: str,
    return_columns: Sequence[str],
    risk_columns: Sequence[str],
    risk_kinds: Sequence[str],
    values: Sequence[Sequence[float | None]],
    notes: Sequence[Sequence[str]],
) -> tuple[list[str], list[str], list[list[float | None]], list[list[str]]]:
    """Return / Risk / All. Every metric stays reachable by changing the view."""
    returns = list(return_columns)
    risks = list(risk_columns)
    width = len(returns)
    if view == COLUMN_RETURN:
        return (
            returns,
            ["return"] * width,
            [list(row[:width]) for row in values],
            [list(row[:width]) for row in notes],
        )
    if view == COLUMN_RISK:
        return (
            risks,
            list(risk_kinds),
            [list(row[width:]) for row in values],
            [list(row[width:]) for row in notes],
        )
    if view == COLUMN_ALL:
        return (
            returns + risks,
            ["return"] * width + list(risk_kinds),
            [list(row) for row in values],
            [list(row) for row in notes],
        )
    raise ValueError("Unknown column view {0}".format(view))


def value_map(
    symbols: Sequence[str],
    return_values: Sequence[Sequence[float | None]],
    risk_values: Sequence[Sequence[float | None]],
) -> dict[str, dict[str, float | None]]:
    """Column name to value, used by the sorter. 1D stays whatever the page already resolved."""
    mapped: dict[str, dict[str, float | None]] = {}
    risk_labels = list(RISK_COLUMN_LABELS)
    for index, symbol in enumerate(symbols):
        cells: dict[str, float | None] = {}
        returns = list(return_values[index]) if index < len(return_values) else []
        risks = list(risk_values[index]) if index < len(risk_values) else []
        for label, value in zip(STOCK_RETURN_HORIZONS, returns):
            cells[label] = value
        for label, value in zip(risk_labels, risks):
            cells[label] = value
        mapped[str(symbol).upper()] = cells
    return mapped


__all__ = [
    "COLUMN_ALL",
    "COLUMN_RETURN",
    "COLUMN_RISK",
    "COLUMN_VIEWS",
    "COLUMNS_KEY",
    "ERA_KEY",
    "FILTER_ALL",
    "FORCED_OPEN_KEY",
    "GROUP_FILTER_KEY",
    "LAYOUTS",
    "LAYOUT_ALL",
    "LAYOUT_GROUPED",
    "LAYOUT_KEY",
    "OPEN_MAP_KEY",
    "QUERY_KEY",
    "SORT_CATALOG",
    "SORT_DESC_KEY",
    "SORT_KEY",
    "SORT_TICKER",
    "cached_horizons",
    "cached_risk",
    "clear_preparation_cache",
    "company_name",
    "dedupe_symbols",
    "filter_members",
    "group_has_match",
    "group_state_key",
    "group_summary_label",
    "member_matches",
    "normalize_query",
    "section_state_key",
    "slice_columns",
    "sort_choices",
    "sort_rows",
    "value_map",
]
