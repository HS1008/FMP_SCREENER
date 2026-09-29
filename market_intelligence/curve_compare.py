"""Same-date complete Treasury curve selection for the Rates & Curve page.

A comparison curve is one observation date that has every required nominal tenor.
Missing prints (weekends, holidays, partial days) resolve to the latest complete
curve on or before the requested date. Tenors are never mixed across dates.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from market_intelligence.catalog import CURVE_TENORS, TIPS_TENORS
from market_intelligence.source_resolve import EQUIVALENTS
from market_intelligence.transforms import shift_months
from market_intelligence.treasury_xml import COMPLETE_NOMINAL_TENORS

COMPARE_NONE = "None"
COMPARE_PRIOR = "Prior session"
COMPARE_WEEK = "1 week"
COMPARE_MONTH = "1 month"
COMPARE_CUSTOM = "Custom date"
COMPARE_OPTIONS = (COMPARE_NONE, COMPARE_PRIOR, COMPARE_WEEK, COMPARE_MONTH, COMPARE_CUSTOM)

REASON_NO_CURVE = "no_complete_curve_on_or_before"
REASON_AFTER_CURRENT = "requested_after_current_curve"

NO_CURVE_MESSAGE = "No complete Treasury curve is available on or before the selected date."
AFTER_CURRENT_MESSAGE = "The comparison date cannot be later than the current Treasury curve."


def nominal_curve_series() -> dict[str, tuple[str, ...]]:
    """Tenor -> canonical series and stored equivalents (Treasury XML, then FRED)."""
    mapping: dict[str, tuple[str, ...]] = {}
    for tenor in COMPLETE_NOMINAL_TENORS:
        series_id = CURVE_TENORS[tenor]
        mapping[tenor] = EQUIVALENTS.get(series_id, (series_id,))
    return mapping


def parse_curve_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def format_curve_date(value: date | str | None) -> str:
    parsed = parse_curve_date(value)
    if parsed is None:
        return "—"
    return "{0} {1}, {2}".format(parsed.strftime("%b"), parsed.day, parsed.year)


def source_display(source_ids: Sequence[str] | None) -> str:
    labels = []
    for source_id in source_ids or []:
        if source_id == "TREASURY":
            label = "U.S. Treasury"
        elif source_id == "FRED":
            label = "FRED"
        else:
            label = str(source_id)
        if label not in labels:
            labels.append(label)
    return ", ".join(labels) if labels else "—"


def comparison_target(mode: str, current: date, custom: date | None = None) -> date | None:
    """Calendar anchor for a quick or custom comparison. ``None`` means no comparison."""
    if mode == COMPARE_NONE:
        return None
    if mode == COMPARE_PRIOR:
        return current - timedelta(days=1)
    if mode == COMPARE_WEEK:
        return current - timedelta(days=7)
    if mode == COMPARE_MONTH:
        return shift_months(current, -1)
    if mode == COMPARE_CUSTOM:
        return custom
    raise ValueError("unknown comparison mode: {0}".format(mode))


def resolve_complete_date(
    complete_dates: Sequence[date],
    target: date,
    *,
    not_after: date | None = None,
) -> dict[str, Any]:
    """Pick the latest complete curve date on or before ``target``.

    ``not_after`` is the current curve date. A target after that date is rejected
    so a comparison cannot resolve later than the curve on screen.
    """
    requested = target
    if not_after is not None and target > not_after:
        return {
            "requested_date": requested,
            "effective_date": None,
            "fallback": False,
            "found": False,
            "reason": REASON_AFTER_CURRENT,
        }
    eligible = [day for day in complete_dates if day <= target and (not_after is None or day <= not_after)]
    if not eligible:
        return {
            "requested_date": requested,
            "effective_date": None,
            "fallback": False,
            "found": False,
            "reason": REASON_NO_CURVE,
        }
    effective = max(eligible)
    return {
        "requested_date": requested,
        "effective_date": effective,
        "fallback": effective != requested,
        "found": True,
        "reason": None,
    }


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def tips_observation_dates(by_series: Mapping[str, Mapping[date, Any]]) -> list[date]:
    """Dates with at least one non-null TIPS tenor. A partial day still counts."""
    days: set[date] = set()
    for series in by_series.values():
        for day, value in series.items():
            if _finite(value) is not None:
                days.add(day)
    return sorted(days)


def tips_points_for_date(by_series: Mapping[str, Mapping[date, Any]], curve_date: date) -> list[dict[str, Any]]:
    """One coherent TIPS date. A tenor missing on ``curve_date`` stays missing.

    Other dates are not read. This is not the nominal complete-curve rule and
    it does not backward-fill an individual tenor.
    """
    points: list[dict[str, Any]] = []
    for tenor, series_id in TIPS_TENORS.items():
        value = _finite((by_series.get(series_id) or {}).get(curve_date))
        points.append(
            {
                "tenor": tenor,
                "series_id": series_id,
                "yield_pct": value,
                "observation_date": curve_date.isoformat() if value is not None else None,
            }
        )
    return points


FED_FUNDS_UNAVAILABLE = "Fed funds target-range history unavailable for this date."


def resolve_fed_funds_target(
    lower_by_date: Mapping[date, Any],
    upper_by_date: Mapping[date, Any],
    curve_date: date,
) -> dict[str, Any] | None:
    """Latest same-date DFEDTARL and DFEDTARU pair on or before ``curve_date``.

    The target range is a policy state that stays in force until it changes.
    This backward-as-of lookup is only for that policy target. It is not a fill
    rule for Treasury tenors, and it never reads a date after ``curve_date``.
    The two limits must share one observation date. An inverted pair is skipped
    rather than swapped into a fabricated range.
    """
    shared: list[date] = []
    for day in set(lower_by_date) & set(upper_by_date):
        if day > curve_date:
            continue
        lower = _finite(lower_by_date.get(day))
        upper = _finite(upper_by_date.get(day))
        if lower is None or upper is None or lower > upper:
            continue
        shared.append(day)
    if not shared:
        return None
    effective = max(shared)
    lower = float(lower_by_date[effective])
    upper = float(upper_by_date[effective])
    return {
        "available": True,
        "curve_date": curve_date.isoformat(),
        "effective_date": effective.isoformat(),
        "lower": lower,
        "upper": upper,
        "carried": effective != curve_date,
    }


def _pct_label(value: float) -> str:
    return "{0:.2f}%".format(value)


def _range_view(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    if payload.get("available") is False:
        return None
    lower = _finite(payload.get("lower"))
    upper = _finite(payload.get("upper"))
    if lower is None or upper is None or lower > upper:
        return None
    return {
        "lower": lower,
        "upper": upper,
        "effective_date": payload.get("effective_date"),
        "curve_date": payload.get("curve_date"),
        "carried": bool(payload.get("carried")),
    }


def _same_range(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return round(float(left["lower"]), 4) == round(float(right["lower"]), 4) and round(float(left["upper"]), 4) == round(float(right["upper"]), 4)


def yield_change_bps(current: Any, comparison: Any) -> float | None:
    """Current minus comparison, in basis points.

    Stored Treasury and TIPS yields are percentage points (4.25 means 4.25%),
    so one percentage point is 100 basis points.
    """
    left = _finite(current)
    right = _finite(comparison)
    if left is None or right is None:
        return None
    return (left - right) * 100.0


def format_yield_pct(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "—"
    return "{0:.2f}%".format(number)


def format_change_bps(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "—"
    return "{0:+.0f} bps".format(number)


def curve_tooltip_lines(
    tenor: str,
    current: Any,
    comparison: Any = None,
    *,
    comparing: bool = False,
    current_label: str = "Current",
    comparison_label: str = "Comparison",
) -> list[str]:
    """Hover lines for one tenor. Comparison fields are omitted when comparison is off."""
    lines = [str(tenor), "{0}: {1}".format(current_label, format_yield_pct(current))]
    if not comparing:
        return lines
    lines.append("{0}: {1}".format(comparison_label, format_yield_pct(comparison)))
    lines.append("Change: {0}".format(format_change_bps(yield_change_bps(current, comparison))))
    return lines


def aligned_curve_changes(
    categories: Sequence[str],
    current_values: Sequence[Any],
    comparison_values: Sequence[Any],
) -> list[float | None]:
    """Per-tenor basis-point change in category order. A missing leg stays missing."""
    changes: list[float | None] = []
    for index, _tenor in enumerate(categories):
        current = current_values[index] if index < len(current_values) else None
        comparison = comparison_values[index] if index < len(comparison_values) else None
        changes.append(yield_change_bps(current, comparison))
    return changes


# Long minus short, matching ``curve.slope_*`` (percent points × 100 = bps).
# The 2s5s10s fly is 2×5Y − 2Y − 10Y. 5s10s30s uses that same body-minus-wings sign.
SPREAD_FORMULAS: dict[str, str] = {
    "2s10s": "DGS10 - DGS2",
    "2s5s10s": "2*DGS5 - DGS2 - DGS10",
    "5s30s": "DGS30 - DGS5",
    "3m2s": "DGS2 - DGS3MO",
    "3m10s": "DGS10 - DGS3MO",
    "5s10s30s": "2*DGS10 - DGS5 - DGS30",
}


_SPREAD_LEGS: dict[str, tuple[str, ...]] = {
    "2s10s": ("DGS10", "DGS2"),
    "2s5s10s": ("DGS5", "DGS2", "DGS10"),
    "5s30s": ("DGS30", "DGS5"),
    "3m2s": ("DGS2", "DGS3MO"),
    "3m10s": ("DGS10", "DGS3MO"),
    "5s10s30s": ("DGS10", "DGS5", "DGS30"),
}


def aligned_spread_history(name: str, histories: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """Same-date spread points from yield histories. A missing leg is omitted, not zero."""
    formula = SPREAD_FORMULAS.get(name)
    legs = _SPREAD_LEGS.get(name)
    if formula is None or legs is None:
        raise ValueError("unknown spread formula: {0}".format(name))
    by_series: dict[str, dict[str, Any]] = {}
    dates: set[str] = set()
    for series_id in legs:
        points: dict[str, Any] = {}
        for row in histories.get(series_id) or []:
            raw_day = row.get("as_of") or row.get("observation_date")
            if raw_day is None or row.get("value") is None:
                continue
            day = str(raw_day)[:10]
            points[day] = row.get("value")
            dates.add(day)
        by_series[series_id] = points
    rows: list[dict[str, Any]] = []
    for day in sorted(dates):
        levels = {series_id: by_series[series_id].get(day) for series_id in legs}
        if any(value is None for value in levels.values()):
            continue
        value = spread_bps(formula, levels)
        if value is None:
            continue
        rows.append({"as_of": day, "value": value})
    return rows


def spread_bps(formula: str, levels: Mapping[str, Any]) -> float | None:
    """Evaluate one stored spread formula. Yields are percentage points. A missing leg is missing."""
    if formula == SPREAD_FORMULAS["2s10s"]:
        return yield_change_bps(levels.get("DGS10"), levels.get("DGS2"))
    if formula == SPREAD_FORMULAS["5s30s"]:
        return yield_change_bps(levels.get("DGS30"), levels.get("DGS5"))
    if formula == SPREAD_FORMULAS["3m2s"]:
        return yield_change_bps(levels.get("DGS2"), levels.get("DGS3MO"))
    if formula == SPREAD_FORMULAS["3m10s"]:
        return yield_change_bps(levels.get("DGS10"), levels.get("DGS3MO"))
    if formula == SPREAD_FORMULAS["2s5s10s"]:
        body = _finite(levels.get("DGS5"))
        left = _finite(levels.get("DGS2"))
        right = _finite(levels.get("DGS10"))
        if body is None or left is None or right is None:
            return None
        return ((2.0 * body) - left - right) * 100.0
    if formula == SPREAD_FORMULAS["5s10s30s"]:
        body = _finite(levels.get("DGS10"))
        left = _finite(levels.get("DGS5"))
        right = _finite(levels.get("DGS30"))
        if body is None or left is None or right is None:
            return None
        return ((2.0 * body) - left - right) * 100.0
    raise ValueError("unknown spread formula: {0}".format(formula))


def _range_notes(view: Mapping[str, Any], *, comparison: bool) -> list[str]:
    curve_day = format_curve_date(view.get("curve_date"))
    title = "Compare Fed funds target" if comparison else "Fed funds target"
    if comparison or view.get("carried"):
        title = "{0} ({1})".format(title, curve_day)
    notes = [
        title,
        "Lower: {0}".format(_pct_label(float(view["lower"]))),
        "Upper: {0}".format(_pct_label(float(view["upper"]))),
    ]
    if view.get("carried") and view.get("effective_date"):
        notes.append("In effect on {0}; set {1}.".format(curve_day, format_curve_date(view.get("effective_date"))))
    return notes


def fed_funds_overlay(
    current: Mapping[str, Any] | None,
    comparison: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Annotation payload for the nominal Treasury curve.

    The range is not a tenor. Identical current and comparison ranges collapse
    to one band. A missing range is omitted rather than drawn as zero.
    """
    current_view = _range_view(current)
    compare_view = _range_view(comparison)
    if current_view is None:
        return {
            "bands": [],
            "notes": [],
            "caption": FED_FUNDS_UNAVAILABLE,
            "unchanged": False,
        }
    unchanged = compare_view is not None and _same_range(current_view, compare_view)
    current_legend = "Fed Funds Range" if unchanged else "Fed Funds Range — Current"
    bands = [
        {
            "role": "current",
            "lower": current_view["lower"],
            "upper": current_view["upper"],
            "lower_label": "Fed funds lower {0}".format(_pct_label(current_view["lower"])),
            "upper_label": "Fed funds upper {0}".format(_pct_label(current_view["upper"])),
            "legend_label": current_legend,
            "fill": True,
        }
    ]
    notes = _range_notes(current_view, comparison=False)
    captions = [
        "Fed funds target on {0} — lower {1}, upper {2}.".format(
            format_curve_date(current_view.get("curve_date")),
            _pct_label(current_view["lower"]),
            _pct_label(current_view["upper"]),
        )
    ]
    if current_view.get("carried"):
        captions.append("Range in effect on this curve date; set {0}.".format(format_curve_date(current_view.get("effective_date"))))
    if unchanged and compare_view is not None:
        captions.append("Fed funds target range is unchanged versus {0}.".format(format_curve_date(compare_view.get("curve_date"))))
        notes.append("Unchanged versus {0}.".format(format_curve_date(compare_view.get("curve_date"))))
    elif compare_view is not None:
        bands.append(
            {
                "role": "compare",
                "lower": compare_view["lower"],
                "upper": compare_view["upper"],
                "lower_label": "Compare lower {0}".format(_pct_label(compare_view["lower"])),
                "upper_label": "Compare upper {0}".format(_pct_label(compare_view["upper"])),
                "legend_label": "Fed Funds Range — Comparison",
                "fill": True,
            }
        )
        notes.extend(_range_notes(compare_view, comparison=True))
        captions.append(
            "Compare {0} — lower {1}, upper {2}.".format(
                format_curve_date(compare_view.get("curve_date")),
                _pct_label(compare_view["lower"]),
                _pct_label(compare_view["upper"]),
            )
        )
    elif comparison is not None:
        captions.append("Fed funds target-range history unavailable for the comparison date.")
    return {
        "bands": bands,
        "notes": notes,
        "caption": " ".join(captions),
        "unchanged": unchanged,
    }


def empty_curve_lookup(*, requested: date | None = None, reason: str | None = None, earliest: date | None = None, latest: date | None = None) -> dict[str, Any]:
    return {
        "requested_date": requested.isoformat() if requested else None,
        "effective_date": None,
        "fallback": False,
        "found": False,
        "reason": reason,
        "curve": [],
        "source_ids": [],
        "earliest_complete_date": earliest.isoformat() if earliest else None,
        "latest_complete_date": latest.isoformat() if latest else None,
    }


__all__ = [
    "AFTER_CURRENT_MESSAGE",
    "COMPARE_CUSTOM",
    "COMPARE_MONTH",
    "COMPARE_NONE",
    "COMPARE_OPTIONS",
    "COMPARE_PRIOR",
    "COMPARE_WEEK",
    "NO_CURVE_MESSAGE",
    "REASON_AFTER_CURRENT",
    "REASON_NO_CURVE",
    "comparison_target",
    "FED_FUNDS_UNAVAILABLE",
    "empty_curve_lookup",
    "fed_funds_overlay",
    "SPREAD_FORMULAS",
    "aligned_curve_changes",
    "aligned_spread_history",
    "curve_tooltip_lines",
    "format_change_bps",
    "format_curve_date",
    "format_yield_pct",
    "nominal_curve_series",
    "spread_bps",
    "yield_change_bps",
    "parse_curve_date",
    "resolve_complete_date",
    "resolve_fed_funds_target",
    "source_display",
    "tips_observation_dates",
    "tips_points_for_date",
]
