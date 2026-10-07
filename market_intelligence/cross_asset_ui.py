"""FOREX, CFTC positioning, commodities, and crypto pages.

Charts receive stored rows only. This module does not call Yahoo, CFTC, EIA,
FRED, or the database driver.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

import streamlit as st

from market_intelligence.cftc_positions import ASSET_GROUPS, CATEGORY_LABELS, POSITIONING_METHODOLOGY
from market_intelligence.commodity_analytics import COMMODITY_METHODOLOGY, COMMODITY_WINDOWS, same_date_ratio, window_returns
from market_intelligence.components.market_chart import lightweight_market_chart
from market_intelligence.components.tenor_chart import column_scaled_return_heatmap, ranked_bar_chart, return_heatmap
from market_intelligence.cross_asset_universe import COMMODITY_INSTRUMENTS, CRYPTO_INSTRUMENTS, CURRENCY_VS_USD, FX_INSTRUMENTS, FX_PAIRS, FX_WINDOWS
from market_intelligence.crypto_analytics import CRYPTO_METHODOLOGY, CRYPTO_WINDOWS, calendar_return, drawdown_series, same_date_ratio as crypto_ratio
from market_intelligence.fx_analytics import FX_METHODOLOGY, latest_window_returns
from market_intelligence.history_range import filter_history_rows, historical_date_range, series_toggles
from market_intelligence.live_1d_ui import ONE_DAY_POLICY_CAPTION, freshness_summary, one_day_cell, one_day_note, quote_price_and_ts
from market_intelligence.markets_analytics import as_day, normalize_selected_to_100
from market_intelligence.return_policy import OneDay, format_eastern, policy_for
from market_intelligence.ui import html_text, load_or_stop, load_quote_optional, page_header, return_badge, signed_percent

_CHART_HEIGHT = 420
_POSITION_METRICS = (
    ("Net / OI", "net_oi_pct"),
    ("1W change", "change_1w"),
    ("4W change", "change_4w"),
    ("1Y percentile", "percentile_1y"),
    ("3Y percentile", "percentile_3y"),
    ("Z-score", "zscore"),
)


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return "{0:.{1}f}".format(float(value), digits)
    except (TypeError, ValueError):
        return "—"


_CARD_INSTRUMENT_BY_LABEL = {"WTI": "CL", "Gold": "GC", "Copper": "HG", "Natural Gas": "NG", "BTC": "BTC", "ETH": "ETH"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stored_quotes(instrument_ids: Sequence[str]) -> dict[str, Mapping[str, Any]]:
    """Latest stored Yahoo quotes for the listed cross-asset instruments (never a provider call)."""
    loaded = load_quote_optional("cross_asset_quotes_latest", list(instrument_ids), default=[])
    if not loaded.get("available"):
        return {}
    return {str(row.get("instrument_id") or "").upper(): row for row in loaded.get("data") or []}


def _cards(cards: Sequence[Mapping[str, Any]], quotes: Mapping[str, Mapping[str, Any]] | None = None, *, now: datetime | None = None) -> None:
    """Metric cards: stored close, or the stored live quote with its policy 1D and own ET observation time."""
    shown = [card for card in cards if card.get("value") is not None or (quotes or {}).get(_CARD_INSTRUMENT_BY_LABEL.get(str(card.get("label")), ""))]
    if not shown:
        return
    moment = now or _now()
    columns = st.columns(len(shown))
    cells: dict[str, OneDay] = {}
    for column, card in zip(columns, shown):
        label = str(card.get("label") or "")
        instrument_id = _CARD_INSTRUMENT_BY_LABEL.get(label, label)
        quote = (quotes or {}).get(instrument_id)
        if quote is None:
            column.metric(label, _fmt(card.get("value")), help="Last stored daily close ({0}). No stored quote; EOD close-to-close.".format(card.get("as_of") or "—"))
            continue
        price, _ts = quote_price_and_ts(quote)
        one_day = one_day_cell(instrument_id, quote, now=moment)
        cells[instrument_id] = one_day
        note = one_day_note(one_day, symbol=instrument_id, now=moment)
        delta = "{0} {1}".format(signed_percent(one_day.value), one_day.basis_label.lower()) if one_day.available else "1D {0}".format("pending" if one_day.price_ts else "N/A")
        column.metric(label, _fmt(price if price is not None else card.get("value")), delta=delta, delta_color="normal" if one_day.available else "off", help=note)
        column.caption("Last updated: {0}".format(format_eastern(one_day.price_ts)) if one_day.price_ts else "Last updated: unavailable")
    if cells:
        st.caption(freshness_summary(cells, now=moment))


def _tuples(points: Sequence[Mapping[str, Any]], *, value_key: str = "value", date_key: str = "as_of") -> list[tuple[date, float]]:
    out: list[tuple[date, float]] = []
    for row in points:
        day = as_day(row.get(date_key))
        value = row.get(value_key)
        if day is None or value is None:
            continue
        out.append((day, float(value)))
    return out


def _clip(points: Sequence[Mapping[str, Any]], start: date, end: date, *, date_key: str = "as_of") -> list[dict[str, Any]]:
    return filter_history_rows(points, start=start, end=end, date_key=date_key)


def _through(points: Sequence[Mapping[str, Any]], end: date, *, date_key: str = "as_of") -> list[dict[str, Any]]:
    """Observations on or before ``end``, including history before the chart From date.

    Return windows and drawdowns need that lookback. The chart itself stays inside From/To.
    """
    kept: list[dict[str, Any]] = []
    for row in points:
        day = as_day(row.get(date_key))
        if day is None or day > end:
            continue
        kept.append(dict(row))
    return kept


def _line(series: Sequence[Mapping[str, Any]], *, key: str, value_format: str = "number", reference: float | None = None) -> None:
    if not series:
        st.caption("No stored observations in this range.")
        return
    lightweight_market_chart(series=series, key=key, height=_CHART_HEIGHT, ranges=True, value_format=value_format, reference_price=reference, align_union=True)


def _rebased(histories: Mapping[str, Sequence[Mapping[str, Any]]], selected: Sequence[tuple[str, str]], *, key: str) -> None:
    labels = {series_id: label for series_id, label in selected}
    missing = [label for series_id, label in selected if not histories.get(series_id)]
    if missing:
        st.caption("No stored history yet for {0}; drawn without it.".format(", ".join(missing)))
    ids = [series_id for series_id, _label in selected if histories.get(series_id)]
    if not ids:
        st.caption("No stored observations for the selected series.")
        return
    result = normalize_selected_to_100({series_id: _tuples(histories.get(series_id) or []) for series_id in ids}, ids)
    if result.get("start") is None:
        st.caption("No common date with a value for every selected series.")
        return
    series = [
        {"label": labels[series_id], "points": [{"as_of": day, "value": value} for day, value in result["series"].get(series_id) or []]}
        for series_id in ids
        if result["series"].get(series_id)
    ]
    st.caption("Index (start = 100) on {0}.".format(result["start"]))
    _line(series, key=key, value_format="index")


def _ranked(labels: Sequence[str], values: Sequence[float | None], *, key: str) -> None:
    ranked_bar_chart(list(labels), list(values), key=key, unit="percent")


def _heatmap(row_labels: Sequence[str], columns: Sequence[str], values: Sequence[Sequence[float | None]], *, key: str) -> None:
    return_heatmap(list(row_labels), list(columns), values, key=key)


FX_CARD_WINDOW_LABELS: tuple[str, ...] = ("1D", "1W", "1M")
_FX_CARD_STYLE = (
    "flex:1 1 150px;min-width:150px;max-width:220px;padding:8px 10px;border:1px solid rgba(128,128,128,0.28);"
    "border-radius:8px;box-sizing:border-box;"
)


def _fx_quote_text(value: Any, instrument_id: str) -> str:
    number = _finite_or_none(value)
    if number is None:
        return "N/A"
    if instrument_id == "DXY":
        return "{0:.2f}".format(number)
    if instrument_id in {"USDJPY"}:
        return "{0:.3f}".format(number)
    return "{0:.4f}".format(number)


def _finite_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def fx_card_with_quote(card: Mapping[str, Any], quote: Mapping[str, Any] | None, *, now: datetime) -> dict[str, Any]:
    """Card plus the stored live quote: current price, policy 1D, and the quote's own observation time.

    1W and 1M stay completed close-to-close from the stored daily closes.
    """
    merged = dict(card)
    instrument_id = str(card.get("instrument_id") or "")
    if not quote:
        merged["one_day"] = None
        return merged
    price, _ts = quote_price_and_ts(quote)
    one_day = one_day_cell(instrument_id, quote, now=now)
    merged["one_day"] = one_day
    merged["one_day_note"] = one_day_note(one_day, symbol=instrument_id, now=now)
    merged["observed_at"] = one_day.price_ts
    if price is not None:
        merged["eod_value"] = card.get("value")
        merged["value"] = price
    returns = dict(card.get("returns") or {})
    returns["1D"] = one_day.value
    merged["returns"] = returns
    return merged


def fx_card_html(card: Mapping[str, Any]) -> str:
    """One FX header card: label, latest quote, 1D/1W/1M badges, as-of or last-updated, staleness."""
    instrument_id = str(card.get("instrument_id") or "")
    label = str(card.get("label") or instrument_id)
    quote = _fx_quote_text(card.get("value"), instrument_id)
    as_of = as_day(card.get("as_of"))
    stale = bool(card.get("stale"))
    one_day = card.get("one_day")
    live = isinstance(one_day, OneDay)
    if live and one_day.price_ts is not None:
        stamp = "Last updated: {0}".format(format_eastern(one_day.price_ts))
        stamp += " · " + (one_day.basis_label if one_day.available else "1D pending")
    elif as_of is None:
        stamp = "no stored observation"
    else:
        stamp = "as of {0} · EOD close-to-close".format(as_of.isoformat())
        if stale:
            stamp += " · stale"
    returns = card.get("returns") or {}
    badges = ""
    for window in FX_CARD_WINDOW_LABELS:
        if window == "1D" and live:
            title = str(card.get("one_day_note") or "")
        elif window == "1D":
            title = "{0}: EOD close-to-close change over the last two stored daily closes; positive means {0} rose. No stored quote.".format(label)
        else:
            title = "{0}: {1} completed close-to-close change in the displayed quote; positive means {0} rose.".format(label, window)
        badges += return_badge(window, returns.get(window), title=title)
    stale_color = "color:#9b1c1c;" if stale and not live else "opacity:0.7;"
    return (
        '<div style="{style}" title="{title}">'
        '<div style="font-size:12px;font-weight:650;opacity:0.85;">{label}</div>'
        '<div style="font-size:20px;font-weight:700;line-height:1.25;">{quote}</div>'
        '<div style="display:flex;flex-wrap:wrap;margin-top:2px;">{badges}</div>'
        '<div style="font-size:11px;margin-top:4px;{stale_color}">{stamp}</div>'
        "</div>"
    ).format(
        style=_FX_CARD_STYLE,
        title=html_text("{0}: {1} in quote convention. {2}.".format(label, "newest stored Yahoo quote" if live else "latest stored Yahoo daily close", stamp)),
        label=html_text(label),
        quote=html_text(quote),
        badges=badges,
        stale_color=stale_color,
        stamp=html_text(stamp),
    )


def _fx_header(cards: Sequence[Mapping[str, Any]], quotes: Mapping[str, Mapping[str, Any]] | None = None, *, now: datetime | None = None) -> None:
    """Every tracked FX instrument from config (DXY once) in a wrapping grid."""
    if not cards:
        return
    moment = now or _now()
    merged = [fx_card_with_quote(card, (quotes or {}).get(str(card.get("instrument_id") or "")), now=moment) for card in cards]
    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:stretch;">' + "".join(fx_card_html(card) for card in merged) + "</div>",
        unsafe_allow_html=True,
    )
    cells = {str(card.get("instrument_id")): card["one_day"] for card in merged if isinstance(card.get("one_day"), OneDay)}
    if cells:
        st.caption(freshness_summary(cells, now=moment))
    st.caption(
        "Quote convention: positive means the displayed pair rose; for USD/JPY, USD/CAD, USD/CHF, and USD/CNH that is USD strength, "
        "so the heatmap's foreign-vs-USD rows carry the opposite sign. 1W/1M are completed close-to-close over 5 and 21 provider daily observations. "
        + ONE_DAY_POLICY_CAPTION
        + " Stale marks a pair whose newest daily close trails the newest FX close by more than 5 days."
    )
    st.caption("Sessions: " + " | ".join(policy_for(symbol).description for symbol in ("EURUSD", "DXY", "USDCNH")))


def render_forex_page() -> None:
    page_header("FOREX", "US dollar level and major-currency performance versus the dollar. Stored Yahoo daily bars.", fred=False)
    payload = load_or_stop("forex_context")
    quotes = _stored_quotes([spec.instrument_id for spec in FX_INSTRUMENTS])
    _fx_header(payload.get("cards") or [], quotes, now=_now())
    _forex_history_section(payload)


@st.fragment
def _forex_history_section(payload: Mapping[str, Any]) -> None:
    """Range selector and every range-driven chart. A range or toggle change reruns only this section."""
    start, end = historical_date_range(key="forex_range", earliest=as_day(payload.get("earliest")), latest=as_day(payload.get("latest")))
    if start is None or end is None or start > end:
        st.info("No Yahoo FX history is stored yet.")
        st.subheader("US Dollar Index")
        st.subheader("Major Currencies vs USD")
        st.subheader("FX Performance vs USD")
        st.subheader("FX Return Heatmap")
        st.subheader("Major FX Pairs")
        _methodology(FX_METHODOLOGY)
        return
    dxy = _clip(payload.get("dxy") or [], start, end)
    st.subheader("US Dollar Index")
    st.caption("DXY level. Not rebased. Date and level are on the chart readout.")
    _line([{"label": "DXY", "points": dxy}], key="forex_dxy")
    versus = {key: _clip(rows, start, end) for key, rows in (payload.get("versus_usd") or {}).items()}
    st.subheader("Major Currencies vs USD")
    st.caption("Rising means the foreign currency strengthened versus USD. USD/JPY, USD/CAD, USD/CHF, and USD/CNH are inverted before rebasing.")
    chosen = series_toggles(CURRENCY_VS_USD, key="forex_versus", group_label="Currencies", default=[row[0] for row in CURRENCY_VS_USD])
    selected = [(series_id, label) for series_id, label in CURRENCY_VS_USD if series_id in chosen]
    _rebased(versus, selected, key="forex_versus_chart")
    st.subheader("FX Performance vs USD")
    horizon = st.selectbox("Horizon", [label for label, _lag in FX_WINDOWS], key="forex_horizon")
    rank_labels: list[str] = []
    rank_values: list[float | None] = []
    heat_rows: list[list[float | None]] = []
    heat_labels: list[str] = []
    for series_id, label in CURRENCY_VS_USD:
        points = _tuples(_through(payload.get("versus_usd", {}).get(series_id) or [], end))
        returns = latest_window_returns(points)
        rank_labels.append(label)
        rank_values.append(returns.get(horizon))
        heat_labels.append(label)
        heat_rows.append([returns.get(name) for name, _lag in FX_WINDOWS])
    st.caption("Positive means the foreign currency strengthened versus USD over provider daily observations ending at the selected To date.")
    _ranked(rank_labels, rank_values, key="forex_rank")
    st.subheader("FX Return Heatmap")
    st.caption("Each period column has its own symmetric color scale centred on zero (limit = the column's largest absolute value). Missing cells are N/A.")
    column_scaled_return_heatmap(heat_labels, [label for label, _lag in FX_WINDOWS], heat_rows, key="forex_heat")
    st.subheader("Major FX Pairs")
    st.caption("Raw Yahoo quote convention. Several pairs are rebased to 100 so different quote scales are not drawn on one axis.")
    if payload.get("usdcnh_note"):
        st.caption(str(payload["usdcnh_note"]))
    pair_options = list(FX_PAIRS)
    pair_choice = series_toggles(pair_options, key="forex_pairs", group_label="Pairs", default=["EURUSD"])
    raw = {key: _clip(rows, start, end) for key, rows in (payload.get("pairs") or {}).items()}
    picked = [(series_id, label) for series_id, label in pair_options if series_id in pair_choice]
    if len(picked) == 1:
        series_id, label = picked[0]
        _line([{"label": label, "points": raw.get(series_id) or []}], key="forex_raw_pair")
    else:
        _rebased(raw, picked, key="forex_raw_pairs")
    _methodology(FX_METHODOLOGY)


def _latest_category_row(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=lambda row: as_day(row.get("position_date")) or date.min)


def render_positioning_page() -> None:
    page_header(
        "CFTC COT Positioning",
        "Traders in Financial Futures for financial contracts. Disaggregated COT for physical commodities.",
        fred=False,
    )
    payload = load_or_stop("positioning_context")
    as_of = payload.get("as_of")
    published = payload.get("published")
    st.caption("Positions as of {0}".format(as_of or "—"))
    st.caption("Published (scheduled) {0}. CFTC does not return a publication timestamp on these records. Friday is the regular release day after a Tuesday position date.".format(published or "—"))
    markets = list(payload.get("markets") or [])
    st.subheader("Cross-Asset Positioning")
    group = st.selectbox("Asset group", ASSET_GROUPS, key="cftc_group")
    visible = [market for market in markets if group == "All" or market.get("asset_group") == group]
    category_keys: list[str] = []
    for market in visible:
        for key in market.get("categories") or {}:
            if key not in category_keys:
                category_keys.append(key)
    if not category_keys:
        category_keys = list(CATEGORY_LABELS)
    category = st.selectbox("Trader category", category_keys, format_func=lambda key: CATEGORY_LABELS.get(key, key), key="cftc_category")
    metric_label = st.selectbox("Metric", [label for label, _field in _POSITION_METRICS], key="cftc_metric")
    metric_field = dict(_POSITION_METRICS)[metric_label]
    labels: list[str] = []
    values: list[float | None] = []
    for market in visible:
        row = _latest_category_row((market.get("categories") or {}).get(category) or [])
        if row is None:
            continue
        labels.append(str(market.get("label")))
        number = row.get(metric_field)
        if metric_field == "net_oi_pct" and number is not None:
            number = float(number) / 100.0
        elif metric_field in {"change_1w", "change_4w"} and number is not None:
            number = float(number) / 100.0
        values.append(number)
    unit = "percent" if metric_field in {"net_oi_pct", "change_1w", "change_4w"} else None
    if unit:
        ranked_bar_chart(labels, values, key="cftc_overview", unit="percent")
    else:
        ranked_bar_chart(labels, [None if value is None else float(value) for value in values], key="cftc_overview")
    st.caption("One metric at a time. A high percentile means historically high net-long positioning for that category, not a buy or sell signal.")
    market_labels = [str(market.get("label")) for market in markets]
    if not market_labels:
        st.info("No TFF or Disaggregated positions are stored yet.")
        _methodology(POSITIONING_METHODOLOGY)
        return
    market_label = st.selectbox("Market", market_labels, key="cftc_market")
    market = next(item for item in markets if item.get("label") == market_label)
    detail_keys = list((market.get("categories") or {}).keys()) or category_keys
    detail_category = st.selectbox("Trader category", detail_keys, format_func=lambda key: CATEGORY_LABELS.get(key, key), key="cftc_detail_category")
    history = list((market.get("categories") or {}).get(detail_category) or [])
    earliest = min((as_day(row.get("position_date")) for row in history if as_day(row.get("position_date"))), default=None)
    latest = max((as_day(row.get("position_date")) for row in history if as_day(row.get("position_date"))), default=None)
    start, end = historical_date_range(key="cftc_range", earliest=earliest, latest=latest)
    if start is None or end is None or start > end:
        window = []
    else:
        window = [row for row in history if (day := as_day(row.get("position_date"))) is not None and start <= day <= end]
    st.subheader("Net Positioning / Open Interest")
    st.caption("Percent of open interest. The zero line is a reference, not a signal. Readout: position date, and the line is Net/OI.")
    net_points = [{"as_of": row.get("position_date"), "value": row.get("net_oi_pct")} for row in window if row.get("net_oi_pct") is not None]
    _line([{"label": "Net / OI", "points": net_points}], key="cftc_net", value_format="percent", reference=0)
    if window and window[-1].get("net_contracts") is not None:
        st.caption("Latest net contracts {0}. Net/OI {1}%.".format(window[-1].get("net_contracts"), _fmt(window[-1].get("net_oi_pct"))))
    st.subheader("Positioning Percentile")
    st.caption("Context levels 10, 50, and 90 are not buy or sell zones.")
    percentile = [
        {"label": "1Y", "points": [{"as_of": row.get("position_date"), "value": row.get("percentile_1y")} for row in window if row.get("percentile_1y") is not None]},
        {"label": "3Y", "points": [{"as_of": row.get("position_date"), "value": row.get("percentile_3y")} for row in window if row.get("percentile_3y") is not None]},
    ]
    if window:
        first = window[0].get("position_date")
        last = window[-1].get("position_date")
        for level in (10, 50, 90):
            percentile.append({"label": str(level), "points": [{"as_of": first, "value": level}, {"as_of": last, "value": level}]})
    _line([item for item in percentile if item["points"]], key="cftc_percentile")
    st.subheader("Positioning Change")
    st.caption("Percentage points of Net/OI. This is not a raw contract change.")
    _line(
        [
            {"label": "1W change", "points": [{"as_of": row.get("position_date"), "value": row.get("change_1w")} for row in window if row.get("change_1w") is not None]},
            {"label": "4W change", "points": [{"as_of": row.get("position_date"), "value": row.get("change_4w")} for row in window if row.get("change_4w") is not None]},
        ],
        key="cftc_change",
    )
    st.subheader("Price vs Positioning")
    st.caption(str(market.get("price_note") or ""))
    price_rows = [row for row in window if row.get("price") is not None]
    if not market.get("price_proxy"):
        st.caption("Price vs Positioning is omitted for this market.")
    elif not price_rows:
        st.caption("No aligned price observations are stored for this market.")
    else:
        st.caption("Price uses the proxy named above, aligned to the position date. It is not plotted on the Net/OI axis.")
        _line([{"label": "Price proxy", "points": [{"as_of": row.get("position_date"), "value": row.get("price")} for row in price_rows]}], key="cftc_price")
    _methodology(POSITIONING_METHODOLOGY)


def _commodity_histories(payload: Mapping[str, Any], start: date, end: date) -> dict[str, list[dict[str, Any]]]:
    return {key: _clip(rows, start, end) for key, rows in (payload.get("prices") or {}).items()}


def _commodity_rank_and_heat(histories: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    order = [(row.instrument_id, row.display_name) for row in COMMODITY_INSTRUMENTS if row.instrument_id != "VIX"]
    st.subheader("Commodity Performance")
    horizon = st.selectbox("Horizon", [label for label, _lag in COMMODITY_WINDOWS], key="commodity_horizon")
    labels: list[str] = []
    values: list[float | None] = []
    heat: list[list[float | None]] = []
    heat_labels: list[str] = []
    for instrument_id, label in order:
        returns = window_returns(_tuples(histories.get(instrument_id) or []))
        labels.append(label)
        values.append(returns.get(horizon))
        heat_labels.append(label)
        heat.append([returns.get(name) for name, _lag in COMMODITY_WINDOWS])
    st.caption("Provider trading-day observations on the Yahoo futures proxy. Not a calendar-day crypto window.")
    _ranked(labels, values, key="commodity_rank")
    st.subheader("Commodity Return Heatmap")
    _heatmap(heat_labels, [label for label, _lag in COMMODITY_WINDOWS], heat, key="commodity_heat")


def render_commodities_page() -> None:
    page_header(
        "Commodities",
        "Yahoo futures proxies for prices. Overview, Energy, Metals, and Agriculture. FRED spot and EIA weekly fundamentals stay separate series.",
    )
    st.caption("Gold (LBMA daily) was removed from FRED in 2022. No substitute is invented for that FRED series. Gold here is a Yahoo futures proxy.")
    payload = load_or_stop("commodities_context")
    _cards(payload.get("cards") or [], _stored_quotes([row.instrument_id for row in COMMODITY_INSTRUMENTS if row.instrument_id != "VIX"]), now=_now())
    st.caption(ONE_DAY_POLICY_CAPTION + " Futures proxies use their exchange's electronic session (CME Globex 18:00-17:00 ET; CBOT grains 19:00-13:20 CT; ICE Brent 20:00-18:00 ET).")
    _commodity_history_section(payload)


@st.fragment
def _commodity_history_section(payload: Mapping[str, Any]) -> None:
    """Section radio, range selector, and charts. Section/range changes rerun only this body."""
    section = st.radio("Section", ["Overview", "Energy", "Metals", "Agriculture"], horizontal=True, key="commodity_section")
    start, end = historical_date_range(key="commodity_range", earliest=as_day(payload.get("earliest")), latest=as_day(payload.get("latest")))
    histories = _commodity_histories(payload, start, end) if start and end and start <= end else {}
    rank_histories = {key: _through(rows, end) for key, rows in (payload.get("prices") or {}).items()} if end is not None else {}
    labels = payload.get("labels") or {}
    if section == "Overview":
        _commodity_rank_and_heat(rank_histories)
        st.subheader("Commodity Price Comparison")
        options = [(instrument_id, str(labels.get(instrument_id) or instrument_id)) for instrument_id, _name in ((row.instrument_id, row.display_name) for row in COMMODITY_INSTRUMENTS if row.instrument_id != "VIX")]
        chosen = series_toggles(options, key="commodity_compare", group_label="Commodities", default=["CL", "GC"])
        picked = [(instrument_id, label) for instrument_id, label in options if instrument_id in chosen]
        if len(picked) == 1:
            instrument_id, label = picked[0]
            _line([{"label": label, "points": histories.get(instrument_id) or []}], key="commodity_one")
        else:
            _rebased(histories, picked, key="commodity_compare_chart")
    elif section == "Energy":
        st.subheader("WTI vs Brent")
        st.caption("Yahoo futures proxies, rebased. FRED WTI spot is the separate official series below.")
        _rebased(histories, [("CL", "WTI"), ("BZ", "Brent")], key="energy_wti_brent")
        fred = payload.get("fred") or {}
        if fred.get("DCOILWTICO"):
            st.caption("WTI spot (FRED/EIA DCOILWTICO), dollars per barrel. Not the Yahoo futures proxy.")
            _line([{"label": "WTI spot", "points": _clip(fred["DCOILWTICO"], start, end) if start and end else fred["DCOILWTICO"]}], key="energy_wti_spot")
        st.subheader("Natural Gas")
        _line([{"label": "Natural gas futures proxy", "points": histories.get("NG") or []}], key="energy_ng")
        if fred.get("DHHNGSP") and start and end:
            st.caption("Henry Hub spot (FRED/EIA DHHNGSP). Not the Yahoo futures proxy.")
            _line([{"label": "Henry Hub spot", "points": _clip(fred["DHHNGSP"], start, end)}], key="energy_hh")
        eia = payload.get("eia") or {}
        for alias, title in (
            ("crude_stocks", "U.S. Commercial Crude Inventories"),
            ("cushing_crude_stocks", "Cushing Crude Inventories"),
            ("crude_production", "U.S. Crude Oil Production"),
            ("working_gas_storage", "U.S. Natural Gas Storage"),
        ):
            st.subheader(title)
            block = eia.get(alias) or {}
            units = block.get("units") or "weekly official series"
            st.caption("{0}. Weekly observations are not interpolated to daily.".format(units))
            points = _clip(block.get("points") or [], start, end) if start and end else list(block.get("points") or [])
            _line([{"label": title, "points": points}], key="eia_{0}".format(alias))
    elif section == "Metals":
        for instrument_id, title in (("GC", "Gold"), ("SI", "Silver"), ("HG", "Copper")):
            st.subheader(title)
            st.caption("Yahoo futures proxy.")
            _line([{"label": title, "points": histories.get(instrument_id) or []}], key="metal_{0}".format(instrument_id))
        st.subheader("Gold/Copper")
        st.caption("Same-date gold proxy close divided by copper proxy close, then rebased to 100. Observational market-price ratio only.")
        ratio = same_date_ratio(_tuples(histories.get("GC") or []), _tuples(histories.get("HG") or []))
        rebased = normalize_selected_to_100({"ratio": ratio}, ["ratio"])
        if rebased.get("start") is None:
            st.caption("No same-date gold and copper observations in this range.")
        else:
            _line(
                [{"label": "Gold/Copper", "points": [{"as_of": day, "value": value} for day, value in rebased["series"]["ratio"]]}],
                key="gold_copper",
                value_format="index",
            )
        fred = (payload.get("fred") or {}).get("PCOPPUSDM") or []
        if fred:
            st.caption("Global copper (FRED/IMF PCOPPUSDM) is monthly and is not spliced into the Yahoo copper futures proxy.")
            _line([{"label": "PCOPPUSDM", "points": _clip(fred, start, end) if start and end else fred}], key="pcoppusdm", )
    else:
        st.subheader("Agriculture")
        options = [("ZC", "Corn"), ("ZW", "Wheat"), ("ZS_F", "Soybeans")]
        chosen = series_toggles(options, key="ags", group_label="Agriculture", default=["ZC", "ZW", "ZS_F"])
        picked = [item for item in options if item[0] in chosen]
        if len(picked) <= 1 and picked:
            _line([{"label": picked[0][1], "points": histories.get(picked[0][0]) or []}], key="ags_one")
        else:
            _rebased(histories, picked, key="ags_chart")
    _methodology(COMMODITY_METHODOLOGY)


def render_crypto_page() -> None:
    page_header("Crypto", "Bitcoin and Ethereum daily closes. UTC dates, including weekends.", fred=False)
    payload = load_or_stop("crypto_context")
    _cards(payload.get("cards") or [], _stored_quotes([row.instrument_id for row in CRYPTO_INSTRUMENTS]), now=_now())
    st.caption(ONE_DAY_POLICY_CAPTION + " Crypto sessions are UTC calendar days: 1D is the current price over the 00:00 UTC open of the current day.")
    _crypto_history_section(payload)


@st.fragment
def _crypto_history_section(payload: Mapping[str, Any]) -> None:
    """Range selector and charts. Range/horizon changes rerun only this body."""
    start, end = historical_date_range(key="crypto_range", earliest=as_day(payload.get("earliest")), latest=as_day(payload.get("latest")))
    prices = payload.get("prices") or {}
    if start is None or end is None or start > end:
        st.info("No Yahoo crypto history is stored yet.")
        st.subheader("Bitcoin")
        st.subheader("Ethereum")
        st.subheader("Bitcoin vs Ethereum")
        st.subheader("BTC / ETH Relative Strength")
        st.subheader("Crypto Performance")
        st.subheader("52-Week Drawdown")
        _methodology(CRYPTO_METHODOLOGY)
        return
    btc = _clip(prices.get("BTC") or [], start, end)
    eth = _clip(prices.get("ETH") or [], start, end)
    st.subheader("Bitcoin")
    _line([{"label": "BTC/USD", "points": btc}], key="crypto_btc")
    st.subheader("Ethereum")
    _line([{"label": "ETH/USD", "points": eth}], key="crypto_eth")
    st.subheader("Bitcoin vs Ethereum")
    _rebased({"BTC": btc, "ETH": eth}, [("BTC", "Bitcoin"), ("ETH", "Ethereum")], key="crypto_compare")
    st.subheader("BTC / ETH Relative Strength")
    st.caption("Rising = BTC outperforming ETH. Falling = ETH outperforming BTC.")
    ratio = crypto_ratio(_tuples(btc), _tuples(eth))
    rebased = normalize_selected_to_100({"ratio": ratio}, ["ratio"])
    if rebased.get("start") is None:
        st.caption("No same-date BTC and ETH closes in this range.")
    else:
        st.caption("Relative index (start = 100).")
        _line(
            [{"label": "BTC / ETH", "points": [{"as_of": day, "value": value} for day, value in rebased["series"]["ratio"]]}],
            key="crypto_ratio",
            value_format="index",
        )
    st.subheader("Crypto Performance")
    horizon = st.selectbox("Horizon", [label for label, _days in CRYPTO_WINDOWS], key="crypto_horizon")
    btc_points = _tuples(_through(prices.get("BTC") or [], end))
    eth_points = _tuples(_through(prices.get("ETH") or [], end))
    _ranked(
        ["BTC", "ETH"],
        [calendar_return(btc_points, days=dict(CRYPTO_WINDOWS)[horizon]), calendar_return(eth_points, days=dict(CRYPTO_WINDOWS)[horizon])],
        key="crypto_rank",
    )
    st.caption("Calendar-day windows: 7D is seven calendar days, not five equity sessions.")
    st.subheader("52-Week Drawdown")
    btc_dd = [(day, value) for day, value in drawdown_series(btc_points) if start <= day <= end]
    eth_dd = [(day, value) for day, value in drawdown_series(eth_points) if start <= day <= end]
    _line(
        [
            {"label": "BTC", "points": [{"as_of": day, "value": value * 100.0} for day, value in btc_dd]},
            {"label": "ETH", "points": [{"as_of": day, "value": value * 100.0} for day, value in eth_dd]},
        ],
        key="crypto_drawdown",
        value_format="percent",
    )
    st.caption("Trailing 365 calendar days. Missing until that window exists. Never positive.")
    _methodology(CRYPTO_METHODOLOGY)


def _methodology(text: str) -> None:
    with st.expander("Methodology & sources", expanded=False):
        st.write(text)
