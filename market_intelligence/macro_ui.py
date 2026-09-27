"""Chart-first Macro page. Reads PostgreSQL only.

The five groups are a layout. Catalog categories are unchanged. No provider
requests, no score, and no raw observation table.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

import streamlit as st

from market_intelligence.components.market_chart import lightweight_market_chart
from market_intelligence.components.tenor_chart import policy_rate_chart
from market_intelligence.history_range import historical_date_range, pills_layout_kwargs
from market_intelligence.macro_dashboard import (
    CHARTS,
    COINCIDENT_NOTE,
    GROUP_LABELS,
    GROUP_ORDER,
    LEADING_NOTE,
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


def _load_source(source_id: str) -> list[dict[str, Any]]:
    if "." in source_id:
        rows = load_or_stop("metric_history", source_id, limit=MACRO_HISTORY_LIMIT) or []
        return [{"as_of": row.get("as_of"), "value": row.get("value")} for row in rows]
    rows = load_or_stop("observation_history", source_id, limit=MACRO_HISTORY_LIMIT) or []
    return [{"as_of": row.get("observation_date"), "value": row.get("value")} for row in rows]


def _render_group(group: str) -> None:
    note = _GROUP_NOTES.get(group)
    if note:
        st.caption(note)
    st.caption(VINTAGE_NOTE)
    histories = {source_id: _load_source(source_id) for source_id in group_source_ids(group)}
    earliest, latest = history_window_bounds(
        [rows for source_id, rows in histories.items() if source_id != "USREC"]
    )
    start, end = historical_date_range(key="macro_{0}".format(group), earliest=earliest, latest=latest)
    bands = recession_intervals(histories.get("USREC") or [])
    if start is not None and end is not None and start <= end:
        bands = [band for band in bands if band["end"] >= start.isoformat() and band["start"] <= end.isoformat()]
    for chart in CHARTS[group]:
        _render_chart(group, chart, histories, start, end, bands)
    with st.expander("Methodology & sources"):
        for line in methodology_lines(group):
            st.markdown(line)


def _render_chart(
    group: str,
    chart: Mapping[str, Any],
    histories: Mapping[str, list[dict[str, Any]]],
    start: date | None,
    end: date | None,
    bands: list[dict[str, str]],
) -> None:
    st.subheader(chart["title"])
    if chart.get("caption"):
        st.caption(str(chart["caption"]))
    mode = None
    series_mode = None
    window_mode = None
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
    else:
        st.caption(chart_unit(chart))
    if start is None or end is None:
        return
    if start > end:
        return
    if chart["kind"] == "policy":
        _render_policy(chart, histories, start, end, bands)
        return
    lines = selected_lines(chart, mode=mode, series_mode=series_mode, window_mode=window_mode)
    series = []
    for source_id, label, scale in lines:
        points = prepare_line_points(
            histories.get(source_id) or [],
            start=start,
            end=end,
            scale_series=source_id if scale else None,
        )
        if points:
            series.append({"label": label, "points": points})
    if not series:
        st.caption("No stored observations in this range.")
        return
    reference = chart.get("reference")
    lightweight_market_chart(
        series=series,
        ranges=True,
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
