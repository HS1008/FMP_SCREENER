"""Chart-first US Markets and Global Markets pages.

Streamlit reads prepared PostgreSQL rows only. Charts receive those rows.
This module does not call Yahoo, IBKR, FRED, or the database driver.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Sequence

import streamlit as st

from market_intelligence.components.market_chart import lightweight_market_chart
from market_intelligence.components.tenor_chart import column_scaled_return_heatmap, ranked_bar_chart, return_heatmap
from market_intelligence.equity_live import attach_live_1d_to_sector_rows, preferred_canonical_sector_rows
from market_intelligence.ibkr_live_universe import (
    STOCK_RETURN_HORIZONS,
    display_quote_rows,
    quote_symbol,
    stock_heatmap_rows,
    stock_horizon_values,
)
from market_intelligence.live_session import heatmap_freshness_label, market_session_state, quote_observation_status
from market_intelligence.history_range import historical_date_range, pills_layout_kwargs, series_toggles
from market_intelligence.markets_analytics import (
    GLOBAL_METHODOLOGY,
    HORIZONS,
    US_METHODOLOGY,
    as_day,
    classify_return,
    clip_points,
    heatmap_rows,
    normalize_selected_to_100,
    overlay_stored_quote_returns,
    normalized_ratio,
    period_change,
    price_ratio_points,
    sector_bar_pairs,
    subsector_matrix,
    trailing_drawdown,
)
from market_intelligence.sector_mapping import CANONICAL_SECTORS
from market_intelligence.taxonomy import (
    NO_SUBSECTOR_CLASSIFICATION,
    GLOBAL_CORE_ETFS,
    GLOBAL_DEFAULT_SELECTED,
    GLOBAL_LEADERSHIP,
    GLOBAL_MARKET_ETFS,
    GLOBAL_SNAPSHOT_SYMBOLS,
    SECTOR_PROXIES,
    US_DRAWDOWN_SYMBOLS,
    US_INDEX_ETFS,
    US_LEADERSHIP,
    US_PERFORMANCE_ETFS,
    constituent_label,
    index_etf_label,
)
from market_intelligence.ui import load_optional, load_or_stop, load_quote_optional, page_header

_CHART_HEIGHT = 420
_HORIZON_LABELS = [label for label, _field, _sessions in HORIZONS]


def render_us_markets_page() -> None:
    """US equity indexes, relative performance, sectors, subsectors, and drawdowns."""
    page_header(
        "US Equities",
        "Index snapshot, relative performance, sector returns, and drawdowns from stored adjusted closes.",
        fred=False,
    )
    history = load_or_stop("us_markets_history")
    aligned = _aligned_panel(load_optional("aligned_us_equity_returns"))
    _index_snapshot(history)
    start, end = _range_selector(history, key="markets_us")
    price_mode = _single_choice("Display", ["Indexed to 100", "Absolute"], key="us_price_mode", default="Indexed to 100")
    _us_index_performance(history, start, end, mode=price_mode)
    _us_relative_performance(history, start, end, mode=price_mode)
    return_mode = _single_choice(
        "Sector performance",
        ["Absolute Performance", "Relative vs SPY"],
        key="us_sector_mode",
        default="Absolute Performance",
    )
    _us_return_heatmaps(aligned, mode=return_mode)
    _drawdown_section(history, US_DRAWDOWN_SYMBOLS, start, end)
    _methodology(US_METHODOLOGY, sectors=True)


def render_global_markets_page() -> None:
    """Regional USD ETF performance and relative strength versus the US."""
    page_header(
        "Global Markets",
        "Regional equity performance through USD-listed ETF proxies. Returns are what a USD investor experienced.",
        fred=False,
    )
    history = load_or_stop("global_markets_history")
    labels = {symbol: label for symbol, label in GLOBAL_MARKET_ETFS}
    _snapshot_row(history, GLOBAL_SNAPSHOT_SYMBOLS, labels)
    start, end = _range_selector(history, key="markets_global")
    _performance_section(
        "Global Equity Performance",
        "Index (start = 100) on the first common adjusted close of the series selected below.",
        history,
        GLOBAL_MARKET_ETFS,
        start,
        end,
        key="global_performance",
        default=GLOBAL_DEFAULT_SELECTED,
    )
    _ranked_return_section(
        "Global Market Performance",
        "USD ETF return over the selected session window.",
        history,
        GLOBAL_MARKET_ETFS,
        key="global_rank",
    )
    st.subheader("US vs Developed ex-US vs Emerging Markets")
    st.caption("Index (start = 100) for SPY, VEA, and VWO on their first common date in the selected range.")
    _fixed_performance(history, GLOBAL_CORE_ETFS, start, end, key="global_core")
    for symbol, title, caption in GLOBAL_LEADERSHIP:
        _ratio_chart(
            history,
            symbol,
            "SPY",
            title="{0} / SPY".format(symbol),
            caption="{0}. {1}".format(title, caption),
            start=start,
            end=end,
            key="global_ratio_{0}".format(symbol),
        )
    _heatmap_section(
        "Global Return Heatmap",
        "Percentage returns from adjusted USD ETF prices. Finalized EOD session windows.",
        history,
        list(GLOBAL_MARKET_ETFS),
        key="global_heatmap",
    )
    _methodology(GLOBAL_METHODOLOGY)


_BADGE_COLORS = {
    "positive": ("#e5f6ec", "#0b6b3a"),
    "negative": ("#fdecec", "#9b1c1c"),
    "neutral": ("#f3f4f6", "#4b5563"),
    "unavailable": ("#f3f4f6", "#4b5563"),
}


def _html_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _return_badge(label: str, value: Any, *, title: str = "") -> str:
    kind = classify_return(value)
    background, foreground = _BADGE_COLORS[kind]
    shown = "N/A" if kind == "unavailable" else _signed_percent(value)
    tip = ' title="{0}"'.format(_html_text(title)) if title else ""
    return (
        '<span{4} style="display:inline-block;margin:2px 4px 0 0;padding:2px 8px;border-radius:999px;'
        'font-size:12px;font-weight:650;line-height:1.5;background:{0};color:{1};">{2} {3}</span>'
    ).format(background, foreground, _html_text(label), shown, tip)


def _ibkr_quote_price(row: Mapping[str, Any] | None) -> float | None:
    if not row:
        return None
    provenance = row.get("provenance") or {}
    if not isinstance(provenance, Mapping):
        provenance = {}
    price = provenance.get("current_price")
    if not isinstance(price, (int, float)):
        price = row.get("last_price")
    if not isinstance(price, (int, float)):
        return None
    return float(price)


def _dashboard_loaded_rows() -> list[Mapping[str, Any]]:
    loaded = load_quote_optional("dashboard_quotes_latest", default=[])
    if not loaded.get("available"):
        return []
    return list(display_quote_rows(loaded.get("data") or []))


def _provenance(row: Mapping[str, Any]) -> Mapping[str, Any]:
    provenance = row.get("provenance") or {}
    return provenance if isinstance(provenance, Mapping) else {}


def _index_quote_prices() -> dict[str, float]:
    prices: dict[str, float] = {}
    for row in _dashboard_loaded_rows():
        symbol = quote_symbol(row)
        price = _ibkr_quote_price(row)
        if symbol and price is not None:
            prices[symbol] = price
    return prices


def _since_open_by_symbol() -> dict[str, float | None]:
    found: dict[str, float | None] = {}
    for row in _dashboard_loaded_rows():
        symbol = quote_symbol(row)
        value = _provenance(row).get("open_to_current")
        found[symbol] = float(value) if isinstance(value, (int, float)) else None
    return found


def _dashboard_quote_legs(quotes: Sequence[Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
    legs: dict[str, dict[str, Any]] = {}
    for row in display_quote_rows(list(quotes or [])):
        symbol = quote_symbol(row)
        provenance = _provenance(row)
        live = provenance.get("open_to_current")
        if not isinstance(live, (int, float)):
            live = None
        session = provenance.get("session_date")
        legs[symbol] = {
            "live_return": live,
            "return_basis": "RTH_OPEN" if live is not None else None,
            "session_open_date": session,
            "session_open": provenance.get("session_open"),
            "current": {
                "market_data_status": quote_observation_status(row.get("quote_ts")),
                "observation_ts": row.get("quote_ts"),
                "session_date": session,
                "session": provenance.get("session"),
            },
        }
    return legs


def _since_open_columns(columns: Sequence[str]) -> list[str]:
    labels = list(columns)
    if labels and labels[0] == "1D":
        labels[0] = "Since open"
    return labels


def _index_snapshot(history: Mapping[str, Any]) -> None:
    """Five index cards. Each horizon badge is colored on its own sign."""
    st.subheader("Index Snapshot")
    returns = history.get("returns") or {}
    stored_prices = history.get("latest_price") or {}
    live_prices = _index_quote_prices()
    since_open = _since_open_by_symbol()
    cards: list[str] = []
    for symbol, name in US_INDEX_ETFS:
        window = returns.get(symbol) or {}
        price = live_prices.get(symbol)
        if price is None:
            price = stored_prices.get(symbol)
        since = since_open.get(symbol)
        badges = _return_badge(
            "Since open",
            since,
            title="Since open uses the most recent regular-session open. Extended-hours prices are included when Yahoo supplies them.",
        ) + "".join(
            _return_badge(label, window.get(label)) for label in ("1W", "1M")
        )
        cards.append(
            '<div style="flex:1 1 210px;min-width:190px;padding:10px 12px;border:1px solid rgba(128,128,128,0.35);border-radius:10px;">'
            '<div style="font-weight:700;font-size:15px;">{0}</div>'
            '<div style="font-size:12px;opacity:0.82;">{1}</div>'
            '<div style="font-size:22px;font-weight:700;margin:4px 0 6px 0;">{2}</div>'
            "<div>{3}</div></div>".format(_html_text(symbol), _html_text(name), _html_text(_price(price)), badges)
        )
    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;gap:10px;">{0}</div>'.format("".join(cards)),
        unsafe_allow_html=True,
    )
    latest = (history.get("bounds") or {}).get("latest")
    st.caption(
        "The price on each card is the newest stored Yahoo price. When that quote has no price, the card shows the last stored adjusted close. "
        "Since open is that price divided by the regular-session open, including extended-hours prices when Yahoo supplies them. "
        "1W and 1M are stored trading sessions (5 and 21), not calendar days. "
        "Each badge is colored independently. Missing data is N/A. Session returns through {0}.".format(latest or "—")
    )


def _single_choice(label: str, options: Sequence[str], *, key: str, default: str) -> str:
    if key not in st.session_state:
        st.session_state[key] = default
    selected = st.pills(label, list(options), selection_mode="single", key=key, **pills_layout_kwargs())
    if selected in (None, "", []):
        return default
    if isinstance(selected, list):
        return str(selected[0]) if selected else default
    return str(selected)


def _us_index_performance(history: Mapping[str, Any], start: date | None, end: date | None, *, mode: str) -> None:
    st.subheader("Index Performance")
    indexed = mode != "Absolute"
    if indexed:
        st.caption("Indexed to 100 on the first session in the range where every selected series has an adjusted close. This display also applies to the ratios below.")
    else:
        st.caption("Absolute adjusted close. Series are not rebased. The ratios below use the raw price ratio.")
    labeled = tuple((symbol, index_etf_label(symbol, name)) for symbol, name in US_PERFORMANCE_ETFS)
    selected = series_toggles(labeled, key="us_performance", group_label="Index Performance")
    if not selected:
        st.caption("Select at least one series.")
        return
    names = {symbol: label for symbol, label in labeled}
    options = [(symbol, names.get(symbol, symbol)) for symbol in selected]
    if indexed:
        _rebasing_chart(history, options, start, end, key="us_performance")
    else:
        _absolute_price_chart(history, options, start, end, key="us_performance_absolute")
    _provider_caption(history, selected)


def _absolute_price_chart(
    history: Mapping[str, Any],
    options: Sequence[tuple[str, str]],
    start: date | None,
    end: date | None,
    *,
    key: str,
) -> None:
    if start is None or end is None or start > end:
        st.caption("No stored history in this range.")
        return
    series = []
    for symbol, label in options:
        points = _clip_bars(history, symbol, start, end)
        if points:
            series.append(_line(label, points))
    if not series:
        st.caption("No stored adjusted closes in this range.")
        return
    lightweight_market_chart(series=series, value_format="number", ranges=True, height=_CHART_HEIGHT, key=key, align_union=True)


def _us_relative_performance(history: Mapping[str, Any], start: date | None, end: date | None, *, mode: str) -> None:
    st.subheader("Relative Performance")
    indexed = mode != "Absolute"
    if indexed:
        st.caption("Indexed to 100 rebases each price ratio at the first overlapping session. It does not divide two indexed price series.")
    else:
        st.caption("Absolute ratio is the adjusted price of the numerator divided by SPY.")
    for symbol, title, caption in US_LEADERSHIP:
        _us_ratio_chart(history, symbol, "SPY", title=title, caption=caption, start=start, end=end, indexed=indexed, key="us_ratio_{0}".format(symbol))


def _us_ratio_chart(
    history: Mapping[str, Any],
    asset: str,
    benchmark: str,
    *,
    title: str,
    caption: str,
    start: date | None,
    end: date | None,
    indexed: bool,
    key: str,
) -> None:
    st.subheader(title)
    st.caption(caption)
    if start is None or end is None or start > end:
        st.caption("No stored history in this range.")
        return
    asset_bars = _clip_bars(history, asset, start, end)
    bench_bars = _clip_bars(history, benchmark, start, end)
    if indexed:
        payload = normalized_ratio(asset_bars, bench_bars)
        points = payload.get("points") or []
        value_format = "index"
        series_label = "Relative index (start = 100)"
    else:
        points = price_ratio_points(asset_bars, bench_bars)
        value_format = "ratio"
        series_label = "{0} / {1}".format(asset, benchmark)
    if not points:
        st.caption("No overlapping adjusted closes for {0} and {1} in this range.".format(asset, benchmark))
        return
    change = period_change(points)
    latest = change.get("latest")
    if isinstance(latest, (int, float)):
        shown = "{0:.2f}".format(latest) if indexed else "{0:.4f}".format(latest)
        move = change.get("change")
        move_text = _signed_percent(move) if isinstance(move, (int, float)) else "N/A"
        st.caption("Latest {0} · {1} over the visible period.".format(shown, move_text))
    lightweight_market_chart(
        points=[{"as_of": day.isoformat(), "value": value} for day, value in points],
        series_label=series_label,
        value_format=value_format,
        ranges=True,
        height=_CHART_HEIGHT,
        key=key,
    )
    _provider_caption(history, [asset, benchmark])


def _aligned_panel(result: Mapping[str, Any]) -> dict[str, Any]:
    if not result.get("available"):
        return {
            "available": False,
            "reason": result.get("error") or "read_failed",
            "spy_returns": {},
            "sectors": [],
            "subsectors": {},
            "themes_omitted": [],
            "method": "",
            "endpoint": None,
            "adjustment_basis": None,
            "source_id": "EQUITY_EOD",
        }
    data = result.get("data") or {}
    if not isinstance(data, Mapping):
        return {"available": False, "reason": "read_failed", "spy_returns": {}, "sectors": [], "subsectors": {}, "themes_omitted": []}
    return dict(data)


def _aligned_source_caption(panel: Mapping[str, Any]) -> None:
    if not panel.get("available"):
        st.caption("Sector and subsector returns need an EQUITY_EOD SPY session with one adjustment basis. That endpoint is missing, so older snapshots are not used.")
        return
    endpoint = panel.get("endpoint") or "unavailable"
    basis = panel.get("adjustment_basis") or "unspecified"
    method = panel.get("method") or "equal_dollar_daily_rebalance_v1"
    st.caption(
        "Shared EQUITY_EOD session endpoint {0}. Adjustment basis {1}. Sector ETFs, curated baskets, and SPY use these sessions. A missing endpoint or a different adjustment basis is N/A. Basket method {2}.".format(
            endpoint, basis, method
        )
    )


def _quote_snapshot(panel: Mapping[str, Any], *, mode: str) -> dict[str, Any]:
    loaded = load_quote_optional("dashboard_quotes_latest", default=[])
    quotes = loaded.get("data") if loaded.get("available") else []
    overlaid = overlay_stored_quote_returns(panel, _dashboard_quote_legs(quotes))
    freshness = overlaid.get("quote_freshness") or {}
    relative = mode == "Relative vs SPY"
    updated = freshness.get("relative_updated") if relative else freshness.get("updated")
    statuses = freshness.get("relative_statuses") if relative else freshness.get("statuses")
    parsed = None
    if updated:
        try:
            parsed = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    endpoint = as_day(overlaid.get("endpoint"))
    caption = heatmap_freshness_label(
        statuses=list(statuses or []),
        updated=parsed,
        market_state=market_session_state(),
        endpoint=endpoint,
    )
    return {"panel": overlaid, "caption": caption}


def _return_matrix(panel: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], *, mode: str) -> dict[str, Any]:
    return subsector_matrix(
        rows,
        panel.get("spy_returns") or {},
        mode=mode,
        spy_eod_returns=panel.get("spy_eod_returns"),
        spy_quote=panel.get("spy_quote_1d"),
    )


@st.fragment(run_every=15)
def _us_return_heatmaps(panel: Mapping[str, Any], *, mode: str) -> None:
    """Heatmaps read the shared quote cache. This fragment does not open TWS."""
    snapshot = _quote_snapshot(panel, mode=mode)
    priced = snapshot["panel"]
    st.caption(snapshot["caption"])
    _us_sector_heatmap(priced, mode=mode)
    _us_subsector_heatmap(priced, mode=mode)
    _individual_stock_heatmap(priced)


def _us_sector_heatmap(panel: Mapping[str, Any], *, mode: str) -> None:
    st.subheader("Sector Performance")
    analytical = "relative" if mode == "Relative vs SPY" else "absolute"
    if analytical == "relative":
        st.caption("Relative vs SPY subtracts the SPY return on the same session and price basis, in percentage points. +2.30% means the sector outperformed SPY by 2.30 percentage points. A live quote that does not share SPY's session uses the EQUITY_EOD pair.")
    else:
        st.caption("Absolute percentage return of each sector ETF between the shared trading sessions.")
    st.caption("Each horizon has its own color scale. Technology is the Information Technology sector (XLK). Missing cells are N/A.")
    _mapped_caption()
    _aligned_source_caption(panel)
    rows = list(panel.get("sectors") or [])
    if not rows:
        return
    matrix = _return_matrix(panel, rows, mode=analytical)
    st.caption(
        "Since open is the newest stored Yahoo price divided by the most recent regular-session open. "
        "Extended-hours prices are included when Yahoo supplies them. A missing open is N/A."
    )
    column_scaled_return_heatmap(
        [row["label"] for row in matrix["rows"]],
        _since_open_columns(matrix["columns"]),
        [row["values"] for row in matrix["rows"]],
        notes=[row["notes"] for row in matrix["rows"]],
        key="us_sector_heatmap_{0}".format(analytical),
    )


def _us_subsector_heatmap(panel: Mapping[str, Any], *, mode: str) -> None:
    st.subheader("Subsector Performance")
    st.caption("Rows are curated current-context baskets, not the dataset's official industry list. The heatmap uses the Absolute / Relative vs SPY control above.")
    themes = [str(label) for label in (panel.get("themes_omitted") or []) if label]
    if themes:
        st.caption("Cross-sector themes are omitted: {0}.".format(", ".join(themes)))
    names = list(CANONICAL_SECTORS)
    if "us_subsector_sector" not in st.session_state:
        st.session_state["us_subsector_sector"] = "Technology"
    sector = st.selectbox("Sector", names, key="us_subsector_sector")
    if not panel.get("available"):
        st.caption("No subsector returns can be priced without the shared SPY endpoint. Coverage is not guessed.")
        return
    rows = list((panel.get("subsectors") or {}).get(str(sector)) or [])
    if not rows:
        st.caption(NO_SUBSECTOR_CLASSIFICATION)
        return
    st.caption(
        "Each horizon is the equal-dollar daily-rebalanced basket through the shared endpoint. A horizon keeps only members with prices on both sessions. Fewer than 2 members is N/A. One missing constituent does not hide the sector. A session with no observable member return is N/A."
    )
    analytical = "relative" if mode == "Relative vs SPY" else "absolute"
    matrix = _return_matrix(panel, rows, mode=analytical)
    column_scaled_return_heatmap(
        [row["label"] for row in matrix["rows"]],
        _since_open_columns(matrix["columns"]),
        [row["values"] for row in matrix["rows"]],
        notes=[row["notes"] for row in matrix["rows"]],
        key="us_subsector_heatmap_{0}_{1}".format(sector, analytical),
    )
    _constituent_detail(matrix["rows"])


def _stored_stock_returns(panel: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """EOD horizon returns already priced for basket members. 1D on the stock card stays IBKR."""
    found: dict[str, Mapping[str, Any]] = {}
    for rows in (panel.get("subsectors") or {}).values():
        for row in rows:
            for member in row.get("constituents") or []:
                symbol = str(member.get("symbol") or "").upper()
                if symbol and symbol not in found:
                    found[symbol] = member.get("returns") or {}
    return found


def _individual_stock_heatmap(panel: Mapping[str, Any] | None = None) -> None:
    """Approved stocks only. Quotes come from PostgreSQL; this does not open TWS."""
    st.subheader("Individual Stocks")
    endpoint = as_day((panel or {}).get("endpoint")) if panel else None
    st.caption(
        "Since open is the newest stored Yahoo price divided by the most recent regular-session open, including extended hours when Yahoo supplies them. "
        "1W, 1M, 3M, 6M, and 1Y are stored session windows (5, 21, 63, 126, and 252) through {0}. "
        "A ticker listed in more than one group uses the same stored quote. "
        "A missing open, price, or stored close is N/A.".format(endpoint.isoformat() if endpoint else "the equity endpoint")
    )
    loaded = load_quote_optional("dashboard_quotes_latest", default=[])
    if not loaded.get("available"):
        st.caption("Stored Yahoo quotes are unavailable ({0}).".format(loaded.get("error") or "unread"))
        return
    stored = _stored_stock_returns(panel or {})
    rows = stock_heatmap_rows(loaded.get("data") or [])
    groups: list[str] = []
    for row in rows:
        if row["group"] not in groups:
            groups.append(row["group"])
    if not groups:
        st.caption("No approved individual stocks are configured.")
        return
    for group in groups:
        members = [row for row in rows if row["group"] == group]
        st.caption(group)
        values = [stock_horizon_values(row["open_to_current"], stored.get(row["symbol"])) for row in members]
        notes = []
        for row, horizon_values in zip(members, values):
            row_notes = []
            for label, value in zip(STOCK_RETURN_HORIZONS, horizon_values):
                if label == "1D":
                    note = row["note"]
                    if quote_observation_status(row.get("quote_ts")) == "STALE":
                        note = "{0} · stale".format(note).strip(" ·")
                    row_notes.append(note)
                elif value is None:
                    row_notes.append("No stored close")
                else:
                    row_notes.append("EQUITY_EOD")
            notes.append(row_notes)
        column_scaled_return_heatmap(
            [
                "{0} · {1}".format(row["symbol"], _price(row["price"]))
                for row in members
            ],
            _since_open_columns(STOCK_RETURN_HORIZONS),
            values,
            notes=notes,
            key="us_stock_heatmap_{0}".format(group),
        )


def _constituent_detail(rows: Sequence[Mapping[str, Any]]) -> None:
    labels: dict[str, str] = {}
    details: dict[str, Mapping[str, Any]] = {}
    groups: dict[str, str] = {}
    for row in rows:
        for member in row.get("constituents") or []:
            symbol = str(member.get("symbol") or "").upper()
            if not symbol or symbol in labels:
                continue
            labels[symbol] = constituent_label(symbol, str(member.get("company") or ""))
            details[symbol] = member
            groups[symbol] = str(row.get("label") or "")
    options = sorted(labels, key=lambda symbol: labels[symbol])
    if not options:
        return
    chosen = st.selectbox(
        "Constituent",
        options,
        format_func=lambda symbol: labels.get(str(symbol), str(symbol)),
        key="us_constituent",
    )
    member = details.get(str(chosen)) or {}
    returns = member.get("returns") or {}
    st.caption("{0} · {1}".format(labels.get(str(chosen), str(chosen)), groups.get(str(chosen), "")))
    columns = st.columns(len(HORIZONS))
    for column, (label, _field, _sessions) in zip(columns, HORIZONS):
        value = returns.get(label)
        column.metric(label, "N/A" if not isinstance(value, (int, float)) else _signed_percent(value))
    source = str(member.get("quote_1d") or "HISTORICAL")
    if member.get("quote_unused"):
        st.caption("1D is EQUITY_EOD. A stored quote was not applied because it does not share the basket session and price basis.")
    elif source == "HISTORICAL":
        st.caption("1D is EQUITY_EOD, the same session as this basket cell.")
    else:
        st.caption("1D is {0}, the same session as this basket cell.".format(source))


def _snapshot_row(history: Mapping[str, Any], symbols: Sequence[str], labels: Mapping[str, str]) -> None:
    prices = history.get("latest_price") or {}
    returns = history.get("returns") or {}
    visible = [symbol for symbol in symbols if prices.get(symbol) is not None]
    if not visible:
        return
    for offset in range(0, len(visible), 2):
        cols = st.columns(2)
        for column, symbol in zip(cols, visible[offset : offset + 2]):
            window = returns.get(symbol) or {}
            column.metric(
                labels.get(symbol, symbol),
                _price(prices.get(symbol)),
                delta="{0} 1D · {1} 1M".format(_signed_percent(window.get("1D")), _signed_percent(window.get("1M"))),
            )


def _range_selector(history: Mapping[str, Any], *, key: str) -> tuple[date | None, date | None]:
    bounds = history.get("bounds") or {}
    return historical_date_range(key=key, earliest=as_day(bounds.get("earliest")), latest=as_day(bounds.get("latest")))


def _performance_section(
    title: str,
    caption: str,
    history: Mapping[str, Any],
    options: Sequence[tuple[str, str]],
    start: date | None,
    end: date | None,
    *,
    key: str,
    default: Sequence[str] | None,
) -> None:
    st.subheader(title)
    st.caption(caption)
    selected = series_toggles(options, key=key, group_label=title, default=default)
    names = {symbol: label for symbol, label in options}
    if not selected:
        st.caption("Select at least one series.")
        return
    _rebasing_chart(history, [(symbol, names.get(symbol, symbol)) for symbol in selected], start, end, key=key)
    _provider_caption(history, selected)


def _fixed_performance(
    history: Mapping[str, Any],
    options: Sequence[tuple[str, str]],
    start: date | None,
    end: date | None,
    *,
    key: str,
) -> None:
    _rebasing_chart(history, list(options), start, end, key=key)
    _provider_caption(history, [symbol for symbol, _label in options])


def _rebasing_chart(
    history: Mapping[str, Any],
    options: Sequence[tuple[str, str]],
    start: date | None,
    end: date | None,
    *,
    key: str,
) -> None:
    if start is None or end is None or start > end:
        st.caption("No stored history in this range.")
        return
    clipped = {symbol: _clip_bars(history, symbol, start, end) for symbol, _label in options}
    normalized = normalize_selected_to_100(clipped, [symbol for symbol, _label in options])
    if normalized.get("start") is None:
        st.caption("No common date with valid adjusted closes for the selected series.")
        return
    st.caption("Common start {0}.".format(normalized["start"].isoformat()))
    series = []
    for symbol, label in options:
        points = normalized["series"].get(symbol) or []
        if points:
            series.append(_line(label, points))
    if series:
        lightweight_market_chart(
            series=series,
            value_format="index",
            ranges=True,
            height=_CHART_HEIGHT,
            key=key,
            align_union=True,
        )


def _ratio_chart(
    history: Mapping[str, Any],
    asset: str,
    benchmark: str,
    *,
    title: str,
    caption: str,
    start: date | None,
    end: date | None,
    key: str,
) -> None:
    st.subheader(title)
    st.caption(caption)
    if start is None or end is None or start > end:
        st.caption("No stored history in this range.")
        return
    ratio = normalized_ratio(_clip_bars(history, asset, start, end), _clip_bars(history, benchmark, start, end))
    points = ratio.get("points") or []
    if not points:
        st.caption("No overlapping adjusted closes for {0} and {1} in this range.".format(asset, benchmark))
        return
    lightweight_market_chart(
        points=[{"as_of": day.isoformat(), "value": value} for day, value in points],
        series_label="Relative index (start = 100)",
        value_format="index",
        ranges=True,
        height=_CHART_HEIGHT,
        key=key,
    )
    _provider_caption(history, [asset, benchmark])


def _sector_sections(sectors: Mapping[str, Any], live: Mapping[str, Any], history: Mapping[str, Any]) -> None:
    raw = (sectors.get("datasets") or {}).get("ETF_RS_VS_SPY") or []
    rows = attach_live_1d_to_sector_rows(preferred_canonical_sector_rows(raw), live)
    st.subheader("Sector Performance")
    horizon = _horizon_choice("Sector return horizon", key="us_sector_return")
    pairs, state = sector_bar_pairs(rows, horizon, kind="return")
    st.caption(state)
    _mapped_caption()
    if pairs:
        ranked_bar_chart(
            [label for label, _value in pairs],
            [value for _label, value in pairs],
            key="us_sector_return_{0}".format(horizon),
            unit="percent",
            benchmark=_spy_benchmark(history, live, horizon, use_live=_live_return_marker(state)),
        )
    else:
        st.caption("No stored sector returns for this horizon.")
    st.subheader("Sector Relative Strength vs SPY")
    rs_horizon = _horizon_choice("Sector relative-strength horizon", key="us_sector_rs")
    rs_pairs, rs_state = sector_bar_pairs(rows, rs_horizon, kind="rs")
    st.caption(rs_state)
    if rs_pairs:
        ranked_bar_chart(
            [label for label, _value in rs_pairs],
            [value for _label, value in rs_pairs],
            key="us_sector_rs_{0}".format(rs_horizon),
            unit="percent",
        )
    else:
        st.caption("No stored sector relative strength for this horizon.")


def _live_return_marker(state: str) -> bool:
    return state.startswith("Live 1D")


def _spy_benchmark(history: Mapping[str, Any], live: Mapping[str, Any], horizon: str, *, use_live: bool) -> tuple[str, float] | None:
    if use_live:
        spy = (live.get("by_symbol") or {}).get("SPY") or {}
        value = spy.get("live_return")
        if isinstance(value, (int, float)):
            return ("SPY", float(value))
        return None
    value = ((history.get("returns") or {}).get("SPY") or {}).get(horizon)
    if isinstance(value, (int, float)):
        return ("SPY", float(value))
    return None


def _ranked_return_section(
    title: str,
    caption: str,
    history: Mapping[str, Any],
    options: Sequence[tuple[str, str]],
    *,
    key: str,
) -> None:
    st.subheader(title)
    horizon = _horizon_choice(title, key=key)
    st.caption("{0} {1} = {2} stored sessions.".format(caption, horizon, _sessions(horizon)))
    labels = []
    values = []
    returns = history.get("returns") or {}
    for symbol, label in options:
        value = (returns.get(symbol) or {}).get(horizon)
        if isinstance(value, (int, float)):
            labels.append(label)
            values.append(float(value))
    if labels:
        ranked_bar_chart(labels, values, key="{0}_{1}".format(key, horizon), unit="percent", benchmark=_spy_benchmark(history, {}, horizon, use_live=False) if "SPY" not in {symbol for symbol, _label in options} else None)
    else:
        st.caption("No stored returns for this horizon.")


def _heatmap_section(
    title: str,
    caption: str,
    history: Mapping[str, Any],
    order: Sequence[tuple[str, str]],
    *,
    key: str,
) -> None:
    st.subheader(title)
    st.caption(caption)
    matrix = heatmap_rows(order, history.get("returns") or {})
    populated = any(value is not None for row in matrix["rows"] for value in row["values"])
    if not populated:
        st.caption("No stored session returns for this heatmap.")
        return
    return_heatmap(
        [row["label"] for row in matrix["rows"]],
        matrix["columns"],
        [row["values"] for row in matrix["rows"]],
        key=key,
    )


def _drawdown_section(history: Mapping[str, Any], symbols: Sequence[str], start: date | None, end: date | None) -> None:
    st.subheader("Drawdowns")
    st.caption("Percent below the trailing 252-session adjusted-close high. A new high is 0%. History shorter than 252 sessions stays missing.")
    if start is None or end is None or start > end:
        st.caption("No stored history in this range.")
        return
    series = []
    for symbol in symbols:
        path = trailing_drawdown((history.get("bars") or {}).get(symbol) or [])
        visible = clip_points(path, start=start, end=end)
        if visible:
            series.append(_line(symbol, visible, scale=100.0))
    if not series:
        st.caption("Fewer than 252 stored sessions, so the 52-week drawdown is missing.")
        return
    lightweight_market_chart(
        series=series,
        value_format="percent",
        ranges=True,
        height=_CHART_HEIGHT,
        key="us_drawdown",
        align_union=True,
    )


def _methodology(lines: Sequence[str], *, sectors: bool = False) -> None:
    with st.expander("Methodology & sources"):
        for line in lines:
            st.markdown("- {0}".format(line))
        if sectors:
            st.markdown(
                "- Sector map: {0}.".format(
                    ", ".join("{0} {1}".format(symbol, name) for name, symbol in SECTOR_PROXIES.items())
                )
            )


def _mapped_caption() -> None:
    st.caption("Canonical sector ETFs: {0}.".format(", ".join("{0} {1}".format(symbol, name) for name, symbol in SECTOR_PROXIES.items())))


def _horizon_choice(label: str, *, key: str) -> str:
    if key not in st.session_state:
        st.session_state[key] = "1M"
    selected = st.pills(label, _HORIZON_LABELS, selection_mode="single", key=key, **pills_layout_kwargs())
    if selected in (None, "", []):
        return "1M"
    if isinstance(selected, list):
        return str(selected[0]) if selected else "1M"
    return str(selected)


def _clip_bars(history: Mapping[str, Any], symbol: str, start: date, end: date) -> list[tuple[date, float]]:
    points: list[tuple[date, float]] = []
    for row in (history.get("bars") or {}).get(symbol) or []:
        day = as_day(row.get("date"))
        if day is None or day < start or day > end:
            continue
        try:
            value = float(row.get("value"))
        except (TypeError, ValueError):
            continue
        points.append((day, value))
    return points


def _line(label: str, points: Sequence[tuple[date, float]], *, scale: float = 1.0) -> dict[str, Any]:
    return {
        "label": label,
        "points": [{"as_of": day.isoformat(), "value": value * scale} for day, value in points],
    }


def _provider_caption(history: Mapping[str, Any], symbols: Sequence[str]) -> None:
    meta = history.get("meta") or {}
    bits = []
    providers = set()
    for symbol in symbols:
        item = meta.get(symbol) or {}
        provider = item.get("provider") or "unavailable"
        basis = item.get("adjustment_basis") or ", ".join(item.get("adjustment_bases") or []) or "unspecified"
        source = item.get("source_id") or "unspecified source"
        providers.add(str(provider))
        bits.append("{0} {1} {2} ({3})".format(symbol, source, provider, basis))
    if len(providers) > 1:
        st.caption("Each series uses one stored provider and is not spliced. {0}.".format("; ".join(bits)))
    elif bits:
        st.caption("Market-monitor adjusted close · {0}.".format("; ".join(bits)))


def _price(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return "{0:.2f}".format(value)


def _signed_percent(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return "{0:+.2f}%".format(float(value) * 100.0)


def _sessions(horizon: str) -> int:
    for label, _field, sessions in HORIZONS:
        if label == horizon:
            return sessions
    return 0


__all__ = ["render_global_markets_page", "render_us_markets_page"]
