"""Same-date complete Treasury curve selection for the Rates & Curve page.

A comparison curve is one observation date that has every required nominal tenor.
Missing prints (weekends, holidays, partial days) resolve to the latest complete
curve on or before the requested date. Tenors are never mixed across dates.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from market_intelligence.catalog import CURVE_TENORS
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
    "empty_curve_lookup",
    "format_curve_date",
    "nominal_curve_series",
    "parse_curve_date",
    "resolve_complete_date",
    "source_display",
]
