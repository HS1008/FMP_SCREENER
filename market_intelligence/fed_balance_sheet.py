"""Federal Reserve H.4.1 balance-sheet comparison.

Reads already stored observations. Does not call FRED, forward-fill, or
replace the current level with a historical one. Weekly prints are matched
with an as-of rule: the latest finite observation on or before the target.
"""

from __future__ import annotations

import bisect
import html
import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from market_intelligence.components.market_chart import observation_day
from market_intelligence.display_dates import format_calendar_date

MILLIONS_LABEL = "Millions of USD"
MISSING = "—"

# Official H.4.1 Wednesday levels, millions of USD. Not week averages and not
# "change" transforms. Verified against FRED series titles and units.
BALANCE_SHEET_SECTIONS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "assets",
        "Assets",
        (
            ("WALCL", "Total Assets"),
            ("TREAST", "Treasuries"),
            ("WSHOMCB", "MBS"),
            ("WSHOFADSL", "Agency Debt"),
            ("WLCFLPCL", "Primary Credit"),
        ),
    ),
    (
        "liabilities",
        "Liabilities",
        (
            ("WRBWFRBL", "Bank Reserves"),
            ("WCICL", "Currency in Circulation"),
            ("WDTGAL", "Treasury General Account (TGA)"),
            ("WLRRAL", "Reverse Repo"),
        ),
    ),
    (
        "equity",
        "Equity / Capital",
        (
            ("WCPIL", "Capital Paid In"),
            ("WCSL", "Surplus"),
        ),
    ),
)

BALANCE_SHEET_SERIES: tuple[tuple[str, str, str], ...] = tuple(
    (series_id, label, section_label)
    for _section_id, section_label, rows in BALANCE_SHEET_SECTIONS
    for series_id, label in rows
)


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


def shift_years(day: date, years: int) -> date:
    """Calendar shift. February 29 lands on February 28 in a non-leap year."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return date(day.year + years, 2, 28)


def normalize_history(rows: Sequence[Any]) -> list[tuple[date, float]]:
    """Ascending finite observations. Duplicate dates keep the last finite value.

    Missing, NaN, and infinite values are omitted. Nothing is filled forward.
    """
    by_day: dict[date, float] = {}
    for row in rows:
        if isinstance(row, Mapping):
            raw_day = row.get("as_of")
            if raw_day is None and "observation_date" in row:
                raw_day = row.get("observation_date")
            if raw_day is None and "time" in row:
                raw_day = row.get("time")
            raw_value = row.get("value")
        elif isinstance(row, tuple) and len(row) >= 2:
            raw_day, raw_value = row[0], row[1]
        else:
            continue
        day = observation_day(raw_day)
        number = _finite(raw_value)
        if day is None or number is None:
            continue
        by_day[day] = number
    return sorted(by_day.items())


def latest_observation(history: Sequence[tuple[date, float]]) -> tuple[date, float] | None:
    if not history:
        return None
    return history[-1]


def previous_observation(history: Sequence[tuple[date, float]]) -> tuple[date, float] | None:
    """The observation immediately before the latest one. A single print has none."""
    if len(history) < 2:
        return None
    return history[-2]


def observation_on_or_before(
    history: Sequence[tuple[date, float]],
    target: date | None,
) -> tuple[date, float] | None:
    """Latest finite print on or before ``target``. Never a later print."""
    if not history or target is None:
        return None
    dates = [day for day, _value in history]
    index = bisect.bisect_right(dates, target) - 1
    if index < 0:
        return None
    return history[index]


def percent_change(current: float | None, base: float | None) -> float | None:
    """``(current - base) / base * 100``. A zero or missing base is not 0%."""
    if current is None or base is None:
        return None
    if not math.isfinite(current) or not math.isfinite(base) or base == 0.0:
        return None
    result = (current - base) / base * 100.0
    if not math.isfinite(result):
        return None
    return result


def absolute_change(current: float | None, base: float | None) -> float | None:
    """``current - base`` in the series' native units. Missing inputs stay missing."""
    if current is None or base is None:
        return None
    if not math.isfinite(current) or not math.isfinite(base):
        return None
    result = current - base
    if not math.isfinite(result):
        return None
    return result


def column_headers(*, mode: str, comparing: bool) -> tuple[str, ...]:
    """Table headers. Comparison level sits beside Current only while a date is selected."""
    absolute = str(mode).lower() == "absolute"
    if comparing:
        lead = "Absolute Change From Current" if absolute else "% Change From Current"
    else:
        lead = "Absolute Change Since Last" if absolute else "% Change Since Last"
    if absolute:
        changes = (lead, "Absolute Change 1Y", "Absolute Change 3Y")
    else:
        changes = (lead, "% Change 1Y", "% Change 3Y")
    if comparing:
        return ("Component", "Current", "Comparison", *changes)
    return ("Component", "Current", *changes)


@dataclass(frozen=True)
class ComponentSnapshot:
    series_id: str
    label: str
    section_id: str
    section_label: str
    current: float | None
    current_date: date | None
    prior: float | None
    prior_date: date | None
    year_1: float | None
    year_1_date: date | None
    year_3: float | None
    year_3_date: date | None
    historical: float | None
    historical_date: date | None


def build_component(
    series_id: str,
    label: str,
    section_id: str,
    section_label: str,
    rows: Sequence[Any],
    *,
    comparison_date: date | None = None,
) -> ComponentSnapshot:
    """Current level plus since-last, 1Y, 3Y, and optional historical bases.

    1Y and 3Y are measured from the current observation, not from the
    comparison date. The historical base is the latest print on or before the
    requested date.
    """
    history = normalize_history(rows)
    current = latest_observation(history)
    prior = previous_observation(history)
    year_1 = None
    year_3 = None
    if current is not None:
        year_1 = observation_on_or_before(history, shift_years(current[0], -1))
        year_3 = observation_on_or_before(history, shift_years(current[0], -3))
        if year_1 is not None and year_1[0] == current[0]:
            year_1 = None
        if year_3 is not None and year_3[0] == current[0]:
            year_3 = None
    historical = observation_on_or_before(history, comparison_date) if comparison_date is not None else None
    if historical is not None and current is not None and historical[0] > current[0]:
        historical = None
    return ComponentSnapshot(
        series_id=series_id,
        label=label,
        section_id=section_id,
        section_label=section_label,
        current=None if current is None else current[1],
        current_date=None if current is None else current[0],
        prior=None if prior is None else prior[1],
        prior_date=None if prior is None else prior[0],
        year_1=None if year_1 is None else year_1[1],
        year_1_date=None if year_1 is None else year_1[0],
        year_3=None if year_3 is None else year_3[1],
        year_3_date=None if year_3 is None else year_3[0],
        historical=None if historical is None else historical[1],
        historical_date=None if historical is None else historical[0],
    )


def build_statement(
    histories: Mapping[str, Sequence[Any]],
    *,
    comparison_date: date | None = None,
) -> tuple[ComponentSnapshot, ...]:
    return tuple(
        build_component(
            series_id,
            label,
            section_id,
            section_label,
            histories.get(series_id) or (),
            comparison_date=comparison_date,
        )
        for section_id, section_label, rows in BALANCE_SHEET_SECTIONS
        for series_id, label in rows
    )


def statement_as_of(rows: Sequence[ComponentSnapshot]) -> date | None:
    """Latest H.4.1 print among components that have a current value."""
    dates = [row.current_date for row in rows if row.current_date is not None]
    if not dates:
        return None
    return max(dates)


def current_dates_differ(rows: Sequence[ComponentSnapshot]) -> bool:
    dates = {row.current_date for row in rows if row.current_date is not None}
    return len(dates) > 1


def comparison_lines(
    rows: Sequence[ComponentSnapshot],
    comparison_date: date | None,
) -> tuple[str, ...]:
    """Requested date plus the Wednesday print actually used."""
    if comparison_date is None:
        return ()
    lines = ["Comparison: {0}".format(format_calendar_date(comparison_date))]
    used = [row.historical_date for row in rows if row.historical_date is not None]
    if not used:
        lines.append("No H.4.1 observation on or before this date.")
        return tuple(lines)
    unique = sorted(set(used))
    if len(unique) == 1 and len(used) == len(rows):
        lines.append("Using H.4.1 observation: {0}".format(format_calendar_date(unique[0])))
    elif len(unique) == 1:
        lines.append("Using H.4.1 observation: {0}".format(format_calendar_date(unique[0])))
        lines.append("Some components have no observation on or before this date.")
    else:
        lines.append(
            "Using each component's latest H.4.1 observation on or before {0}.".format(
                format_calendar_date(comparison_date)
            )
        )
    return tuple(lines)


def displayed_change(row: ComponentSnapshot, *, mode: str, comparing: bool, horizon: str) -> float | None:
    """One cell. ``horizon`` is ``lead``, ``1y``, or ``3y``."""
    current = row.current
    if horizon == "lead":
        base = row.historical if comparing else row.prior
    elif horizon == "1y":
        base = row.year_1
    elif horizon == "3y":
        base = row.year_3
    else:
        return None
    if str(mode).lower() == "absolute":
        return absolute_change(current, base)
    return percent_change(current, base)


def format_millions(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return MISSING
    if abs(value - round(value)) < 1e-6:
        return "{0:,.0f}".format(value)
    return "{0:,.2f}".format(value)


def format_percent(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return MISSING
    return "{0:+.2f}%".format(value)


def format_absolute(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return MISSING
    if abs(value - round(value)) < 1e-6:
        return "{0:+,.0f}".format(value)
    return "{0:+,.2f}".format(value)


def _format_change(value: float | None, *, mode: str) -> str:
    if str(mode).lower() == "absolute":
        return format_absolute(value)
    return format_percent(value)


def _change_class(value: float | None) -> str:
    if value is None or not math.isfinite(value) or value == 0:
        return "num"
    if value > 0:
        return "num pos"
    return "num neg"


def _cell_title(row: ComponentSnapshot, *, comparing: bool) -> str:
    parts = []
    if row.current_date is not None:
        parts.append("Observation {0}".format(format_calendar_date(row.current_date)))
    if comparing and row.historical_date is not None:
        parts.append("Comparison observation {0}".format(format_calendar_date(row.historical_date)))
    return ". ".join(parts)


def _comparison_cell(row: ComponentSnapshot, comparison_date: date | None) -> str:
    """Level on or before the requested date. A different print date is shown in the cell."""
    level = html.escape(format_millions(row.historical))
    if (
        comparison_date is not None
        and row.historical_date is not None
        and row.historical_date != comparison_date
    ):
        level += '<span class="fed-bs-asof">{0}</span>'.format(
            html.escape(format_calendar_date(row.historical_date))
        )
    return level


def statement_html(
    rows: Sequence[ComponentSnapshot],
    *,
    mode: str,
    comparing: bool,
    comparison_date: date | None = None,
) -> str:
    """Dark-theme table. Numbers are right-aligned. There is no index column."""
    headers = column_headers(mode=mode, comparing=comparing)
    head = "".join("<th>{0}</th>".format(html.escape(label)) for label in headers)
    body: list[str] = []
    last_section = None
    span = len(headers)
    for row in rows:
        if row.section_id != last_section:
            body.append(
                '<tr class="fed-bs-section"><td colspan="{0}">{1}</td></tr>'.format(
                    span, html.escape(row.section_label)
                )
            )
            last_section = row.section_id
        lead = displayed_change(row, mode=mode, comparing=comparing, horizon="lead")
        year_1 = displayed_change(row, mode=mode, comparing=comparing, horizon="1y")
        year_3 = displayed_change(row, mode=mode, comparing=comparing, horizon="3y")
        title = _cell_title(row, comparing=comparing)
        tip = ' title="{0}"'.format(html.escape(title, quote=True)) if title else ""
        comparison = ""
        if comparing:
            comparison = '<td class="num">{0}</td>'.format(_comparison_cell(row, comparison_date))
        body.append(
            "<tr>"
            "<td{0}>{1}</td>"
            '<td class="num">{2}</td>'
            "{3}"
            '<td class="{4}">{5}</td>'
            '<td class="{6}">{7}</td>'
            '<td class="{8}">{9}</td>'
            "</tr>".format(
                tip,
                html.escape(row.label),
                html.escape(format_millions(row.current)),
                comparison,
                _change_class(lead),
                html.escape(_format_change(lead, mode=mode)),
                _change_class(year_1),
                html.escape(_format_change(year_1, mode=mode)),
                _change_class(year_3),
                html.escape(_format_change(year_3, mode=mode)),
            )
        )
    style = (
        "<style>"
        ".fed-bs{width:100%;max-width:100%;overflow-x:auto;}"
        ".fed-bs table{width:100%;border-collapse:collapse;font-size:14px;line-height:1.35;}"
        ".fed-bs th,.fed-bs td{padding:7px 8px;border-bottom:1px solid rgba(128,128,128,0.28);vertical-align:middle;}"
        ".fed-bs th{font-size:12px;font-weight:650;text-align:right;white-space:normal;}"
        ".fed-bs th:first-child,.fed-bs td:first-child{text-align:left;}"
        ".fed-bs td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;}"
        ".fed-bs-asof{display:block;font-size:11px;font-weight:500;opacity:0.75;white-space:nowrap;}"
        ".fed-bs td.pos{color:#2ecc71;}"
        ".fed-bs td.neg{color:#e74c3c;}"
        ".fed-bs tr.fed-bs-section td{padding-top:12px;font-size:12px;font-weight:700;letter-spacing:0.04em;"
        "text-transform:uppercase;background:rgba(128,128,128,0.16);border-bottom:1px solid rgba(128,128,128,0.35);}"
        "@media (max-width:720px){.fed-bs table{font-size:12px;}.fed-bs th,.fed-bs td{padding:6px 4px;}}"
        "</style>"
    )
    table = '<div class="fed-bs"><table><thead><tr>{0}</tr></thead><tbody>{1}</tbody></table></div>'.format(
        head, "".join(body)
    )
    return style + table


def history_bounds(histories: Mapping[str, Sequence[Any]]) -> tuple[date | None, date | None]:
    days: list[date] = []
    for rows in histories.values():
        for day, _value in normalize_history(rows):
            days.append(day)
    if not days:
        return None, None
    return min(days), max(days)


__all__ = [
    "BALANCE_SHEET_SECTIONS",
    "BALANCE_SHEET_SERIES",
    "MILLIONS_LABEL",
    "MISSING",
    "ComponentSnapshot",
    "absolute_change",
    "build_component",
    "build_statement",
    "column_headers",
    "comparison_lines",
    "current_dates_differ",
    "displayed_change",
    "format_absolute",
    "format_millions",
    "format_percent",
    "history_bounds",
    "latest_observation",
    "normalize_history",
    "observation_on_or_before",
    "percent_change",
    "previous_observation",
    "shift_years",
    "statement_as_of",
    "statement_html",
]
