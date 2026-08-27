"""
Trailing daily return correlation to SPY for sector / industry rotation heatmaps.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

CORR_COL = "Corr vs SPY"
DEFAULT_CORR_WINDOW = 63
SPY_SYMBOL = "SPY"
# Bump when rotation metrics / heatmap columns change (invalidates Streamlit + precomputed bundles).
ROTATION_BUNDLE_REVISION = "corr-spy-v1"


def rotation_cache_revision(price_revision: str) -> str:
    """Combine on-disk price cache fingerprint with rotation logic revision."""
    return f"{price_revision}|{ROTATION_BUNDLE_REVISION}"


def bundle_includes_corr_vs_spy(bundle: dict | None) -> bool:
    """True if a cached or precomputed bundle already has the correlation column."""
    if not isinstance(bundle, dict) or not bundle.get("ok"):
        return False
    for key in ("heatmap", "metrics"):
        df = bundle.get(key)
        if isinstance(df, pd.DataFrame) and CORR_COL in df.columns:
            return True
    return False


def _min_corr_observations(window: int) -> int:
    return max(20, int(window * 0.67))


def trailing_corr_to_spy_from_wide(
    wide: pd.DataFrame,
    tickers: str | tuple[str, ...],
    *,
    spy_symbol: str = SPY_SYMBOL,
    window: int = DEFAULT_CORR_WINDOW,
) -> float:
    """
    Trailing Pearson correlation of daily pct returns vs SPY.

    For multiple tickers, uses an equal-weight basket of member daily returns
    (rows require all basket members and SPY to have valid returns).
    """
    spy = str(spy_symbol).upper().strip()
    if wide is None or wide.empty or spy not in wide.columns:
        return float("nan")

    if isinstance(tickers, str):
        tks: tuple[str, ...] = (str(tickers).upper().strip(),)
    else:
        tks = tuple(str(t).upper().strip() for t in tickers)

    if not tks or any(t not in wide.columns for t in tks):
        return float("nan")

    cols = list(tks) + [spy]
    prices = wide.loc[:, cols].apply(pd.to_numeric, errors="coerce")
    rets = prices.pct_change()

    if len(tks) == 1:
        basket_ret = rets[tks[0]]
    else:
        member_rets = rets[list(tks)]
        basket_ret = member_rets.dropna(how="any").mean(axis=1)

    spy_ret = rets[spy]
    aligned = pd.concat([basket_ret.rename("b"), spy_ret.rename("s")], axis=1).dropna()
    min_obs = _min_corr_observations(window)
    tail = aligned.tail(window)
    if len(tail) < min_obs:
        return float("nan")
    corr = tail["b"].corr(tail["s"])
    if corr is None or not np.isfinite(float(corr)):
        return float("nan")
    return float(corr)


def heatmap_display_columns(metric_cols: tuple[str, ...]) -> tuple[str, ...]:
    """RS percentage columns plus correlation (not scaled as percent)."""
    return (*metric_cols, CORR_COL)


def build_rotation_heatmap_table(
    metrics_df: pd.DataFrame,
    metric_cols: tuple[str, ...],
) -> pd.DataFrame:
    """
    Heatmap table: RS columns as percentage points; ``Corr vs SPY`` as decimal correlation.
    """
    display_cols = heatmap_display_columns(metric_cols)
    if metrics_df.empty:
        return pd.DataFrame(columns=display_cols)

    df = metrics_df.copy()
    if "Industry_label" not in df.columns:
        df["Industry_label"] = df["Industry"].astype(str) + " (" + df["ETF"].astype(str) + ")"

    present = [c for c in display_cols if c in df.columns]
    out = df.set_index("Industry_label")[present].copy()
    for c in metric_cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce") * 100.0
    if CORR_COL in out.columns:
        out[CORR_COL] = pd.to_numeric(out[CORR_COL], errors="coerce")
    return out.sort_values("3M RS %", ascending=False, na_position="last")


def metrics_table_columns(metric_cols: tuple[str, ...]) -> tuple[str, ...]:
    """Standard detail table columns including correlation."""
    return ("ETF", "Industry", *metric_cols, CORR_COL)
