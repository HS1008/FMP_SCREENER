"""Categorical tenor-curve chart for Streamlit.

Apache ECharts 6.1.0 (Apache-2.0) is vendored in ``frontend/``. The file is
still named ``echarts.common.min.js``, but it is the full build: the common
build names heatmap without shipping the series renderer. The component draws
the ordered curve or category chart it is given. It does not fetch market data,
and it does not turn tenor labels into dates.

Callers pass an explicit axis. Points are never sorted alphabetically, missing
levels stay missing, and duplicate tenors keep the first row.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from market_intelligence.components.chart_component import chart_component
from market_intelligence.display_dates import format_calendar_date
from market_intelligence.markets_analytics import column_color_scales, heatmap_cell_color
from market_intelligence.perf import span

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
    return format_calendar_date(day)


def _long_date(day: date) -> str:
    return format_calendar_date(day)


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
        "tooltip": _dark_tooltip(),
    }


def _component():
    return chart_component(
        "tenor_chart",
        html=_HTML,
        css=_CSS,
        library_js=_FRONTEND / "echarts.common.min.js",
        chart_js=_FRONTEND / "chart.js",
        inline_js=_JS,
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
    """Mount one prepared ECharts option. The browser does not fetch market data."""
    with span("tenor_chart.mount"):
        _mount_echarts(option, key=key, desktop_height=desktop_height, mobile_height=mobile_height)


def _mount_echarts(option, key, desktop_height, mobile_height) -> None:
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
    """Tooltip stays inside the chart. ECharts otherwise appends it to document.body."""
    return {
        "trigger": trigger,
        "triggerOn": "mousemove|click",
        "confine": True,
        "appendToBody": False,
        "enterable": False,
        "transitionDuration": 0,
        "className": "mi-echart-tooltip",
        "renderMode": "html",
        "backgroundColor": "rgba(22, 24, 28, 0.96)",
        "borderColor": "rgba(255, 255, 255, 0.14)",
        "borderWidth": 1,
        "padding": [8, 10],
        "textStyle": {"color": "#f4f6f8", "fontSize": 13},
        "extraCssText": (
            "background:rgba(22,24,28,0.96)!important;color:#f4f6f8!important;"
            "border:1px solid rgba(255,255,255,0.14)!important;border-radius:8px;"
            "box-shadow:none;padding:8px 10px;pointer-events:none;"
        ),
        "axisPointer": {"type": "line", "snap": True},
    }


def _category_slots(
    categories: Sequence[Any],
    values: Sequence[Any],
    *,
    unit: str | None,
    notes: Sequence[str] | None = None,
    details: Sequence[Sequence[str]] | None = None,
) -> list[dict[str, Any] | None]:
    slots: list[dict[str, Any] | None] = []
    note_lines = [str(line) for line in notes] if notes else []
    for index, label in enumerate(categories):
        raw = values[index] if index < len(values) else None
        number = _finite_number(raw)
        if number is None:
            slots.append(None)
            continue
        point: dict[str, Any] = {"value": number, "name": str(label), "tenor": str(label)}
        if unit:
            point["unit"] = unit
        if note_lines:
            point["notes"] = note_lines
        if details and index < len(details) and details[index]:
            point["detail_lines"] = [str(line) for line in details[index]]
        slots.append(point)
    return slots


def _padded_limits(numbers: Sequence[float]) -> tuple[float, float] | None:
    """Include every plotted level, then pad. A flat series still gets room around it."""
    finite = [number for number in numbers if isinstance(number, (int, float)) and math.isfinite(number)]
    if not finite:
        return None
    low = min(finite)
    high = max(finite)
    span = high - low
    pad = max(span * 0.08, 0.2)
    if span == 0:
        pad = 0.25
    return low - pad, high + pad


_LINE_COLORS = ("#4c78a8", "#e15759", "#59a14f", "#f2c14e", "#b07aa1", "#76b7b2")
_LEGEND_BAND_COLORS = {"current": "#9ec1ff", "compare": "#f2c14e"}


_BAND_STYLE = {
    "current": {
        "color": "#9ec1ff",
        "fill": "rgba(158, 193, 255, 0.14)",
        "dashed": False,
        "upper_position": "insideEndTop",
        "lower_position": "insideEndBottom",
    },
    "compare": {
        "color": "#f2c14e",
        "fill": "rgba(242, 193, 78, 0.08)",
        "dashed": True,
        "upper_position": "insideStartTop",
        "lower_position": "insideStartBottom",
    },
}


def _policy_mark(bands: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Horizontal policy references. They are not category-axis points."""
    lines: list[dict[str, Any]] = []
    areas: list[list[dict[str, Any]]] = []
    for band in bands:
        lower = _finite_number(band.get("lower"))
        upper = _finite_number(band.get("upper"))
        if lower is None or upper is None:
            continue
        style = _BAND_STYLE.get(str(band.get("role") or "current"), _BAND_STYLE["current"])
        color = style["color"]
        dashed = bool(style["dashed"])
        for level, label, position in (
            (upper, str(band.get("upper_label") or ""), style["upper_position"]),
            (lower, str(band.get("lower_label") or ""), style["lower_position"]),
        ):
            lines.append(
                {
                    "yAxis": level,
                    "name": label,
                    "lineStyle": {"color": color, "type": "dashed" if dashed else "solid", "width": 1},
                    "label": {
                        "show": True,
                        "formatter": label,
                        "position": position,
                        "color": color,
                        "fontSize": 11,
                    },
                }
            )
        if band.get("fill"):
            areas.append(
                [
                    {"yAxis": lower, "itemStyle": {"color": style["fill"]}},
                    {"yAxis": upper},
                ]
            )
    mark_line = {"silent": True, "symbol": "none", "data": lines} if lines else None
    mark_area = {"silent": True, "data": areas} if areas else None
    return mark_line, mark_area


def build_category_line_option(
    categories: Sequence[Any],
    series: Sequence[Mapping[str, Any]],
    *,
    y_title: str = "",
    connect_nulls: bool = False,
    bands: Sequence[Mapping[str, Any]] | None = None,
    point_notes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Ordered category lines. Nulls stay null. Categories are not parsed as dates.

    ``bands`` are y-axis mark lines and an optional shaded area. They are not
    added to the category axis.
    """
    labels = [str(label) for label in categories]
    encoded = []
    levels: list[float] = []
    for index, item in enumerate(series):
        values = list(item.get("values") or [])
        for raw in values:
            number = _finite_number(raw)
            if number is not None:
                levels.append(number)
        color = str(item.get("color") or _LINE_COLORS[index % len(_LINE_COLORS)])
        encoded.append(
            {
                "type": "line",
                "name": str(item.get("name") or ""),
                "data": _category_slots(
                    labels,
                    values,
                    unit=item.get("unit"),
                    notes=point_notes if index == 0 else None,
                    details=item.get("details") if index == 0 else None,
                ),
                "connectNulls": bool(connect_nulls),
                "showSymbol": True,
                "symbol": "circle",
                "label": {"show": False},
                "lineStyle": {"width": 2, "color": color},
                "itemStyle": {"color": color},
            }
        )
    legend_names = [str(item.get("name") or "") for item in encoded if item.get("name")]
    if bands and encoded:
        mark_line, mark_area = _policy_mark(bands)
        if mark_line is not None:
            encoded[0]["markLine"] = mark_line
        if mark_area is not None:
            encoded[0]["markArea"] = mark_area
        for band in bands:
            lower = _finite_number(band.get("lower"))
            upper = _finite_number(band.get("upper"))
            if lower is not None:
                levels.append(lower)
            if upper is not None:
                levels.append(upper)
            legend = str(band.get("legend_label") or "").strip()
            if not legend:
                continue
            color = _LEGEND_BAND_COLORS.get(str(band.get("role") or "current"), _LEGEND_BAND_COLORS["current"])
            legend_names.append(legend)
            encoded.append(
                {
                    "name": legend,
                    "type": "line",
                    "data": [],
                    "legendHoverLink": False,
                    "showSymbol": False,
                    "silent": True,
                    "lineStyle": {"width": 8, "color": color},
                    "itemStyle": {"color": color},
                    "tooltip": {"show": False},
                }
            )
    limits = _padded_limits(levels)
    y_axis: dict[str, Any] = {"type": "value", "name": y_title, "scale": limits is None, "splitLine": {"show": True}}
    if limits is not None:
        y_axis["min"] = limits[0]
        y_axis["max"] = limits[1]
    show_legend = len(legend_names) >= 2
    return {
        "animation": False,
        "legend": {"show": show_legend, "top": 0, "data": legend_names},
        "toolbox": {"show": False},
        "dataZoom": [],
        "grid": {"left": 8, "right": 16, "top": 52 if show_legend else 28, "bottom": 4, "containLabel": True},
        "tooltip": _dark_tooltip(),
        "xAxis": {"type": "category", "data": labels, "boundaryGap": True, "axisLabel": {"interval": 0}},
        "yAxis": y_axis,
        "series": encoded,
    }


def build_policy_rate_option(frame: Mapping[str, Any]) -> dict[str, Any]:
    """Fed funds target band plus effective funds and SOFR.

    Dates stay ``YYYY-MM-DD`` strings. The span series is the upper-minus-lower
    gap stacked on the lower limit so the fill sits between the two prints.
    Recession intervals are mark areas on the time axis. Missing prints stay null.
    """
    def points(key: str) -> list[list[Any]]:
        rows = []
        for item in frame.get(key) or ():
            if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) < 2:
                continue
            day = str(item[0])
            value = _finite_number(item[1])
            rows.append([day, value])
        return rows

    bands = []
    for band in frame.get("bands") or ():
        if not isinstance(band, Mapping):
            continue
        start = str(band.get("start") or "")[:10]
        end = str(band.get("end") or "")[:10]
        if len(start) == 10 and len(end) == 10 and start <= end:
            bands.append([{"xAxis": start}, {"xAxis": end}])
    mark_area = {
        "silent": True,
        "itemStyle": {"color": "rgba(148, 163, 184, 0.16)"},
        "data": bands,
    } if bands else None
    lower = {
        "name": "Target lower",
        "type": "line",
        "stack": "target",
        "policyRole": "lower",
        "data": points("lower"),
        "connectNulls": False,
        "showSymbol": False,
        "step": "end",
        "lineStyle": {"width": 1.5, "color": "#4c78a8"},
        "itemStyle": {"color": "#4c78a8"},
        "areaStyle": {"color": "rgba(76, 120, 168, 0)"},
    }
    span = {
        "name": "Target span",
        "type": "line",
        "stack": "target",
        "policyRole": "span",
        "data": points("span"),
        "connectNulls": False,
        "showSymbol": False,
        "step": "end",
        "lineStyle": {"width": 0, "color": "transparent"},
        "areaStyle": {"color": "rgba(76, 120, 168, 0.22)"},
        "emphasis": {"disabled": True},
        "tooltip": {"show": False},
    }
    upper = {
        "name": "Target upper",
        "type": "line",
        "policyRole": "line",
        "data": points("upper"),
        "connectNulls": False,
        "showSymbol": False,
        "step": "end",
        "lineStyle": {"width": 1.5, "color": "#4c78a8"},
        "itemStyle": {"color": "#4c78a8"},
    }
    effective = {
        "name": "Effective Fed Funds",
        "type": "line",
        "policyRole": "line",
        "data": points("effective"),
        "connectNulls": False,
        "showSymbol": False,
        "lineStyle": {"width": 1.5, "color": "#e15759"},
        "itemStyle": {"color": "#e15759"},
    }
    sofr = {
        "name": "SOFR",
        "type": "line",
        "policyRole": "line",
        "data": points("sofr"),
        "connectNulls": False,
        "showSymbol": False,
        "lineStyle": {"width": 1.5, "color": "#f2c14e"},
        "itemStyle": {"color": "#f2c14e"},
    }
    if mark_area is not None:
        effective["markArea"] = mark_area
    option = {
        "chartKind": "policy_rates",
        "animation": False,
        "legend": {
            "show": True,
            "top": 0,
            "data": ["Target lower", "Target upper", "Effective Fed Funds", "SOFR"],
        },
        "toolbox": {"show": False},
        "dataZoom": [],
        "grid": {"left": 8, "right": 16, "top": 36, "bottom": 8, "containLabel": True},
        "tooltip": _dark_tooltip(),
        "xAxis": {"type": "time", "axisLabel": {"hideOverlap": True}},
        "yAxis": {"type": "value", "name": "Percent", "scale": True, "splitLine": {"show": True}},
        "series": [lower, span, upper, effective, sofr],
    }
    option["policyReadout"] = policy_readout(frame)
    return option


def policy_readout(frame: Mapping[str, Any]) -> dict[str, Any]:
    """Latest in-window print for each policy series. Missing stays missing.

    The date is the latest observation among series that have a value. A row
    whose own date differs is kept so the readout can show it. Nothing is
    copied onto a later day.
    """
    specs = (
        ("lower", "Target lower"),
        ("upper", "Target upper"),
        ("effective", "Effective Fed Funds"),
        ("sofr", "SOFR"),
    )
    rows: list[dict[str, Any]] = []
    for key, label in specs:
        last_day = None
        last_value = None
        for item in frame.get(key) or ():
            if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) < 2:
                continue
            value = _finite_number(item[1])
            if value is None:
                continue
            last_day = str(item[0])[:10]
            last_value = value
        rows.append({"label": label, "date": last_day, "value": last_value})
    dates = [str(row["date"]) for row in rows if row["date"]]
    return {"date": max(dates) if dates else None, "rows": rows}


def policy_rate_chart(frame: Mapping[str, Any], *, key: str) -> None:
    """Mount the policy-rate band. The browser does not fetch data."""
    render_echarts(
        build_policy_rate_option(frame),
        key=key,
        desktop_height=540,
        mobile_height=460,
    )


def category_line_chart(
    categories: Sequence[Any],
    series: Sequence[Mapping[str, Any]],
    *,
    key: str,
    y_title: str = "",
    connect_nulls: bool = False,
    bands: Sequence[Mapping[str, Any]] | None = None,
    point_notes: Sequence[str] | None = None,
) -> None:
    """Ordered category lines. Nulls stay null. Categories are not parsed as dates."""
    render_echarts(
        build_category_line_option(
            categories,
            series,
            y_title=y_title,
            connect_nulls=connect_nulls,
            bands=bands,
            point_notes=point_notes,
        ),
        key=key,
    )


def ranked_bar_chart(
    categories: Sequence[Any],
    values: Sequence[Any],
    *,
    key: str,
    unit: str | None = None,
    title: str = "",
    benchmark: tuple[str, float] | None = None,
) -> None:
    """Horizontal bars sorted by value. Missing values are omitted, never drawn as zero.

    ``benchmark`` is an optional ``(label, value)`` reference line on the same
    numeric scale as the bars. It is not included in the ranking.
    """
    pairs: list[tuple[str, float]] = []
    for label, raw in zip(categories, values):
        number = _finite_number(raw)
        if number is None:
            continue
        pairs.append((str(label), number))
    pairs.sort(key=lambda item: item[1])
    labels = [label for label, _value in pairs]
    data = [{"value": value, "name": label, **({"unit": unit} if unit else {})} for label, value in pairs]
    series: dict[str, Any] = {"type": "bar", "data": data, "label": {"show": False}}
    if benchmark is not None:
        bench_label, bench_value = benchmark
        number = _finite_number(bench_value)
        if number is not None:
            series["markLine"] = {
                "symbol": "none",
                "silent": True,
                "lineStyle": {"type": "dashed", "color": "#f4f6f8", "width": 1},
                "label": {"formatter": str(bench_label), "color": "#f4f6f8"},
                "data": [{"xAxis": number}],
            }
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
            "series": [series],
        },
        key=key,
        desktop_height=min(640, max(280, 36 * max(len(labels), 1) + 48)),
        mobile_height=min(560, max(260, 32 * max(len(labels), 1) + 40)),
    )


def return_heatmap(
    row_labels: Sequence[str],
    column_labels: Sequence[str],
    values: Sequence[Sequence[Any]],
    *,
    key: str,
) -> None:
    """Category heatmap. ``values[row][col]`` is a fractional return or null.

    Null cells are omitted. They are not drawn as zero. Row order is the
    order of ``row_labels``.
    """
    cells: list[dict[str, Any]] = []
    numbers: list[float] = []
    for row_index, row_label in enumerate(row_labels):
        row = values[row_index] if row_index < len(values) else ()
        for col_index, column in enumerate(column_labels):
            raw = row[col_index] if col_index < len(row) else None
            number = _finite_number(raw)
            if number is None:
                continue
            percent = number * 100.0
            numbers.append(percent)
            cells.append(
                {
                    "value": [col_index, row_index, percent],
                    "row": str(row_label),
                    "column": str(column),
                    "display": "{0:+.2f}%".format(percent),
                }
            )
    if not cells:
        return
    low = min(0.0, min(numbers))
    high = max(0.0, max(numbers))
    if low == high:
        high = low + 1.0
    render_echarts(
        {
            "chartKind": "heatmap",
            "animation": False,
            "legend": {"show": False},
            "toolbox": {"show": False},
            "dataZoom": [],
            "grid": {"left": 8, "right": 16, "top": 8, "bottom": 52, "containLabel": True},
            "tooltip": _dark_tooltip(trigger="item"),
            "xAxis": {
                "type": "category",
                "data": [str(label) for label in column_labels],
                "axisLabel": {"interval": 0},
                "splitArea": {"show": False},
            },
            "yAxis": {
                "type": "category",
                "data": [str(label) for label in row_labels],
                "inverse": True,
                "axisLabel": {"interval": 0},
                "splitArea": {"show": False},
            },
            "visualMap": {
                "min": low,
                "max": high,
                "dimension": 2,
                "seriesIndex": 0,
                "calculable": False,
                "orient": "horizontal",
                "left": "center",
                "bottom": 0,
                "itemWidth": 12,
                "itemHeight": 120,
                "inRange": {"color": ["#b34040", "#2c3036", "#3d8c5a"]},
                "textStyle": {"color": "#f4f6f8"},
            },
            "series": [
                {
                    "type": "heatmap",
                    "data": cells,
                    "label": {"show": False},
                    "emphasis": {"disabled": True},
                    "itemStyle": {"borderColor": "rgba(255,255,255,0.08)", "borderWidth": 1},
                }
            ],
        },
        key=key,
        desktop_height=min(640, max(320, 36 * max(len(row_labels), 1) + 96)),
        mobile_height=min(680, max(340, 40 * max(len(row_labels), 1) + 108)),
    )


def build_column_scaled_heatmap_option(
    row_labels: Sequence[str],
    column_labels: Sequence[str],
    values: Sequence[Sequence[Any]],
    *,
    notes: Sequence[Sequence[Any]] | None = None,
) -> dict[str, Any]:
    """Heatmap whose color scale is computed independently for each column.

    Cell values are fractional returns. Missing cells display N/A and do not
    enter that column's scale. The printed value is the percentage.
    """
    scales = column_color_scales(values)
    cells: list[dict[str, Any]] = []
    for row_index, row_label in enumerate(row_labels):
        row = values[row_index] if row_index < len(values) else ()
        row_notes = notes[row_index] if notes is not None and row_index < len(notes) else ()
        for col_index, column in enumerate(column_labels):
            raw = row[col_index] if col_index < len(row) else None
            number = _finite_number(raw)
            scale = scales[col_index]["max_abs"] if col_index < len(scales) else 0.0
            note = row_notes[col_index] if col_index < len(row_notes) else None
            if number is None:
                display = "N/A"
                color = heatmap_cell_color(None, scale)
                plot = 0.0
            else:
                display = "{0:+.2f}%".format(number * 100.0)
                color = heatmap_cell_color(number, scale)
                plot = number * 100.0
            cell: dict[str, Any] = {
                "value": [col_index, row_index, plot],
                "row": str(row_label),
                "column": str(column),
                "display": display,
                "itemStyle": {"color": color},
            }
            if note:
                cell["note"] = str(note)
            cells.append(cell)
    return {
        "chartKind": "heatmap",
        "animation": False,
        "legend": {"show": False},
        "toolbox": {"show": False},
        "dataZoom": [],
        "grid": {"left": 8, "right": 12, "top": 8, "bottom": 8, "containLabel": True},
        "tooltip": _dark_tooltip(trigger="item"),
        "xAxis": {
            "type": "category",
            "data": [str(label) for label in column_labels],
            "axisLabel": {"interval": 0},
            "splitArea": {"show": False},
        },
        "yAxis": {
            "type": "category",
            "data": [str(label) for label in row_labels],
            "inverse": True,
            "axisLabel": {"interval": 0},
            "splitArea": {"show": False},
        },
        "series": [
            {
                "type": "heatmap",
                "data": cells,
                "label": {"show": True, "fontSize": 11, "color": "#f4f6f8"},
                "emphasis": {"disabled": True},
                "itemStyle": {"borderColor": "rgba(255,255,255,0.08)", "borderWidth": 1},
            }
        ],
    }


def column_scaled_return_heatmap(
    row_labels: Sequence[str],
    column_labels: Sequence[str],
    values: Sequence[Sequence[Any]],
    *,
    key: str,
    notes: Sequence[Sequence[Any]] | None = None,
) -> None:
    """Per-column symmetric heatmap. Nulls render as N/A, not as 0.00%."""
    if not row_labels:
        return
    render_echarts(
        build_column_scaled_heatmap_option(row_labels, column_labels, values, notes=notes),
        key=key,
        desktop_height=min(760, max(320, 46 * max(len(row_labels), 1) + 72)),
        mobile_height=min(820, max(340, 52 * max(len(row_labels), 1) + 88)),
    )


def build_signed_change_bar_option(
    categories: Sequence[Any],
    values: Sequence[Any],
    *,
    y_title: str = "bps",
) -> dict[str, Any]:
    """Vertical bars around a zero line. Missing changes stay empty, not zero."""
    labels = [str(label) for label in categories]
    data: list[dict[str, Any] | None] = []
    finite: list[float] = [0.0]
    for index, label in enumerate(labels):
        raw = values[index] if index < len(values) else None
        number = _finite_number(raw)
        if number is None:
            data.append(None)
            continue
        finite.append(number)
        color = "#3d8c5a" if number > 0 else "#b34040" if number < 0 else "#9aa0a6"
        data.append(
            {
                "value": number,
                "name": label,
                "tenor": label,
                "unit": "bps",
                "itemStyle": {"color": color},
                "detail_lines": [label, "Change: {0:+.1f} bps".format(number)],
            }
        )
    low = min(finite)
    high = max(finite)
    span = high - low
    pad = max(span * 0.08, 1.0)
    return {
        "animation": False,
        "legend": {"show": False},
        "toolbox": {"show": False},
        "dataZoom": [],
        "grid": {"left": 8, "right": 12, "top": 16, "bottom": 4, "containLabel": True},
        "tooltip": _dark_tooltip(trigger="item"),
        "xAxis": {"type": "category", "data": labels, "axisLabel": {"interval": 0}},
        "yAxis": {
            "type": "value",
            "name": y_title,
            "min": low - pad,
            "max": high + pad,
            "scale": False,
            "splitLine": {"show": True},
        },
        "series": [
            {
                "type": "bar",
                "name": "Change",
                "data": data,
                "label": {"show": False},
                "markLine": {
                    "silent": True,
                    "symbol": "none",
                    "lineStyle": {"color": "#f4f6f8", "width": 1.5},
                    "data": [{"yAxis": 0, "label": {"show": False}}],
                },
            }
        ],
    }


def signed_change_bar_chart(
    categories: Sequence[Any],
    values: Sequence[Any],
    *,
    key: str,
    y_title: str = "bps",
) -> None:
    """Change bars. Sign is the direction from zero; color only reinforces it."""
    render_echarts(
        build_signed_change_bar_option(categories, values, y_title=y_title),
        key=key,
        desktop_height=320,
        mobile_height=280,
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
