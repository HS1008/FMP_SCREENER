"""
Sector ETF rotation vs SPY for the SPY benchmark dashboard tab.

Each row is one sector proxy from ``config.SECTOR_ETF_MAP``; relative strength is that ETF
rebased to 1 on the first aligned date divided by SPY rebased the same way (same convention as
``tech_rotation_engine`` industry baskets vs XLK).
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

import config
import data_loader
import tech_rotation_engine

BENCHMARK = "SPY"
SPY_SECTOR_LOOKBACK_CAL_DAYS: int = 520

METRIC_COLS: tuple[str, ...] = tech_rotation_engine.METRIC_COLS
TRADING_1W = int(config.TRADING_DAYS_1W)
TRADING_1M = int(config.TRADING_DAYS_1M)
TRADING_3M = int(config.TRADING_DAYS_3M)
TRADING_6M = 126
TRADING_12M = int(config.TRADING_DAYS_1Y)


def sector_etf_rows() -> tuple[tuple[str, str], ...]:
    """Sorted (FMP sector name, sector ETF ticker) pairs."""
    return tuple(sorted(config.SECTOR_ETF_MAP.items(), key=lambda x: str(x[0])))


def all_rotation_symbols() -> tuple[str, ...]:
    """SPY plus every sector ETF in ``SECTOR_ETF_MAP`` (deduped, sorted)."""
    syms: set[str] = {BENCHMARK}
    for _, etf in config.SECTOR_ETF_MAP.items():
        syms.add(str(etf).upper().strip())
    return tuple(sorted(syms))


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


def _etf_rs_vs_benchmark(wide: pd.DataFrame, etf: str, benchmark: str = BENCHMARK) -> pd.Series:
    """RS ratio series: rebased ETF / rebased benchmark (first row where both valid = anchor)."""
    eu = str(etf).upper().strip()
    bu = str(benchmark).upper().strip()
    if eu not in wide.columns or bu not in wide.columns:
        return pd.Series(dtype=float)
    aligned = wide[[eu, bu]].apply(pd.to_numeric, errors="coerce").dropna(how="any")
    if aligned.empty or len(aligned) < 2:
        return pd.Series(dtype=float)
    row0 = aligned.iloc[0]
    if (row0 == 0).any() or row0.isna().any():
        return pd.Series(dtype=float)
    norm = aligned.div(row0)
    rs = (norm[eu] / norm[bu]).replace([np.inf, -np.inf], np.nan).dropna()
    return rs


def get_spy_sector_rotation_prices(
    session: Any,
    api_key: str,
    *,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Long-format dividend-adjusted closes: ``date``, ``symbol``, ``adjClose``."""
    end = date.today()
    start = end - timedelta(days=SPY_SECTOR_LOOKBACK_CAL_DAYS)
    mw = max(1, int(getattr(config, "DASHBOARD_PRICE_FETCH_MAX_WORKERS", config.PRICE_FETCH_MAX_WORKERS)))
    return data_loader.get_price_histories_long(
        session, api_key, list(all_rotation_symbols()), start, end, force_refresh=force_refresh, max_workers=mw
    )


def calculate_sector_rs_vs_spy_metrics(price_df: pd.DataFrame) -> pd.DataFrame:
    """One row per sector ETF vs SPY; metric columns are decimals (heatmap ×100)."""
    if price_df.empty or "symbol" not in price_df.columns:
        return pd.DataFrame(columns=["ETF", "Industry", *METRIC_COLS])

    wide = price_df.pivot(index="date", columns="symbol", values="adjClose").sort_index()
    if BENCHMARK not in wide.columns:
        return pd.DataFrame(columns=["ETF", "Industry", *METRIC_COLS])

    rows: list[dict[str, Any]] = []
    for sector_name, etf in sector_etf_rows():
        etf_u = str(etf).upper().strip()
        rs = _etf_rs_vs_benchmark(wide, etf_u, BENCHMARK)
        if rs.empty:
            continue
        rows.append(
            {
                "ETF": etf_u,
                "Industry": str(sector_name),
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


def build_sector_rotation_heatmap_table(metrics_df: pd.DataFrame) -> pd.DataFrame:
    """Heatmap index: sector name (ETF ticker); same column layout as industry rotation."""
    if metrics_df.empty:
        return pd.DataFrame(columns=list(METRIC_COLS))

    df = metrics_df.copy()
    df["Industry_label"] = df["Industry"].astype(str) + " (" + df["ETF"].astype(str) + ")"
    out = df.set_index("Industry_label")[list(METRIC_COLS)].copy()
    for c in METRIC_COLS:
        out[c] = pd.to_numeric(out[c], errors="coerce") * 100.0
    out = out.sort_values("3M RS %", ascending=False, na_position="last")
    return out


def _build_sector_rs_ratio_history(price_df: pd.DataFrame) -> pd.DataFrame:
    """Wide RS ratio levels (ETF/SPY) by date for optional detail."""
    if price_df.empty:
        return pd.DataFrame()
    wide = price_df.pivot(index="date", columns="symbol", values="adjClose").sort_index()
    if BENCHMARK not in wide.columns:
        return pd.DataFrame()
    cols: dict[str, pd.Series] = {}
    for sector_name, etf in sector_etf_rows():
        etf_u = str(etf).upper().strip()
        rs = _etf_rs_vs_benchmark(wide, etf_u, BENCHMARK)
        if rs.empty:
            continue
        safe = str(sector_name).replace("/", "-").replace(" ", "_")[:40]
        cols[f"{safe}_{etf_u}_vs_{BENCHMARK}"] = rs
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(cols).dropna(how="all").sort_index()


def build_spy_sector_rotation_bundle(
    session: Any,
    api_key: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Bundle aligned with sector rotation engines: prices, metrics, heatmap, RS history."""
    try:
        prices = get_spy_sector_rotation_prices(session, api_key, force_refresh=force_refresh)
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
            "error": "Missing price history for SPY or sector ETFs.",
            "as_of": None,
            "prices": prices,
            "metrics": pd.DataFrame(),
            "heatmap": pd.DataFrame(),
            "rs_ratio_history": pd.DataFrame(),
        }

    metrics = calculate_sector_rs_vs_spy_metrics(prices)
    if metrics.empty:
        return {
            "ok": False,
            "error": "Could not compute sector RS vs SPY (insufficient overlapping history).",
            "as_of": pd.to_datetime(prices["date"], errors="coerce").max().date()
            if not prices.empty
            else date.today(),
            "prices": prices,
            "metrics": metrics,
            "heatmap": pd.DataFrame(),
            "rs_ratio_history": pd.DataFrame(),
        }

    heatmap = build_sector_rotation_heatmap_table(metrics)
    rs_hist = _build_sector_rs_ratio_history(prices)
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
