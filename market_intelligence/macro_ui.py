"""Chart-first Macro page. Reads PostgreSQL only.

The five groups are a layout. Catalog categories are unchanged. No provider
requests, no score, and no raw observation table.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

import streamlit as st

from market_intelligence.components.market_chart import lightweight_market_chart
from market_intelligence.components.tenor_chart import policy_rate_chart
from market_intelligence.display_dates import DATE_INPUT_FORMAT, format_calendar_date
from market_intelligence.fed_balance_sheet import (
    MILLIONS_LABEL,
    build_statement,
    comparison_lines,
    current_dates_differ,
    history_bounds,
    statement_as_of,
    statement_html,
)
from market_intelligence.history_range import historical_date_range, pills_layout_kwargs, quick_range_bounds, series_toggles
from market_intelligence.macro_dashboard import (
    CHARTS,
    COINCIDENT_NOTE,
    GROUP_LABELS,
    GROUP_ORDER,
    SHARED_2000_GROUPS,
    materialize_derived,
    LEADING_NOTE,
    POLICY_RATES_DEFAULT_START,
    VINTAGE_NOTE,
    chart_format,
    chart_unit,
    group_source_ids,
    history_window_bounds,
    methodology_lines,
    policy_rate_frame,
    prepare_line_points,
    recession_intervals,
    selected_lines,
)
from market_intelligence.perf import span
from market_intelligence.read_models import MACRO_HISTORY_LIMIT
from market_intelligence.ui import load_or_stop, page_header

_GROUP_NOTES = {
    "leading": LEADING_NOTE,
    "coincident": COINCIDENT_NOTE,
}
_CHART_HEIGHT = 400


def render_macro_dashboard() -> None:
    """Fed, inflation, leading, coincident, and lagging charts on the existing Macro page."""
    page_header(
        "Macro & Liquidity",
        "Monetary policy, inflation, and a curated set of leading, coincident, and lagging indicators.",
    )
    label = _choice("Macro section", [GROUP_LABELS[key] for key in GROUP_ORDER], key="macro_section", default=GROUP_LABELS["fed"])
    group = next((key for key in GROUP_ORDER if GROUP_LABELS[key] == label), "fed")
    _render_group(group)


def _choice(label: str, options: list[str], *, key: str, default: str) -> str:
    if key not in st.session_state:
        st.session_state[key] = default
    selected = st.pills(label, options, selection_mode="single", key=key, **pills_layout_kwargs())
    if selected in (None, "", []):
        return default
    if isinstance(selected, list):
        return str(selected[0]) if selected else default
    return str(selected)


def _load_sources(source_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """All of a group's histories in two batched reads (series observations, derived metrics).

    One read per group instead of one per series. Rows are normalised to
    ``{"as_of", "value"}`` so chart code is unchanged.
    """
    series_ids = [source_id for source_id in source_ids if "." not in source_id]
    metric_ids = [source_id for source_id in source_ids if "." in source_id]
    histories: dict[str, list[dict[str, Any]]] = {}
    if series_ids:
        grouped = load_or_stop("observation_histories", list(series_ids), limit=MACRO_HISTORY_LIMIT) or {}
        for source_id in series_ids:
            histories[source_id] = [{"as_of": row.get("observation_date"), "value": row.get("value")} for row in grouped.get(source_id) or []]
    if metric_ids:
        grouped = load_or_stop("metric_histories", list(metric_ids), limit=MACRO_HISTORY_LIMIT) or {}
        for source_id in metric_ids:
            histories[source_id] = [{"as_of": row.get("as_of"), "value": row.get("value")} for row in grouped.get(source_id) or []]
    return histories


def _clip_bands(bands: list[dict[str, str]], start: date | None, end: date | None) -> list[dict[str, str]]:
    if start is None or end is None or start > end:
        return []
    return [band for band in bands if band["end"] >= start.isoformat() and band["start"] <= end.isoformat()]


def _chart_histories(chart: Mapping[str, Any], histories: Mapping[str, list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """Observation lists for a chart, including every toggle mode.

    M2's level and YoY live under ``series_by_mode``. Leaving them out makes the
    shared M2/NFCI window start at NFCI instead of the earlier M2 history.
    """
    source_ids: list[str] = []
    for item in chart.get("series") or ():
        source_ids.append(str(item[0]))
    for mode_rows in (chart.get("series_by_mode") or {}).values():
        for item in mode_rows:
            source_ids.append(str(item[0]))
    ordered: list[str] = []
    for source_id in source_ids:
        if source_id not in ordered:
            ordered.append(source_id)
    return [histories.get(source_id) or [] for source_id in ordered]


def _render_group(group: str) -> None:
    note = _GROUP_NOTES.get(group)
    if note:
        st.caption(note)
    st.caption(VINTAGE_NOTE)
    with span("macro.histories"):
        histories = _load_sources(list(group_source_ids(group)))
    materialize_derived(group, histories)
    bands = recession_intervals(histories.get("USREC") or [])
    if group == "fed":
        _render_fed(histories, bands)
    else:
        earliest, latest = history_window_bounds(
            [rows for source_id, rows in histories.items() if source_id != "USREC"]
        )
        default_start = POLICY_RATES_DEFAULT_START if group in SHARED_2000_GROUPS else None
        start, end = historical_date_range(
            key="macro_{0}".format(group),
            earliest=earliest,
            latest=latest,
            default_start=default_start,
        )
        window_bands = _clip_bands(bands, start, end)
        for chart in CHARTS[group]:
            _render_chart(group, chart, histories, start, end, window_bands)
    with st.expander("Methodology & sources"):
        for line in methodology_lines(group):
            st.markdown(line)


_FED_QUICK_RANGES = ("1M", "3M", "6M", "YTD", "1Y", "3Y", "Full range")


def fed_shared_observation_rows(histories: Mapping[str, list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """Policy, M2, and NFCI histories that share one From/To window.

    The balance sheet is omitted. Its comparison date is not a chart window.
    """
    policy = next(chart for chart in CHARTS["fed"] if chart["kind"] == "policy")
    rest = [chart for chart in CHARTS["fed"] if chart["kind"] not in {"policy", "balance_sheet"}]
    rows = list(_chart_histories(policy, histories))
    for chart in rest:
        rows.extend(_chart_histories(chart, histories))
    return rows


def _apply_fed_quick_range(key: str, earliest: date, latest: date) -> None:
    pending = st.session_state.pop("{0}_quick".format(key), None)
    if not isinstance(pending, str):
        return
    start, end = quick_range_bounds(pending, earliest=earliest, latest=latest)
    st.session_state["{0}_from".format(key)] = start
    st.session_state["{0}_to".format(key)] = end


def _render_fed_quick_ranges(key: str) -> None:
    columns = st.columns(len(_FED_QUICK_RANGES))
    for column, label in zip(columns, _FED_QUICK_RANGES):
        if column.button(label, key="{0}_btn_{1}".format(key, label)):
            st.session_state["{0}_quick".format(key)] = label
            st.rerun()


def _render_fed(histories: Mapping[str, list[dict[str, Any]]], bands: list[dict[str, str]]) -> None:
    """One From/To for policy rates, M2, and NFCI. Default From stays 01/01/2000."""
    policy = next(chart for chart in CHARTS["fed"] if chart["kind"] == "policy")
    sheet = next(chart for chart in CHARTS["fed"] if chart["kind"] == "balance_sheet")
    rest = [chart for chart in CHARTS["fed"] if chart["kind"] not in {"policy", "balance_sheet"}]
    st.subheader(policy["title"])
    st.caption(chart_unit(policy))
    earliest, latest = history_window_bounds(fed_shared_observation_rows(histories))
    window_key = "macro_fed_policy"
    if earliest is not None and latest is not None:
        _apply_fed_quick_range(window_key, earliest, latest)
    start, end = historical_date_range(
        key=window_key,
        earliest=earliest,
        latest=latest,
        default_start=POLICY_RATES_DEFAULT_START,
    )
    if earliest is not None and latest is not None:
        _render_fed_quick_ranges(window_key)
    if start is not None and end is not None and start <= end:
        window_bands = _clip_bands(bands, start, end)
        _render_policy(policy, histories, start, end, window_bands)
        _render_balance_sheet(sheet, histories)
        for chart in rest:
            _render_chart("fed", chart, histories, start, end, window_bands, chart_ranges=False)
    else:
        _render_balance_sheet(sheet, histories)


def _render_balance_sheet(chart: Mapping[str, Any], histories: Mapping[str, list[dict[str, Any]]]) -> None:
    st.subheader(chart["title"])
    mode_label = _choice(
        "Change Display",
        ["Percentage", "Absolute"],
        key="fed_bs_change_display",
        default="Percentage",
    )
    mode = "absolute" if mode_label == "Absolute" else "percentage"
    sheet_histories = {item[0]: histories.get(item[0]) or [] for item in chart.get("series") or ()}
    earliest, latest = history_bounds(sheet_histories)
    compare = st.checkbox("Compare to a previous date", key="fed_bs_compare")
    comparison_date = None
    if compare and earliest is not None and latest is not None:
        comparison_date = st.date_input(
            "Comparison date",
            value=None,
            min_value=earliest,
            max_value=latest,
            key="fed_bs_compare_date",
            format=DATE_INPUT_FORMAT,
            help="Current balances stay in the table. The first change column uses the latest H.4.1 observation on or before this date.",
        )
        if isinstance(comparison_date, date):
            pass
        else:
            comparison_date = None
    rows = build_statement(sheet_histories, comparison_date=comparison_date if compare else None)
    as_of = statement_as_of(rows)
    as_of_text = "As of {0}".format(format_calendar_date(as_of)) if as_of is not None else "As of —"
    st.caption("{0} · {1}".format(as_of_text, MILLIONS_LABEL))
    if current_dates_differ(rows):
        st.caption("Components with an earlier print keep that observation. Hover a name for its date.")
    for line in comparison_lines(rows, comparison_date if compare else None):
        st.caption(line)
    if as_of is None:
        st.caption("No stored H.4.1 observations for this table.")
        return
    st.markdown(
        statement_html(
            rows,
            mode=mode,
            comparing=comparison_date is not None,
            comparison_date=comparison_date if compare else None,
        ),
        unsafe_allow_html=True,
    )


def _render_chart(
    group: str,
    chart: Mapping[str, Any],
    histories: Mapping[str, list[dict[str, Any]]],
    start: date | None,
    end: date | None,
    bands: list[dict[str, str]],
    *,
    chart_ranges: bool = True,
) -> None:
    if chart["kind"] == "balance_sheet":
        _render_balance_sheet(chart, histories)
        return
    if chart.get("heading"):
        st.markdown("**{0}**".format(chart["heading"]))
    st.subheader(chart["title"])
    if chart["kind"] == "unavailable":
        st.info(str(chart.get("caption") or "This series is not available from a free structured source."))
        return
    if chart.get("caption"):
        st.caption(str(chart["caption"]))
    mode = None
    series_mode = None
    window_mode = None
    chosen = None
    if chart["kind"] == "toggle":
        mode = _choice(
            chart["title"],
            list(chart["modes"]),
            key="macro_{0}_{1}".format(group, _slug(chart["title"])),
            default=str(chart["default"]),
        )
        st.caption(chart_unit(chart, mode=mode))
    elif chart["kind"] == "momentum":
        series_mode = _choice(
            "Series",
            list(chart["series_modes"]),
            key="macro_{0}_{1}_series".format(group, _slug(chart["title"])),
            default=str(chart["default_series"]),
        )
        window_mode = _choice(
            "Window",
            list(chart["window_modes"]),
            key="macro_{0}_{1}_window".format(group, _slug(chart["title"])),
            default=str(chart["default_window"]),
        )
        st.caption(chart_unit(chart))
    elif chart["kind"] == "multi":
        hidden = {str(item) for item in chart.get("default_off") or ()}
        default = [item[0] for item in chart["series"] if item[0] not in hidden]
        chosen = series_toggles(
            [(item[0], item[1]) for item in chart["series"]],
            key="macro_{0}_{1}".format(group, _slug(chart["title"])),
            group_label=chart["title"],
            default=default if hidden else None,
        )
        st.caption(chart_unit(chart))
    else:
        st.caption(chart_unit(chart))
    if chart["kind"] == "multi" and not chosen:
        st.caption("Select at least one series.")
        return
    if start is None or end is None:
        return
    if start > end:
        return
    if chart["kind"] == "policy":
        _render_policy(chart, histories, start, end, bands)
        return
    lines = selected_lines(chart, mode=mode, series_mode=series_mode, window_mode=window_mode, chosen=chosen)
    color_index = {
        item[0]: index for index, item in enumerate(chart.get("series") or ())
    }
    series = []
    for source_id, label, scale in lines:
        points = prepare_line_points(
            histories.get(source_id) or [],
            start=start,
            end=end,
            scale_series=source_id if scale else None,
        )
        if points:
            row = {"label": label, "points": points, "color_index": color_index.get(source_id, len(series))}
            style = (chart.get("styles") or {}).get(source_id)
            axis = (chart.get("axes") or {}).get(source_id)
            if style:
                row["style"] = style
            if axis:
                row["price_scale"] = axis
            series.append(row)
    if not series:
        st.caption("No stored observations in this range.")
        return
    reference = chart.get("reference")
    lightweight_market_chart(
        series=series,
        ranges=chart_ranges,
        value_format=chart_format(chart, mode=mode),
        reference_price=None if reference is None else float(reference),
        recession_bands=bands,
        keep_missing=True,
        align_union=False,
        height=_CHART_HEIGHT,
        key="macro-{0}-{1}".format(group, _slug(chart["title"])),
    )


def _render_policy(
    chart: Mapping[str, Any],
    histories: Mapping[str, list[dict[str, Any]]],
    start: date,
    end: date,
    bands: list[dict[str, str]],
) -> None:
    frame = policy_rate_frame(
        histories.get("DFEDTARL") or [],
        histories.get("DFEDTARU") or [],
        histories.get("DFF") or [],
        histories.get("SOFR") or [],
        bands,
        start=start,
        end=end,
    )
    if not frame["categories"]:
        st.caption("No stored observations in this range.")
        return
    has_lower = any(item[1] is not None for item in frame["lower"])
    has_upper = any(item[1] is not None for item in frame["upper"])
    if not has_lower and not has_upper:
        st.caption("Fed funds target-range history unavailable in this range.")
    policy_rate_chart(frame, key="macro-fed-policy-rates")


def _slug(title: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in title).strip("-")
