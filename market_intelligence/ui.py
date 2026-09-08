"""Shared Streamlit helpers for DB-only Market Intelligence pages.

Pages import only this module (plus pandas/plotly/streamlit). No provider, filesystem
precomputed, QC or writer-DB imports. Data comes from the read-only role through
:mod:`market_intelligence.readonly_db`; ``st.cache_data`` caches DB reads only.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Callable

import pandas as pd
import streamlit as st

from market_intelligence.catalog import FRED_ATTRIBUTION
from market_intelligence.readonly_db import ReadOnlyUnavailable, readonly_connection

CACHE_TTL_SECONDS = 300
NEG_COLOR = "#c0392b"
POS_COLOR = "#1e8449"
LOW_POS_COLOR = "#d4ac0d"
MISSING_COLOR = "#7f8c8d"


def _run_readonly(fn: Callable[[Any], Any], *args: Any) -> Any:
    with readonly_connection() as conn:
        return fn(conn, *args)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def cached_read(fn_name: str, *args: Any) -> Any:
    """Cache a read-model call by function name (module attribute) and args."""
    from market_intelligence import read_models

    fn = getattr(read_models, fn_name)
    return _run_readonly(fn, *args)


def clear_read_cache() -> None:
    cached_read.clear()


def page_header(title: str, caption: str, *, fred: bool = True) -> None:
    st.set_page_config(page_title=title, page_icon="📊", layout="wide")
    st.title(title)
    st.caption(caption)
    cols = st.columns([6, 1])
    with cols[1]:
        if st.button("Refresh DB cache", help="Clears this page's cached PostgreSQL reads. No provider calls."):
            clear_read_cache()
            st.rerun()
    if fred:
        st.caption(FRED_ATTRIBUTION)


def unavailable(exc: ReadOnlyUnavailable) -> None:
    st.error("Market Intelligence data is unavailable ({0}).".format(exc.reason))
    st.markdown(
        "Set `DATABASE_READONLY_URL` to the `mi_readonly` role (see `db/roles/market_intelligence_readonly.sql` "
        "and `docs/MARKET_INTELLIGENCE.md`). This page never falls back to writer credentials, "
        "never calls providers, and never runs ingestion."
    )
    st.stop()


def load_or_stop(fn_name: str, *args: Any) -> Any:
    try:
        return cached_read(fn_name, *args)
    except ReadOnlyUnavailable as exc:
        unavailable(exc)
    except Exception as exc:  # noqa: BLE001 - sanitized
        st.error("Query failed ({0}). Check Data Health for migration status.".format(exc.__class__.__name__))
        st.stop()


# ---- formatting -------------------------------------------------------------------------

def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return isinstance(value, float) and (math.isnan(value) or math.isinf(value))
    except TypeError:
        return False


def fmt(value: Any, units: str | None, *, digits: int = 2) -> str:
    if _is_missing(value):
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    u = (units or "").lower()
    if u == "bps":
        return "{0:+.0f} bps".format(v)
    if u == "pct":
        return "{0:.{d}f}%".format(v, d=digits)
    if u == "pp":
        return "{0:+.2f} pp".format(v)
    if u == "pctile":
        return "{0:.0f}th".format(v)
    if u == "fraction":
        return "{0:+.2f}%".format(v * 100)
    if abs(v) >= 1e6:
        return "{0:,.0f}".format(v)
    if abs(v) >= 1000:
        return "{0:,.1f}".format(v)
    return "{0:.{d}f}".format(v, d=digits)


def fmt_signed(value: Any, units: str | None) -> str:
    if _is_missing(value):
        return "—"
    v = float(value)
    u = (units or "").lower()
    if u == "bps":
        return "{0:+.0f} bps".format(v)
    if u == "pct":
        return "{0:+.2f}%".format(v)
    if u == "fraction":
        return "{0:+.2f}%".format(v * 100)
    return "{0:+,.2f}".format(v)


def freshness_chip(status: str | None) -> str:
    return {"FRESH": "🟢 Fresh", "STALE": "🟠 Stale", "UNKNOWN": "⚪ Unknown"}.get(str(status or "").upper(), "⚪ {0}".format(status or "n/a"))


def transport_chip(status: str | None) -> str:
    return {"OK": "🟢 OK", "FAILED": "🔴 Failed", "PARTIAL": "🟠 Partial", "METADATA_REJECTED": "🔴 Metadata rejected", "SKIPPED": "⚪ Skipped", "NEVER_ATTEMPTED": "⚪ Never"}.get(str(status or "").upper(), str(status or "n/a"))


def age_text(iso: str | None, *, now: datetime | None = None) -> str:
    if not iso:
        return "—"
    try:
        ts = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    delta = now - ts
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "{0:.0f} min ago".format(hours * 60)
    if hours < 48:
        return "{0:.1f} h ago".format(hours)
    return "{0:.0f} d ago".format(hours / 24)


# ---- heatmap ---------------------------------------------------------------------------------

def heat_color(value: Any, *, low_positive_threshold: float = 0.01) -> str:
    """Negative red, positive green, low positive yellow, missing grey (legend on page)."""
    if _is_missing(value):
        return "background-color: {0}; color: white".format(MISSING_COLOR)
    v = float(value)
    if v < 0:
        return "background-color: {0}; color: white".format(NEG_COLOR)
    if v < low_positive_threshold:
        return "background-color: {0}; color: black".format(LOW_POS_COLOR)
    return "background-color: {0}; color: white".format(POS_COLOR)


def heat_marker(value: Any, *, low_positive_threshold: float = 0.01) -> str:
    """Text fallback for the colour scale (same legend): 🟥 negative, 🟨 low positive, 🟩 positive, ⬜ missing."""
    if _is_missing(value):
        return "⬜"
    v = float(value)
    if v < 0:
        return "🟥"
    if v < low_positive_threshold:
        return "🟨"
    return "🟩"


def styled_heatmap(frame: pd.DataFrame, value_columns: list[str], *, low_positive_threshold: float = 0.01, as_percent: bool = True):
    display = frame.copy()
    try:
        styler = display.style
    except (AttributeError, ImportError):
        # pandas Styler needs jinja2 (requirements.txt pins it); degrade to marker + number text so
        # missing cells still read as "—" and never as zero.
        for col in value_columns:
            display[col] = [
                "{0} {1}".format(heat_marker(v, low_positive_threshold=low_positive_threshold), "—" if _is_missing(v) else ("{0:+.2f}%".format(v * 100) if as_percent else "{0:+.3f}".format(v)))
                for v in display[col]
            ]
        return display
    for col in value_columns:
        styler = styler.map(lambda v: heat_color(v, low_positive_threshold=low_positive_threshold), subset=[col])
    fmt_map = {col: (lambda v: "—" if _is_missing(v) else ("{0:+.2f}%".format(v * 100) if as_percent else "{0:+.3f}".format(v))) for col in value_columns}
    styler = styler.format(fmt_map, na_rep="—")
    return styler


def heatmap_legend(low_positive_threshold: float = 0.01) -> None:
    st.caption(
        "Legend: 🟥 negative · 🟨 low positive (0 to {0:.0%}) · 🟩 positive · ⬜ missing (shown as — , never as zero). "
        "Values are shown numerically in every cell.".format(low_positive_threshold)
    )


def history_chart(rows: list[dict[str, Any]], *, x: str, y: str, title: str, units: str | None) -> None:
    if not rows:
        st.info("No history stored for this series.")
        return
    frame = pd.DataFrame(rows)
    frame[x] = pd.to_datetime(frame[x], errors="coerce")
    frame[y] = pd.to_numeric(frame[y], errors="coerce")
    frame = frame.dropna(subset=[x])
    try:
        import plotly.express as px

        fig = px.line(frame, x=x, y=y, title=title)
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=40, b=10), yaxis_title=units or "")
        st.plotly_chart(fig, use_container_width=True)
    except ImportError:  # pragma: no cover
        st.line_chart(frame.set_index(x)[y])


def as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


__all__ = [
    "CACHE_TTL_SECONDS",
    "age_text",
    "as_date",
    "cached_read",
    "clear_read_cache",
    "fmt",
    "fmt_signed",
    "freshness_chip",
    "heat_color",
    "heat_marker",
    "heatmap_legend",
    "history_chart",
    "load_or_stop",
    "page_header",
    "styled_heatmap",
    "transport_chip",
    "unavailable",
]
