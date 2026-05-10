"""
Utilities industry rotation vs XLU (relative strength heatmap).

Each row is an **equal-weight basket** of listed proxies, rebased to 1 on the
first aligned date, then averaged; RS vs XLU is that composite divided by XLU rebased the same way.
Uses dividend-adjusted prices from ``data_loader.get_price_histories_long``.

Breadth proxy row pairs **XLU** with **VPU** (Vanguard Utilities ETF).
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

import config
import data_loader

BENCHMARK = "XLU"

INDUSTRY_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Regulated Electric Utilities", ("NEE", "SO", "DUK", "EXC")),
    ("Independent Power Producers (IPPs)", ("VST", "CEG", "NRG")),
    ("Nuclear Energy", ("CEG", "NEE", "LEU")),
    ("Natural Gas Utilities", ("ATO", "NI", "SWX")),
    ("Renewable Utilities & Clean Power", ("NEE", "BEPC", "AES")),
    ("Water Utilities", ("AWK", "WTRG", "CWT")),
    ("Distributed Energy & Backup Power", ("BE", "GNRC")),
    ("Utility Infrastructure & Transmission", ("FE", "AEP", "XEL")),
    ("Yield-Sensitive Defensive Utilities", ("ED", "ETR", "PEG")),
    ("Utility Breadth Proxy", ("XLU", "VPU")),
)


def _unique_rotation_symbols() -> tuple[str, ...]:
    syms: set[str] = {BENCHMARK}
    for _, tickers in INDUSTRY_GROUPS:
        for t in tickers:
            syms.add(str(t).upper().strip())
    return tuple(sorted(syms))


ALL_ROTATION_SYMBOLS: tuple[str, ...] = _unique_rotation_symbols()

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


def get_utilities_rotation_prices(
    session: Any,
    api_key: str,
    *,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Long-format dividend-adjusted closes for XLU and all group proxy symbols."""
    end = date.today()
    start = end - timedelta(days=ROTATION_LOOKBACK_CAL_DAYS)
    return data_loader.get_price_histories_long(
        session, api_key, ALL_ROTATION_SYMBOLS, start, end, force_refresh=force_refresh
    )


def _rs_pct_change(rs: pd.Series, trading_days: int) -> float:
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
    rs = pd.to_numeric(rs, errors="coerce").dropna()
    if len(rs) < window:
        return float("nan")
    ma = rs.rolling(window, min_periods=window).mean()
    last_ma = float(ma.iloc[-1])
    last_rs = float(rs.iloc[-1])
    if last_ma == 0 or math.isnan(last_ma) or math.isnan(last_rs):
        return float("nan")
    return last_rs / last_ma - 1.0


def _composite_rs_vs_benchmark(
    wide: pd.DataFrame,
    tickers: tuple[str, ...],
    benchmark: str,
) -> pd.Series:
    tks = tuple(str(t).upper().strip() for t in tickers)
    for t in tks:
        if t not in wide.columns:
            return pd.Series(dtype=float)
    if benchmark not in wide.columns:
        return pd.Series(dtype=float)

    sub = wide.loc[:, list(tks)].apply(pd.to_numeric, errors="coerce")
    bench = pd.to_numeric(wide[benchmark], errors="coerce")
    aligned = pd.concat([sub, bench.rename("__bench")], axis=1).dropna(how="any")
    if aligned.empty or len(aligned) < 2:
        return pd.Series(dtype=float)

    row0 = aligned.iloc[0]
    if (row0 == 0).any() or row0.isna().any():
        return pd.Series(dtype=float)
    norm = aligned.div(row0)
    g = norm[list(tks)].mean(axis=1)
    b = norm["__bench"]
    rs = (g / b).replace([np.inf, -np.inf], np.nan).dropna()
    return rs


def calculate_relative_strength_metrics(price_df: pd.DataFrame) -> pd.DataFrame:
    if price_df.empty or "symbol" not in price_df.columns:
        return pd.DataFrame(columns=["ETF", "Industry", *METRIC_COLS])

    wide = price_df.pivot(index="date", columns="symbol", values="adjClose").sort_index()
    if BENCHMARK not in wide.columns:
        return pd.DataFrame(columns=["ETF", "Industry", *METRIC_COLS])

    rows: list[dict[str, Any]] = []
    for industry, tickers in INDUSTRY_GROUPS:
        rs = _composite_rs_vs_benchmark(wide, tickers, BENCHMARK)
        if rs.empty:
            continue
        proxy_label = ", ".join(str(t).upper() for t in tickers)
        rows.append(
            {
                "ETF": proxy_label,
                "Industry": industry,
                "1W RS %": _rs_pct_change(rs, TRADING_1W),
                "1M RS %": _rs_pct_change(rs, TRADING_1M),
                "3M RS %": _rs_pct_change(rs, TRADING_3M),
                "6M RS %": _rs_pct_change(rs, TRADING_6M),
                "12M RS %": _rs_pct_change(rs, TRADING_12M),
                "RS vs 50 DMA %": _rs_vs_dma_pct(rs, 50),
                "RS vs 200 DMA %": _rs_vs_dma_pct(rs, 200),
            }
        )

    return pd.DataFrame(rows)


def build_rotation_heatmap_table(metrics_df: pd.DataFrame) -> pd.DataFrame:
    if metrics_df.empty:
        return pd.DataFrame(columns=list(METRIC_COLS))

    df = metrics_df.copy()
    df["Industry_label"] = df["Industry"].astype(str) + " (" + df["ETF"].astype(str) + ")"
    out = df.set_index("Industry_label")[list(METRIC_COLS)].copy()
    for c in METRIC_COLS:
        out[c] = pd.to_numeric(out[c], errors="coerce") * 100.0
    return out.sort_values("3M RS %", ascending=False, na_position="last")


def _group_column_slug(industry: str, tickers: tuple[str, ...]) -> str:
    if len(tickers) == 1:
        return f"{tickers[0]}_vs_{BENCHMARK}"
    safe = industry.replace("/", "-").replace(" ", "_")[:40]
    return f"{safe}_vs_{BENCHMARK}"


def _build_rs_ratio_history(price_df: pd.DataFrame) -> pd.DataFrame:
    if price_df.empty:
        return pd.DataFrame()
    wide = price_df.pivot(index="date", columns="symbol", values="adjClose").sort_index()
    if BENCHMARK not in wide.columns:
        return pd.DataFrame()
    cols: dict[str, pd.Series] = {}
    for industry, tickers in INDUSTRY_GROUPS:
        rs = _composite_rs_vs_benchmark(wide, tickers, BENCHMARK)
        if rs.empty:
            continue
        cols[_group_column_slug(industry, tickers)] = rs
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(cols).dropna(how="all").sort_index()


def build_utilities_rotation_bundle(
    session: Any,
    api_key: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    try:
        prices = get_utilities_rotation_prices(session, api_key, force_refresh=force_refresh)
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
            "error": "Missing price history for XLU or rotation symbols.",
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
            "error": "Could not compute RS metrics (insufficient overlapping history vs XLU).",
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
