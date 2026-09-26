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
ECHARTS_VERSION = "6.1.0"
DESKTOP_HEIGHT = 390
MOBILE_HEIGHT = 320

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FULL_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


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


def _long_date(day: date) -> str:
    return "{0} {1}, {2}".format(_FULL_MONTHS[day.month - 1], day.day, day.year)


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
    long_label = "" if resolved is None else _long_date(resolved)

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
                "curve_date_long": long_label,
            }
        )
    return {
        "axis": ordered_axis,
        "points": points,
        "missing_tenors": missing,
        "curve_date": iso,
        "curve_date_label": label,
        "curve_date_long": long_label,
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
                "curve_date_long": str(point.get("curve_date_long") or ""),
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
            "backgroundColor": "rgba(22, 24, 28, 0.96)",
            "borderColor": "rgba(255, 255, 255, 0.14)",
            "borderWidth": 1,
            "padding": [8, 10],
            "textStyle": {"color": "#f4f6f8", "fontSize": 13},
            "extraCssText": (
                "background:rgba(22,24,28,0.96)!important;color:#f4f6f8!important;"
                "border:1px solid rgba(255,255,255,0.14)!important;border-radius:8px;"
                "box-shadow:none;padding:8px 10px;"
            ),
            "axisPointer": {"type": "line", "snap": True},
        },
    }


def _component():
    # Register on each script run. AppTest replaces the component registry between
    # runs, so a process-wide cache would mount an unregistered name.
    return components.component(
        "tenor_chart",
        html=_HTML,
        css=_CSS,
        js=_JS,
        isolate_styles=True,
    )


def tenor_curve_chart(payload: Mapping[str, Any], *, key: str | None = None) -> None:
    """Mount one categorical curve. ``payload`` is ``build_tenor_curve`` output."""
    option = echarts_option(payload)
    render_echarts(option, key=key)


def render_echarts(
    option: Mapping[str, Any],
    *,
    key: str | None = None,
    desktop_height: int = DESKTOP_HEIGHT,
    mobile_height: int = MOBILE_HEIGHT,
) -> None:
    """Mount one prepared ECharts option. The browser does not fetch data."""
    _component()(
        data={
            "option": option,
            "desktop_height": int(desktop_height),
            "mobile_height": int(mobile_height),
        },
        key=key,
        width="stretch",
        height=int(desktop_height),
    )


def _dark_tooltip(*, trigger: str = "axis") -> dict[str, Any]:
    return {
        "trigger": trigger,
        "triggerOn": "mousemove|click",
        "confine": True,
        "backgroundColor": "rgba(22, 24, 28, 0.96)",
        "borderColor": "rgba(255, 255, 255, 0.14)",
        "borderWidth": 1,
        "padding": [8, 10],
        "textStyle": {"color": "#f4f6f8", "fontSize": 13},
        "extraCssText": (
            "background:rgba(22,24,28,0.96)!important;color:#f4f6f8!important;"
            "border:1px solid rgba(255,255,255,0.14)!important;border-radius:8px;"
            "box-shadow:none;padding:8px 10px;"
        ),
    }


def _category_slots(categories: Sequence[Any], values: Sequence[Any], *, unit: str | None) -> list[dict[str, Any] | None]:
    slots: list[dict[str, Any] | None] = []
    for index, label in enumerate(categories):
        raw = values[index] if index < len(values) else None
        number = _finite_number(raw)
        if number is None:
            slots.append(None)
            continue
        point: dict[str, Any] = {"value": number, "name": str(label)}
        if unit:
            point["unit"] = unit
        slots.append(point)
    return slots


def category_line_chart(
    categories: Sequence[Any],
    series: Sequence[Mapping[str, Any]],
    *,
    key: str,
    y_title: str = "",
    connect_nulls: bool = False,
) -> None:
    """Ordered category lines. Nulls stay null. Categories are not parsed as dates."""
    labels = [str(label) for label in categories]
    encoded = []
    for item in series:
        encoded.append(
            {
                "type": "line",
                "name": str(item.get("name") or ""),
                "data": _category_slots(labels, list(item.get("values") or []), unit=item.get("unit")),
                "connectNulls": bool(connect_nulls),
                "showSymbol": True,
                "symbol": "circle",
                "label": {"show": False},
            }
        )
    render_echarts(
        {
            "animation": False,
            "legend": {"show": len(encoded) > 1, "top": 0},
            "toolbox": {"show": False},
            "dataZoom": [],
            "grid": {"left": 8, "right": 12, "top": 36, "bottom": 4, "containLabel": True},
            "tooltip": _dark_tooltip(),
            "xAxis": {"type": "category", "data": labels, "boundaryGap": True, "axisLabel": {"interval": 0}},
            "yAxis": {"type": "value", "name": y_title, "scale": True, "splitLine": {"show": True}},
            "series": encoded,
        },
        key=key,
    )


def ranked_bar_chart(
    categories: Sequence[Any],
    values: Sequence[Any],
    *,
    key: str,
    unit: str | None = None,
    title: str = "",
) -> None:
    """Horizontal bars sorted by value. Missing values are omitted, never drawn as zero."""
    pairs: list[tuple[str, float]] = []
    for label, raw in zip(categories, values):
        number = _finite_number(raw)
        if number is None:
            continue
        pairs.append((str(label), number))
    pairs.sort(key=lambda item: item[1])
    labels = [label for label, _value in pairs]
    data = [{"value": value, "name": label, **({"unit": unit} if unit else {})} for label, value in pairs]
    render_echarts(
        {
            "animation": False,
            "legend": {"show": False},
            "toolbox": {"show": False},
            "dataZoom": [],
            "title": {"show": bool(title), "text": title, "left": 0, "textStyle": {"fontSize": 13, "fontWeight": 500}},
            "grid": {"left": 8, "right": 16, "top": 28 if title else 8, "bottom": 4, "containLabel": True},
            "tooltip": _dark_tooltip(trigger="item"),
            "xAxis": {"type": "value", "scale": True, "splitLine": {"show": True}},
            "yAxis": {"type": "category", "data": labels, "axisLabel": {"interval": 0}},
            "series": [{"type": "bar", "data": data, "label": {"show": False}}],
        },
        key=key,
        desktop_height=min(640, max(280, 36 * max(len(labels), 1) + 48)),
        mobile_height=min(560, max(260, 32 * max(len(labels), 1) + 40)),
    )


def category_bar_chart(
    categories: Sequence[Any],
    values: Sequence[Any],
    *,
    key: str,
    y_title: str = "",
    unit: str | None = None,
) -> None:
    """Vertical category bars. A missing category stays empty rather than zero."""
    labels = [str(label) for label in categories]
    render_echarts(
        {
            "animation": False,
            "legend": {"show": False},
            "toolbox": {"show": False},
            "dataZoom": [],
            "grid": {"left": 8, "right": 12, "top": 16, "bottom": 4, "containLabel": True},
            "tooltip": _dark_tooltip(trigger="item"),
            "xAxis": {"type": "category", "data": labels, "axisLabel": {"interval": 0}},
            "yAxis": {"type": "value", "name": y_title, "scale": True, "splitLine": {"show": True}},
            "series": [
                {
                    "type": "bar",
                    "data": _category_slots(labels, values, unit=unit),
                    "label": {"show": False},
                }
            ],
        },
        key=key,
        desktop_height=340,
        mobile_height=300,
    )
