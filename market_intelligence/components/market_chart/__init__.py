"""Reusable time-series chart for Streamlit.

TradingView Lightweight Charts 5.2.1 (Apache-2.0) is vendored in ``frontend/``.
The component draws points it is given. It does not fetch market data.

The horizontal scale is a real time scale. Categorical tenor curves do not
belong here: this library has no categorical axis, and its yield-curve scale
counts months.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import streamlit.components.v2 as components

_FRONTEND = Path(__file__).resolve().parent / "frontend"
_HTML = (_FRONTEND / "chart.html").read_text(encoding="utf-8")
_CSS = (_FRONTEND / "chart.css").read_text(encoding="utf-8")
_JS = (
    (_FRONTEND / "lightweight-charts.standalone.production.js").read_text(encoding="utf-8")
    + "\n"
    + (_FRONTEND / "chart.js").read_text(encoding="utf-8")
)
_MOUNT = None

LIGHTWEIGHT_CHARTS_VERSION = "5.2.1"


def observation_day(value: Any) -> date | None:
    """Calendar day for a stored observation, without a timezone conversion.

    Date-only strings keep their first ten characters. A timestamp is not
    converted through UTC, so an evening America/New_York close stays on that
    session's calendar date.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
    return None


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def time_series_points(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Ascending Lightweight Charts points: ``{"time": "YYYY-MM-DD", "value": float}``.

    Missing values are omitted. Duplicate dates keep the last finite value.
    Times are date strings, never epoch timestamps.
    """
    by_day: dict[str, float] = {}
    for row in rows:
        day = observation_day(row.get("time") if "time" in row else row.get("as_of"))
        number = _finite_number(row.get("value"))
        if day is None or number is None:
            continue
        by_day[day.isoformat()] = number
    return [{"time": day, "value": by_day[day]} for day in sorted(by_day)]


def _component():
    global _MOUNT
    if _MOUNT is None:
        _MOUNT = components.component(
            "market_chart",
            html=_HTML,
            css=_CSS,
            js=_JS,
            isolate_styles=True,
        )
    return _MOUNT


def lightweight_market_chart(
    points: Sequence[Mapping[str, Any]],
    *,
    series_label: str,
    height: int = 440,
    key: str | None = None,
) -> None:
    """Render one time series. ``points`` must already be ``time_series_points`` output."""
    payload = time_series_points(points)
    _component()(
        data={
            "points": payload,
            "series_label": series_label,
            "chart_kind": "time_series",
            "height": int(height),
        },
        key=key,
        width="stretch",
        height=int(height),
    )
