"""Click-through stock performance dialog for the US Equities heatmaps.

A heatmap row click stores the canonical ticker in session state; the page then
opens one :func:`st.dialog` that reads that ticker's stored daily closes from
PostgreSQL (one symbol, one window; nothing is preloaded for the universe) and
draws them with the shared Lightweight Charts component.

Controls inside the dialog rerun only the dialog. Dismissing it clears the
session-state request so a persisted component click cannot reopen it.
Streamlit never calls Yahoo here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

import streamlit as st

from market_intelligence.components.market_chart import CHART_RANGE_KINDS, lightweight_market_chart
from market_intelligence.live_1d_ui import observation_label, one_day_cell, one_day_note, quote_price_and_ts
from market_intelligence.price_returns import PRICE_RETURN_BASIS, history_floor
from market_intelligence.return_policy import bar_is_provisional, parse_day
from market_intelligence.taxonomy import constituent_company_name
from market_intelligence.ui import load_optional, load_quote_optional, return_badge

STOCK_DIALOG_KEY = "mi_stock_dialog_symbol"
RANGE_KEY = "mi_stock_dialog_range"
MODE_KEY = "mi_stock_dialog_mode"
DEFAULT_RANGE = "3Y"
MODE_CUMULATIVE = "Cumulative %"
MODE_PRICE = "Price"
RANGE_OPTIONS: tuple[str, ...] = CHART_RANGE_KINDS
SERIES_CAPTION = (
    "Split-adjusted daily closes from stored Yahoo history; cash dividends are excluded, so this is a price return, "
    "not a total return. Completed sessions only: the in-progress session is not plotted."
)


def request_stock_dialog(symbol: str) -> None:
    """Heatmap click handler. Stores the canonical ticker; the page opens the dialog on this run."""
    ticker = str(symbol or "").upper().strip()
    if ticker:
        st.session_state[STOCK_DIALOG_KEY] = ticker


def clear_stock_dialog() -> None:
    st.session_state.pop(STOCK_DIALOG_KEY, None)


def requested_stock() -> str | None:
    value = st.session_state.get(STOCK_DIALOG_KEY)
    return str(value) if value else None


def range_start(kind: str, end: date) -> date | None:
    """Calendar start for a range button; ``None`` means the full stored window."""
    if kind == "1M":
        return end - timedelta(days=30)
    if kind == "3M":
        return end - timedelta(days=91)
    if kind == "6M":
        return end - timedelta(days=182)
    if kind == "YTD":
        return date(end.year, 1, 1)
    if kind == "1Y":
        return end.replace(year=end.year - 1) if not (end.month == 2 and end.day == 29) else end.replace(year=end.year - 1, day=28)
    if kind == "3Y":
        return end.replace(year=end.year - 3) if not (end.month == 2 and end.day == 29) else end.replace(year=end.year - 3, day=28)
    return None


def completed_closes(
    bars: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    now: datetime,
) -> list[tuple[date, float]]:
    """Ascending ``(session, close)`` on the split-adjusted basis, excluding provisional bars."""
    by_day: dict[date, float] = {}
    for row in bars:
        day = parse_day(row.get("bar_date"))
        if day is None:
            continue
        basis = str(row.get("adjustment_basis") or row.get("basis") or PRICE_RETURN_BASIS)
        if basis != PRICE_RETURN_BASIS:
            continue
        quality = str(row.get("bar_quality") or row.get("quality") or "")
        if quality == "PROVISIONAL" or bar_is_provisional(symbol, day, now=now):
            continue
        raw = row.get("close_price", row.get("close"))
        try:
            close = float(raw) if raw is not None and not isinstance(raw, bool) else None
        except (TypeError, ValueError):
            close = None
        if close is None or close <= 0:
            continue
        by_day[day] = close
    return sorted(by_day.items())


def window_series(
    closes: Sequence[tuple[date, float]],
    *,
    kind: str,
    mode: str,
) -> dict[str, Any]:
    """Slice to the range and express as cumulative % (rebased to the first close in the range) or price."""
    if not closes:
        return {"points": [], "start": None, "end": None, "base": None, "requested_start": None}
    end = closes[-1][0]
    start = range_start(kind, end)
    chosen = [(day, close) for day, close in closes if start is None or day >= start]
    if not chosen:
        chosen = list(closes[-1:])
    base = chosen[0][1]
    points: list[dict[str, Any]] = []
    for day, close in chosen:
        value = (close / base - 1.0) * 100.0 if mode == MODE_CUMULATIVE else close
        points.append({"time": day.isoformat(), "value": value})
    return {"points": points, "start": chosen[0][0], "end": end, "base": base, "requested_start": start}


def _dialog_title(symbol: str) -> str:
    name = constituent_company_name(symbol)
    return symbol if name == symbol else "{0} · {1}".format(symbol, name)


def _single(label: str, options: Sequence[str], *, key: str, default: str) -> str:
    if key not in st.session_state or st.session_state.get(key) not in options:
        st.session_state[key] = default
    selected = st.pills(label, list(options), selection_mode="single", key=key)
    if selected in (None, "", []):
        return default
    return str(selected[0]) if isinstance(selected, list) else str(selected)


def _latest_quote(symbol: str) -> Mapping[str, Any] | None:
    loaded = load_quote_optional("dashboard_quotes_latest", default=[])
    if not loaded.get("available"):
        return None
    for row in loaded.get("data") or []:
        provenance = row.get("provenance") or {}
        candidates = (
            row.get("display_name"),
            provenance.get("symbol") if isinstance(provenance, Mapping) else None,
            row.get("symbol"),
            row.get("instrument_id"),
        )
        if any(str(item or "").upper().strip() == symbol for item in candidates):
            return row
    return None


def render_stock_dialog_body(symbol: str, *, now: datetime | None = None) -> None:
    """Dialog contents. Separated from the decorator so tests can call it directly."""
    moment = now or datetime.now(timezone.utc)
    today = moment.astimezone(timezone.utc).date()
    floor = history_floor(today)
    loaded = load_optional("stock_history_bars", symbol, since=floor, default={})
    data = loaded.get("data") or {}
    if not loaded.get("available"):
        st.warning("Stored price history is unavailable ({0}).".format(loaded.get("error") or "unread"))
        return
    closes = completed_closes(list(data.get("bars") or []), symbol=symbol, now=moment)
    earliest_stored = parse_day(data.get("earliest_stored"))
    controls = st.columns([3, 2])
    with controls[0]:
        kind = _single("Range", RANGE_OPTIONS, key=RANGE_KEY, default=DEFAULT_RANGE)
    with controls[1]:
        mode = _single("Show", (MODE_CUMULATIVE, MODE_PRICE), key=MODE_KEY, default=MODE_CUMULATIVE)
    quote = _latest_quote(symbol)
    price, price_ts = quote_price_and_ts(quote)
    one_day = one_day_cell(symbol, quote, now=moment)
    quote_bits: list[str] = []
    if price is not None:
        quote_bits.append("Latest quote {0:.2f}".format(price))
        quote_bits.append(observation_label(price_ts))
    else:
        quote_bits.append("Latest quote unavailable")
    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">'
        "<span>{0}</span>{1}</div>".format(
            " · ".join(quote_bits),
            return_badge("1D", one_day.value, title=one_day_note(one_day, symbol=symbol, now=moment)) if quote is not None else "",
        ),
        unsafe_allow_html=True,
    )
    if not closes:
        st.info("No completed daily closes are stored for {0} yet. Nothing is plotted until the collector stores history.".format(symbol))
        return
    series = window_series(closes, kind=kind, mode=mode)
    label = "Cumulative return" if mode == MODE_CUMULATIVE else "Close"
    lightweight_market_chart(
        series["points"],
        series_label=label,
        height=380,
        key="stock_dialog_chart_{0}".format(symbol),
        value_format="percent" if mode == MODE_CUMULATIVE else "number",
        reference_price=series["base"] if mode == MODE_PRICE else None,
    )
    start = series["start"]
    end = series["end"]
    requested = series["requested_start"]
    if start is not None and end is not None:
        if mode == MODE_CUMULATIVE:
            st.caption(
                "Cumulative % is each completed close divided by the first completed close in the range ({0}, {1:.2f}) minus one. "
                "Last completed close {2}.".format(start.isoformat(), series["base"], end.isoformat())
            )
        else:
            st.caption("Split-adjusted closes from {0} to {1}; the dashed line is the first close in the range.".format(start.isoformat(), end.isoformat()))
    if earliest_stored is not None and requested is not None and earliest_stored > requested:
        st.caption(
            "Available since {0}: stored history begins there, so this range shows the actual sessions from that date. "
            "Earlier sessions are either before the listing or not yet stored. No prices are invented.".format(earliest_stored.isoformat())
        )
    st.caption(SERIES_CAPTION)


def render_stock_dialog_if_requested(*, now: datetime | None = None) -> bool:
    """Open the dialog for the requested ticker. Returns True when a dialog was shown."""
    symbol = requested_stock()
    if not symbol:
        return False

    @st.dialog(_dialog_title(symbol), width="large", on_dismiss=clear_stock_dialog)
    def _show(ticker: str) -> None:
        render_stock_dialog_body(ticker, now=now)

    _show(symbol)
    return True


def stock_row_click_handler() -> Callable[[str], None]:
    return request_stock_dialog


__all__ = [
    "DEFAULT_RANGE",
    "MODE_CUMULATIVE",
    "MODE_PRICE",
    "RANGE_OPTIONS",
    "STOCK_DIALOG_KEY",
    "clear_stock_dialog",
    "completed_closes",
    "range_start",
    "render_stock_dialog_body",
    "render_stock_dialog_if_requested",
    "request_stock_dialog",
    "requested_stock",
    "window_series",
]
