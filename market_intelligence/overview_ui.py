"""Market Overview page: the template layout rendered from one frozen snapshot.

Sections follow ``Market_Overview_Template.xlsx`` in order. Each section has a
chevron that only collapses/expands it (state kept in ``st.session_state`` so it
survives reruns and returning to the page) and a title that only navigates to
the full page and subsection. In the Sectors section each instrument name is a
link that opens the US Markets subsector heatmap with that sector pre-selected
through its canonical mapping (keyed by the sector ETF, not the display text).

The Excel export and the tables read the same ``overview_snapshot`` read model;
nothing on this page fetches, recalculates, or writes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import pandas as pd
import streamlit as st

from market_intelligence.components.link_table import link_table
from market_intelligence.live_1d_ui import ONE_DAY_POLICY_CAPTION
from market_intelligence.navigation_links import clear_origin, drill_to_subsector, navigate_to
from market_intelligence.overview_export import EXCEL_MIME, EXPORT_TZ, export_filename, fill_template
from market_intelligence.overview_live import EOD_LABEL, compose_overview
from market_intelligence.overview_snapshot import SECTION_SECTORS, STALE_AFTER_DAYS
from market_intelligence.page_registry import PAGE_BY_ROUTE
from market_intelligence.return_policy import BASIS_LAST_CLOSE, BASIS_SESSION_OPEN, format_eastern
from market_intelligence.ui import (
    CACHE_TTL_SECONDS,
    MISSING_COLOR,
    NEG_COLOR,
    POS_COLOR,
    compact_as_of,
    load_or_stop,
    load_quote_optional,
    page_header,
)

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


_SHORT_BASIS = {BASIS_SESSION_OPEN: "since open", BASIS_LAST_CLOSE: "since last close"}


def format_as_of(row: Mapping[str, Any]) -> str:
    """EOD rows show the session date. Rows with a stored quote show that quote's own ET observation time."""
    observed = row.get("observed_at")
    if observed:
        stamp = format_eastern(observed)
        if stamp:
            basis = _SHORT_BASIS.get(str(row.get("one_day_basis") or ""))
            label = str(row.get("one_day_label") or "")
            suffix = basis if basis and label not in ("pending", "N/A") else (label or "")
            return "{0} · {1}".format(stamp, suffix) if suffix else stamp
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
        record["As of / last updated"] = format_as_of(row)
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
        render_sector_link_table(section, frame, signed, key=key)
    else:
        st.dataframe(_styled(frame, signed), key=key, hide_index=True, use_container_width=True, height=height)
    notes = section.get("notes") or []
    if notes or section.get("source") or any(row.get("note") for row in rows):
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
            for row in rows:
                if row.get("note"):
                    st.caption("{0}: {1}".format(row.get("label"), row["note"]))
            live = [row for row in rows if row.get("observed_at")]
            if live:
                st.caption("1D basis per row (each row carries its own observation time; rows are not simultaneous):")
                for row in live:
                    st.caption("{0}: {1}".format(row.get("label"), row.get("one_day_note") or row.get("one_day_label")))
            eod_rows = [row for row in rows if row.get("one_day_basis") == "EOD_CLOSE_TO_CLOSE"]
            if eod_rows:
                st.caption("{0}: {1}".format(EOD_LABEL, ", ".join(str(row.get("label")) for row in eod_rows)))


def table_height(row_count: int) -> int:
    """Show every template row without an inner scrollbar (Streamlit rows are 35px)."""
    return min(TABLE_ROW_PX * (row_count + 1) + 3, TABLE_MAX_PX)


def _drill(row: Mapping[str, Any]) -> None:
    drill = row.get("drill") or {}
    if drill.get("route_id"):
        navigate_to(str(drill["route_id"]), anchor=drill.get("anchor"), state=drill.get("state") or {})


def _cell_tone(text: Any, *, signed: bool) -> str:
    value = str(text or "")
    if value in ("", MISSING_TEXT):
        return "missing"
    if not signed:
        return "plain"
    if value.startswith("-"):
        return "negative"
    if value.startswith("+") and value.lstrip("+").rstrip("% bps").strip("0.") != "":
        return "positive"
    return "plain"


def sector_link_rows(section: Mapping[str, Any], frame: pd.DataFrame, signed: Sequence[str]) -> list[dict[str, Any]]:
    """Link-table rows for the sectors section: stable ETF key, pre-formatted cells, tones.

    Only rows that carry a drill target are clickable; the id is the row key
    (sector ETF symbol), never the displayed name.
    """
    out: list[dict[str, Any]] = []
    rows = list(section.get("rows") or [])
    signed_set = set(signed)
    for index, row in enumerate(rows):
        if index >= len(frame):
            break
        record = frame.iloc[index]
        cells = [{"text": str(record[column]), "tone": _cell_tone(record[column], signed=column in signed_set)} for column in frame.columns]
        if cells:
            cells[0] = {"text": str(record[frame.columns[0]]), "tone": "plain"}
        out.append(
            {
                "id": str(row.get("key") or row.get("label")),
                "cells": cells,
                "help": "Open the {0} subsector heatmap on US Markets.".format(row.get("label")),
            }
        )
    return out


def render_sector_link_table(section: Mapping[str, Any], frame: pd.DataFrame, signed: Sequence[str], *, key: str) -> None:
    """Sectors table whose instrument names open that sector's subsector heatmap."""
    rows = list(section.get("rows") or [])
    by_key = {str(row.get("key") or row.get("label")): row for row in rows}

    def open_sector(row_id: str) -> None:
        # Same resolver as the US Markets sector-row click; the snapshot's own
        # drill payload is the fallback for a key the resolver does not know.
        if drill_to_subsector(row_id):
            return
        row = by_key.get(row_id)
        if row is not None:
            _drill(row)

    link_table(
        [str(column) for column in frame.columns],
        sector_link_rows(section, frame, signed),
        key=key,
        on_row_click=open_sector,
        link_help="Opens this sector in the US Markets subsector heatmap.",
    )
    st.caption("Click a sector name to open that sector's subsector heatmap on US Markets.")


def _section_as_of(section: Mapping[str, Any]) -> str:
    first, last = section.get("as_of_min"), section.get("as_of_max")
    live = sum(1 for row in section.get("rows") or [] if row.get("observed_at"))
    if not first:
        if live:
            return "no stored EOD observations · {0} row{1} from stored quotes".format(live, "" if live == 1 else "s")
        return "no observations"
    text = "as of {0}".format(first) if first == last else "as of {0} … {1}".format(first, last)
    if live:
        text += " · {0} live 1D row{1}".format(live, "" if live == 1 else "s")
    return text


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _workbook_bytes(snapshot_id: str, _snapshot: dict[str, Any]) -> bytes:
    """Workbook per composed snapshot id. The composed snapshot is frozen, so the bytes are too."""
    return fill_template(_snapshot, exported_at=datetime.now(EXPORT_TZ))


def render_export_controls(snapshot: Mapping[str, Any]) -> None:
    left, right = st.columns([1.4, 4.6])
    composed = dict(snapshot)
    snapshot_id = str(snapshot.get("snapshot_id"))
    with left:
        # Deferred data: the workbook is built only when the button is clicked,
        # from the same composed snapshot the tables above were rendered from.
        st.download_button(
            "Export to Excel",
            data=lambda: _workbook_bytes(snapshot_id, composed),
            file_name=export_filename(),
            mime=EXCEL_MIME,
            help="Builds the Market Overview template from this exact snapshot when clicked. All sections export regardless of collapse state.",
            key="overview_export_button",
            on_click="ignore",
        )
    with right:
        published = snapshot.get("published_at")
        prepared = "prepared by the server job {0}".format(format_eastern(published)) if published else "composed on this request (no published snapshot yet)"
        st.caption(
            "EOD snapshot {0} {1} · observations {2} to {3} · {4} rows, {5} missing, {6} stale · {7} rows carry a stored-quote 1D. "
            "Export is generated on click from this same composed snapshot ({8}).".format(
                snapshot.get("eod_snapshot_id") or snapshot.get("snapshot_id"),
                prepared,
                snapshot.get("as_of_min") or MISSING_TEXT,
                snapshot.get("as_of_max") or MISSING_TEXT,
                snapshot.get("rows_total"),
                snapshot.get("rows_missing"),
                snapshot.get("rows_stale"),
                snapshot.get("live_rows") or 0,
                snapshot_id,
            )
        )


def _composed_snapshot() -> dict[str, Any]:
    """Published EOD snapshot (one indexed read) plus the stored-quote 1D overlay."""
    base = load_or_stop("overview_snapshot_published")
    equity = load_quote_optional("dashboard_quotes_latest", default=[])
    cross = load_quote_optional("cross_asset_quotes_latest", default=[])
    return compose_overview(
        base,
        equity_quotes=list(equity.get("data") or []) if equity.get("available") else [],
        cross_asset_quotes=list(cross.get("data") or []) if cross.get("available") else [],
        now=datetime.now(timezone.utc),
    )


def render_market_overview() -> None:
    clear_origin()
    snapshot = _composed_snapshot()
    dates = [row.get("as_of") for section in snapshot.get("sections") or [] for row in section.get("rows") or []]
    as_of, _freshness = compact_as_of([day for day in dates if day])
    errors = snapshot.get("read_errors") or {}
    warning = None
    if errors:
        warning = "Some stored reads failed and their sections are blank: {0}.".format(", ".join(sorted(errors)))
    page_header(
        "Market Overview",
        "Stored end-of-day observations in the Market Overview template layout, with a stored-quote 1D where a live quote exists. Click a section title to open the full page; use the chevron to collapse it.",
        fred=True,  # yields and credit OAS come from FRED; its terms require the attribution
        as_of=as_of,
        freshness="STALE" if snapshot.get("rows_stale") else None,
        warning=warning,
    )
    render_export_controls(snapshot)
    st.caption(ONE_DAY_POLICY_CAPTION + " Rows without a stored quote are {0}. Each live row shows its own observation time in America/New_York.".format(EOD_LABEL))
    if snapshot.get("rows_total") and snapshot.get("rows_missing") == snapshot.get("rows_total"):
        st.info("No stored observations yet for any Market Overview instrument. Rows fill in after the ingestion jobs run; nothing is fabricated in the meantime.")
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
