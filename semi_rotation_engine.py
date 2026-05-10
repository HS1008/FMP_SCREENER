"""
Semiconductor subgroup rotation vs XLK (equal-weight subgroup levels vs Technology benchmark).

Uses dividend-adjusted prices from ``data_loader.get_price_histories_long``.
Subgroup RS metrics use **raw** equal-weight average price (or single name / ETF) divided by XLK
(e.g. Mega-Cap Semis = SMH / XLK).
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

import config
import data_loader

BENCHMARK = "XLK"

# Ordered subgroup name -> member symbols (equal-weight index except single-name / single-ETF groups).
SUBGROUP_DEFS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AI Compute / GPUs", ("NVDA", "AMD")),
    ("Semiconductor Equipment", ("ASML", "AMAT", "LRCX", "KLAC")),
    ("Memory", ("MU",)),
    ("Networking / Connectivity", ("AVGO", "MRVL")),
    ("Analog / Industrial", ("TXN", "ADI", "ON")),
    ("Foundry / Manufacturing", ("TSM",)),
    ("Mobile / Consumer Chips", ("QCOM",)),
    ("Broad Semi Breadth", ("XSD",)),
    ("Mega-Cap Semis", ("SMH",)),
)

ALL_SEMI_SYMBOLS: tuple[str, ...] = tuple(
    dict.fromkeys(
        [BENCHMARK]
        + [s for _, syms in SUBGROUP_DEFS for s in syms]
    )
)

SEMI_LOOKBACK_CAL_DAYS: int = 520

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


def get_semi_rotation_prices(
    session: Any,
    api_key: str,
    *,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Long-format ``date``, ``symbol``, ``adjClose`` for XLK, SMH, XSD, and all subgroup members."""
    end = date.today()
    start = end - timedelta(days=SEMI_LOOKBACK_CAL_DAYS)
    return data_loader.get_price_histories_long(
        session, api_key, ALL_SEMI_SYMBOLS, start, end, force_refresh=force_refresh
    )


def _wide_prices(price_df: pd.DataFrame) -> pd.DataFrame:
    if price_df.empty:
        return pd.DataFrame()
    return price_df.pivot(index="date", columns="symbol", values="adjClose").sort_index()


def _subgroup_equal_weight_raw(wide: pd.DataFrame, symbols: tuple[str, ...]) -> pd.Series:
    """Equal-weight average of raw adjusted closes; inner-join dates where all symbols trade."""
    syms = [s.upper() for s in symbols]
    for s in syms:
        if s not in wide.columns:
            return pd.Series(dtype=float)
    sub = wide[syms].copy()
    sub = sub.dropna(how="any")
    if sub.empty:
        return pd.Series(dtype=float)
    return sub.mean(axis=1)


def build_subgroup_indexes(price_df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalized subgroup indexes (100 at first row where all members have prices).

    Columns match subgroup names; ``date`` is the index.
    """
    wide = _wide_prices(price_df)
    if wide.empty or BENCHMARK not in wide.columns:
        return pd.DataFrame()

    out_cols: dict[str, pd.Series] = {}
    for name, syms in SUBGROUP_DEFS:
        sub = wide[[s for s in syms if s in wide.columns]]
        if sub.shape[1] != len(syms):
            continue
        sub = sub.dropna(how="any")
        if sub.empty:
            continue
        if len(syms) == 1:
            base = float(sub.iloc[0, 0])
            if base == 0 or math.isnan(base):
                continue
            idx = 100.0 * sub.iloc[:, 0] / base
        else:
            normed: list[pd.Series] = []
            for c in sub.columns:
                b0 = float(sub[c].iloc[0])
                if b0 == 0 or math.isnan(b0):
                    normed = []
                    break
                normed.append(100.0 * sub[c] / b0)
            if not normed:
                continue
            idx = pd.concat(normed, axis=1).mean(axis=1)
        out_cols[name] = idx

    if not out_cols:
        return pd.DataFrame()
    return pd.DataFrame(out_cols).sort_index()


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


def calculate_semi_relative_strength_metrics(price_df: pd.DataFrame) -> pd.DataFrame:
    """
    RS vs XLK from **raw** equal-weight subgroup levels (or single ticker / ETF) / XLK.

    ``price_df`` is the long panel from :func:`get_semi_rotation_prices` (same as used to build indexes).
    (Spec text may call this ``index_df``; use this price panel, not the normalized index table, for RS.)
    """
    if price_df.empty:
        return pd.DataFrame(columns=["Subgroup", "Symbols", *METRIC_COLS])

    wide = _wide_prices(price_df)
    if wide.empty or BENCHMARK not in wide.columns:
        return pd.DataFrame(columns=["Subgroup", "Symbols", *METRIC_COLS])

    xlk = pd.to_numeric(wide[BENCHMARK], errors="coerce")
    rows: list[dict[str, Any]] = []

    for name, syms in SUBGROUP_DEFS:
        ew = _subgroup_equal_weight_raw(wide, syms)
        if ew.empty:
            continue
        aligned = pd.concat([ew.rename("ew"), xlk.rename("xlk")], axis=1).dropna(how="any")
        if aligned.empty:
            continue
        rs = aligned["ew"] / aligned["xlk"]
        rs = rs.replace([np.inf, -np.inf], np.nan).dropna()
        if rs.empty:
            continue

        sym_str = ", ".join(s.upper() for s in syms)
        rows.append(
            {
                "Subgroup": name,
                "Symbols": sym_str,
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


def build_semi_rotation_heatmap_table(metrics_df: pd.DataFrame) -> pd.DataFrame:
    """Index = subgroup; values in percentage points; sort by 3M RS % desc."""
    if metrics_df.empty:
        return pd.DataFrame(columns=list(METRIC_COLS))

    out = metrics_df.set_index("Subgroup")[list(METRIC_COLS)].copy()
    for c in METRIC_COLS:
        out[c] = pd.to_numeric(out[c], errors="coerce") * 100.0
    out = out.sort_values("3M RS %", ascending=False, na_position="last")
    return out


def build_semi_rotation_bundle(
    session: Any,
    api_key: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    try:
        prices = get_semi_rotation_prices(session, api_key, force_refresh=force_refresh)
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "as_of": None,
            "prices": pd.DataFrame(),
            "indexes": pd.DataFrame(),
            "metrics": pd.DataFrame(),
            "heatmap": pd.DataFrame(),
        }

    if prices.empty or BENCHMARK not in prices["symbol"].unique():
        return {
            "ok": False,
            "error": "Missing XLK or semiconductor rotation price history.",
            "as_of": None,
            "prices": prices,
            "indexes": pd.DataFrame(),
            "metrics": pd.DataFrame(),
            "heatmap": pd.DataFrame(),
        }

    indexes = build_subgroup_indexes(prices)
    metrics = calculate_semi_relative_strength_metrics(prices)
    if metrics.empty:
        return {
            "ok": False,
            "error": "Could not compute semiconductor RS vs XLK (check symbol overlap and history length).",
            "as_of": pd.to_datetime(prices["date"], errors="coerce").max().date()
            if not prices.empty
            else date.today(),
            "prices": prices,
            "indexes": indexes,
            "metrics": metrics,
            "heatmap": pd.DataFrame(),
        }

    heatmap = build_semi_rotation_heatmap_table(metrics)
    as_of = pd.to_datetime(prices["date"], errors="coerce").max()
    as_of_d = as_of.date() if pd.notna(as_of) else date.today()

    return {
        "ok": True,
        "error": None,
        "as_of": as_of_d,
        "prices": prices,
        "indexes": indexes,
        "metrics": metrics,
        "heatmap": heatmap,
    }
