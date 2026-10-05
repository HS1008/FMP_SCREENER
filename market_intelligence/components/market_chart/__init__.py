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

from market_intelligence.components.chart_component import chart_component
from market_intelligence.perf import span

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


def time_series_points(rows: Sequence[Mapping[str, Any]], *, keep_missing: bool = False) -> list[dict[str, Any]]:
    """Ascending Lightweight Charts points: ``{"time": "YYYY-MM-DD", "value": float}``.

    Missing values are omitted unless ``keep_missing`` is set. A kept gap is
    ``{"time"}`` with no value, so the line breaks instead of connecting across
    a missing observation. Duplicate dates keep the last finite value.
    Times are date strings, never epoch timestamps.
    """
    by_day: dict[str, float | None] = {}
    for row in rows:
        day = observation_day(row.get("time") if "time" in row else row.get("as_of"))
        if day is None:
            continue
        number = _finite_number(row.get("value"))
        key = day.isoformat()
        if number is None:
            if keep_missing and key not in by_day:
                by_day[key] = None
            continue
        by_day[key] = number
    points: list[dict[str, Any]] = []
    for day in sorted(by_day):
        if by_day[day] is None:
            points.append({"time": day})
        else:
            points.append({"time": day, "value": by_day[day]})
    return points


def _series_label(value: Any) -> str:
    if value is None:
        return "Value"
    text = str(value).strip()
    return text or "Value"


def _series_specs(
    points: Sequence[Mapping[str, Any]] | None,
    series_label: str,
    series: Sequence[Mapping[str, Any]] | None,
    *,
    keep_missing: bool = False,
) -> list[tuple[str, list[dict[str, Any]]]]:
    if series is not None:
        return [
            (_series_label(item.get("label")), time_series_points(item.get("points") or (), keep_missing=keep_missing))
            for item in series
        ]
    return [(_series_label(series_label), time_series_points(points or (), keep_missing=keep_missing))]


def _align_to_union(
    specs: Sequence[tuple[str, Sequence[Mapping[str, Any]]]],
) -> list[dict[str, Any]]:
    """Share one timeline. A series missing a date gets ``{"time"}`` only."""
    lookups: list[tuple[str, dict[str, float | None]]] = []
    days: set[str] = set()
    for label, rows in specs:
        values: dict[str, float | None] = {}
        for row in rows:
            day = str(row["time"])
            if "value" in row and row["value"] is not None:
                values[day] = row["value"]
            else:
                values[day] = None
            days.add(day)
        lookups.append((label, values))
    ordered = sorted(days)
    aligned: list[dict[str, Any]] = []
    for label, values in lookups:
        series_points: list[dict[str, Any]] = []
        for day in ordered:
            if values.get(day) is not None:
                series_points.append({"time": day, "value": values[day]})
            else:
                series_points.append({"time": day})
        aligned.append({"label": label, "points": series_points})
    return aligned


def _recession_bands(bands: Sequence[Mapping[str, Any]] | None) -> list[dict[str, str]]:
    """Inclusive ``YYYY-MM-DD`` intervals. Invalid or inverted spans are dropped."""
    cleaned: list[dict[str, str]] = []
    for band in bands or ():
        start = observation_day(band.get("start"))
        end = observation_day(band.get("end"))
        if start is None or end is None or start > end:
            continue
        cleaned.append({"start": start.isoformat(), "end": end.isoformat()})
    return cleaned


def build_market_chart_payload(
    points: Sequence[Mapping[str, Any]] | None = None,
    *,
    series_label: str = "Value",
    series: Sequence[Mapping[str, Any]] | None = None,
    ranges: bool = False,
    value_format: str = "number",
    reference_price: float | None = None,
    keep_missing: bool = False,
    recession_bands: Sequence[Mapping[str, Any]] | None = None,
    align_union: bool = True,
) -> dict[str, Any]:
    """Data passed to the chart, before the component mount.

    ``series`` items are ``{"label", "points"}``. Each points sequence is
    normalized with :func:`time_series_points`. When ``series`` is omitted,
    ``points`` and ``series_label`` are the single series. Dates are
    ``YYYY-MM-DD`` strings. With ``align_union``, a date present on another
    series becomes a whitespace point so the crosshair shares one timeline.
    ``align_union=False`` keeps each series on its own observation dates.
    That is required for mixed frequencies: inserting the other series' dates
    would split one line into a fragment per print.
    """
    specs = _series_specs(points, series_label, series, keep_missing=keep_missing)
    plotted = _align_to_union(specs) if align_union else [
        {"label": label, "points": list(rows)} for label, rows in specs
    ]
    sources = list(series) if series is not None else [{"label": series_label}]
    for index, row in enumerate(plotted):
        source = sources[index] if index < len(sources) else {}
        slot = source.get("color_index")
        if slot is not None:
            row["colorIndex"] = int(slot)
        style = source.get("style")
        if style:
            row["style"] = str(style)
        price_scale = source.get("price_scale")
        if price_scale:
            row["priceScale"] = str(price_scale)
    payload: dict[str, Any] = {
        "series": plotted,
        "ranges": bool(ranges),
        "value_format": value_format,
    }
    if reference_price is not None:
        payload["reference_price"] = float(reference_price)
    cleaned_bands = _recession_bands(recession_bands)
    if cleaned_bands:
        payload["recession_bands"] = cleaned_bands
    return payload


def _component():
    return chart_component(
        "market_chart",
        html=_HTML,
        css=_CSS,
        library_js=_FRONTEND / "lightweight-charts.standalone.production.js",
        chart_js=_FRONTEND / "chart.js",
        inline_js=_JS,
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
    reference_price: float | None = None,
    keep_missing: bool = False,
    recession_bands: Sequence[Mapping[str, Any]] | None = None,
    align_union: bool = True,
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
        reference_price=reference_price,
        keep_missing=keep_missing,
        recession_bands=recession_bands,
        align_union=align_union,
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
    with span("market_chart.mount"):
        _component()(
            data=data,
            key=key,
            width="stretch",
            height=resolved_height,
        )
