"""User-facing calendar dates for the dashboard.

Long-range history uses ``MM/DD/YYYY``. Intraday stamps keep the clock and
use the same calendar-date portion. Stored keys and provider payloads stay
ISO dates; this module is for labels, selectors, and chart readouts.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from market_intelligence.components.market_chart import observation_day

MISSING_DATE = "—"
DATE_INPUT_FORMAT = "MM/DD/YYYY"


def format_calendar_date(value: Any) -> str:
    """Format one calendar day as ``MM/DD/YYYY``.

    Date-only strings keep their first ten characters. A ``datetime`` uses its
    own calendar date and is not shifted through UTC. Missing values are an
    em dash.
    """
    if isinstance(value, datetime):
        day: date | None = value.date()
    elif isinstance(value, date):
        day = value
    else:
        day = observation_day(value)
    if day is None:
        return MISSING_DATE
    return "{0:02d}/{1:02d}/{2:04d}".format(day.month, day.day, day.year)
