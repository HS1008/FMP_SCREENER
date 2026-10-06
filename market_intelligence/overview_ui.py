"""Market Overview page: the template layout rendered from one frozen snapshot.

Sections follow ``Market_Overview_Template.xlsx`` in order. Each section has a
chevron that only collapses/expands it (state kept in ``st.session_state`` so it
survives reruns and returning to the page) and a title that only navigates to
the full page and subsection. Sector rows open the US Markets subsector heatmap
with that sector pre-selected through its canonical mapping.

The Excel export and the tables read the same ``overview_snapshot`` read model;
nothing on this page fetches, recalculates, or writes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

import pandas as pd
import streamlit as st

from market_intelligence.navigation_links import clear_origin, navigate_to
from market_intelligence.overview_export import EXCEL_MIME, EXPORT_TZ, export_filename, fill_template
from market_intelligence.overview_snapshot import SECTION_SECTORS, STALE_AFTER_DAYS
from market_intelligence.page_registry import PAGE_BY_ROUTE
from market_intelligence.ui import CACHE_TTL_SECONDS, MISSING_COLOR, NEG_COLOR, POS_COLOR, compact_as_of, load_or_stop, page_header

COLLAPSE_STATE_KEY = "overview_sections_collapsed"
EXPAND_ICON = "▾"
COLLAPSE_ICON = "▸"
MISSING_TEXT = "—"
TABLE_ROW_PX = 35
TABLE_MAX_PX = 560
RISK_COLUMNS: tuple[tuple[str, str], ...] = (("vol_ann", "Vol 3M (ann.)"), ("sharpe", "Sharpe 3M"), ("max_dd", "Max DD 3M"))

_LEVEL_FORMATS: dict[str, str] = {
    "price": "{0:,.2f}",
    "ratio": "{0:.4f}",
    "yield_pct": "{0:.2f}%",
    "bps": "{0:.0f} bps",
    "index": "{0:.2f}",
    "fx": "{0:.4f}",
}


def _collapsed_state() -> dict[str, bool]:
    state = st.session_state.get(COLLAPSE_STATE_KEY)
    if not isinstance(state, dict):
        state = {}
        st.session_state[COLLAPSE_STATE_KEY] = state
    return state


def is_collapsed(section_id: str) -> bool:
    """Sections start expanded on the first visit of a session."""
    return bool(_collapsed_state().get(section_id, False))


def toggle_collapsed(section_id: str) -> None:
    state = _collapsed_state()
    state[section_id] = not state.get(section_id, False)


def format_level(value: Any, level_kind: str) -> str:
    if value is None:
        return MISSING_TEXT
    return _LEVEL_FORMATS.get(level_kind, "{0:.2f}").format(float(value))


def format_change(value: Any, change_kind: str) -> str:
    if value is None:
        return MISSING_TEXT
    if change_kind == "bps":
        return "{0:+.0f} bps".format(float(value))
    return "{0:+.2f}%".format(float(value) * 100.0)


def format_as_of(row: Mapping[str, Any]) -> str:
    day = row.get("as_of")
    if not day:
        return MISSING_TEXT
    if row.get("status") == "stale":
        return "{0} ⚠ stale".format(day)
    return str(day)


def _change_style(text: Any) -> str:
    """Colour a pre-formatted signed cell: '—' grey, negative red, positive green."""
    value = str(text or "")
    if value in ("", MISSING_TEXT):
        return "color: {0}".format(MISSING_COLOR)
    if value.startswith("-"):
        return "color: {0}; font-weight: 600".format(NEG_COLOR)
    if value.startswith("+") and value.lstrip("+").rstrip("% bps").strip("0.") != "":
        return "color: {0}; font-weight: 600".format(POS_COLOR)
    return ""


def _format_risk(key: str, value: Any) -> str:
    if value is None:
        return MISSING_TEXT
    if key == "sharpe":
        return "{0:.2f}".format(float(value))
    return "{0:.2f}%".format(float(value) * 100.0)


def row_change_kind(section: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """Section unit, except rows that carry their own (MOVE is a percent move inside the bps Yield Curve)."""
    return str(row.get("change_kind") or section.get("change_kind") or "fraction")


def section_frame(section: Mapping[str, Any]) -> tuple[pd.DataFrame, list[str]]:
    """Display frame for one section (cells pre-formatted as text) and the signed columns to colour."""
    change_columns: Sequence[str] = section.get("change_columns") or []
    level_kind = str(section.get("level_kind") or "price")
    records: list[dict[str, Any]] = []
    for row in section.get("rows") or []:
        record: dict[str, Any] = {"Instrument": row.get("label")}
        record["Level"] = format_level(row.get("level"), str(row.get("level_kind") or level_kind))
        record["As of"] = format_as_of(row)
        kind = row_change_kind(section, row)
        for label in change_columns:
            record[label] = format_change((row.get("changes") or {}).get(label), kind)
        if section.get("risk"):
            risk = row.get("risk") or {}
            for key, title in RISK_COLUMNS:
                record[title] = _format_risk(key, risk.get(key))
        if row.get("description"):
            # Last so the level and moves stay on screen on a phone; the template's
            # description rows are secondary to the numbers.
            record["ETF" if section.get("section_id") == SECTION_SECTORS else "Description"] = row.get("description")
        records.append(record)
    frame = pd.DataFrame(records)
    signed = [label for label in change_columns if label in frame.columns]
    if section.get("risk") and "Max DD 3M" in frame.columns:
        signed.append("Max DD 3M")
    return frame, signed


def _styled(frame: pd.DataFrame, signed: Sequence[str]):
    styler = frame.style
    if signed:
        styler = styler.map(_change_style, subset=list(signed))
    return styler


def _open_label(section: Mapping[str, Any]) -> str:
    spec = PAGE_BY_ROUTE.get(str(section.get("route_id") or ""))
    return "Open {0} →".format(spec.title if spec else "full section")


def render_section(section: Mapping[str, Any]) -> None:
    section_id = str(section.get("section_id"))
    collapsed = is_collapsed(section_id)
    with st.container(horizontal=True, vertical_alignment="center"):
        if st.button(
            COLLAPSE_ICON if collapsed else EXPAND_ICON,
            key="overview_toggle_{0}".format(section_id),
            type="tertiary",
            help="Collapse or expand this section. Does not navigate.",
        ):
            toggle_collapsed(section_id)
            st.rerun()
        if st.button(
            str(section.get("title")),
            key="overview_title_{0}".format(section_id),
            type="tertiary",
            help="{0}. Opens the full page at this section.".format(_open_label(section).rstrip(" →")),
        ):
            navigate_to(str(section.get("route_id")), anchor=section.get("anchor"))
        st.caption(_section_as_of(section))
    if collapsed:
        return
    rows = section.get("rows") or []
    if not rows:
        st.info("No stored observations for this section.")
        return
    frame, signed = section_frame(section)
    key = "overview_table_{0}".format(section_id)
    height = table_height(len(rows))
    if section_id == SECTION_SECTORS:
        event = st.dataframe(
            _styled(frame, signed),
            key=key,
            hide_index=True,
            use_container_width=True,
            height=height,
            on_select="rerun",
            selection_mode="single-row",
        )
        selected = list(getattr(getattr(event, "selection", None), "rows", []) or [])
        if selected:
            _drill(rows[int(selected[0])])
        st.caption("Select a sector row, or one of the tiles below, to open that sector's subsector heatmap on US Markets.")
        render_sector_tiles(rows)
    else:
        st.dataframe(_styled(frame, signed), key=key, hide_index=True, use_container_width=True, height=height)
    notes = section.get("notes") or []
    if notes or section.get("source"):
        with st.expander("Definitions and sources", expanded=False):
            if section.get("source"):
                st.caption("Source: {0}".format(section["source"]))
            for note in notes:
                st.caption(str(note))
            stale_rows = [row.get("label") for row in rows if row.get("status") == "stale"]
            missing_rows = [row.get("label") for row in rows if row.get("status") == "missing"]
            if stale_rows:
                st.caption("Stale (older than {0} days): {1}".format(STALE_AFTER_DAYS, ", ".join(str(label) for label in stale_rows)))
            if missing_rows:
                st.caption("Missing (no stored value; never shown as zero): {0}".format(", ".join(str(label) for label in missing_rows)))


def table_height(row_count: int) -> int:
    """Show every template row without an inner scrollbar (Streamlit rows are 35px)."""
    return min(TABLE_ROW_PX * (row_count + 1) + 3, TABLE_MAX_PX)


def _drill(row: Mapping[str, Any]) -> None:
    drill = row.get("drill") or {}
    if drill.get("route_id"):
        navigate_to(str(drill["route_id"]), anchor=drill.get("anchor"), state=drill.get("state") or {})


def render_sector_tiles(rows: Sequence[Mapping[str, Any]]) -> None:
    """One small button per canonical sector; each opens the subsector heatmap for that sector."""
    with st.container(horizontal=True):
        for row in rows:
            drill = row.get("drill") or {}
            if not drill.get("route_id"):
                continue
            label = str(row.get("label"))
            if st.button(
                label,
                key="overview_sector_tile_{0}".format(row.get("key")),
                type="secondary",
                help="Open the {0} subsector heatmap on US Markets.".format(label),
            ):
                _drill(row)


def _section_as_of(section: Mapping[str, Any]) -> str:
    first, last = section.get("as_of_min"), section.get("as_of_max")
    if not first:
        return "no observations"
    if first == last:
        return "as of {0}".format(first)
    return "as of {0} … {1}".format(first, last)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _workbook_bytes(snapshot_id: str, _snapshot: dict[str, Any]) -> bytes:
    """Workbook per snapshot id. The snapshot is frozen, so the bytes are too."""
    return fill_template(_snapshot, exported_at=datetime.now(EXPORT_TZ))


def render_export_controls(snapshot: Mapping[str, Any]) -> None:
    left, right = st.columns([1.4, 4.6])
    with left:
        st.download_button(
            "Export to Excel",
            data=_workbook_bytes(str(snapshot.get("snapshot_id")), dict(snapshot)),
            file_name=export_filename(),
            mime=EXCEL_MIME,
            help="Downloads the Market Overview template filled from this snapshot. All sections export regardless of collapse state.",
            key="overview_export_button",
        )
    with right:
        st.caption(
            "Snapshot {0} generated {1} UTC · observations {2} to {3} · {4} rows, {5} missing, {6} stale. "
            "Export is generated in memory from this same snapshot.".format(
                snapshot.get("snapshot_id"),
                str(snapshot.get("generated_at") or "")[:16].replace("T", " "),
                snapshot.get("as_of_min") or MISSING_TEXT,
                snapshot.get("as_of_max") or MISSING_TEXT,
                snapshot.get("rows_total"),
                snapshot.get("rows_missing"),
                snapshot.get("rows_stale"),
            )
        )


def render_market_overview() -> None:
    clear_origin()
    snapshot = load_or_stop("overview_snapshot")
    dates = [row.get("as_of") for section in snapshot.get("sections") or [] for row in section.get("rows") or []]
    as_of, _freshness = compact_as_of([day for day in dates if day])
    errors = snapshot.get("read_errors") or {}
    warning = None
    if errors:
        warning = "Some stored reads failed and their sections are blank: {0}.".format(", ".join(sorted(errors)))
    page_header(
        "Market Overview",
        "Stored end-of-day observations in the Market Overview template layout. Click a section title to open the full page; use the chevron to collapse it.",
        fred=False,
        as_of=as_of,
        freshness="STALE" if snapshot.get("rows_stale") else None,
        warning=warning,
    )
    render_export_controls(snapshot)
    for section in snapshot.get("sections") or []:
        render_section(section)


__all__ = [
    "COLLAPSE_STATE_KEY",
    "format_change",
    "format_level",
    "is_collapsed",
    "render_market_overview",
    "render_section",
    "section_frame",
    "toggle_collapsed",
]
