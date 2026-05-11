"""
Basic Materials industry ETF rotation vs XLB (relative strength heatmap).

Uses dividend-adjusted prices from ``data_loader.get_price_histories_long``.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

import config
import data_loader
import rotation_prefetch_slice

BENCHMARK = "XLB"

# Industry ETF -> display industry name (symbol added in heatmap index)
INDUSTRY_ETF_MAP: dict[str, str] = {
    "XME": "Metals & Mining",
    "GDX": "Gold Miners",
    "SIL": "Silver Miners",
    "COPX": "Copper Miners",
    "SLX": "Steel",
    "URA": "Uranium",
    "PYZ": "Chemicals",
    "WOOD": "Timber & Forestry",
    "MOO": "Agriculture Inputs / Fertilizers",
}

ALL_ROTATION_SYMBOLS: tuple[str, ...] = (BENCHMARK,) + tuple(INDUSTRY_ETF_MAP.keys())

ROTATION_LOOKBACK_CAL_DAYS: int = 520

TRADING_1W: int = int(config.TRADING_DAYS_1W)
TRADING_1M: int = int(config.TRADING_DAYS_1M)
TRADING_3M: int = int(config.TRADING_DAYS_3M)
TRADING_6M: int = 126
TRADING_12M: int = int(config.TRADING_DAYS_1Y)

METRIC_COLS: tuple[str, ...] = (
    "1W RS %",
    "1M RS %",
    "3M RS %",
    "6M RS %",
    "12M RS %",
    "RS vs 50 DMA %",
    "RS vs 200 DMA %",
)


def get_materials_rotation_prices(
    session: Any,
    api_key: str,
    *,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Long-format dividend-adjusted closes for XLB and industry ETFs.

    Columns: ``date``, ``symbol``, ``adjClose``.
    """
    end = date.today()
    start = end - timedelta(days=ROTATION_LOOKBACK_CAL_DAYS)
    return data_loader.get_price_histories_long(
        session, api_key, ALL_ROTATION_SYMBOLS, start, end, force_refresh=force_refresh
    )


def _rs_pct_change(rs: pd.Series, trading_days: int) -> float:
    """Percentage change in RS ratio over ``trading_days`` bars (ending at last row)."""
    rs = pd.to_numeric(rs, errors="coerce").dropna()
    n = int(trading_days)
    if len(rs) < n + 1:
        return float("nan")
    a = float(rs.iloc[-1])
    b = float(rs.iloc[-1 - n])
    if b == 0 or math.isnan(a) or math.isnan(b):
        return float("nan")
    return a / b - 1.0


def _rs_vs_dma_pct(rs: pd.Series, window: int) -> float:
    """Latest RS / trailing ``window``-day mean RS - 1."""
    rs = pd.to_numeric(rs, errors="coerce").dropna()
    if len(rs) < window:
        return float("nan")
    ma = rs.rolling(window, min_periods=window).mean()
    last_ma = float(ma.iloc[-1])
    last_rs = float(rs.iloc[-1])
    if last_ma == 0 or math.isnan(last_ma) or math.isnan(last_rs):
        return float("nan")
    return last_rs / last_ma - 1.0


def calculate_relative_strength_metrics(price_df: pd.DataFrame) -> pd.DataFrame:
    """
    RS ratio = ETF adjClose / XLB adjClose; horizon returns vs XLB and RS vs moving averages.

    Metric columns store **decimals** (e.g. 0.05 = 5%); heatmap builder converts to percentage points.
    """
    if price_df.empty or "symbol" not in price_df.columns:
        return pd.DataFrame(
            columns=["ETF", "Industry", *METRIC_COLS],
        )

    wide = price_df.pivot(index="date", columns="symbol", values="adjClose").sort_index()
    if BENCHMARK not in wide.columns:
        return pd.DataFrame(columns=["ETF", "Industry", *METRIC_COLS])

    bench = pd.to_numeric(wide[BENCHMARK], errors="coerce")
    rows: list[dict[str, Any]] = []

    for etf, industry in INDUSTRY_ETF_MAP.items():
        if etf not in wide.columns:
            continue
        etf_px = pd.to_numeric(wide[etf], errors="coerce")
        rs = etf_px / bench
        rs = rs.replace([np.inf, -np.inf], np.nan).dropna()
        if rs.empty:
            continue

        row = {
            "ETF": etf,
            "Industry": industry,
            "1W RS %": _rs_pct_change(rs, TRADING_1W),
            "1M RS %": _rs_pct_change(rs, TRADING_1M),
            "3M RS %": _rs_pct_change(rs, TRADING_3M),
            "6M RS %": _rs_pct_change(rs, TRADING_6M),
            "12M RS %": _rs_pct_change(rs, TRADING_12M),
            "RS vs 50 DMA %": _rs_vs_dma_pct(rs, 50),
            "RS vs 200 DMA %": _rs_vs_dma_pct(rs, 200),
        }
        rows.append(row)

    return pd.DataFrame(rows)


def build_rotation_heatmap_table(metrics_df: pd.DataFrame) -> pd.DataFrame:
    """
    Heatmap-ready table: index = ``Industry (ETF)``, values = percentage points, sorted by 3M RS %.
    """
    if metrics_df.empty:
        return pd.DataFrame(columns=list(METRIC_COLS))

    df = metrics_df.copy()
    df["Industry_label"] = df["Industry"].astype(str) + " (" + df["ETF"].astype(str) + ")"
    out = df.set_index("Industry_label")[list(METRIC_COLS)].copy()
    for c in METRIC_COLS:
        out[c] = pd.to_numeric(out[c], errors="coerce") * 100.0
    out = out.sort_values("3M RS %", ascending=False, na_position="last")
    return out


def _build_rs_ratio_history(price_df: pd.DataFrame) -> pd.DataFrame:
    """Wide RS ratio levels (not %) by date for optional detail view."""
    if price_df.empty:
        return pd.DataFrame()
    wide = price_df.pivot(index="date", columns="symbol", values="adjClose").sort_index()
    if BENCHMARK not in wide.columns:
        return pd.DataFrame()
    bench = pd.to_numeric(wide[BENCHMARK], errors="coerce")
    cols: dict[str, pd.Series] = {}
    for etf in INDUSTRY_ETF_MAP:
        if etf not in wide.columns:
            continue
        rs = pd.to_numeric(wide[etf], errors="coerce") / bench
        cols[f"{etf}_vs_{BENCHMARK}"] = rs
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(cols).dropna(how="all").sort_index()


def build_materials_rotation_bundle(
    session: Any,
    api_key: str,
    *,
    force_refresh: bool = False,
    prefetched_prices_long: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Prices, RS metrics, heatmap table, and optional RS ratio history."""
    try:
        sliced = rotation_prefetch_slice.slice_sector_rotation_prices(
            prefetched_prices_long, ALL_ROTATION_SYMBOLS, BENCHMARK
        )
        prices = (
            sliced
            if sliced is not None
            else get_materials_rotation_prices(session, api_key, force_refresh=force_refresh)
        )
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "as_of": None,
            "prices": pd.DataFrame(),
            "metrics": pd.DataFrame(),
            "heatmap": pd.DataFrame(),
            "rs_ratio_history": pd.DataFrame(),
        }

    if prices.empty or BENCHMARK not in prices["symbol"].unique():
        return {
            "ok": False,
            "error": "Missing price history for XLB or rotation ETFs.",
            "as_of": None,
            "prices": prices,
            "metrics": pd.DataFrame(),
            "heatmap": pd.DataFrame(),
            "rs_ratio_history": pd.DataFrame(),
        }

    metrics = calculate_relative_strength_metrics(prices)
    if metrics.empty:
        return {
            "ok": False,
            "error": "Could not compute RS metrics (insufficient overlapping history vs XLB).",
            "as_of": pd.to_datetime(prices["date"], errors="coerce").max().date()
            if not prices.empty
            else date.today(),
            "prices": prices,
            "metrics": metrics,
            "heatmap": pd.DataFrame(),
            "rs_ratio_history": pd.DataFrame(),
        }

    heatmap = build_rotation_heatmap_table(metrics)
    rs_hist = _build_rs_ratio_history(prices)
    as_of = pd.to_datetime(prices["date"], errors="coerce").max()
    as_of_d = as_of.date() if pd.notna(as_of) else date.today()

    return {
        "ok": True,
        "error": None,
        "as_of": as_of_d,
        "prices": prices,
        "metrics": metrics,
        "heatmap": heatmap,
        "rs_ratio_history": rs_hist,
    }
