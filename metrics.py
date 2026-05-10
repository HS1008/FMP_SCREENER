"""
Numeric helpers for returns, Z-scores, and window performance.

Pure pandas / numpy — no network calls.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_returns(df: pd.DataFrame, price_col: str = "adjClose") -> pd.Series:
    """Daily simple returns from a price column (decimal, e.g. 0.01 = +1%)."""
    px = pd.to_numeric(df[price_col], errors="coerce")
    return px.pct_change()


def cumulative_return(returns: pd.Series) -> float:
    """Total compounded return (%) over a window of *daily decimal* returns."""
    r = pd.to_numeric(returns, errors="coerce").dropna()
    if r.empty:
        return float("nan")
    return float((np.prod(1.0 + r.to_numpy(dtype=float)) - 1.0) * 100.0)


def compounded_total_percent_from_prices(prices: pd.Series) -> float:
    """Total % change from first to last price in the series (inclusive)."""
    p = pd.to_numeric(prices, errors="coerce").dropna()
    if len(p) < 2:
        return float("nan")
    a, b = float(p.iloc[0]), float(p.iloc[-1])
    if a == 0:
        return float("nan")
    return float((b / a - 1.0) * 100.0)


def zscore_within_series(s: pd.Series) -> pd.Series:
    """Z-score using population std (ddof=0). All-constant series -> zeros."""
    x = pd.to_numeric(s, errors="coerce")
    mu = x.mean()
    sig = x.std(ddof=0)
    if sig == 0 or pd.isna(sig):
        return pd.Series(0.0, index=x.index)
    return (x - mu) / sig


def return_last_n_trading_days(df: pd.DataFrame, price_col: str, n: int) -> float:
    """Compounded % return over the last `n` trading rows of `price_col`."""
    if df.empty or n < 2:
        return float("nan")
    sub = df.sort_values("date").tail(n)
    if len(sub) < 2:
        return float("nan")
    r = calculate_returns(sub, price_col=price_col).dropna()
    return cumulative_return(r)


def return_ytd_first_trading_day(df: pd.DataFrame, price_col: str, year: int) -> float:
    """
    YTD total return (%): first available trading row on/after Jan 1 of `year`
    through the last row in `df`.
    """
    if df.empty:
        return float("nan")
    d = df.sort_values("date").copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    y0 = pd.Timestamp(year, 1, 1)
    sub = d.loc[d["date"] >= y0]
    if sub.empty:
        return float("nan")
    px = pd.to_numeric(sub[price_col], errors="coerce").dropna()
    return compounded_total_percent_from_prices(px)


def return_12m_skip_1m(df: pd.DataFrame, price_col: str, n_1y: int, n_1m: int) -> float:
    """
    Calculates 12 month momentum excluding the most recent 1 month.
    Uses price from roughly 252 trading days ago to price from roughly 21 trading days ago.
    Returns percent return.
    """
    px = df.sort_values("date").copy()
    px[price_col] = pd.to_numeric(px[price_col], errors="coerce")
    px = px.dropna(subset=[price_col])

    if len(px) < n_1y:
        return float("nan")

    start_price = px[price_col].iloc[-n_1y]
    end_price = px[price_col].iloc[-n_1m]

    if start_price == 0 or pd.isna(start_price) or pd.isna(end_price):
        return float("nan")

    return float((end_price / start_price - 1.0) * 100.0)


def annualized_return(returns: pd.Series) -> float:
    """Mean daily return * 252 (decimal -> annualized scale)."""
    r = pd.to_numeric(returns, errors="coerce").dropna()
    if r.empty:
        return float("nan")
    return float(r.mean() * 252.0)


def annualized_volatility(returns: pd.Series) -> float:
    r = pd.to_numeric(returns, errors="coerce").dropna()
    if r.empty or len(r) < 2:
        return float("nan")
    return float(r.std(ddof=1) * np.sqrt(252.0))


def sharpe_ratio(returns: pd.Series, rf_rate: float) -> float:
    r = pd.to_numeric(returns, errors="coerce").dropna()
    if r.empty or len(r) < 2:
        return float("nan")
    daily_rf = (1.0 + float(rf_rate)) ** (1.0 / 252.0) - 1.0
    excess = r - daily_rf
    vol = excess.std(ddof=1) * np.sqrt(252.0)
    if vol == 0 or np.isnan(vol):
        return float("nan")
    return float(excess.mean() * 252.0 / vol)


def max_drawdown(returns: pd.Series) -> float:
    """
    Largest peak-to-trough loss from daily *decimal* simple returns.

    Returns a positive percentage (e.g. 28.5 means about a 28.5% peak-to-trough decline).
    """
    r = pd.to_numeric(returns, errors="coerce").dropna()
    if r.empty or len(r) < 2:
        return float("nan")
    wealth = (1.0 + r).cumprod()
    peak = wealth.cummax()
    trough = (wealth / peak - 1.0).min()
    return float(abs(float(trough)) * 100.0)


def price_factor_metrics(
    df: pd.DataFrame,
    price_col: str,
    *,
    n_1w: int,
    n_1m: int,
    n_3m: int,
    n_1y: int,
) -> dict[str, float]:
    """
    Return / risk stats from a price history table (used for momentum + risk factors).

    Uses the last `n_1y` (capped by available rows) daily returns for volatility and max drawdown.
    """
    out: dict[str, float] = {
        "ret_1w_pct": float("nan"),
        "ret_1m_pct": float("nan"),
        "ret_3m_pct": float("nan"),
        "ret_1y_pct": float("nan"),
        "ret_12m_skip1m_pct": float("nan"),
        "momentum_accel": float("nan"),
        "mom_vol_adj": float("nan"),
        "above_200dma": float("nan"),
        "trend_strength_50_200": float("nan"),
        "distance_200dma": float("nan"),
        "ret_ytd_pct": float("nan"),
        "ann_volatility": float("nan"),
        "max_drawdown_pct": float("nan"),
    }
    if df.empty or price_col not in df.columns:
        return out
    px = df.sort_values("date").copy()
    px["date"] = pd.to_datetime(px["date"], errors="coerce")
    px[price_col] = pd.to_numeric(px[price_col], errors="coerce")
    px = px.dropna(subset=["date", price_col])
    if len(px) < 2:
        return out

    y = int(px["date"].max().year)
    out["ret_1w_pct"] = return_last_n_trading_days(px, price_col, max(2, min(n_1w, len(px))))
    out["ret_1m_pct"] = return_last_n_trading_days(px, price_col, max(2, min(n_1m, len(px))))
    out["ret_3m_pct"] = return_last_n_trading_days(px, price_col, max(2, min(n_3m, len(px))))
    n_y = max(2, min(n_1y, len(px)))
    out["ret_1y_pct"] = return_last_n_trading_days(px, price_col, n_y)
    # Skip-month momentum reduces short-term mean-reversion noise.
    out["ret_12m_skip1m_pct"] = return_12m_skip_1m(px, price_col, n_1y, n_1m)
    out["ret_ytd_pct"] = return_ytd_first_trading_day(px, price_col, y)

    rets = calculate_returns(px, price_col=price_col).dropna()
    tail = rets.tail(min(n_1y, len(rets))) if len(rets) else rets
    if len(tail) >= 2:
        out["ann_volatility"] = annualized_volatility(tail)
        out["max_drawdown_pct"] = max_drawdown(tail)
        # Volatility-adjusted momentum keeps high raw returns honest if volatility is extreme.
        vol_pct = out["ann_volatility"] * 100.0 if pd.notna(out["ann_volatility"]) else float("nan")
        if pd.notna(vol_pct) and vol_pct != 0 and pd.notna(out["ret_12m_skip1m_pct"]):
            out["mom_vol_adj"] = float(out["ret_12m_skip1m_pct"] / vol_pct)

    # Acceleration: recent 3M trend versus the longer skip-month trend.
    if pd.notna(out["ret_3m_pct"]) and pd.notna(out["ret_12m_skip1m_pct"]):
        out["momentum_accel"] = float(out["ret_3m_pct"] - out["ret_12m_skip1m_pct"])

    # Trend confirmation and structure:
    # - above_200dma: binary trend filter (1/0)
    # - trend_strength_50_200: medium trend vs long trend (MA50/MA200 - 1)
    # - distance_200dma: extension from long trend (price/MA200 - 1)
    latest_price = pd.to_numeric(px[price_col].iloc[-1], errors="coerce")
    pnum = pd.to_numeric(px[price_col], errors="coerce")
    sma_200 = pnum.rolling(200).mean().iloc[-1]
    sma_50 = pnum.rolling(50).mean().iloc[-1]
    if pd.notna(latest_price) and pd.notna(sma_200) and sma_200 != 0:
        out["above_200dma"] = 1.0 if latest_price > sma_200 else 0.0
        out["distance_200dma"] = float(latest_price / sma_200 - 1.0)
    if pd.notna(sma_50) and pd.notna(sma_200) and sma_200 != 0:
        out["trend_strength_50_200"] = float(sma_50 / sma_200 - 1.0)
    return out
