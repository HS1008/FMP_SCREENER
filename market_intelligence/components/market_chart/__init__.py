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


def _series_label(value: Any) -> str:
    if value is None:
        return "Value"
    text = str(value).strip()
    return text or "Value"


def _series_specs(
    points: Sequence[Mapping[str, Any]] | None,
    series_label: str,
    series: Sequence[Mapping[str, Any]] | None,
) -> list[tuple[str, list[dict[str, Any]]]]:
    if series is not None:
        return [
            (_series_label(item.get("label")), time_series_points(item.get("points") or ()))
            for item in series
        ]
    return [(_series_label(series_label), time_series_points(points or ()))]


def _align_to_union(
    specs: Sequence[tuple[str, Sequence[Mapping[str, Any]]]],
) -> list[dict[str, Any]]:
    """Share one timeline. A series missing a date gets ``{"time"}`` only."""
    lookups: list[tuple[str, dict[str, float]]] = []
    days: set[str] = set()
    for label, rows in specs:
        values = {str(row["time"]): row["value"] for row in rows}
        days.update(values)
        lookups.append((label, values))
    ordered = sorted(days)
    aligned: list[dict[str, Any]] = []
    for label, values in lookups:
        series_points: list[dict[str, Any]] = []
        for day in ordered:
            if day in values:
                series_points.append({"time": day, "value": values[day]})
            else:
                series_points.append({"time": day})
        aligned.append({"label": label, "points": series_points})
    return aligned


def build_market_chart_payload(
    points: Sequence[Mapping[str, Any]] | None = None,
    *,
    series_label: str = "Value",
    series: Sequence[Mapping[str, Any]] | None = None,
    ranges: bool = False,
    value_format: str = "number",
) -> dict[str, Any]:
    """Data passed to the chart, before the component mount.

    ``series`` items are ``{"label", "points"}``. Each points sequence is
    normalized with :func:`time_series_points`. When ``series`` is omitted,
    ``points`` and ``series_label`` are the single series. Dates are
    ``YYYY-MM-DD`` strings. A gap against the union of dates is a whitespace
    point with no ``value`` key, so the line is not zero-filled.
    """
    return {
        "series": _align_to_union(_series_specs(points, series_label, series)),
        "ranges": bool(ranges),
        "value_format": value_format,
    }


def _component():
    # Register on each script run. AppTest replaces the component registry between
    # runs, so a process-wide cache would mount an unregistered name.
    return components.component(
        "market_chart",
        html=_HTML,
        css=_CSS,
        js=_JS,
        isolate_styles=True,
    )


def lightweight_market_chart(
    points: Sequence[Mapping[str, Any]] | None = None,
    *,
    series_label: str = "Value",
    series: Sequence[Mapping[str, Any]] | None = None,
    height: int | None = None,
    key: str | None = None,
    ranges: bool = False,
    value_format: str = "number",
) -> None:
    """Render one or more time series on a shared calendar axis.

    ``lightweight_market_chart(points, series_label=...)`` still draws one
    series. Pass ``series`` as ``{"label", "points"}`` items to draw several;
    ``points`` is then ignored. ``points`` may be raw ``as_of`` rows or
    :func:`time_series_points` output.
    """
    payload = build_market_chart_payload(
        points,
        series_label=series_label,
        series=series,
        ranges=ranges,
        value_format=value_format,
    )
    resolved_height = 420 if height is None else int(height)
    data: dict[str, Any] = {
        **payload,
        "chart_kind": "time_series",
        "height": resolved_height,
    }
    if series is None and payload["series"]:
        data["points"] = payload["series"][0]["points"]
        data["series_label"] = payload["series"][0]["label"]
    _component()(
        data=data,
        key=key,
        width="stretch",
        height=resolved_height,
    )
