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


def _run_readonly(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    with readonly_connection() as conn:
        return fn(conn, *args, **kwargs)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def cached_read(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """Cache a read-model call by function name (module attribute) and args."""
    from market_intelligence import read_models

    fn = getattr(read_models, fn_name)
    return _run_readonly(fn, *args, **kwargs)


def clear_read_cache() -> None:
    cached_read.clear()


def _safe_page_config(title: str) -> None:
    try:
        st.set_page_config(page_title=title, page_icon="📊", layout="wide")
    except Exception:  # noqa: BLE001 - already configured by the app shell
        pass


def page_header(title: str, caption: str | None = None, *, fred: bool = True, as_of: str | None = None, freshness: str | None = None, warning: str | None = None) -> None:
    _safe_page_config(title)
    head = st.columns([5.2, 1.6, 1.0])
    with head[0]:
        st.title(title)
        if caption:
            st.caption(caption)
    with head[1]:
        if as_of or freshness:
            st.caption("As of {0}".format(as_of or "—"))
            if freshness:
                st.caption(freshness_chip(freshness))
    with head[2]:
        if st.button("Refresh", help="Reloads cached database reads only. No provider calls."):
            clear_read_cache()
            st.rerun()
    if warning:
        st.warning(warning)
    if fred:
        st.caption(FRED_ATTRIBUTION)


def compact_as_of(dates: list[Any], *, freshness: str | None = None) -> tuple[str | None, str | None]:
    parsed = [as_date(value) for value in dates]
    present = sorted({value.isoformat() for value in parsed if value is not None})
    if not present:
        return None, freshness
    if len(present) == 1:
        return present[0], freshness
    return "{0} … {1}".format(present[0], present[-1]), freshness


def implied_prior_yield(yield_pct: Any, change_bps: Any) -> float | None:
    if _is_missing(yield_pct) or _is_missing(change_bps):
        return None
    return float(yield_pct) - (float(change_bps) / 100.0)


def unavailable(exc: ReadOnlyUnavailable) -> None:
    st.error("Market Intelligence data is unavailable ({0}).".format(exc.reason))
    st.markdown(
        "Set `DATABASE_READONLY_URL` to the `mi_readonly` role (see `db/roles/market_intelligence_readonly.sql` "
        "and `docs/MARKET_INTELLIGENCE.md`). This page never falls back to writer credentials, "
        "never calls providers, and never runs ingestion."
    )
    st.stop()


def load_or_stop(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    try:
        return cached_read(fn_name, *args, **kwargs)
    except ReadOnlyUnavailable as exc:
        unavailable(exc)
    except Exception as exc:  # noqa: BLE001 - sanitized
        st.error("Query failed ({0}). Check Data Health for migration status.".format(exc.__class__.__name__))
        st.stop()


def load_optional(fn_name: str, *args: Any, default: Any = None, **kwargs: Any) -> dict[str, Any]:
    """Load an optional read model without stopping the page.

    Identity/security failures (``ReadOnlyUnavailable``) still fail closed via
    :func:`unavailable`. Query/schema failures return ``available=False`` so
    unrelated categories can render.
    """
    empty = {} if default is None else default
    try:
        return {"data": cached_read(fn_name, *args, **kwargs), "available": True, "error": None}
    except ReadOnlyUnavailable as exc:
        unavailable(exc)
        return {"data": empty, "available": False, "error": "CONFIGURATION_REQUIRED"}
    except Exception as exc:  # noqa: BLE001 - contained empty state
        return {"data": empty, "available": False, "error": exc.__class__.__name__}


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
    key = str(status or "").upper()
    surface = {
        "CURRENT": "🟢 Current",
        "DELAYED": "🟡 Delayed",
        "STALE": "🟠 Stale",
        "UNAVAILABLE": "⚪ Unavailable",
        "BLOCKED": "🔴 Blocked",
        "FRESH": "🟢 Current",
        "LATEST_AVAILABLE": "🟢 Current",
        "AWAITING_RELEASE": "🟢 Current",
        "CURRENT_TO_SOURCE": "🟢 Current to source",
        "HEALTHY_CURRENT": "🟢 Current",
        "HEALTHY_PUBLICATION_LAG": "🟢 Publication lag",
        "ON_DEMAND": "🟢 On demand",
        "STALE_INGESTION": "🟠 Stale ingestion",
        "STALE_UPSTREAM": "🟡 Upstream lag",
        "INGESTION_OVERDUE": "🟡 Delayed",
        "INVALID_FUTURE": "🟡 Delayed",
        "MISSING": "⚪ Unavailable",
        "TRANSPORT_FAILURE": "🔴 Blocked",
        "UNKNOWN": "⚪ Unavailable",
    }
    return surface.get(key, "⚪ {0}".format(status or "unavailable"))


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

# Metric-specific near-zero bands. Returns stay in fraction units; spreads use bps.
HEATMAP_SCALES: dict[str, dict[str, Any]] = {
    "1D return": {"threshold": 0.005, "as_percent": True, "units_label": "return (fraction)"},
    "1W return": {"threshold": 0.01, "as_percent": True, "units_label": "return (fraction)"},
    "1M return": {"threshold": 0.02, "as_percent": True, "units_label": "return (fraction)"},
    "3M return": {"threshold": 0.03, "as_percent": True, "units_label": "return (fraction)"},
    "6M return": {"threshold": 0.05, "as_percent": True, "units_label": "return (fraction)"},
    "12M return": {"threshold": 0.08, "as_percent": True, "units_label": "return (fraction)"},
    "1D RS": {"threshold": 0.005, "as_percent": True, "units_label": "RS change (fraction)"},
    "1W RS": {"threshold": 0.01, "as_percent": True, "units_label": "RS change (fraction)"},
    "1M RS": {"threshold": 0.015, "as_percent": True, "units_label": "RS change (fraction)"},
    "3M RS": {"threshold": 0.025, "as_percent": True, "units_label": "RS change (fraction)"},
    "6M RS": {"threshold": 0.04, "as_percent": True, "units_label": "RS change (fraction)"},
    "12M RS": {"threshold": 0.06, "as_percent": True, "units_label": "RS change (fraction)"},
    "1D (bps)": {"threshold": 1.0, "as_percent": False, "units_label": "bps", "number_format": "+.0f"},
    "1W (bps)": {"threshold": 2.0, "as_percent": False, "units_label": "bps", "number_format": "+.0f"},
    "1M (bps)": {"threshold": 5.0, "as_percent": False, "units_label": "bps", "number_format": "+.0f"},
}


def heatmap_scale_for(column: str) -> dict[str, Any]:
    return HEATMAP_SCALES.get(
        column,
        {"threshold": 0.01, "as_percent": True, "units_label": "value"},
    )


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


def _format_heat_value(value: Any, *, as_percent: bool, number_format: str | None = None) -> str:
    if _is_missing(value):
        return "—"
    v = float(value)
    if as_percent:
        return "{0:+.2f}%".format(v * 100)
    if number_format:
        return ("{0:" + number_format + "}").format(v)
    return "{0:+.3f}".format(v)


def styled_heatmap(
    frame: pd.DataFrame,
    value_columns: list[str],
    *,
    low_positive_threshold: float | None = None,
    as_percent: bool | None = None,
    column_scales: dict[str, dict[str, Any]] | None = None,
):
    """Color numeric columns with metric-specific near-zero bands when known.

    Pass ``column_scales`` or rely on :data:`HEATMAP_SCALES` keys matching column names.
    Legacy callers may still pass a single ``low_positive_threshold`` / ``as_percent``.
    """
    display = frame.copy()
    scales = column_scales or {}

    def _scale(col: str) -> dict[str, Any]:
        if col in scales:
            return scales[col]
        known = heatmap_scale_for(col)
        if low_positive_threshold is not None or as_percent is not None:
            return {
                "threshold": low_positive_threshold if low_positive_threshold is not None else known["threshold"],
                "as_percent": as_percent if as_percent is not None else known["as_percent"],
                "units_label": known.get("units_label", "value"),
                "number_format": known.get("number_format"),
            }
        return known

    try:
        styler = display.style
    except (AttributeError, ImportError):
        for col in value_columns:
            scale = _scale(col)
            display[col] = [
                "{0} {1}".format(
                    heat_marker(v, low_positive_threshold=float(scale["threshold"])),
                    _format_heat_value(v, as_percent=bool(scale["as_percent"]), number_format=scale.get("number_format")),
                )
                for v in display[col]
            ]
        return display
    for col in value_columns:
        scale = _scale(col)
        threshold = float(scale["threshold"])
        styler = styler.map(lambda v, t=threshold: heat_color(v, low_positive_threshold=t), subset=[col])
    fmt_map = {}
    for col in value_columns:
        scale = _scale(col)
        pct = bool(scale["as_percent"])
        number_format = scale.get("number_format")
        fmt_map[col] = lambda v, p=pct, nf=number_format: _format_heat_value(v, as_percent=p, number_format=nf)
    styler = styler.format(fmt_map, na_rep="—")
    return styler


def heatmap_legend(low_positive_threshold: float = 0.01, *, scale_note: str | None = None) -> None:
    note = scale_note or "near-zero band is metric-specific (see column labels)"
    st.caption(
        "Legend: 🟥 negative · 🟨 near zero ({0}) · 🟩 positive · ⬜ missing (shown as — , never as zero). "
        "Values remain numeric in every cell. Default band example: 0 to {1}.".format(note, low_positive_threshold)
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
    "HEATMAP_SCALES",
    "age_text",
    "as_date",
    "cached_read",
    "clear_read_cache",
    "compact_as_of",
    "fmt",
    "fmt_signed",
    "freshness_chip",
    "heat_color",
    "heat_marker",
    "heatmap_legend",
    "heatmap_scale_for",
    "history_chart",
    "implied_prior_yield",
    "load_optional",
    "load_or_stop",
    "page_header",
    "styled_heatmap",
    "transport_chip",
    "unavailable",
]
