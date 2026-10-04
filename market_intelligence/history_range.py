"""Inclusive stored-history windows for Streamlit charts.

The helpers never call FRED or Yahoo. They do not fill, interpolate, or invent
boundary observations. Quick-range buttons inside the chart zoom whatever
points the page already passed in.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime
from typing import Any, Mapping, Sequence

import streamlit as st

from market_intelligence.components.market_chart import observation_day
from market_intelligence.display_dates import DATE_INPUT_FORMAT
from market_intelligence.read_models import FULL_HISTORY_LIMIT

STORED_HISTORY_LIMIT = FULL_HISTORY_LIMIT


def filter_history_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    start: date,
    end: date,
    date_key: str = "as_of",
) -> list[dict[str, Any]]:
    """Copy rows whose observation date is inside ``[start, end]``.

    Rows outside the window are omitted. Missing values stay missing. The
    input sequence is not mutated.
    """
    if start > end:
        return []
    kept: list[dict[str, Any]] = []
    for row in rows:
        day = observation_day(row.get(date_key))
        if day is None or day < start or day > end:
            continue
        kept.append(dict(row))
    return kept


def union_history_bounds(
    groups: Sequence[Sequence[Mapping[str, Any]]],
    *,
    date_key: str = "as_of",
) -> tuple[date | None, date | None]:
    """Earliest and latest real observation across every selected series."""
    days: list[date] = []
    for rows in groups:
        for row in rows:
            if row.get("value") is None:
                continue
            day = observation_day(row.get(date_key))
            if day is not None:
                days.append(day)
    if not days:
        return None, None
    return min(days), max(days)


def align_range_selection(
    *,
    earliest: date,
    latest: date,
    previous_span: tuple[date, date] | None,
    current_from: date | None,
    current_to: date | None,
    default_start: date | None = None,
    previous_default: date | None = None,
) -> tuple[date, date]:
    """Default to the full stored span, or to ``default_start`` when one is set.

    A selection that still matches the previous full span follows a newly
    expanded or shrunk stored span. A selection that still matches the previous
    default start follows that default. Any other From date is kept, including
    a date earlier than ``default_start``. Dates outside the stored span are
    pulled back to that span. From and To are not swapped here.
    """
    preferred = earliest
    if default_start is not None and earliest <= default_start <= latest:
        preferred = default_start
    start = current_from
    end = current_to
    tracking_default = previous_default is not None and start == previous_default
    tracking_full = (
        previous_span is not None
        and start == previous_span[0]
        and not tracking_default
        and default_start is None
    )
    if start is None or tracking_default:
        start = preferred
    elif tracking_full:
        start = earliest
    if end is None or (previous_span is not None and end == previous_span[1]):
        end = latest
    if start < earliest or start > latest:
        start = earliest
    if end < earliest or end > latest:
        end = latest
    return start, end


def chart_series_from_histories(
    selected: Sequence[tuple[str, str]],
    histories: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    start: date,
    end: date,
) -> tuple[list[dict[str, Any]], list[str]]:
    """One Lightweight series per selected id that has a value in the window.

    The second list is labels with no stored observation in the window.
    Gaps inside a series are left for the chart; nothing is forward-filled.
    """
    series: list[dict[str, Any]] = []
    empty: list[str] = []
    for series_id, label in selected:
        rows = filter_history_rows(histories.get(series_id) or [], start=start, end=end)
        points = [
            {"as_of": row.get("as_of"), "value": row.get("value")}
            for row in rows
            if row.get("value") is not None
        ]
        if points:
            series.append({"label": label, "points": points})
        else:
            empty.append(label)
    return series, empty


def ordered_selection(options: Sequence[str], selected: Sequence[str]) -> list[str]:
    """Canonical order, dropping ids that are not in ``options``."""
    chosen = set(selected)
    return [item for item in options if item in chosen]


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return observation_day(value)


def historical_date_range(
    *,
    key: str,
    earliest: date | None,
    latest: date | None,
    default_start: date | None = None,
) -> tuple[date | None, date | None]:
    """From / To selectors defaulting to the full stored span.

    ``default_start`` is the initial From date when it falls inside the stored
    span. Earlier history stays selectable. Returns ``(None, None)`` when
    nothing is stored. When From is after To, the invalid dates are returned
    and a short caption is shown. Values are not swapped.
    """
    if earliest is None or latest is None or earliest > latest:
        st.caption("No stored history for this chart.")
        return None, None
    start_key = "{0}_from".format(key)
    end_key = "{0}_to".format(key)
    span_key = "{0}_span".format(key)
    default_key = "{0}_default".format(key)
    previous = st.session_state.get(span_key)
    previous_span = previous if isinstance(previous, tuple) and len(previous) == 2 else None
    preferred = earliest
    if default_start is not None and earliest <= default_start <= latest:
        preferred = default_start
    start_default, end_default = align_range_selection(
        earliest=earliest,
        latest=latest,
        previous_span=previous_span,
        current_from=_as_date(st.session_state.get(start_key)),
        current_to=_as_date(st.session_state.get(end_key)),
        default_start=default_start,
        previous_default=_as_date(st.session_state.get(default_key)),
    )
    if _as_date(st.session_state.get(start_key)) != start_default:
        st.session_state[start_key] = start_default
    if _as_date(st.session_state.get(end_key)) != end_default:
        st.session_state[end_key] = end_default
    st.session_state[span_key] = (earliest, latest)
    st.session_state[default_key] = preferred
    left, right = st.columns(2)
    start_value = left.date_input(
        "From",
        min_value=earliest,
        max_value=latest,
        key=start_key,
        format=DATE_INPUT_FORMAT,
    )
    end_value = right.date_input(
        "To",
        min_value=earliest,
        max_value=latest,
        key=end_key,
        format=DATE_INPUT_FORMAT,
    )
    start = _as_date(start_value) or start_default
    end = _as_date(end_value) or end_default
    if start > end:
        st.caption("From must be on or before To.")
    return start, end


def pills_layout_kwargs() -> dict[str, Any]:
    """Pass ``wrap`` only when this Streamlit build accepts it.

    The production checkout venv has ``st.pills`` but rejects ``wrap``. On
    builds that support it, wrapping keeps rating tiles inside the page width.
    """
    if "wrap" in inspect.signature(st.pills).parameters:
        return {"wrap": True}
    return {}


def series_toggles(
    options: Sequence[tuple[str, str]],
    *,
    key: str,
    group_label: str,
    default: Sequence[str] | None = None,
) -> list[str]:
    """Multi-select pills with Select all / Clear all.

    Empty selection stays empty. Order follows ``options``. ``default`` is the
    initial selection; Select all still selects every option.
    """
    labels = {series_id: label for series_id, label in options}
    ids = [series_id for series_id, _label in options]
    pills_key = "{0}_pills".format(key)
    if pills_key not in st.session_state:
        chosen = list(default) if default is not None else list(ids)
        st.session_state[pills_key] = [item for item in chosen if item in ids]
    select_col, clear_col = st.columns(2)
    if select_col.button("Select all", key="{0}_select_all".format(key)):
        st.session_state[pills_key] = list(ids)
    if clear_col.button("Clear all", key="{0}_clear_all".format(key)):
        st.session_state[pills_key] = []
    selected = st.pills(
        group_label,
        ids,
        selection_mode="multi",
        format_func=lambda series_id: labels.get(series_id, series_id),
        key=pills_key,
        label_visibility="collapsed",
        **pills_layout_kwargs(),
    )
    if selected is None:
        return []
    if isinstance(selected, str):
        return ordered_selection(ids, [selected])
    return ordered_selection(ids, list(selected))
