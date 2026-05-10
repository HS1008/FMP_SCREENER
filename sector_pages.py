"""
Per-sector ETF vs benchmark analysis (live FMP prices), trend/technicals, and risk stats.

Structured so you can reuse the same helpers for all sector ETFs (e.g. XLK, XLF, ...).
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import pandas as pd
import requests

import config
import data_loader


# Extra calendar days beyond `default_price_window()` so 12M skip-1M and 6M
# windows have enough history even after alignment and holidays.
_SECTOR_EXTRA_LOOKBACK_CAL_DAYS: int = 120

# Additional calendar history for single-ETF trend charts (200-DMA + 12M skip-1M).
_TREND_EXTRA_CAL_DAYS: int = 520


def total_return_from_prices(prices: pd.Series) -> float:
    """
    Simple total return from the first to the last price in the series (decimal).

    Example: 0.10 means +10% from the first observation to the last.
    """
    p = pd.to_numeric(prices, errors="coerce").dropna()
    if len(p) < 2:
        return float("nan")
    a, b = float(p.iloc[0]), float(p.iloc[-1])
    if a == 0:
        return float("nan")
    return b / a - 1.0


def return_over_trading_days(df: pd.DataFrame, price_col: str, n: int) -> float:
    """
    Simple total return (decimal) over the last `n` trading sessions.

    Compares the latest close to the close `n` rows earlier (n trading gaps).
    Requires at least n + 1 rows after sorting by date.
    """
    if n < 1:
        return float("nan")
    px = df.sort_values("date").dropna(subset=[price_col, "date"])
    if len(px) < n + 1:
        return float("nan")
    end = float(px[price_col].iloc[-1])
    start = float(px[price_col].iloc[-1 - n])
    if start == 0 or pd.isna(end) or pd.isna(start):
        return float("nan")
    return end / start - 1.0


def skip_month_return(
    df: pd.DataFrame,
    price_col: str,
    n_12m: int = 252,
    n_1m: int = 21,
) -> float:
    """
    ~12-month total return (decimal) ending about one month before the last row.

    Uses the same indexing convention as `metrics.return_12m_skip_1m`:
    end price at row (-n_1m), start price at row (-n_12m), return end/start - 1.

    Requires at least `n_12m` rows (see `metrics.return_12m_skip_1m`).
    """
    px = df.sort_values("date").copy()
    px[price_col] = pd.to_numeric(px[price_col], errors="coerce")
    px = px.dropna(subset=[price_col])
    if len(px) < n_12m:
        return float("nan")
    start_price = float(px[price_col].iloc[-n_12m])
    end_price = float(px[price_col].iloc[-n_1m])
    if start_price == 0 or pd.isna(start_price) or pd.isna(end_price):
        return float("nan")
    return end_price / start_price - 1.0


def _column_names(sector_etf: str, benchmark: str) -> tuple[str, str, str, str]:
    etf_u = sector_etf.upper()
    b_u = benchmark.upper()
    return (
        f"{etf_u}_price",
        f"{b_u}_price",
        f"{etf_u}_return",
        f"{b_u}_return",
    )


def _extended_price_window(as_of: date | None = None) -> tuple[date, date]:
    start, end = data_loader.default_price_window(as_of)
    start = start - timedelta(days=_SECTOR_EXTRA_LOOKBACK_CAL_DAYS)
    return start, end


def get_sector_vs_spy_data(
    session: requests.Session,
    api_key: str,
    sector_etf: str = "XLK",
    benchmark: str = "SPY",
    *,
    sector_name: str = "Technology",
    force_refresh: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Pull dividend-adjusted histories, align by date, and build XLK vs SPY analytics.

    Returns:
        (detail_df, summary_dict)

    `detail_df` columns:
        date, {ETF}_price, {BENCH}_price, {ETF}_return, {BENCH}_return,
        {ETF}_index, {BENCH}_index, relative_strength_ratio

    `summary_dict` includes relative returns vs benchmark (decimal): 1W, 1M, 3M, 6M,
    12M skip-1M, plus as_of_date.
    """
    etf_sym = sector_etf.upper().strip()
    bench_sym = benchmark.upper().strip()
    col_px_etf, col_px_bench, col_ret_etf, col_ret_bench = _column_names(etf_sym, bench_sym)
    col_idx_etf = f"{etf_sym}_index"
    col_idx_bench = f"{bench_sym}_index"

    empty_cols = [
        "date",
        col_px_etf,
        col_px_bench,
        col_ret_etf,
        col_ret_bench,
        col_idx_etf,
        col_idx_bench,
        "relative_strength_ratio",
    ]

    def _empty_summary(as_of: date | None = None) -> dict[str, Any]:
        d = as_of or date.today()
        return {
            "sector": sector_name,
            "sector_etf": etf_sym,
            "benchmark": bench_sym,
            "relative_return_1w": float("nan"),
            "relative_return_1m": float("nan"),
            "relative_return_3m": float("nan"),
            "relative_return_6m": float("nan"),
            "relative_return_12m_skip_1m": float("nan"),
            "as_of_date": d,
        }

    date_from, date_to = _extended_price_window()

    try:
        long_px = data_loader.get_price_histories_long(
            session, api_key, [etf_sym, bench_sym], date_from, date_to, force_refresh=force_refresh
        )
    except Exception:
        return pd.DataFrame(columns=empty_cols), _empty_summary(date_to)

    if long_px.empty or "symbol" not in long_px.columns:
        return pd.DataFrame(columns=empty_cols), _empty_summary(date_to)

    e = long_px.loc[long_px["symbol"] == etf_sym, ["date", "adjClose"]].copy()
    b = long_px.loc[long_px["symbol"] == bench_sym, ["date", "adjClose"]].copy()
    if e.empty or b.empty:
        return pd.DataFrame(columns=empty_cols), _empty_summary(date_to)

    if "adjClose" not in e.columns or "adjClose" not in b.columns:
        return pd.DataFrame(columns=empty_cols), _empty_summary(date_to)
    e["date"] = pd.to_datetime(e["date"], errors="coerce").dt.normalize()
    b["date"] = pd.to_datetime(b["date"], errors="coerce").dt.normalize()
    e = e.rename(columns={"adjClose": col_px_etf})
    b = b.rename(columns={"adjClose": col_px_bench})

    merged = pd.merge(e, b, on="date", how="inner").sort_values("date").reset_index(drop=True)
    merged = merged.dropna(subset=[col_px_etf, col_px_bench])
    if merged.empty:
        return pd.DataFrame(columns=empty_cols), _empty_summary(date_to)

    merged[col_ret_etf] = merged[col_px_etf].pct_change()
    merged[col_ret_bench] = merged[col_px_bench].pct_change()

    first_etf = float(merged[col_px_etf].iloc[0])
    first_bench = float(merged[col_px_bench].iloc[0])
    if first_etf == 0 or first_bench == 0:
        return pd.DataFrame(columns=empty_cols), _empty_summary(merged["date"].iloc[-1].date())

    merged[col_idx_etf] = 100.0 * merged[col_px_etf] / first_etf
    merged[col_idx_bench] = 100.0 * merged[col_px_bench] / first_bench
    merged["relative_strength_ratio"] = merged[col_px_etf] / merged[col_px_bench]

    n_1w = int(config.TRADING_DAYS_1W)
    n_1m = int(config.TRADING_DAYS_1M)
    n_3m = int(config.TRADING_DAYS_3M)
    n_6m = int(config.TRADING_DAYS_1M * 6)
    r_etf_1w = return_over_trading_days(merged, col_px_etf, n_1w)
    r_bench_1w = return_over_trading_days(merged, col_px_bench, n_1w)
    r_etf_1m = return_over_trading_days(merged, col_px_etf, n_1m)
    r_bench_1m = return_over_trading_days(merged, col_px_bench, n_1m)
    r_etf_3m = return_over_trading_days(merged, col_px_etf, n_3m)
    r_bench_3m = return_over_trading_days(merged, col_px_bench, n_3m)
    r_etf_6m = return_over_trading_days(merged, col_px_etf, n_6m)
    r_bench_6m = return_over_trading_days(merged, col_px_bench, n_6m)
    r_etf_skip = skip_month_return(merged, col_px_etf, config.TRADING_DAYS_1Y, config.TRADING_DAYS_1M)
    r_bench_skip = skip_month_return(merged, col_px_bench, config.TRADING_DAYS_1Y, config.TRADING_DAYS_1M)

    as_of = merged["date"].iloc[-1]
    as_of_d = as_of.date() if hasattr(as_of, "date") else date.today()

    summary: dict[str, Any] = {
        "sector": sector_name,
        "sector_etf": etf_sym,
        "benchmark": bench_sym,
        "relative_return_1w": float(r_etf_1w - r_bench_1w),
        "relative_return_1m": float(r_etf_1m - r_bench_1m),
        "relative_return_3m": float(r_etf_3m - r_bench_3m),
        "relative_return_6m": float(r_etf_6m - r_bench_6m),
        "relative_return_12m_skip_1m": float(r_etf_skip - r_bench_skip),
        "as_of_date": as_of_d,
    }

    out = merged[
        [
            "date",
            col_px_etf,
            col_px_bench,
            col_ret_etf,
            col_ret_bench,
            col_idx_etf,
            col_idx_bench,
            "relative_strength_ratio",
        ]
    ].copy()

    return out, summary


def _trend_price_window(as_of: date | None = None) -> tuple[date, date]:
    """Wider pull than `_extended_price_window` so 200-DMA and skip-month windows are warm-started."""
    start, end = data_loader.default_price_window(as_of)
    start = start - timedelta(days=_SECTOR_EXTRA_LOOKBACK_CAL_DAYS + _TREND_EXTRA_CAL_DAYS)
    return start, end


def get_sector_etf_trend_data(
    session: requests.Session,
    api_key: str,
    sector_etf: str = "XLK",
    *,
    force_refresh: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Single-sector ETF trend pack: horizon returns and DMAs.

    Returns:
        (detail_df, summary_dict)

    `detail_df` columns:
        date, price, daily_return, dma_50, dma_100, dma_200
    """
    sym = sector_etf.upper().strip()
    empty_cols = ["date", "price", "daily_return", "dma_50", "dma_100", "dma_200"]

    def _empty_summary(as_of: date | None = None) -> dict[str, Any]:
        d = as_of or date.today()
        return {
            "sector_etf": sym,
            "as_of_date": d,
            "latest_price": float("nan"),
            "return_1w": float("nan"),
            "return_1m": float("nan"),
            "return_3m": float("nan"),
            "return_12m_skip_1m": float("nan"),
            "dma_50": float("nan"),
            "dma_100": float("nan"),
            "dma_200": float("nan"),
        }

    date_from, date_to = _trend_price_window()

    try:
        raw = data_loader.get_price_history(session, api_key, sym, date_from, date_to, force_refresh=force_refresh)
    except Exception:
        return pd.DataFrame(columns=empty_cols), _empty_summary(date_to)

    if raw.empty or "adjClose" not in raw.columns:
        return pd.DataFrame(columns=empty_cols), _empty_summary(date_to)

    df = raw[["date", "adjClose"]].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df = df.rename(columns={"adjClose": "price"})
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna(subset=["date", "price"]).sort_values("date").reset_index(drop=True)
    if df.empty:
        return pd.DataFrame(columns=empty_cols), _empty_summary(date_to)

    df["daily_return"] = df["price"].pct_change()
    df["dma_50"] = df["price"].rolling(50, min_periods=50).mean()
    df["dma_100"] = df["price"].rolling(100, min_periods=100).mean()
    df["dma_200"] = df["price"].rolling(200, min_periods=200).mean()

    n_1w = int(config.TRADING_DAYS_1W)
    n_1m = int(config.TRADING_DAYS_1M)
    n_3m = int(config.TRADING_DAYS_3M)
    r_1w = return_over_trading_days(df, "price", n_1w)
    r_1m = return_over_trading_days(df, "price", n_1m)
    r_3m = return_over_trading_days(df, "price", n_3m)
    r_12_skip = skip_month_return(df, "price", int(config.TRADING_DAYS_1Y), int(config.TRADING_DAYS_1M))

    last = df.iloc[-1]
    as_of = last["date"]
    as_of_d = as_of.date() if hasattr(as_of, "date") else date.today()
    latest_price = float(last["price"])
    d50 = float(last["dma_50"]) if pd.notna(last["dma_50"]) else float("nan")
    d100 = float(last["dma_100"]) if pd.notna(last["dma_100"]) else float("nan")
    d200 = float(last["dma_200"]) if pd.notna(last["dma_200"]) else float("nan")

    summary: dict[str, Any] = {
        "sector_etf": sym,
        "as_of_date": as_of_d,
        "latest_price": latest_price,
        "return_1w": float(r_1w),
        "return_1m": float(r_1m),
        "return_3m": float(r_3m),
        "return_12m_skip_1m": float(r_12_skip),
        "dma_50": d50,
        "dma_100": d100,
        "dma_200": d200,
    }

    out = df[["date", "price", "daily_return", "dma_50", "dma_100", "dma_200"]].copy()
    return out, summary


def annualized_volatility_from_returns(returns: pd.Series) -> float:
    """
    Trailing realized volatility (decimal, annualized): stdev(daily simple returns) * sqrt(252).

    Uses sample standard deviation (ddof=1). Returns NaN if fewer than two valid returns.
    """
    r = pd.to_numeric(returns, errors="coerce").dropna()
    if len(r) < 2:
        return float("nan")
    sig = float(r.std(ddof=1))
    if math.isnan(sig):
        return float("nan")
    return sig * math.sqrt(252.0)


def max_drawdown_from_prices(prices: pd.Series) -> float:
    """
    Maximum drawdown over a price path (decimal, <= 0).

    For each point: wealth = price / cumulative peak-to-date - 1; return min(wealth).
    """
    p = pd.to_numeric(prices, errors="coerce").dropna()
    if len(p) < 2:
        return float("nan")
    peak = p.cummax()
    underwater = p / peak - 1.0
    return float(underwater.min())


def current_drawdown_from_rolling_high(prices: pd.Series, lookback_days: int) -> float:
    """
    Drawdown of latest price vs the high over the last `lookback_days` observations (decimal, <= 0).

    Formula: latest_price / max(price over window) - 1
    """
    if lookback_days < 1:
        return float("nan")
    p = pd.to_numeric(prices, errors="coerce").dropna()
    if len(p) < lookback_days:
        return float("nan")
    window = p.tail(lookback_days)
    hi = float(window.max())
    last = float(p.iloc[-1])
    if hi == 0 or math.isnan(hi) or math.isnan(last):
        return float("nan")
    return last / hi - 1.0


def get_sector_risk_data(
    session: requests.Session,
    api_key: str,
    sector_etf: str = "XLK",
    *,
    force_refresh: bool = False,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    """
    Realized volatility, drawdown stats, and placeholder implied vol for one sector ETF.

    Returns:
        (summary_dict, vol_curve_df, drawdown_ts_df)

    `vol_curve_df` columns: label, value, type, sort_order
    `drawdown_ts_df` columns: date, drawdown  (price / expanding peak - 1)
    """
    sym = sector_etf.upper().strip()
    n_1y = int(config.TRADING_DAYS_1Y)
    n_3m = int(config.TRADING_DAYS_3M)
    n_20d = 20

    def _empty_summary(as_of: date | None = None) -> dict[str, Any]:
        d = as_of or date.today()
        nan = float("nan")
        return {
            "sector_etf": sym,
            "as_of_date": d,
            "trailing_1y_vol": nan,
            "trailing_3m_vol": nan,
            "trailing_20d_vol": nan,
            "implied_1m_vol": nan,
            "implied_3m_vol": nan,
            "implied_6m_vol": nan,
            "implied_1y_vol": nan,
            "trailing_1y_max_drawdown": nan,
            "current_drawdown_1y_high": nan,
            "current_drawdown_3m_high": nan,
        }

    empty_vol = pd.DataFrame(columns=["label", "value", "type", "sort_order"])
    empty_dd = pd.DataFrame(columns=["date", "drawdown"])

    date_from, date_to = _trend_price_window()

    try:
        raw = data_loader.get_price_history(session, api_key, sym, date_from, date_to, force_refresh=force_refresh)
    except Exception:
        return _empty_summary(date_to), empty_vol, empty_dd

    if raw.empty or "adjClose" not in raw.columns:
        return _empty_summary(date_to), empty_vol, empty_dd

    df = raw[["date", "adjClose"]].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df["price"] = pd.to_numeric(df["adjClose"], errors="coerce")
    df = df.dropna(subset=["date", "price"]).sort_values("date").reset_index(drop=True)
    if df.empty:
        return _empty_summary(date_to), empty_vol, empty_dd

    df["daily_return"] = df["price"].pct_change()
    rets = df["daily_return"].dropna()

    r_1y = rets.tail(n_1y) if len(rets) >= n_1y else pd.Series(dtype=float)
    r_3m = rets.tail(n_3m) if len(rets) >= n_3m else pd.Series(dtype=float)
    r_20 = rets.tail(n_20d) if len(rets) >= n_20d else pd.Series(dtype=float)

    v_1y = annualized_volatility_from_returns(r_1y) if len(rets) >= n_1y else float("nan")
    v_3m = annualized_volatility_from_returns(r_3m) if len(rets) >= n_3m else float("nan")
    v_20 = annualized_volatility_from_returns(r_20) if len(rets) >= n_20d else float("nan")

    px = df["price"]
    px_1y = px.tail(n_1y) if len(px) >= n_1y else pd.Series(dtype=float)
    dd_1y_max = max_drawdown_from_prices(px_1y) if len(px_1y) >= 2 else float("nan")

    dd_vs_1y_high = current_drawdown_from_rolling_high(px, n_1y) if len(px) >= n_1y else float("nan")
    dd_vs_3m_high = current_drawdown_from_rolling_high(px, n_3m) if len(px) >= n_3m else float("nan")

    # TODO: replace with FMP options/implied volatility data if available.
    implied_1m = float("nan")
    implied_3m = float("nan")
    implied_6m = float("nan")
    implied_1y = float("nan")

    as_of = df["date"].iloc[-1]
    as_of_d = as_of.date() if hasattr(as_of, "date") else date.today()

    summary: dict[str, Any] = {
        "sector_etf": sym,
        "as_of_date": as_of_d,
        "trailing_1y_vol": float(v_1y),
        "trailing_3m_vol": float(v_3m),
        "trailing_20d_vol": float(v_20),
        "implied_1m_vol": implied_1m,
        "implied_3m_vol": implied_3m,
        "implied_6m_vol": implied_6m,
        "implied_1y_vol": implied_1y,
        "trailing_1y_max_drawdown": float(dd_1y_max),
        "current_drawdown_1y_high": float(dd_vs_1y_high),
        "current_drawdown_3m_high": float(dd_vs_3m_high),
    }

    vol_rows: list[tuple[str, float, str, int]] = [
        ("Trailing 1Y Vol", float(v_1y), "realized", 1),
        ("Trailing 3M Vol", float(v_3m), "realized", 2),
        ("Trailing 20D Vol", float(v_20), "realized", 3),
        ("Implied 1M Vol", implied_1m, "implied", 4),
        ("Implied 3M Vol", implied_3m, "implied", 5),
        ("Implied 6M Vol", implied_6m, "implied", 6),
        ("Implied 1Y Vol", implied_1y, "implied", 7),
    ]
    vol_curve_df = pd.DataFrame(vol_rows, columns=["label", "value", "type", "sort_order"])

    peak = df["price"].expanding(min_periods=1).max()
    drawdown = df["price"] / peak - 1.0
    drawdown_ts_df = pd.DataFrame({"date": df["date"], "drawdown": drawdown})

    return summary, vol_curve_df, drawdown_ts_df
