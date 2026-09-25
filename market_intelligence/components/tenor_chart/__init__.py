"""Categorical tenor-curve chart for Streamlit.

Apache ECharts 6.1.0 (Apache-2.0) is vendored in ``frontend/``. The component
draws the ordered curve it is given. It does not fetch market data, and it does
not turn tenor labels into dates.

Callers pass an explicit axis. Points are never sorted alphabetically, missing
levels stay missing, and duplicate tenors keep the first row.
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
    (_FRONTEND / "echarts.common.min.js").read_text(encoding="utf-8")
    + "\n"
    + (_FRONTEND / "chart.js").read_text(encoding="utf-8")
)
_MOUNT = None

ECHARTS_VERSION = "6.1.0"
DESKTOP_HEIGHT = 390
MOBILE_HEIGHT = 320

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def calendar_day(value: Any) -> date | None:
    """Calendar day without a timezone conversion.

    Date-only strings keep their first ten characters. A timestamp uses its own
    calendar fields, so an evening America/New_York close stays on that session.
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


def _index_name(ticker: str, tenor: str, explicit: Any) -> str:
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if ticker.startswith("^"):
        return ticker[1:]
    return ticker or tenor


def _short_date(day: date) -> str:
    return "{0} {1}, {2}".format(_MONTHS[day.month - 1], day.day, day.year)


def build_tenor_curve(
    rows: Sequence[Mapping[str, Any]],
    *,
    axis: Sequence[str],
    curve_date: Any = None,
    y_title: str = "Level",
) -> dict[str, Any]:
    """Ordered curve payload. ``axis`` is the category order.

    Missing tenors stay null. Nothing is zero-filled, interpolated, or given a
    synthetic date. The first row for a tenor wins; later duplicates are ignored.
    """
    ordered_axis: list[str] = []
    seen_axis: set[str] = set()
    for label in axis:
        tenor = str(label).strip()
        if not tenor or tenor in seen_axis:
            continue
        seen_axis.add(tenor)
        ordered_axis.append(tenor)
    if not ordered_axis:
        raise ValueError("tenor axis is required")

    chosen: dict[str, dict[str, Any]] = {}
    for row in rows:
        tenor = str(row.get("tenor") or "").strip()
        if tenor not in seen_axis or tenor in chosen:
            continue
        ticker = str(row.get("ticker") or row.get("yahoo_ticker") or "").strip()
        chosen[tenor] = {
            "tenor": tenor,
            "ticker": ticker,
            "name": _index_name(ticker, tenor, row.get("name")),
            "value": _finite_number(row.get("value")),
        }

    resolved = calendar_day(curve_date)
    if curve_date is None:
        row_days = {
            day
            for row in rows
            if (day := calendar_day(row.get("curve_date") if "curve_date" in row else row.get("as_of"))) is not None
        }
        resolved = next(iter(row_days)) if len(row_days) == 1 else None
    iso = None if resolved is None else resolved.isoformat()
    label = "" if resolved is None else _short_date(resolved)

    points: list[dict[str, Any]] = []
    missing: list[str] = []
    for tenor in ordered_axis:
        slot = chosen.get(tenor)
        value = None if slot is None else slot["value"]
        if value is None:
            missing.append(tenor)
        points.append(
            {
                "tenor": tenor,
                "ticker": "" if slot is None else slot["ticker"],
                "name": tenor if slot is None else slot["name"],
                "value": value,
                "curve_date": iso,
                "curve_date_label": label,
            }
        )
    return {
        "axis": ordered_axis,
        "points": points,
        "missing_tenors": missing,
        "curve_date": iso,
        "curve_date_label": label,
        "y_title": str(y_title).strip() or "Level",
    }


def echarts_option(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Structural ECharts option. Theme colors and formatters are applied in JS.

    There is no time axis, no dataZoom, and no legend. Null values stay null so
    the line does not invent a point.
    """
    axis = [str(label) for label in payload.get("axis") or ()]
    by_tenor = {str(point.get("tenor")): point for point in payload.get("points") or () if isinstance(point, Mapping)}
    series_data: list[dict[str, Any] | None] = []
    for tenor in axis:
        point = by_tenor.get(tenor)
        if not isinstance(point, Mapping):
            series_data.append(None)
            continue
        value = _finite_number(point.get("value"))
        series_data.append(
            {
                "value": value,
                "tenor": tenor,
                "ticker": str(point.get("ticker") or ""),
                "name": str(point.get("name") or tenor),
                "curve_date": point.get("curve_date"),
                "curve_date_label": str(point.get("curve_date_label") or ""),
            }
        )
    return {
        "animation": False,
        "legend": {"show": False},
        "toolbox": {"show": False},
        "dataZoom": [],
        "grid": {"left": 8, "right": 12, "top": 36, "bottom": 4, "containLabel": True},
        "xAxis": {
            "type": "category",
            "data": axis,
            "boundaryGap": True,
            "axisTick": {"alignWithLabel": True},
            "axisLabel": {"interval": 0},
        },
        "yAxis": {
            "type": "value",
            "name": str(payload.get("y_title") or "Level"),
            "scale": True,
            "splitLine": {"show": True},
        },
        "series": [
            {
                "type": "line",
                "data": series_data,
                "connectNulls": False,
                "clip": False,
                "showSymbol": True,
                "symbol": "circle",
                "label": {"show": True, "position": "top"},
                "labelLayout": {"hideOverlap": False, "moveOverlap": "shiftY"},
            }
        ],
        "tooltip": {
            "trigger": "axis",
            "triggerOn": "mousemove|click",
            "confine": True,
            "axisPointer": {"type": "line", "snap": True},
        },
    }


def _component():
    global _MOUNT
    if _MOUNT is None:
        _MOUNT = components.component(
            "tenor_chart",
            html=_HTML,
            css=_CSS,
            js=_JS,
            isolate_styles=True,
        )
    return _MOUNT


def tenor_curve_chart(payload: Mapping[str, Any], *, key: str | None = None) -> None:
    """Mount one categorical curve. ``payload`` is ``build_tenor_curve`` output."""
    option = echarts_option(payload)
    _component()(
        data={"option": option, "curve_date": payload.get("curve_date")},
        key=key,
        width="stretch",
        height=DESKTOP_HEIGHT,
    )
