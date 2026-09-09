"""Helpers so Streamlit tables stay Arrow-safe.

Streamlit converts dataframes with pyarrow. A column that mixes formatted
strings with numpy scalars (for example trade_count as numpy.int64) raises
ArrowTypeError and blanks the whole Strategy Monitor page.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    try:
        if pd.isna(value):
            return "—"
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item") and not isinstance(value, (bytes, str)):
        try:
            value = value.item()
        except Exception:
            pass
    text = str(value).strip()
    return text if text else "—"


def arrow_safe_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with every cell as a plain string."""
    out = frame.copy()
    for column in out.columns:
        out[column] = out[column].map(_cell)
    return out
