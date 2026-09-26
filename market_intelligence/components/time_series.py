"""Mount stored time series on the shared Lightweight Charts component."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from market_intelligence.components.market_chart import lightweight_market_chart, observation_day, time_series_points


def mount_stored_series(
    rows: list[dict[str, Any]] | pd.Series,
    *,
    label: str,
    key: str,
    x: str = "as_of",
    y: str = "value",
) -> bool:
    """Draw one dated series. Returns False when the points are not one observation per day.

    Intraday timestamps that share a calendar day stay on the caller's existing chart
    so collapsing them onto a daily axis does not change the series.
    """
    records: list[dict[str, Any]] = []
    if isinstance(rows, pd.Series):
        for stamp, value in rows.items():
            records.append({x: stamp, y: value})
    else:
        records = list(rows)
    finite = []
    for row in records:
        day = observation_day(row.get("time") if "time" in row else row.get(x))
        value = row.get(y)
        if day is None or value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        finite.append((day, value))
    points = time_series_points([{x: day, y: value} for day, value in finite])
    if not points or len(points) != len(finite):
        return False
    lightweight_market_chart(points, series_label=label, height=380, key=key)
    return True
