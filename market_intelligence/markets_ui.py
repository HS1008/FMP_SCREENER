"""Chart-first US Markets and Global Markets pages.

Streamlit reads prepared PostgreSQL rows only. Charts receive those rows.
This module does not call Yahoo, IBKR, FRED, or the database driver.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import streamlit as st

from market_intelligence.components.market_chart import lightweight_market_chart
from market_intelligence.components.tenor_chart import column_scaled_return_heatmap, ranked_bar_chart
from market_intelligence.equity_live import attach_live_1d_to_sector_rows, preferred_canonical_sector_rows
from market_intelligence.ibkr_live_universe import (
    STOCK_RETURN_HORIZONS,
    display_quote_rows,
    quote_symbol,
    quotes_by_symbol,
    stock_groups_by_era,
    stock_heatmap_rows,
    stock_horizon_values,
    subsector_groups,
)
from market_intelligence.live_1d_ui import (
    EOD_ONLY_CAPTION,
    ONE_DAY_POLICY_CAPTION,
    basis_badge_title,
    freshness_summary,
    one_day_cell,
    one_day_note,
)
from market_intelligence.navigation_links import PENDING_SCROLL_KEY, SUBSECTOR_SELECTOR_KEY, apply_pending_scroll, select_subsector
from market_intelligence.price_returns import PRICE_RETURN_CAPTION, latest_completed_close, parse_timestamp
from market_intelligence.return_policy import OneDay, format_eastern, quote_freshness
from market_intelligence.live_session import heatmap_freshness_label, market_session_state, quote_observation_status
from market_intelligence.stock_dialog import render_stock_dialog_if_requested, request_stock_dialog
from market_intelligence.risk_metrics import (
    RISK_COLUMN_KINDS,
    RISK_COLUMN_LABELS,
    STOCK_RISK_CAPTION,
    risk_row_cells,
)
from market_intelligence.stock_board import (
    COLUMNS_KEY,
    COLUMN_ALL,
    COLUMN_VIEWS,
    ERA_KEY,
    FILTER_ALL,
    FORCED_OPEN_KEY,
    GROUP_FILTER_KEY,
    LAYOUTS,
    LAYOUT_ALL,
    LAYOUT_GROUPED,
    LAYOUT_KEY,
    OPEN_MAP_KEY,
    QUERY_KEY,
    SORT_CATALOG,
    SORT_DESC_KEY,
    SORT_KEY,
    cached_horizons,
    cached_risk,
    dedupe_symbols,
    filter_members,
    group_has_match,
    group_state_key,
    group_summary_label,
    normalize_query,
    section_state_key,
    slice_columns,
    sort_choices,
    sort_rows,
    value_map,
)
from market_intelligence.history_range import historical_date_range, pills_layout_kwargs, series_toggles
from market_intelligence.markets_analytics import (
    GLOBAL_METHODOLOGY,
    HORIZONS,
    US_METHODOLOGY,
    as_day,
    clip_points,
    heatmap_rows,
    normalize_selected_to_100,
    overlay_stored_quote_returns,
    normalized_ratio,
    period_change,
    price_ratio_points,
    sector_bar_pairs,
    subsector_matrix,
)
from market_intelligence.taxonomy import (
    GLOBAL_CORE_ETFS,
    GLOBAL_DEFAULT_SELECTED,
    GLOBAL_LEADERSHIP,
    GLOBAL_MARKET_ETFS,
    GLOBAL_SNAPSHOT_SYMBOLS,
    SECTOR_PROXIES,
    US_INDEX_ETFS,
    US_LEADERSHIP,
    US_PERFORMANCE_ETFS,
    index_etf_label,
)
from market_intelligence.perf import span
from market_intelligence.ui import (
    BADGE_COLORS,
    html_text,
    load_optional,
    load_or_stop,
    load_quote_optional,
    page_header,
    return_badge,
)

# jobs.yahoo_dashboard_quotes is installed on a */15 cron. Heatmaps read those
# stored quotes, so the fragment follows that cadence instead of polling every
# 15 seconds.
YAHOO_HEATMAP_REFRESH_SECONDS = 15 * 60

_CHART_HEIGHT = 420
_ET = ZoneInfo("America/New_York")
_HORIZON_LABELS = [label for label, _field, _sessions in HORIZONS]

def _section_is_open(anchor: str, *, default_open: bool) -> bool:
    """Session state for a keyed expander, before the widget is created this run."""
    if st.session_state.get(PENDING_SCROLL_KEY) == anchor:
        return True
    key = section_state_key(anchor)
    if key in st.session_state:
        return bool(st.session_state[key])
    return default_open


def _render_section(title: str, *, anchor: str, default_open: bool, render: Any) -> None:
    """Collapsible section. The body runs only while the section is open.

    A pending in-page scroll opens the destination first, so the anchor exists
    on the same run that scrolls to it. The key is the anchor, not the label.
    """
    key = section_state_key(anchor)
    if st.session_state.get(PENDING_SCROLL_KEY) == anchor:
        st.session_state[key] = True
    box = st.expander(title, expanded=default_open, key=key, on_change="rerun")
    if not box.open:
        return
    with box:
        st.markdown('<div id="{0}"></div>'.format(html_text(anchor)), unsafe_allow_html=True)
        render()


def render_us_markets_page() -> None:
    """US equity indexes, relative performance, sectors, and subsectors."""
    page_header(
        "US Equities",
        "Index snapshot, relative performance, and sector returns from stored adjusted closes.",
        fred=False,
    )
    with span("us_equities.history"):
        history = load_or_stop("us_markets_history")
        # Sector and subsector heatmaps are the only consumers of this panel.
        # Stock baskets stay available from aligned_us_equity_returns for other
        # callers; this page reads Yahoo bars for the symbols it actually draws.
        needs_panel = _section_is_open("sector-performance", default_open=True) or _section_is_open(
            "subsector-performance", default_open=True
        )
        aligned = _aligned_panel(
            load_optional("aligned_us_equity_returns", include_stock_baskets=False) if needs_panel else {"available": False, "data": {}}
        )
    _render_section("Index Snapshot", anchor="index-snapshot", default_open=True, render=lambda: _index_snapshot(history))
    start, end = _range_selector(history, key="markets_us")
    price_mode = _single_choice("Display", ["Indexed to 100", "Absolute"], key="us_price_mode", default="Indexed to 100")
    _render_section(
        "Index Performance",
        anchor="index-performance",
        default_open=True,
        render=lambda: _us_index_performance(history, start, end, mode=price_mode),
    )
    _render_section(
        "Relative Performance",
        anchor="relative-performance",
        default_open=True,
        render=lambda: _us_relative_performance(history, start, end, mode=price_mode),
    )
    return_mode = _single_choice(
        "Sector performance",
        ["Absolute Performance", "Relative vs SPY"],
        key="us_sector_mode",
        default="Absolute Performance",
    )
    _us_return_heatmaps(aligned, mode=return_mode)
    # Search, sort, and expansion live outside the 15-minute heatmap fragment.
    # A fragment-local widget is reset when a control outside the fragment
    # reruns the whole page (the Refresh button). The open-state map is what
    # the stock fragment restores.
    _render_stock_board()
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
    _global_heatmap(history, list(GLOBAL_MARKET_ETFS), key="global_heatmap")
    _methodology(GLOBAL_METHODOLOGY)


def _global_heatmap(history: Mapping[str, Any], order: Sequence[tuple[str, str]], *, key: str) -> None:
    """Regional ETF heatmap. 1D follows the stored-quote policy per row; 1W-1Y stay completed close-to-close."""
    st.subheader("Global Return Heatmap")
    st.caption("Percentage returns from adjusted USD ETF prices. 1W and longer are completed close-to-close session windows.")
    matrix = heatmap_rows(order, history.get("returns") or {})
    moment = _now()
    _prices, one_days = _index_quote_state(_dashboard_loaded_rows(), now=moment)
    endpoint = as_day((history.get("bounds") or {}).get("latest"))
    cells: dict[str, OneDay] = {}
    notes: list[list[str | None]] = []
    values: list[list[Any]] = []
    for row in matrix["rows"]:
        symbol = str(row.get("symbol") or "")
        row_values = list(row["values"])
        row_notes: list[str | None] = [
            "Completed close-to-close over stored sessions{0}.".format(" through {0}".format(endpoint.isoformat()) if endpoint else "")
            for _column in matrix["columns"]
        ]
        one_day = one_days.get(symbol)
        if one_day is not None and one_day.available and "1D" in matrix["columns"]:
            index = matrix["columns"].index("1D")
            row_values[index] = one_day.value
            row_notes[index] = one_day_note(one_day, symbol=symbol, now=moment)
            cells[symbol] = one_day
        elif "1D" in matrix["columns"]:
            index = matrix["columns"].index("1D")
            row_notes[index] = "EOD close-to-close: no usable stored quote. " + EOD_ONLY_CAPTION
        values.append(row_values)
        notes.append(row_notes)
    populated = any(value is not None for row in values for value in row)
    if not populated:
        st.caption("No stored session returns for this heatmap.")
        return
    st.caption(ONE_DAY_POLICY_CAPTION + " Each horizon has its own color scale. Missing cells are N/A.")
    if cells:
        st.caption(freshness_summary(cells, now=moment))
    column_scaled_return_heatmap(
        [row["label"] for row in matrix["rows"]],
        matrix["columns"],
        values,
        notes=notes,
        key=key,
    )


_BADGE_COLORS = BADGE_COLORS
_html_text = html_text
_return_badge = return_badge


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _index_quote_state(rows: Sequence[Mapping[str, Any]], *, now: datetime | None = None) -> tuple[dict[str, float], dict[str, OneDay]]:
    """One pass over the prepared quote rows. Prices and policy 1D stay paired."""
    moment = now or _now()
    prices: dict[str, float] = {}
    one_day: dict[str, OneDay] = {}
    for row in rows:
        symbol = quote_symbol(row)
        price = _ibkr_quote_price(row)
        if symbol and price is not None:
            prices[symbol] = price
        one_day[symbol] = one_day_cell(symbol, row, now=moment)
    return prices, one_day


def _dashboard_quote_legs(quotes: Sequence[Mapping[str, Any]] | None, *, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Policy 1D per approved symbol in the shape ``overlay_stored_quote_returns`` expects.

    ``return_basis`` is SESSION_OPEN or LAST_CLOSE; ``session_open_date`` is the
    reference session so a sector cell and SPY are only paired on one session
    and basis. A pending 1D contributes no live leg.
    """
    moment = now or _now()
    legs: dict[str, dict[str, Any]] = {}
    for row in display_quote_rows(list(quotes or [])):
        symbol = quote_symbol(row)
        provenance = _provenance(row)
        one_day = one_day_cell(symbol, row, now=moment)
        legs[symbol] = {
            "live_return": one_day.value,
            "return_basis": one_day.basis if one_day.available else None,
            "session_open_date": one_day.reference_date.isoformat() if one_day.reference_date else None,
            "session_open": provenance.get("session_open"),
            "reference_price": one_day.reference_price,
            "one_day": one_day,
            "current": {
                "market_data_status": quote_observation_status(row.get("quote_ts")),
                "observation_ts": row.get("quote_ts"),
                "session_date": provenance.get("session_date"),
                "session": provenance.get("session"),
            },
        }
    return legs


def _since_open_columns(columns: Sequence[str]) -> list[str]:
    """Column labels. 1D keeps its name; the basis (since open / since last close) is in each cell note."""
    return list(columns)


@st.fragment(run_every=YAHOO_HEATMAP_REFRESH_SECONDS)
def _index_snapshot(history: Mapping[str, Any]) -> None:
    """Five index cards. Each horizon badge is colored on its own sign.

    This fragment re-reads stored quotes on the Yahoo collection cadence. It does
    not call a provider, and it does not touch the historical From/To range.
    """
    returns = history.get("returns") or {}
    stored_prices = history.get("latest_price") or {}
    moment = _now()
    loaded = load_quote_optional("dashboard_quotes_latest", default=[])
    quotes_available = bool(loaded.get("available"))
    rows = list(display_quote_rows(loaded.get("data") or [])) if quotes_available else []
    if not quotes_available:
        st.caption(
            "Stored quote read failed ({0}). 1D is N/A and is not filled from daily history. The price below is the last stored adjusted close when one exists.".format(
                loaded.get("error") or "unread"
            )
        )
    live_prices, one_days = _index_quote_state(rows, now=moment)
    cards: list[str] = []
    for symbol, name in US_INDEX_ETFS:
        window = returns.get(symbol) or {}
        price = live_prices.get(symbol)
        if price is None:
            price = stored_prices.get(symbol)
        one_day = one_days.get(symbol)
        if one_day is None:
            one_day = one_day_cell(symbol, None, now=moment)
        badges = _return_badge(
            "1D",
            one_day.value,
            title=one_day_note(one_day, symbol=symbol, now=moment) + " " + basis_badge_title(one_day),
        ) + "".join(
            _return_badge(label, window.get(label), title="Completed close-to-close over {0} stored sessions.".format(5 if label == "1W" else 21))
            for label in ("1W", "1M")
        )
        stamp = format_eastern(one_day.price_ts) if one_day.price_ts else ""
        updated = "Last updated: {0}".format(stamp) if stamp else "Last updated: unavailable (showing last stored close)"
        basis = one_day.basis_label if one_day.available else ("pending" if one_day.price_ts else "N/A")
        cards.append(
            '<div style="flex:1 1 210px;min-width:190px;padding:10px 12px;border:1px solid rgba(128,128,128,0.35);border-radius:10px;">'
            '<div style="font-weight:700;font-size:15px;">{0}</div>'
            '<div style="font-size:12px;opacity:0.82;">{1}</div>'
            '<div style="font-size:22px;font-weight:700;margin:4px 0 6px 0;">{2}</div>'
            "<div>{3}</div>"
            '<div style="font-size:11px;opacity:0.75;margin-top:6px;" title="{5}">{4} · {5}</div></div>'.format(
                _html_text(symbol), _html_text(name), _html_text(_price(price)), badges, _html_text(basis), _html_text(updated)
            )
        )
    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;gap:10px;">{0}</div>'.format("".join(cards)),
        unsafe_allow_html=True,
    )
    latest = (history.get("bounds") or {}).get("latest")
    st.caption(freshness_summary({symbol: one_days[symbol] for symbol, _name in US_INDEX_ETFS if symbol in one_days}, now=moment))
    st.caption("Page draw {0}. Observation times above are each quote's own timestamp, not this draw.".format(format_eastern(moment)))
    st.caption(
        "The price on each card is the newest stored Yahoo observation and each card shows that observation's own time in America/New_York; "
        "cards are not simultaneous. When no quote is stored the card shows the last stored adjusted close. "
        + ONE_DAY_POLICY_CAPTION
        + " 1W and 1M are completed close-to-close over stored trading sessions (5 and 21), not calendar days. "
        "Each badge is colored independently. Missing data is N/A. Completed sessions through {0}.".format(latest or "—")
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
        "Shared EQUITY_EOD session endpoint {0}. Adjustment basis {1}. Curated stock baskets and their SPY comparison use these sessions. A missing endpoint or a different adjustment basis is N/A. Basket method {2}.".format(
            endpoint, basis, method
        )
    )
    sector_endpoint = panel.get("sector_endpoint")
    if sector_endpoint and str(sector_endpoint) != str(endpoint):
        sector_basis = panel.get("sector_adjustment_basis") or "unspecified"
        st.caption(
            "Sector ETFs use Yahoo market-monitor closes through {0} ({1}). Relative sector performance subtracts that Yahoo SPY return. Stock baskets stay on the EQUITY_EOD endpoint above.".format(
                sector_endpoint, sector_basis
            )
        )


def _quote_snapshot(panel: Mapping[str, Any], quotes: Sequence[Mapping[str, Any]], *, mode: str, now: datetime | None = None) -> dict[str, Any]:
    overlaid = overlay_stored_quote_returns(panel, _dashboard_quote_legs(quotes, now=now))
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


def _return_matrix(
    panel: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    spy_returns: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return subsector_matrix(
        rows,
        spy_returns if spy_returns is not None else (panel.get("spy_returns") or {}),
        mode=mode,
        spy_eod_returns=panel.get("spy_eod_returns"),
        spy_quote=panel.get("spy_quote_1d"),
    )


@st.fragment(run_every=YAHOO_HEATMAP_REFRESH_SECONDS)
def _us_return_heatmaps(panel: Mapping[str, Any], *, mode: str) -> None:
    """Heatmaps read one stored quote snapshot. This fragment does not open TWS."""
    with span("us_equities.heatmaps"):
        now = _now()
        loaded = load_quote_optional("dashboard_quotes_latest", default=[])
        quotes = list(loaded.get("data") or []) if loaded.get("available") else []
        snapshot = _quote_snapshot(panel, quotes, mode=mode, now=now)
        priced = snapshot["panel"]
        st.caption(snapshot["caption"])
        _render_section(
            "Sector Performance",
            anchor="sector-performance",
            default_open=True,
            render=lambda: _us_sector_heatmap(priced, mode=mode, quotes=quotes, now=now),
        )
        subsector_open = _section_is_open("subsector-performance", default_open=True)
        subsector_symbols = _selected_subsector_symbols() if subsector_open else ()
        bars, bars_available, bars_error = _price_bars_by_symbol(subsector_symbols)
        legs: dict[str, dict[str, Any]] = {}
        _render_section(
            "Subsector Performance",
            anchor="subsector-performance",
            default_open=True,
            render=lambda: _us_subsector_heatmap(
                priced,
                mode=mode,
                loaded=loaded,
                quotes=quotes,
                bars=bars,
                bars_available=bars_available,
                bars_error=bars_error,
                legs=legs,
                now=now,
            ),
        )
        # A sector-row click reruns only this fragment, so the one-time scroll
        # request it leaves behind is served here, after the destination exists.
        # A cross-page drill is served by dashboard.main the same way; whichever
        # runs first consumes the request.
        apply_pending_scroll()


def _us_sector_heatmap(
    panel: Mapping[str, Any],
    *,
    mode: str,
    quotes: Sequence[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> None:
    analytical = "relative" if mode == "Relative vs SPY" else "absolute"
    if analytical == "relative":
        st.caption("Relative vs SPY subtracts the SPY return on the same session and price basis, in percentage points. +2.30% means the sector outperformed SPY by 2.30 percentage points. A live quote that does not share SPY's session and 1D basis uses the EQUITY_EOD pair.")
    else:
        st.caption("Absolute percentage return of each sector ETF between the shared trading sessions.")
    st.caption("Each horizon has its own color scale. Technology is the Information Technology sector (XLK). Missing cells are N/A.")
    _mapped_caption()
    _aligned_source_caption(panel)
    rows = list(panel.get("sectors") or [])
    if not rows:
        return
    matrix = _return_matrix(
        panel,
        rows,
        mode=analytical,
        spy_returns=panel.get("sector_spy_returns") or panel.get("spy_returns"),
    )
    st.caption(ONE_DAY_POLICY_CAPTION + " Longer horizons are completed close-to-close.")
    if quotes:
        moment = now or _now()
        by_symbol = quotes_by_symbol(list(quotes))
        cells = {
            str(row.get("symbol") or ""): one_day_cell(str(row.get("symbol") or ""), by_symbol.get(str(row.get("symbol") or "")), now=moment)
            for row in matrix["rows"]
            if row.get("symbol")
        }
        st.caption(freshness_summary(cells, now=moment))
    st.caption("Click a sector name or any cell in its row to open that sector in the Subsector Performance heatmap below.")
    column_scaled_return_heatmap(
        [row["label"] for row in matrix["rows"]],
        _since_open_columns(matrix["columns"]),
        [row["values"] for row in matrix["rows"]],
        notes=[row["notes"] for row in matrix["rows"]],
        key="us_sector_heatmap_{0}".format(analytical),
        row_ids=[str(row.get("symbol") or row["label"]) for row in matrix["rows"]],
        on_row_click=_open_subsector_for_sector,
    )


def _open_subsector_for_sector(sector_id: str) -> None:
    """Sector heatmap click: select that sector below and scroll to it on this run."""
    select_subsector(sector_id)


def _observed(value: Any) -> str:
    ts = parse_timestamp(value)
    if ts is None:
        return ""
    return "Last updated: {0}".format(format_eastern(ts))


def _selected_subsector_symbols() -> tuple[str, ...]:
    """ETFs for the sector currently selected, plus SPY. Does not create the selectbox."""
    groups = subsector_groups()
    names = [name for name, _members in groups]
    selected = st.session_state.get(SUBSECTOR_SELECTOR_KEY)
    if selected not in names:
        selected = names[0] if names else None
    members = dict(groups).get(str(selected), ())
    return dedupe_symbols([symbol for symbol, _label in members] + ["SPY"])


def _price_bars_by_symbol(symbols: Sequence[str]) -> tuple[dict[str, list[dict[str, Any]]], bool, str | None]:
    """One batched daily-history read. Empty input does not query.

    Returns ``(bars, available, error)``. ``available`` is False only when the
    read failed. An empty bar list with ``available`` True means the symbols
    have no stored rows.
    """
    wanted = dedupe_symbols(symbols)
    if not wanted:
        return {}, True, None
    loaded = load_optional("dashboard_price_bars", tuple(sorted(wanted)))
    grouped: dict[str, list[dict[str, Any]]] = {}
    if not loaded.get("available"):
        return grouped, False, str(loaded.get("error") or "unread")
    for row in loaded.get("data") or []:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        grouped.setdefault(symbol, []).append(
            {
                "bar_date": row.get("bar_date"),
                "close": row.get("close_price"),
                "adj_close": row.get("adj_close_price"),
                "basis": row.get("adjustment_basis"),
                "quality": row.get("bar_quality"),
            }
        )
    return grouped, True, None


def _remember_legs(
    cache: dict[str, dict[str, Any]],
    symbol: str,
    bars: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Completed close-to-close horizons depend only on stored bars. Compute each ticker once."""
    found = cache.get(symbol)
    if found is not None:
        return found
    found = cached_horizons(symbol, bars)
    if symbol:
        cache[symbol] = found
    return found


def _horizon_cells(
    *,
    quote: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    note: str,
    symbol: str = "",
    legs_cache: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
    open_to_current: float | None = None,
    price: Any = None,
    quote_ts: Any = None,
    quote_failure: str | None = None,
    history_failure: str | None = None,
) -> tuple[list[float | None], list[str]]:
    """One heatmap row: policy 1D from the stored quote, 1W-1Y from completed closes.

    ``open_to_current`` / ``price`` / ``quote_ts`` are accepted for callers that
    still pass them but are not used for any value: the quote row decides 1D
    and the stored bars decide the longer horizons.
    """
    del open_to_current, price
    moment = now or _now()
    if legs_cache is not None:
        legs = _remember_legs(legs_cache, symbol, bars)
    else:
        legs = completed_price_horizons(bars)
    stored = {label: legs[label]["value"] for label in legs}
    one_day = one_day_cell(symbol, quote, now=moment)
    values = stock_horizon_values(one_day.value, stored)
    stamp = quote.get("quote_ts") if quote else quote_ts
    observed = _observed(stamp)
    notes: list[str] = []
    for label in STOCK_RETURN_HORIZONS:
        if label == "1D":
            if quote_failure:
                notes.append(
                    "N/A: stored quote read failed ({0}). 1D is not filled from daily history.".format(quote_failure)
                )
            elif quote is None:
                notes.append(one_day_note(one_day, symbol=symbol, extra=(note, "no stored quote for {0}".format(symbol)), now=moment))
            else:
                notes.append(one_day_note(one_day, symbol=symbol, extra=(note,), now=moment))
        elif history_failure:
            notes.append("N/A: daily history read failed ({0}). This is not an empty history.".format(history_failure))
        else:
            endpoint = legs[label].get("endpoint")
            parts = [
                "completed close-to-close",
                str(legs[label]["reason"]),
                "last completed close {0}".format(endpoint) if endpoint else "",
                observed,
            ]
            notes.append(" · ".join(part for part in parts if part))
    return values, notes


def _minus_spy(values: Sequence[float | None], spy: Sequence[float | None] | None) -> list[float | None]:
    if not spy:
        return [None for _value in values]
    compared: list[float | None] = []
    for value, base in zip(values, spy):
        if value is None or base is None:
            compared.append(None)
        else:
            compared.append(value - base)
    return compared


def _us_subsector_heatmap(
    _panel: Mapping[str, Any],
    *,
    mode: str,
    loaded: Mapping[str, Any] | None = None,
    quotes: Sequence[Mapping[str, Any]] | None = None,
    bars: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    bars_available: bool = True,
    bars_error: str | None = None,
    legs: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> None:
    moment = now or _now()
    groups = subsector_groups()
    names = [name for name, _members in groups]
    if st.session_state.get(SUBSECTOR_SELECTOR_KEY) not in names:
        st.session_state[SUBSECTOR_SELECTOR_KEY] = names[0]
    sector = st.selectbox("Sector", names, key=SUBSECTOR_SELECTOR_KEY)
    members = dict(groups)[str(sector)]
    st.caption(", ".join("{0} · {1}".format(symbol, label) for symbol, label in members))
    st.caption("Rows are the listed subsector ETFs in list order. " + ONE_DAY_POLICY_CAPTION + " " + PRICE_RETURN_CAPTION)
    if mode == "Relative vs SPY":
        st.caption("Relative vs SPY subtracts SPY's return on the same price basis, in percentage points. 1D is subtracted only when the row and SPY share one 1D basis and reference session.")
    if loaded is None:
        loaded = load_quote_optional("dashboard_quotes_latest", default=[])
        quotes = list(loaded.get("data") or []) if loaded.get("available") else []
    quotes_available = bool(loaded.get("available"))
    quote_failure = None if quotes_available else str(loaded.get("error") or "unread")
    if not quotes_available:
        st.caption(
            "Stored Yahoo quotes are unavailable ({0}). 1D is N/A. Longer horizons still use stored daily history when that read succeeds.".format(
                quote_failure
            )
        )
    elif not quotes:
        st.caption("Stored quote snapshot is empty. 1D is N/A. Longer horizons use stored daily history.")
    by_symbol = quotes_by_symbol(list(quotes or []))
    if bars is None:
        bars, bars_available, bars_error = _price_bars_by_symbol(_selected_subsector_symbols())
    if not bars_available:
        st.caption("Daily history read failed ({0}). Longer returns are N/A. This is not an empty history.".format(bars_error or "unread"))
    bar_map = bars or {}
    if legs is None:
        legs = {}
    history_failure = None if bars_available else (bars_error or "unread")
    spy = by_symbol.get("SPY") or None
    spy_one_day = one_day_cell("SPY", spy, now=moment)
    spy_values, _spy_notes = _horizon_cells(
        quote=spy,
        bars=bar_map.get("SPY") or [],
        note="",
        symbol="SPY",
        legs_cache=legs,
        now=moment,
        quote_failure=quote_failure,
        history_failure=history_failure,
    )
    relative = mode == "Relative vs SPY"
    labels: list[str] = []
    values: list[list[float | None]] = []
    notes: list[list[str]] = []
    cells: dict[str, OneDay] = {}
    for symbol, label in members:
        quote = by_symbol.get(symbol) or None
        provenance = (quote or {}).get("provenance") or {}
        if not isinstance(provenance, Mapping):
            provenance = {}
        row_values, row_notes = _horizon_cells(
            quote=quote,
            bars=bar_map.get(symbol) or [],
            note=str(provenance.get("session") or ""),
            symbol=symbol,
            legs_cache=legs,
            now=moment,
            quote_failure=quote_failure,
            history_failure=history_failure,
        )
        cells[symbol] = one_day_cell(symbol, quote, now=moment)
        if relative:
            row_values = _minus_spy(row_values, spy_values)
            if not _same_one_day_basis(cells[symbol], spy_one_day):
                row_values[0] = None
                row_notes[0] = "N/A: row and SPY 1D are on different bases or reference sessions · " + row_notes[0]
        labels.append("{0} · {1}".format(symbol, label))
        values.append(row_values)
        notes.append(row_notes)
    st.caption(freshness_summary(cells, now=moment))
    column_scaled_return_heatmap(
        labels,
        _since_open_columns(STOCK_RETURN_HORIZONS),
        values,
        notes=notes,
        key="us_subsector_heatmap_{0}_{1}".format(sector, "relative" if relative else "absolute"),
    )


def _same_one_day_basis(left: OneDay, right: OneDay) -> bool:
    return (
        left.available
        and right.available
        and left.basis == right.basis
        and left.reference_date == right.reference_date
    )


def _reset_stock_controls() -> None:
    """Clear discovery controls. Group expansion is kept."""
    st.session_state[QUERY_KEY] = ""
    st.session_state[ERA_KEY] = FILTER_ALL
    st.session_state[GROUP_FILTER_KEY] = FILTER_ALL
    st.session_state[SORT_KEY] = SORT_CATALOG
    st.session_state[SORT_DESC_KEY] = False
    st.session_state[COLUMNS_KEY] = COLUMN_ALL
    st.session_state[LAYOUT_KEY] = LAYOUT_GROUPED


def _on_group_toggle(group: str) -> None:
    """User toggle. A full-page rerun does not call this, so it cannot wipe the map."""
    mapping = dict(st.session_state.get(OPEN_MAP_KEY) or {})
    mapping[str(group)] = bool(st.session_state.get(group_state_key(group), False))
    st.session_state[OPEN_MAP_KEY] = mapping


_COLLAPSE_ONCE_KEY = "us_stock_collapse_once"


def _set_every_group(open_: bool) -> None:
    mapping: dict[str, bool] = {}
    for _title, _caption, groups in stock_groups_by_era():
        for group, _members in groups:
            mapping[group] = open_
            st.session_state[group_state_key(group)] = open_
    st.session_state[OPEN_MAP_KEY] = mapping
    st.session_state[FORCED_OPEN_KEY] = []
    # Search would otherwise reopen matches in this same run. The stock fragment
    # consumes the flag once, so a later rerun can open matches again.
    if open_:
        st.session_state.pop(_COLLAPSE_ONCE_KEY, None)
    else:
        st.session_state[_COLLAPSE_ONCE_KEY] = True


def _select_choice(label: str, options: Sequence[str], *, key: str, default: str) -> str:
    if st.session_state.get(key) not in options:
        st.session_state[key] = default
    return str(st.selectbox(label, list(options), key=key))


def _stock_controls() -> dict[str, Any]:
    """Search, filters, sort, and column view. State lives in session keys."""
    st.text_input("Search", key=QUERY_KEY, placeholder="Ticker, group, or company", help="Matches the ticker, the group or era name, and a curated company name when one is stored.")
    era_options = [FILTER_ALL] + [title for title, _caption, _groups in stock_groups_by_era()]
    era = _select_choice("Era", era_options, key=ERA_KEY, default=FILTER_ALL)
    group_options = [FILTER_ALL]
    for title, _caption, groups in stock_groups_by_era():
        if era != FILTER_ALL and title != era:
            continue
        group_options.extend(group for group, _members in groups)
    group = _select_choice("Group", group_options, key=GROUP_FILTER_KEY, default=FILTER_ALL)
    sort_by = _select_choice("Sort rows by", sort_choices(), key=SORT_KEY, default=SORT_CATALOG)
    descending = st.checkbox("Descending", key=SORT_DESC_KEY, help="Missing values stay last. Ties break by ticker A to Z.")
    columns = _select_choice("Columns", COLUMN_VIEWS, key=COLUMNS_KEY, default=COLUMN_ALL)
    layout = _select_choice("Layout", LAYOUTS, key=LAYOUT_KEY, default=LAYOUT_GROUPED)
    collapsed = False
    with st.container(horizontal=True, gap="small"):
        if st.button("Expand all", key="us_stock_groups_expand_all", help="Open every stock group."):
            _set_every_group(True)
        if st.button("Collapse all", key="us_stock_groups_collapse_all", help="Close every stock group."):
            _set_every_group(False)
            collapsed = True
        st.button("Reset", key="us_stock_controls_reset", on_click=_reset_stock_controls, help="Clear search, filters, sort, and column view.")
    return {
        "query": str(st.session_state.get(QUERY_KEY) or ""),
        "era": era,
        "group": group,
        "sort_by": sort_by,
        "descending": bool(descending),
        "columns": columns,
        "layout": layout,
        "collapsed": collapsed,
    }


def _coverage_counts(symbols: Sequence[str], quotes_by_row: Sequence[Mapping[str, Any] | None], *, now: datetime) -> tuple[list[float], int, int, list[OneDay]]:
    available: list[float] = []
    pending = 0
    stale = 0
    cells: list[OneDay] = []
    for symbol, quote in zip(symbols, quotes_by_row):
        cell = one_day_cell(symbol, quote, now=now)
        cells.append(cell)
        if cell.available and cell.value is not None:
            available.append(float(cell.value))
        elif cell.price_ts is not None:
            pending += 1
        if cell.price_ts is not None and quote_freshness(symbol, cell.price_ts, now=now) == "stale":
            stale += 1
    return available, pending, stale, cells


def _member_label(
    row: Mapping[str, Any],
    symbol_bars: Sequence[Mapping[str, Any]],
    *,
    quotes_available: bool,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Row label. A stale or pending quote is labeled; it is not shown as a live price."""
    symbol = str(row.get("symbol") or "")
    if quotes_available and row.get("price") is not None:
        cell = one_day_cell(symbol, row.get("quote"), now=now or _now())
        if cell.available:
            return "{0} · {1}".format(symbol, _price(row.get("price"))), ""
        when = format_eastern(cell.price_ts) if cell.price_ts is not None else ""
        status = "stale" if cell.price_ts is not None and quote_freshness(symbol, cell.price_ts, now=now or _now()) == "stale" else "pending"
        label = "{0} · {1}".format(symbol, _price(row.get("price")))
        if when:
            label = "{0} ({1}, {2})".format(label, status, when)
        note = "Displayed price is the stored quote{0}. 1D is N/A. It is not a live price.".format(
            " from {0}".format(when) if when else ""
        )
        return label, note
    closed = latest_completed_close(symbol_bars)
    if closed is not None:
        day, close = closed
        return (
            "{0} · close {1} ({2})".format(symbol, _price(close), day.isoformat()),
            "Displayed price is the stored split-adjusted close on {0}, not a live quote.".format(day.isoformat()),
        )
    return "{0} · —".format(symbol), ""


def _ordered_heatmap(
    rows: Sequence[Mapping[str, Any]],
    *,
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    legs: dict[str, dict[str, Any]],
    moment: datetime,
    quote_failure: str | None,
    history_failure: str | None,
    quotes_available: bool,
    column_view: str,
    sort_by: str,
    descending: bool,
    heatmap_key: str,
    group_note: Mapping[str, str] | None = None,
) -> None:
    """Build one heatmap. Risk and history run only because the caller decided this view is open."""
    built: list[dict[str, Any]] = []
    with span("us_equities.stock_risk"):
        for row in rows:
            symbol = str(row["symbol"])
            symbol_bars = list(bars.get(symbol.upper()) or bars.get(symbol) or [])
            row_values, row_notes = _horizon_cells(
                quote=row.get("quote"),
                bars=symbol_bars,
                note=str(row.get("note") or ""),
                symbol=symbol,
                legs_cache=legs,
                now=moment,
                quote_failure=quote_failure,
                history_failure=history_failure,
            )
            if history_failure:
                risk_values = [None for _label in RISK_COLUMN_LABELS]
                risk_notes = [
                    "N/A: daily history read failed ({0}). This is not an empty history.".format(history_failure)
                    for _label in RISK_COLUMN_LABELS
                ]
            else:
                risk_values, risk_notes = risk_row_cells(cached_risk(symbol, symbol_bars))
            extra = (group_note or {}).get(symbol.upper()) or ""
            close_note = _member_label(row, symbol_bars, quotes_available=quotes_available, now=moment)[1]
            if extra or close_note:
                row_notes = list(row_notes)
                row_notes[0] = " · ".join(part for part in (extra, close_note, row_notes[0]) if part)
            label, _note = _member_label(row, symbol_bars, quotes_available=quotes_available, now=moment)
            built.append(
                {
                    "row": row,
                    "symbol": symbol.upper(),
                    "label": label,
                    "returns": list(row_values),
                    "return_notes": list(row_notes),
                    "risk": list(risk_values),
                    "risk_notes": list(risk_notes),
                }
            )
    values_for_sort = value_map(
        [item["symbol"] for item in built],
        [item["returns"] for item in built],
        [item["risk"] for item in built],
    )
    ordered = sort_rows(
        [{"symbol": item["symbol"], "item": item} for item in built],
        values_for_sort,
        sort_by=sort_by,
        descending=descending,
    )
    ordered_items = [entry["item"] for entry in ordered]
    combined_values = [item["returns"] + item["risk"] for item in ordered_items]
    combined_notes = [item["return_notes"] + item["risk_notes"] for item in ordered_items]
    columns, kinds, sliced_values, sliced_notes = slice_columns(
        column_view,
        _since_open_columns(STOCK_RETURN_HORIZONS),
        RISK_COLUMN_LABELS,
        RISK_COLUMN_KINDS,
        combined_values,
        combined_notes,
    )
    column_scaled_return_heatmap(
        [item["label"] for item in ordered_items],
        columns,
        sliced_values,
        notes=sliced_notes,
        column_kinds=kinds,
        key=heatmap_key,
        row_ids=[item["symbol"] for item in ordered_items],
        on_row_click=request_stock_dialog,
    )


def _render_stock_board() -> None:
    """Discovery controls stay outside the quote fragment so Refresh keeps them."""
    _stock_controls()
    st.subheader("Individual Stocks", anchor="individual-stocks")
    st.caption(ONE_DAY_POLICY_CAPTION + " " + PRICE_RETURN_CAPTION + " A ticker listed in more than one group uses the same stored quote and the same stored bars.")
    st.caption("Click a ticker or any cell in its row to open a 3-year performance chart for that stock.")
    st.caption(STOCK_RISK_CAPTION)
    _individual_stock_heatmap()


@st.fragment(run_every=YAHOO_HEATMAP_REFRESH_SECONDS)
def _individual_stock_heatmap(
    _panel: Mapping[str, Any] | None = None,
    *,
    loaded: Mapping[str, Any] | None = None,
    quotes: Sequence[Mapping[str, Any]] | None = None,
    bars: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    legs: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> None:
    """Approved stocks only. A closed group does not mount a heatmap or compute risk."""
    del bars, legs
    moment = now or _now()
    collapsed_once = bool(st.session_state.pop(_COLLAPSE_ONCE_KEY, False))
    st.caption(
        "Page draw {0}. Quote times are each observation's own timestamp in America/New_York. Closed groups show coverage only and do not read daily history.".format(
            format_eastern(moment)
        )
    )
    if loaded is None:
        loaded = load_quote_optional("dashboard_quotes_latest", default=[])
        quotes = list(loaded.get("data") or []) if loaded.get("available") else []
    quotes_available = bool(loaded.get("available"))
    quote_failure = None if quotes_available else str(loaded.get("error") or "unread")
    if not quotes_available:
        st.caption(
            "Stored quote read failed ({0}). 1D and live prices are unavailable. Completed historical returns and risk still use stored daily bars when that read succeeds.".format(
                quote_failure
            )
        )
    elif not quotes:
        st.caption("Stored quote snapshot is empty. 1D is N/A. Longer horizons use stored daily history for groups you open.")
    rows = stock_heatmap_rows(list(quotes or []))
    rows_by_group: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        rows_by_group.setdefault(str(row["group"]), []).append(row)
    if not rows_by_group:
        st.caption("No approved individual stocks are configured.")
        render_stock_dialog_if_requested(now=moment)
        return
    controls = {
        "query": str(st.session_state.get(QUERY_KEY) or ""),
        "era": str(st.session_state.get(ERA_KEY) or FILTER_ALL),
        "group": str(st.session_state.get(GROUP_FILTER_KEY) or FILTER_ALL),
        "sort_by": str(st.session_state.get(SORT_KEY) or SORT_CATALOG),
        "descending": bool(st.session_state.get(SORT_DESC_KEY, False)),
        "columns": str(st.session_state.get(COLUMNS_KEY) or COLUMN_ALL),
        "layout": str(st.session_state.get(LAYOUT_KEY) or LAYOUT_GROUPED),
        "collapsed": collapsed_once,
    }
    query = str(controls["query"])
    era_filter = str(controls["era"])
    group_filter = str(controls["group"])
    query_active = bool(normalize_query(query))
    open_map = dict(st.session_state.get(OPEN_MAP_KEY) or {})
    eras = stock_groups_by_era()
    if controls["layout"] == LAYOUT_ALL:
        _render_all_tracked(
            eras,
            rows_by_group,
            query=query,
            era_filter=era_filter,
            group_filter=group_filter,
            moment=moment,
            quote_failure=quote_failure,
            quotes_available=quotes_available,
            column_view=str(controls["columns"]),
            sort_by=str(controls["sort_by"]),
            descending=bool(controls["descending"]),
        )
        render_stock_dialog_if_requested(now=moment)
        return
    forced: list[str] = []
    drawn_symbols: list[str] = []
    visible_plan: list[tuple[str, str, str, list[Mapping[str, Any]], bool]] = []
    for era_title, _era_caption, era_groups in eras:
        if era_filter != FILTER_ALL and era_title != era_filter:
            continue
        for group, _members in era_groups:
            if group_filter != FILTER_ALL and group != group_filter:
                continue
            members = rows_by_group.get(group) or []
            shown = filter_members(members, query, group=group, era=era_title)
            if not shown:
                continue
            user_open = bool(open_map.get(group, False))
            matched = query_active and group_has_match(query, group=group, era=era_title, symbols=[str(row["symbol"]) for row in shown])
            show_heatmap = user_open or (matched and not controls["collapsed"])
            if matched and not user_open:
                forced.append(group)
            visible_plan.append((era_title, group, _era_caption, shown, show_heatmap))
            if show_heatmap:
                drawn_symbols.extend(str(row["symbol"]) for row in shown)
    st.session_state[FORCED_OPEN_KEY] = forced
    stock_bars, bars_available, bars_error = _price_bars_by_symbol(dedupe_symbols(drawn_symbols))
    history_failure = None if bars_available else (bars_error or "unread")
    if not bars_available:
        st.caption("Daily history read failed ({0}). Longer returns and risk are N/A for open groups. This is not an empty history.".format(history_failure))
    legs: dict[str, dict[str, Any]] = {}
    cells: dict[str, OneDay] = {}
    last_era = None
    for era_title, group, era_caption, shown, show_heatmap in visible_plan:
        if era_title != last_era:
            st.markdown("#### {0}".format(era_title))
            st.caption(era_caption)
            last_era = era_title
        symbols = [str(row["symbol"]) for row in shown]
        available, pending, stale, group_cells = _coverage_counts(symbols, [row.get("quote") for row in shown], now=moment)
        for symbol, cell in zip(symbols, group_cells):
            cells.setdefault(symbol, cell)
        label = group_summary_label(group, count=len(shown), available=available, pending=pending, stale=stale)
        key = group_state_key(group)
        # The map wins over a fragment widget that a full-page Refresh just reset.
        st.session_state[key] = bool(show_heatmap)
        box = st.expander(label, expanded=False, key=key, on_change=_on_group_toggle, args=(group,))
        if not box.open:
            continue
        with box:
            _ordered_heatmap(
                shown,
                bars=stock_bars,
                legs=legs,
                moment=moment,
                quote_failure=quote_failure,
                history_failure=history_failure,
                quotes_available=quotes_available,
                column_view=str(controls["columns"]),
                sort_by=str(controls["sort_by"]),
                descending=bool(controls["descending"]),
                heatmap_key="us_stock_heatmap_{0}".format(key),
            )
    st.caption(freshness_summary(cells, now=moment))
    render_stock_dialog_if_requested(now=moment)


def _render_all_tracked(
    eras: Sequence[tuple[str, str, Sequence[tuple[str, tuple[str, ...]]]]],
    rows_by_group: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    query: str,
    era_filter: str,
    group_filter: str,
    moment: datetime,
    quote_failure: str | None,
    quotes_available: bool,
    column_view: str,
    sort_by: str,
    descending: bool,
) -> None:
    """One row per ticker. Opt-in: daily history is read for the rows this view will draw."""
    chosen: dict[str, dict[str, Any]] = {}
    for era_title, _caption, era_groups in eras:
        if era_filter != FILTER_ALL and era_title != era_filter:
            continue
        for group, _members in era_groups:
            if group_filter != FILTER_ALL and group != group_filter:
                continue
            for row in rows_by_group.get(group) or []:
                if not filter_members([row], query, group=group, era=era_title):
                    continue
                symbol = str(row["symbol"]).upper()
                entry = chosen.get(symbol)
                if entry is None:
                    entry = dict(row)
                    entry["symbol"] = symbol
                    entry["groups"] = []
                    chosen[symbol] = entry
                if group not in entry["groups"]:
                    entry["groups"].append(group)
    rows = list(chosen.values())
    st.caption("{0} tracked stock{1}. Each ticker is listed once; group memberships are in the 1D note.".format(len(rows), "" if len(rows) == 1 else "s"))
    if not rows:
        st.caption("No stocks match this search.")
        return
    stock_bars, bars_available, bars_error = _price_bars_by_symbol([str(row["symbol"]) for row in rows])
    history_failure = None if bars_available else (bars_error or "unread")
    if not bars_available:
        st.caption("Daily history read failed ({0}). Longer returns and risk are N/A. This is not an empty history.".format(history_failure))
    notes = {str(row["symbol"]).upper(): "Groups: {0}.".format(", ".join(row["groups"])) for row in rows}
    _ordered_heatmap(
        rows,
        bars=stock_bars,
        legs={},
        moment=moment,
        quote_failure=quote_failure,
        history_failure=history_failure,
        quotes_available=quotes_available,
        column_view=column_view,
        sort_by=sort_by,
        descending=descending,
        heatmap_key="us_stock_heatmap_all_tracked",
        group_note=notes,
    )


@st.fragment(run_every=YAHOO_HEATMAP_REFRESH_SECONDS)
def _snapshot_row(history: Mapping[str, Any], symbols: Sequence[str], labels: Mapping[str, str]) -> None:
    """Regional cards. Each card states its own 1D basis and observation time.

    Every regional ETF is on the tracked Yahoo quote watchlist, so 1D follows
    the standard policy when a stored quote exists. A card without a usable
    quote falls back to the stored completed close-to-close 1D, labeled EOD
    with the close date, so a stale card never looks live.
    """
    prices = history.get("latest_price") or {}
    returns = history.get("returns") or {}
    moment = _now()
    loaded = load_quote_optional("dashboard_quotes_latest", default=[])
    if not loaded.get("available"):
        st.caption(
            "Stored quote read failed ({0}). 1D stays on the stored completed close and is labeled EOD. It is not a live quote.".format(
                loaded.get("error") or "unread"
            )
        )
        quote_rows: list[Mapping[str, Any]] = []
    else:
        quote_rows = list(display_quote_rows(loaded.get("data") or []))
    live_prices, one_days = _index_quote_state(quote_rows, now=moment)
    st.caption("Page draw {0}. Card observation times are each quote's own timestamp.".format(format_eastern(moment)))
    # A card needs a price: the stored close or a stored quote. A tracked ETF with
    # a quote but no market-monitor history still shows its live 1D.
    visible = [symbol for symbol in symbols if prices.get(symbol) is not None or live_prices.get(symbol) is not None]
    if not visible:
        return
    endpoint = as_day((history.get("bounds") or {}).get("latest"))
    cells: dict[str, OneDay] = {}
    for offset in range(0, len(visible), 2):
        cols = st.columns(2)
        for column, symbol in zip(cols, visible[offset : offset + 2]):
            window = returns.get(symbol) or {}
            one_day = one_days.get(symbol)
            if one_day is not None and one_day.available:
                cells[symbol] = one_day
                price = live_prices.get(symbol, prices.get(symbol))
                delta = "{0} 1D ({1}) · {2} 1M".format(
                    _signed_percent(one_day.value), one_day.basis_label.lower(), _signed_percent(window.get("1M"))
                )
                help_text = one_day_note(one_day, symbol=symbol, now=moment) + " 1M is completed close-to-close."
            else:
                price = prices.get(symbol) if prices.get(symbol) is not None else live_prices.get(symbol)
                delta = "{0} 1D (EOD) · {1} 1M".format(_signed_percent(window.get("1D")), _signed_percent(window.get("1M")))
                help_text = "1D and 1M are completed close-to-close from stored daily closes{0}. {1}".format(
                    " through {0}".format(endpoint.isoformat()) if endpoint else "", EOD_ONLY_CAPTION
                )
            column.metric(labels.get(symbol, symbol), _price(price), delta=delta, help=help_text)
    if cells:
        st.caption(freshness_summary(cells, now=moment))
    st.caption(ONE_DAY_POLICY_CAPTION)
    missing = [symbol for symbol in visible if symbol not in cells]
    if missing:
        st.caption(
            "No usable stored quote yet for {0}: 1D shows the stored completed close over the prior close{1}.".format(
                ", ".join(missing), " (last close {0})".format(endpoint.isoformat()) if endpoint else ""
            )
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
