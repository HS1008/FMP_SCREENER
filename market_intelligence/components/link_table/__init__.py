"""Static table whose first column is a clickable instrument name.

Streamlit's dataframe cannot attach a callback to a cell, so the Market Overview
sections that navigate per instrument render this small bidirectional component
instead. The browser only draws the pre-formatted text it is given and reports a
click as ``{"rowId": ..., "nonce": ...}``; nothing is fetched or computed there.
Row ids are stable identifiers supplied by the caller, never display text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import streamlit as st
import streamlit.components.v2 as components

_FRONTEND = Path(__file__).resolve().parent / "frontend"
_HTML = (_FRONTEND / "table.html").read_text(encoding="utf-8")
_CSS = (_FRONTEND / "table.css").read_text(encoding="utf-8")
_JS = (_FRONTEND / "table.js").read_text(encoding="utf-8")

CELL_TONES = ("plain", "positive", "negative", "missing")
_CLICK_SEEN_SUFFIX = "__row_click_seen"
ROW_HEIGHT_PX = 36
HEADER_HEIGHT_PX = 40


def link_table_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and normalise rows: ``{"id": str, "cells": [{"text": str, "tone": str}], "help": str}``."""
    out: list[dict[str, Any]] = []
    for row in rows:
        row_id = str(row.get("id") or "").strip()
        if not row_id:
            raise ValueError("link table rows need a stable id")
        cells: list[dict[str, str]] = []
        for cell in row.get("cells") or ():
            if isinstance(cell, Mapping):
                text = str(cell.get("text", ""))
                tone = str(cell.get("tone") or "plain")
            else:
                text = str(cell)
                tone = "plain"
            if tone not in CELL_TONES:
                raise ValueError("unknown cell tone: {0!r}".format(tone))
            cells.append({"text": text, "tone": tone})
        out.append({"id": row_id, "cells": cells, "help": str(row.get("help") or "")})
    return out


def _noop_trigger() -> None:
    """Declared so the ``row_click`` trigger is exposed on the mount result."""
    return None


def _component():
    return components.component("link_table", html=_HTML, css=_CSS, js=_JS, isolate_styles=True)


def link_table_click_row_id(event: Any) -> str | None:
    """Row id carried by a click trigger, or None for an empty/invalid event."""
    if not isinstance(event, Mapping):
        return None
    row_id = event.get("rowId")
    if not isinstance(row_id, str) or not row_id:
        return None
    return row_id


def link_table(
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    key: str,
    on_row_click: Callable[[str], None] | None = None,
    link_help: str = "",
) -> None:
    """Mount the table. ``on_row_click`` receives the clicked row's id once per click."""
    normalised = link_table_rows(rows)
    height = HEADER_HEIGHT_PX + ROW_HEIGHT_PX * max(len(normalised), 1) + 8
    mount_kwargs: dict[str, Any] = {}
    if on_row_click is not None:
        # Streamlit only surfaces a trigger value for events that declare a
        # callback. The click is handled below, in the script body.
        mount_kwargs["on_row_click_change"] = _noop_trigger
    result = _component()(
        data={
            "columns": [str(column) for column in columns],
            "rows": normalised,
            "link_help": str(link_help or ""),
            "clickable": on_row_click is not None,
        },
        key=key,
        width="stretch",
        height=height,
        **mount_kwargs,
    )
    if on_row_click is None:
        return
    event = getattr(result, "row_click", None)
    row_id = link_table_click_row_id(event)
    if row_id is None:
        return
    seen_key = key + _CLICK_SEEN_SUFFIX
    nonce = event.get("nonce")
    if st.session_state.get(seen_key) == nonce:
        return
    st.session_state[seen_key] = nonce
    on_row_click(row_id)


__all__ = ["CELL_TONES", "link_table", "link_table_click_row_id", "link_table_rows"]
