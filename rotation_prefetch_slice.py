"""Slice a batched long-format rotation price dataframe for one sector (no rotation_engine imports)."""

from __future__ import annotations

import pandas as pd


def slice_sector_rotation_prices(
    prefetched: pd.DataFrame | None,
    sector_symbols: tuple[str, ...],
    benchmark: str,
) -> pd.DataFrame | None:
    """Return a copy with only symbols for this sector, or None if slice is unusable."""
    if prefetched is None or prefetched.empty or "symbol" not in prefetched.columns:
        return None
    need = frozenset(str(x).upper().strip() for x in sector_symbols)
    b = str(benchmark).upper().strip()
    su = prefetched["symbol"].astype(str).str.upper()
    out = prefetched.loc[su.isin(need)].copy()
    if out.empty or b not in set(out["symbol"].astype(str).str.upper()):
        return None
    return out
