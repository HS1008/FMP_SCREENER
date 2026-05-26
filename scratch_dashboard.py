"""
Portfolio Stress Test Analyzer — scratch Streamlit dashboard.

FMP primary price data; Yahoo Finance fallback when FMP has no usable history.
Does not use production ranking/rotation engines or dashboard.py.

Run:
  python run_scratch_dashboard.py
"""

from __future__ import annotations

import io
import os
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import config
import data_loader

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_TITLE = "Portfolio Stress Test Analyzer"
SOURCE_FMP = "FMP"
SOURCE_YAHOO = "Yahoo fallback"
SOURCE_MISSING = "Missing"

DEFAULT_HOLDINGS = pd.DataFrame(
    {"Ticker": ["SPY", "AGG", "GLD"], "Allocation %": [60.0, 30.0, 10.0]}
)

YAHOO_CACHE_DIR = config.PROJECT_ROOT / "outputs" / "cache" / "portfolio_yahoo"
UNDERLYING_CACHE_DIR = config.PROJECT_ROOT / "outputs" / "cache" / "portfolio_underlying"
TRADING_DAYS_PER_YEAR = 252
MAX_UNDERLYING_DISPLAY = 50

SCENARIO_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "name": "2000–2002 Tech Crash",
        "start": date(2000, 3, 24),
        "end": date(2002, 10, 9),
        "why": "Tech crash",
    },
    {
        "name": "2008 Credit Crisis",
        "start": date(2007, 10, 9),
        "end": date(2009, 3, 9),
        "why": "Credit crisis",
    },
    {
        "name": "2013 Taper Tantrum",
        "start": date(2013, 5, 1),
        "end": date(2013, 9, 5),
        "why": "Rate sensitivity",
    },
    {
        "name": "2018 Q4",
        "start": date(2018, 9, 20),
        "end": date(2018, 12, 24),
        "why": "Liquidity tightening",
    },
    {
        "name": "2020 Crash",
        "start": date(2020, 2, 19),
        "end": date(2020, 3, 23),
        "why": "Volatility shock",
    },
    {
        "name": "2022 Inflation + Rate Shock",
        "start": date(2022, 1, 3),
        "end": date(2022, 10, 12),
        "why": "Inflation + rate shock",
    },
    {
        "name": "2023–2025 AI Concentration Regime",
        "start": date(2023, 1, 3),
        "end": None,  # latest available
        "why": "AI concentration regime",
    },
)

_FMP_SESSION: Any | None = None


@dataclass
class HoldingRecord:
    ticker: str
    weight: float
    source: str
    failure_reason: str
    prices: pd.Series
    history_start: date | None
    history_end: date | None


# ---------------------------------------------------------------------------
# Config / session
# ---------------------------------------------------------------------------
def fmp_api_key() -> str:
    load_dotenv(config.PROJECT_ROOT / ".env")
    return (os.getenv("FMP_API_KEY") or "").strip()


def fmp_session() -> Any:
    global _FMP_SESSION
    if _FMP_SESSION is None:
        _FMP_SESSION = data_loader.create_http_session()
    return _FMP_SESSION


# ---------------------------------------------------------------------------
# Datetime & tickers
# ---------------------------------------------------------------------------
def normalize_datetime_index(idx: Any) -> pd.DatetimeIndex:
    idx = pd.to_datetime(idx, errors="coerce")
    try:
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_convert(None)
    except Exception:
        pass
    try:
        idx = idx.tz_localize(None)
    except Exception:
        pass
    idx = pd.DatetimeIndex(idx)
    idx = idx[~pd.isna(idx)]
    return idx.normalize()


def safe_timestamp(d: date | datetime | pd.Timestamp | Any) -> pd.Timestamp:
    return pd.Timestamp(d).normalize()


def clean_ticker(raw: str) -> str:
    return str(raw or "").strip().upper()


def clean_holdings_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["Ticker", "Allocation %"])
    work = df.copy()
    if "Ticker" not in work.columns:
        work = work.rename(columns={work.columns[0]: "Ticker"})
    pct_col = "Allocation %" if "Allocation %" in work.columns else work.columns[-1]
    work["Ticker"] = work["Ticker"].astype(str).map(clean_ticker)
    work["Allocation %"] = pd.to_numeric(work[pct_col], errors="coerce").fillna(0.0)
    work = work[work["Ticker"].astype(str).str.len() > 0]
    return work[["Ticker", "Allocation %"]].reset_index(drop=True)


def validate_weights(
    holdings: pd.DataFrame,
    *,
    normalize: bool,
) -> tuple[pd.DataFrame, float, str | None]:
    """Return (holdings, total_pct, warning_message)."""
    h = clean_holdings_df(holdings)
    if h.empty:
        return h, 0.0, "Add at least one holding."
    total = float(h["Allocation %"].sum())
    if normalize and total > 0:
        h = h.copy()
        h["Allocation %"] = h["Allocation %"] / total * 100.0
        total = 100.0
    warn = None
    if abs(total - 100.0) > 0.05:
        warn = f"Total allocation is {total:.2f}% (expected 100%)."
    return h, total, warn


# ---------------------------------------------------------------------------
# Price fetch & cache
# ---------------------------------------------------------------------------
def _yahoo_cache_path(ticker: str) -> Any:
    YAHOO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return YAHOO_CACHE_DIR / f"{clean_ticker(ticker)}.parquet"


def _read_yahoo_cache(ticker: str) -> pd.DataFrame:
    path = _yahoo_cache_path(ticker)
    if not path.is_file():
        return pd.DataFrame(columns=["date", "close"])
    try:
        df = pd.read_parquet(path)
        return df
    except Exception:
        return pd.DataFrame(columns=["date", "close"])


def _write_yahoo_cache(ticker: str, series: pd.Series) -> None:
    if series.empty:
        return
    df = pd.DataFrame({"date": series.index, "close": series.values})
    df.to_parquet(_yahoo_cache_path(ticker), index=False)


def _df_to_price_series(df: pd.DataFrame) -> pd.Series:
    if df.empty or "date" not in df.columns:
        return pd.Series(dtype=float)
    col = None
    for c in ("adjClose", "close", "Close"):
        if c in df.columns:
            col = c
            break
    if col is None:
        try:
            col = data_loader.pick_price_column(df)
        except Exception:
            return pd.Series(dtype=float)
    idx = normalize_datetime_index(df["date"])
    vals = pd.to_numeric(df[col], errors="coerce")
    s = pd.Series(vals.values, index=idx).dropna()
    return s[~s.index.duplicated(keep="last")].sort_index()


def normalize_price_series(series: pd.Series) -> tuple[pd.Series, bool, str]:
    if series is None or series.empty:
        return pd.Series(dtype=float), False, ""
    tz = ""
    try:
        if isinstance(series.index, pd.DatetimeIndex) and series.index.tz is not None:
            tz = str(series.index.tz)
        idx = normalize_datetime_index(series.index)
        vals = pd.to_numeric(series.values, errors="coerce")
        out = pd.Series(vals, index=idx).dropna().sort_index()
        out = out[~out.index.duplicated(keep="last")]
        return out, len(out) >= 5, tz
    except Exception as e:
        return pd.Series(dtype=float), False, str(e)


def filter_series_window(series: pd.Series, date_from: date, date_to: date) -> pd.Series:
    s, _, _ = normalize_price_series(series)
    if s.empty:
        return s
    d0, d1 = safe_timestamp(date_from), safe_timestamp(date_to)
    return s[(s.index >= d0) & (s.index <= d1)]


def fetch_fmp_price_history(
    ticker: str,
    date_from: date,
    date_to: date,
    *,
    force_refresh: bool = False,
) -> tuple[pd.Series, str]:
    sym = clean_ticker(ticker)
    if not sym:
        return pd.Series(dtype=float), "empty ticker"
    api_key = fmp_api_key()
    if not api_key:
        return pd.Series(dtype=float), "FMP API key not set"
    try:
        session = fmp_session()
        hist = data_loader.get_price_history(
            session, api_key, sym, date_from, date_to, force_refresh=force_refresh
        )
        s = _df_to_price_series(hist)
        if len(s) < 5:
            return pd.Series(dtype=float), "FMP: insufficient history"
        return s, ""
    except Exception as e:
        return pd.Series(dtype=float), f"FMP: {type(e).__name__}: {e}"


def fetch_yahoo_price_history(
    ticker: str,
    date_from: date,
    date_to: date,
    *,
    force_refresh: bool = False,
) -> tuple[pd.Series, str]:
    sym = clean_ticker(ticker)
    if not sym:
        return pd.Series(dtype=float), "empty ticker"

    if not force_refresh:
        cached = _read_yahoo_cache(sym)
        s = _df_to_price_series(cached)
        s = filter_series_window(s, date_from, date_to)
        if len(s) >= 5:
            return s, ""

    try:
        import yfinance as yf
    except ImportError:
        return pd.Series(dtype=float), "yfinance not installed"

    try:
        data = yf.download(
            sym,
            start=date_from.isoformat(),
            end=(date_to + timedelta(days=1)).isoformat(),
            auto_adjust=True,
            progress=False,
        )
    except Exception as e:
        err = str(e)
        if "RateLimit" in type(e).__name__ or "Too Many Requests" in err:
            return pd.Series(dtype=float), "Yahoo: rate limited"
        return pd.Series(dtype=float), f"Yahoo: {type(e).__name__}: {e}"

    if data is None or data.empty:
        return pd.Series(dtype=float), "Yahoo: no rows"

    col = "Close" if "Close" in data.columns else None
    if col is None:
        return pd.Series(dtype=float), "Yahoo: no Close column"

    s, ok, _ = normalize_price_series(pd.Series(data[col].values, index=data.index))
    if not ok or len(s) < 5:
        return pd.Series(dtype=float), "Yahoo: insufficient history"

    _write_yahoo_cache(sym, s)
    return filter_series_window(s, date_from, date_to), ""


def fetch_price_history_with_fallback(
    ticker: str,
    date_from: date,
    date_to: date,
    *,
    force_refresh: bool = False,
) -> tuple[pd.Series, str, str]:
    """Returns (prices, source_label, failure_reason)."""
    fmp, fmp_err = fetch_fmp_price_history(ticker, date_from, date_to, force_refresh=force_refresh)
    if len(fmp) >= 5:
        return fmp, SOURCE_FMP, ""

    yahoo, y_err = fetch_yahoo_price_history(ticker, date_from, date_to, force_refresh=force_refresh)
    if len(yahoo) >= 5:
        return yahoo, SOURCE_YAHOO, ""

    reason = y_err or fmp_err or "no usable history"
    return pd.Series(dtype=float), SOURCE_MISSING, reason


def build_price_panel(
    holdings: pd.DataFrame,
    histories: dict[str, pd.Series],
) -> pd.DataFrame:
    """Aligned daily price panel (forward-filled), columns = tickers."""
    tickers = clean_holdings_df(holdings)["Ticker"].tolist()
    frames: dict[str, pd.Series] = {}
    for t in tickers:
        s = histories.get(t, pd.Series(dtype=float))
        if s is not None and not s.empty:
            frames[t] = s
    if not frames:
        return pd.DataFrame()
    panel = pd.DataFrame(frames)
    panel = panel.sort_index().ffill().dropna(how="all")
    return panel


# ---------------------------------------------------------------------------
# Return & risk metrics
# ---------------------------------------------------------------------------
def calculate_returns(prices: pd.DataFrame) -> pd.DataFrame:
    if prices.empty:
        return pd.DataFrame()
    return prices.pct_change().dropna()


def calculate_max_drawdown(price_series: pd.Series) -> float:
    try:
        prices, ok, _ = normalize_price_series(price_series)
        if not ok or len(prices) < 2:
            return float("nan")
        wealth = prices / float(prices.iloc[0])
        peak = wealth.cummax()
        return float((wealth / peak - 1.0).min())
    except Exception:
        return float("nan")


def calculate_total_return(price_series: pd.Series) -> float:
    try:
        prices, ok, _ = normalize_price_series(price_series)
        if not ok or len(prices) < 2:
            return float("nan")
        a, b = float(prices.iloc[0]), float(prices.iloc[-1])
        if a <= 0:
            return float("nan")
        return b / a - 1.0
    except Exception:
        return float("nan")


def calculate_annualized_return(daily_returns: pd.Series, trading_days: int = TRADING_DAYS_PER_YEAR) -> float:
    try:
        r = pd.to_numeric(daily_returns, errors="coerce").dropna()
        if r.empty:
            return float("nan")
        total = float((1.0 + r).prod() - 1.0)
        n = len(r)
        if n < 2:
            return float("nan")
        return float((1.0 + total) ** (trading_days / n) - 1.0)
    except Exception:
        return float("nan")


def calculate_annualized_volatility(
    daily_returns: pd.Series,
    trading_days: int = TRADING_DAYS_PER_YEAR,
) -> float:
    try:
        r = pd.to_numeric(daily_returns, errors="coerce").dropna()
        if len(r) < 3:
            return float("nan")
        return float(r.std(ddof=1) * np.sqrt(trading_days))
    except Exception:
        return float("nan")


def calculate_portfolio_returns(
    price_panel: pd.DataFrame,
    weights: pd.Series,
) -> pd.Series:
    """Daily portfolio returns (buy-and-hold weights, no rebalancing)."""
    rets = calculate_returns(price_panel)
    if rets.empty:
        return pd.Series(dtype=float)
    w = weights.reindex(rets.columns).fillna(0.0)
    if w.sum() > 0:
        w = w / w.sum()
    return (rets * w).sum(axis=1)


def portfolio_equity_curve(portfolio_returns: pd.Series, start_value: float = 1.0) -> pd.Series:
    r = pd.to_numeric(portfolio_returns, errors="coerce").dropna()
    if r.empty:
        return pd.Series(dtype=float)
    return (1.0 + r).cumprod() * start_value


def drawdown_curve(equity: pd.Series) -> pd.Series:
    if equity.empty:
        return pd.Series(dtype=float)
    peak = equity.cummax()
    return equity / peak - 1.0


# ---------------------------------------------------------------------------
# Scenario analysis
# ---------------------------------------------------------------------------
def _scenario_end_date(preset: dict[str, Any], panel: pd.DataFrame) -> date:
    end = preset.get("end")
    if end is not None:
        return end if isinstance(end, date) else pd.Timestamp(end).date()
    if panel.empty:
        return date.today()
    return panel.index.max().date()


def run_scenario_analysis(
    holdings: pd.DataFrame,
    histories: dict[str, pd.Series],
    sources: dict[str, str],
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """
    Returns (summary_df, detail_by_scenario_name).
    """
    h = clean_holdings_df(holdings)
    if h.empty:
        return pd.DataFrame(), {}

    weights = h.set_index("Ticker")["Allocation %"] / 100.0
    panel = build_price_panel(h, histories)
    rows: list[dict[str, Any]] = []
    details: dict[str, pd.DataFrame] = {}

    for preset in SCENARIO_PRESETS:
        start = preset["start"]
        end = _scenario_end_date(preset, panel)
        name = preset["name"]

        sub_panel = panel.loc[(panel.index >= safe_timestamp(start)) & (panel.index <= safe_timestamp(end))]
        notes: list[str] = []
        holding_rows: list[dict[str, Any]] = []

        if sub_panel.empty or sub_panel.shape[0] < 2:
            rows.append(
                {
                    "Scenario": name,
                    "Start": start,
                    "End": end,
                    "Why It Matters": preset["why"],
                    "Portfolio Return": np.nan,
                    "Max Drawdown": np.nan,
                    "Ann. Volatility": np.nan,
                    "Best Holding": "",
                    "Worst Holding": "",
                    "Coverage Warning": "No overlapping price history for scenario window",
                }
            )
            details[name] = pd.DataFrame()
            continue

        port_rets = calculate_portfolio_returns(sub_panel, weights)
        equity = portfolio_equity_curve(port_rets)
        port_total = calculate_total_return(equity)
        port_dd = calculate_max_drawdown(equity)
        port_vol = calculate_annualized_volatility(port_rets)

        holding_returns: dict[str, float] = {}
        for t in h["Ticker"]:
            if t not in sub_panel.columns:
                notes.append(f"{t}: no data in window")
                holding_returns[t] = float("nan")
                continue
            col = sub_panel[t].dropna()
            if col.empty:
                notes.append(f"{t}: insufficient data")
                holding_returns[t] = float("nan")
                continue
            full = histories.get(t, pd.Series(dtype=float))
            win = filter_series_window(full, start, end)
            if len(win) < 2:
                notes.append(f"{t}: limited coverage ({sources.get(t, SOURCE_MISSING)})")
            holding_returns[t] = calculate_total_return(col)

        hr = pd.Series(holding_returns)
        best = hr.idxmax() if hr.notna().any() else ""
        worst = hr.idxmin() if hr.notna().any() else ""

        contrib = {}
        for t, ret in holding_returns.items():
            w = float(weights.get(t, 0.0))
            contrib[t] = w * ret if np.isfinite(ret) else np.nan

        for t in h["Ticker"]:
            holding_rows.append(
                {
                    "Ticker": t,
                    "Source": sources.get(t, SOURCE_MISSING),
                    "Weight %": float(weights.get(t, 0.0) * 100),
                    "Holding Return": holding_returns.get(t, np.nan),
                    "Contribution to Portfolio Return": contrib.get(t, np.nan),
                }
            )

        coverage = "; ".join(notes) if notes else ""
        rows.append(
            {
                "Scenario": name,
                "Start": start,
                "End": end,
                "Why It Matters": preset["why"],
                "Portfolio Return": port_total,
                "Max Drawdown": port_dd,
                "Ann. Volatility": port_vol,
                "Best Holding": best,
                "Worst Holding": worst,
                "Coverage Warning": coverage,
            }
        )
        detail = pd.DataFrame(holding_rows)
        detail["Equity Curve"] = ""  # placeholder column avoided
        details[name] = detail
        details[f"{name}__equity"] = equity
        details[f"{name}__drawdown"] = drawdown_curve(equity)
        details[f"{name}__contrib"] = pd.Series(contrib, name="Contribution")

    return pd.DataFrame(rows), details


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------
def _annualize_mu_cov(daily_returns: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    mu_d = daily_returns.mean().values
    cov_d = daily_returns.cov().values
    mu_a = mu_d * TRADING_DAYS_PER_YEAR
    cov_a = cov_d * TRADING_DAYS_PER_YEAR
    return mu_a, cov_a


def run_monte_carlo_simulation(
    holdings: pd.DataFrame,
    price_panel: pd.DataFrame,
    *,
    horizon_years: int,
    n_sims: int,
    start_value: float,
    annual_contribution: float,
    rebalance: str,
    method: str,
    user_expected: dict[str, float] | None = None,
    conservative_shrink: float = 0.85,
) -> dict[str, Any]:
    h = clean_holdings_df(holdings)
    tickers = h["Ticker"].tolist()
    weights = (h.set_index("Ticker")["Allocation %"] / 100.0).reindex(tickers).fillna(0.0)
    if weights.sum() > 0:
        weights = weights / weights.sum()

    daily = calculate_returns(price_panel)
    daily = daily.dropna(how="all").dropna(axis=1, how="all")
    cols = [c for c in tickers if c in daily.columns]
    if not cols:
        return {"error": "No return history for Monte Carlo."}

    daily = daily[cols].dropna()
    if len(daily) < 30:
        return {"error": "Need at least 30 daily return observations."}

    mu_a, cov_a = _annualize_mu_cov(daily)
    n = len(cols)
    w = weights[cols].values.astype(float)

    if method == "Conservative adjusted returns":
        mu_a = mu_a * conservative_shrink
    elif method == "User-defined expected returns" and user_expected:
        mu_a = np.array([user_expected.get(c, mu_a[i]) for i, c in enumerate(cols)])

    cov_a = np.nan_to_num(cov_a, nan=0.0)
    try:
        chol = np.linalg.cholesky(cov_a + np.eye(n) * 1e-8)
    except np.linalg.LinAlgError:
        cov_a = cov_a + np.eye(n) * 1e-4
        chol = np.linalg.cholesky(cov_a)

    n_days = int(horizon_years * TRADING_DAYS_PER_YEAR)
    rebalance_days = {"None": 0, "Monthly": 21, "Quarterly": 63, "Annually": 252}.get(rebalance, 0)
    contrib_daily = annual_contribution / TRADING_DAYS_PER_YEAR

    rng = np.random.default_rng(42)
    paths = np.zeros((n_sims, n_days + 1))
    paths[:, 0] = start_value

    for sim in range(n_sims):
        value = start_value
        holdings_value = w * value
        for d in range(1, n_days + 1):
            z = rng.standard_normal(n)
            r_a = mu_a + chol @ z
            r_d = r_a / TRADING_DAYS_PER_YEAR
            holdings_value = holdings_value * (1.0 + r_d)
            value = holdings_value.sum() + contrib_daily
            if rebalance_days > 0 and d % rebalance_days == 0 and value > 0:
                holdings_value = w * value
            paths[sim, d] = value

    endings = paths[:, -1]
    percentiles = [5, 25, 50, 75, 95]
    pct_vals = {p: float(np.percentile(endings, p)) for p in percentiles}

    time_pct = {}
    for p in percentiles:
        time_pct[p] = np.percentile(paths, p, axis=0)

    return {
        "paths": paths,
        "endings": endings,
        "percentiles": pct_vals,
        "time_percentiles": time_pct,
        "horizon_years": horizon_years,
        "n_sims": n_sims,
        "tickers": cols,
        "prob_loss": float((endings < start_value).mean()),
        "prob_double": float((endings >= 2 * start_value).mean()),
        "worst": float(endings.min()),
        "best": float(endings.max()),
        "median": float(np.median(endings)),
    }


# ---------------------------------------------------------------------------
# Portfolio summary
# ---------------------------------------------------------------------------
def load_all_holdings(
    holdings: pd.DataFrame,
    date_from: date,
    date_to: date,
    *,
    force_refresh: bool = False,
) -> tuple[dict[str, pd.Series], dict[str, str], dict[str, str], pd.DataFrame]:
    """Returns histories, sources, failure_reasons, coverage_df."""
    h = clean_holdings_df(holdings)
    histories: dict[str, pd.Series] = {}
    sources: dict[str, str] = {}
    failures: dict[str, str] = {}
    coverage_rows: list[dict[str, Any]] = []

    for t in h["Ticker"]:
        prices, src, fail = fetch_price_history_with_fallback(
            t, date_from, date_to, force_refresh=force_refresh
        )
        histories[t] = prices
        sources[t] = src
        failures[t] = fail
        coverage_rows.append(
            {
                "Ticker": t,
                "Source": src,
                "Failure Reason": fail,
                "Data Points": len(prices),
                "History Start": prices.index.min().date() if not prices.empty else None,
                "History End": prices.index.max().date() if not prices.empty else None,
            }
        )

    return histories, sources, failures, pd.DataFrame(coverage_rows)


def _underlying_cache_path(ticker: str) -> Path:
    UNDERLYING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return UNDERLYING_CACHE_DIR / f"{clean_ticker(ticker)}.parquet"


def _read_underlying_cache(ticker: str) -> pd.DataFrame:
    path = _underlying_cache_path(ticker)
    if not path.is_file():
        return pd.DataFrame(columns=["underlying", "name", "weight_pct", "source"])
    try:
        return pd.read_parquet(path)
    except Exception:
        return pd.DataFrame(columns=["underlying", "name", "weight_pct", "source"])


def _write_underlying_cache(ticker: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    df.to_parquet(_underlying_cache_path(ticker), index=False)


def _parse_fmp_etf_holdings(raw: Any) -> pd.DataFrame:
    if not isinstance(raw, list) or not raw:
        return pd.DataFrame(columns=["underlying", "name", "weight_pct"])
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sym = clean_ticker(item.get("asset") or item.get("symbol") or "")
        w = pd.to_numeric(item.get("weightPercentage"), errors="coerce")
        if not sym or pd.isna(w) or float(w) <= 0:
            continue
        rows.append(
            {
                "underlying": sym,
                "name": str(item.get("name") or sym),
                "weight_pct": float(w),
            }
        )
    if not rows:
        return pd.DataFrame(columns=["underlying", "name", "weight_pct"])
    df = pd.DataFrame(rows)
    df = df.groupby(["underlying", "name"], as_index=False)["weight_pct"].sum()
    total = float(df["weight_pct"].sum())
    if total > 0 and abs(total - 100.0) > 1.0:
        df["weight_pct"] = df["weight_pct"] / total * 100.0
    return df.sort_values("weight_pct", ascending=False).reset_index(drop=True)


def _parse_yahoo_fund_holdings(ticker: str) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError:
        return pd.DataFrame(columns=["underlying", "name", "weight_pct"])

    try:
        fd = yf.Ticker(clean_ticker(ticker)).funds_data
        th = fd.top_holdings
    except Exception:
        return pd.DataFrame(columns=["underlying", "name", "weight_pct"])

    if th is None or th.empty:
        return pd.DataFrame(columns=["underlying", "name", "weight_pct"])

    rows: list[dict[str, Any]] = []
    for sym, row in th.iterrows():
        underlying = clean_ticker(sym)
        if not underlying:
            continue
        pct = pd.to_numeric(row.get("Holding Percent"), errors="coerce")
        if pd.isna(pct):
            continue
        pct_f = float(pct) * 100.0 if float(pct) <= 1.0 else float(pct)
        rows.append(
            {
                "underlying": underlying,
                "name": str(row.get("Name") or underlying),
                "weight_pct": pct_f,
            }
        )
    if not rows:
        return pd.DataFrame(columns=["underlying", "name", "weight_pct"])
    df = pd.DataFrame(rows)
    total = float(df["weight_pct"].sum())
    if total > 0 and abs(total - 100.0) > 1.0:
        df["weight_pct"] = df["weight_pct"] / total * 100.0
    return df.sort_values("weight_pct", ascending=False).reset_index(drop=True)


def fetch_fmp_underlying_holdings(ticker: str) -> tuple[pd.DataFrame, str]:
    sym = clean_ticker(ticker)
    api_key = fmp_api_key()
    if not api_key:
        return pd.DataFrame(), "FMP API key not set"
    try:
        raw = data_loader._fmp_get(fmp_session(), api_key, "etf/holdings", symbol=sym)
        df = _parse_fmp_etf_holdings(raw)
        if df.empty:
            return df, "FMP: no underlying holdings"
        return df, ""
    except Exception as e:
        return pd.DataFrame(), f"FMP: {type(e).__name__}: {e}"


def fetch_yahoo_underlying_holdings(ticker: str) -> tuple[pd.DataFrame, str]:
    df = _parse_yahoo_fund_holdings(ticker)
    if df.empty:
        return df, "Yahoo: no underlying holdings"
    return df, ""


def fetch_underlying_holdings(
    ticker: str,
    *,
    force_refresh: bool = False,
) -> tuple[pd.DataFrame, str, str]:
    """
    Returns ``(constituents_df, source_label, failure_reason)``.

    Columns: underlying, name, weight_pct (sums to ~100 within the fund).
    """
    sym = clean_ticker(ticker)
    if not sym:
        return pd.DataFrame(), SOURCE_MISSING, "empty ticker"

    if not force_refresh:
        cached = _read_underlying_cache(sym)
        if not cached.empty:
            out = cached.drop(columns=["source"], errors="ignore")
            src = str(cached["source"].iloc[0]) if "source" in cached.columns else SOURCE_FMP
            return out, src, ""

    fmp_df, fmp_err = fetch_fmp_underlying_holdings(sym)
    if not fmp_df.empty:
        fmp_df = fmp_df.copy()
        fmp_df["source"] = SOURCE_FMP
        _write_underlying_cache(sym, fmp_df.assign(source=SOURCE_FMP))
        return fmp_df.drop(columns=["source"], errors="ignore"), SOURCE_FMP, ""

    yahoo_df, yahoo_err = fetch_yahoo_underlying_holdings(sym)
    if not yahoo_df.empty:
        _write_underlying_cache(sym, yahoo_df.assign(source=SOURCE_YAHOO))
        return yahoo_df, SOURCE_YAHOO, ""

    return pd.DataFrame(), SOURCE_MISSING, yahoo_err or fmp_err or "no underlying holdings"


def _cap_underlying_table(df: pd.DataFrame, max_rows: int = MAX_UNDERLYING_DISPLAY) -> pd.DataFrame:
    if len(df) <= max_rows:
        return df.copy()
    head = df.head(max_rows).copy()
    other_w = float(df.iloc[max_rows:]["weight_pct"].sum())
    if other_w > 0.01:
        other = pd.DataFrame(
            [{"underlying": "OTHER", "name": f"Other ({len(df) - max_rows} holdings)", "weight_pct": other_w}]
        )
        head = pd.concat([head, other], ignore_index=True)
    return head


def build_underlying_allocation(
    holdings: pd.DataFrame,
    *,
    force_refresh: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """
    Roll fund weights into effective underlying stock/asset allocation.

    Returns:
        summary_df — underlying, name, effective_portfolio_pct (sums to ~portfolio invested %)
        detail_df — parent ticker, parent allocation, underlying, weight in parent, effective pct
        warnings
    """
    h = clean_holdings_df(holdings)
    if h.empty:
        return pd.DataFrame(), pd.DataFrame(), []

    detail_rows: list[dict[str, Any]] = []
    effective: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    for _, row in h.iterrows():
        parent = clean_ticker(row["Ticker"])
        parent_pct = float(row["Allocation %"])
        if parent_pct <= 0:
            continue

        constituents, src, err = fetch_underlying_holdings(parent, force_refresh=force_refresh)
        if constituents.empty:
            constituents = pd.DataFrame(
                [{"underlying": parent, "name": parent, "weight_pct": 100.0}]
            )
            src = "Direct holding"
            if err:
                warnings.append(f"{parent}: treated as direct holding ({err})")

        for _, c in constituents.iterrows():
            underlying = clean_ticker(c["underlying"])
            w_in_parent = float(c["weight_pct"])
            eff = parent_pct * w_in_parent / 100.0
            name = str(c.get("name") or underlying)
            detail_rows.append(
                {
                    "Parent Ticker": parent,
                    "Parent Allocation %": parent_pct,
                    "Holdings Source": src,
                    "Underlying": underlying,
                    "Name": name,
                    "Weight in Parent %": w_in_parent,
                    "Effective Portfolio %": eff,
                }
            )
            if underlying not in effective:
                effective[underlying] = {"name": name, "effective_portfolio_pct": 0.0, "parents": []}
            effective[underlying]["effective_portfolio_pct"] += eff
            effective[underlying]["parents"].append(parent)

    detail_df = pd.DataFrame(detail_rows)
    if not effective:
        return pd.DataFrame(), detail_df, warnings

    summary_rows = [
        {
            "Underlying": k,
            "Name": v["name"],
            "Effective Portfolio %": v["effective_portfolio_pct"],
            "Via Fund(s)": ", ".join(sorted(set(v["parents"]))),
        }
        for k, v in effective.items()
    ]
    summary_df = (
        pd.DataFrame(summary_rows)
        .sort_values("Effective Portfolio %", ascending=False)
        .reset_index(drop=True)
    )
    total_eff = float(summary_df["Effective Portfolio %"].sum())
    if abs(total_eff - float(h["Allocation %"].sum())) > 0.5:
        warnings.append(
            f"Effective underlying total is {total_eff:.2f}% vs fund allocation "
            f"{float(h['Allocation %'].sum()):.2f}% (rounding or partial fund holdings data)."
        )
    return summary_df, detail_df, warnings


def calculate_single_asset_metrics(
    ticker: str,
    price_series: pd.Series,
    source: str,
    allocation_pct: float,
) -> dict[str, Any]:
    """Metrics from the ticker's own full available history (independent of other holdings)."""
    s, ok, _ = normalize_price_series(price_series)
    daily = s.pct_change().dropna() if ok else pd.Series(dtype=float)
    return {
        "Ticker": ticker,
        "Allocation %": allocation_pct,
        "Source": source,
        "Latest Price": float(s.iloc[-1]) if not s.empty else np.nan,
        "History Start": s.index.min().date() if not s.empty else None,
        "History End": s.index.max().date() if not s.empty else None,
        "Data Points": len(s),
        "Ann. Return": calculate_annualized_return(daily),
        "Ann. Volatility": calculate_annualized_volatility(daily),
        "Max Drawdown": calculate_max_drawdown(s),
    }


def get_portfolio_overlap_panel(
    price_panel: pd.DataFrame,
    required_tickers: list[str],
) -> tuple[pd.DataFrame, list[str]]:
    """
    Drop rows where any required ticker is NaN.

    Returns ``(overlap_panel, warnings)``.
    """
    cols = [c for c in required_tickers if c in price_panel.columns]
    if not cols:
        return pd.DataFrame(), ["No holdings with valid price data for overlap."]

    overlap = price_panel[cols].dropna(how="any")
    warnings: list[str] = []
    if overlap.empty:
        warnings.append("No overlapping dates across all holdings.")
        return overlap, warnings

    overlap_start = overlap.index.min()
    latest_ticker_start = overlap_start
    latest_ticker_name = ""
    for t in cols:
        full = price_panel[t].dropna()
        if full.empty:
            continue
        t_start = full.index.min()
        if t_start > latest_ticker_start:
            latest_ticker_start = t_start
            latest_ticker_name = t

    if latest_ticker_name:
        warnings.append(
            f"Overlap starts on {overlap_start.date()} because "
            f"{latest_ticker_name} has limited history (begins {latest_ticker_start.date()})."
        )

    return overlap, warnings


def calculate_portfolio_metrics_from_overlap(
    overlap_panel: pd.DataFrame,
    weights: pd.Series,
    risk_free_rate: float,
) -> dict[str, Any]:
    """Portfolio-level metrics computed on the shared overlapping window."""
    if overlap_panel.empty or overlap_panel.shape[0] < 2:
        return {
            "portfolio_ann_return": float("nan"),
            "portfolio_ann_volatility": float("nan"),
            "portfolio_max_drawdown": float("nan"),
            "portfolio_sharpe": float("nan"),
            "portfolio_start_date": None,
            "portfolio_end_date": None,
            "portfolio_data_points": 0,
        }

    daily = calculate_returns(overlap_panel)
    w = weights.reindex(daily.columns).fillna(0.0)
    if w.sum() > 0:
        w = w / w.sum()
    port_r = (daily * w).sum(axis=1)
    equity = portfolio_equity_curve(port_r)

    port_ann_ret = calculate_annualized_return(port_r)
    port_ann_vol = calculate_annualized_volatility(port_r)
    port_dd = calculate_max_drawdown(equity)
    sharpe = (
        (port_ann_ret - risk_free_rate) / port_ann_vol
        if np.isfinite(port_ann_ret) and np.isfinite(port_ann_vol) and port_ann_vol > 0
        else float("nan")
    )
    return {
        "portfolio_ann_return": port_ann_ret,
        "portfolio_ann_volatility": port_ann_vol,
        "portfolio_max_drawdown": port_dd,
        "portfolio_sharpe": sharpe,
        "portfolio_start_date": overlap_panel.index.min().date(),
        "portfolio_end_date": overlap_panel.index.max().date(),
        "portfolio_data_points": len(overlap_panel),
    }


def build_portfolio_summary(
    holdings: pd.DataFrame,
    histories: dict[str, pd.Series],
    sources: dict[str, str],
    *,
    risk_free_rate: float = 0.04,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any], list[str]]:
    """
    Returns ``(asset_metrics_df, correlation_matrix, portfolio_metrics_dict, overlap_warnings)``.

    Asset-level metrics use each ticker's own full history.
    Portfolio-level metrics use the shared overlapping window.
    """
    h = clean_holdings_df(holdings)
    weights = h.set_index("Ticker")["Allocation %"] / 100.0

    asset_rows = [
        calculate_single_asset_metrics(
            t,
            histories.get(t, pd.Series(dtype=float)),
            sources.get(t, SOURCE_MISSING),
            float(weights.get(t, 0) * 100),
        )
        for t in h["Ticker"]
    ]
    asset_df = pd.DataFrame(asset_rows)

    panel = build_price_panel(h, histories)
    tickers_with_data = [t for t in h["Ticker"] if t in panel.columns]
    overlap, overlap_warnings = get_portfolio_overlap_panel(panel, tickers_with_data)

    if not overlap.empty and overlap.shape[0] < 30:
        overlap_warnings.append(
            f"Portfolio overlap window has only {overlap.shape[0]} trading days — "
            "portfolio metrics may not be reliable."
        )

    overlap_daily = calculate_returns(overlap) if not overlap.empty else pd.DataFrame()
    corr = overlap_daily.corr() if not overlap_daily.empty else pd.DataFrame()

    port_metrics = calculate_portfolio_metrics_from_overlap(overlap, weights, risk_free_rate)
    return asset_df, corr, port_metrics, overlap_warnings


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------
def _format_excel_sheet(ws: Any, df: pd.DataFrame, pct_cols: set[str], money_cols: set[str]) -> None:
    if df.empty:
        return
    ws.freeze_panes = "A2"
    if ws.max_row >= 1:
        ws.auto_filter.ref = ws.dimensions
    headers = list(df.columns)
    for col_idx, col_name in enumerate(headers, start=1):
        letter = ws.cell(row=1, column=col_idx).column_letter
        max_len = len(str(col_name))
        for row_idx in range(2, min(len(df) + 2, 100)):
            val = ws.cell(row=row_idx, column=col_idx).value
            if val is not None:
                max_len = max(max_len, len(str(val)))
            if col_name in pct_cols:
                ws.cell(row=row_idx, column=col_idx).number_format = "0.00%"
            elif col_name in money_cols:
                ws.cell(row=row_idx, column=col_idx).number_format = "$#,##0"
        ws.column_dimensions[letter].width = min(max_len + 2, 40)


def build_excel_report(
    holdings: pd.DataFrame,
    holding_summary: pd.DataFrame,
    port_metrics: dict[str, float],
    scenario_summary: pd.DataFrame,
    scenario_details: dict[str, pd.DataFrame],
    mc_results: dict[int, dict[str, Any]],
    coverage: pd.DataFrame,
    correlation: pd.DataFrame,
) -> bytes:
    buffer = io.BytesIO()
    pct_cols = {
        "Allocation %",
        "Weight %",
        "Portfolio Return",
        "Max Drawdown",
        "Ann. Volatility",
        "Ann. Return",
        "Holding Return",
        "Contribution to Portfolio Return",
        "portfolio_ann_return",
        "portfolio_ann_volatility",
        "portfolio_max_drawdown",
    }
    money_cols = {"Starting Value", "Median Ending", "5th Pct Ending", "95th Pct Ending"}

    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        clean_holdings_df(holdings).to_excel(writer, sheet_name="Holdings", index=False)

        holding_summary.to_excel(writer, sheet_name="Asset-Level Metrics", index=False)

        summary_rows = [
            {"Metric": k, "Value": v} for k, v in port_metrics.items()
        ]
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Portfolio Summary", index=False)

        scenario_summary.to_excel(writer, sheet_name="Scenario Summary", index=False)

        detail_frames = []
        for name, df in scenario_details.items():
            if "__" in name or df.empty or not isinstance(df, pd.DataFrame):
                continue
            d = df.copy()
            d.insert(0, "Scenario", name)
            detail_frames.append(d)
        if detail_frames:
            pd.concat(detail_frames, ignore_index=True).to_excel(
                writer, sheet_name="Scenario Holding Details", index=False
            )

        mc_rows: list[dict[str, Any]] = []
        pct_rows: list[dict[str, Any]] = []
        for horizon, res in mc_results.items():
            if "error" in res:
                mc_rows.append({"Horizon Years": horizon, "Error": res["error"]})
                continue
            mc_rows.append(
                {
                    "Horizon Years": horizon,
                    "Simulations": res["n_sims"],
                    "Median Ending": res["median"],
                    "5th Pct Ending": res["percentiles"][5],
                    "25th Pct Ending": res["percentiles"][25],
                    "75th Pct Ending": res["percentiles"][75],
                    "95th Pct Ending": res["percentiles"][95],
                    "Prob Loss": res["prob_loss"],
                    "Prob Double": res["prob_double"],
                    "Worst Ending": res["worst"],
                    "Best Ending": res["best"],
                }
            )
            for p, v in res["percentiles"].items():
                pct_rows.append({"Horizon Years": horizon, "Percentile": p, "Ending Value": v})

        pd.DataFrame(mc_rows).to_excel(writer, sheet_name="Monte Carlo Summary", index=False)
        pd.DataFrame(pct_rows).to_excel(writer, sheet_name="Monte Carlo Percentiles", index=False)
        coverage.to_excel(writer, sheet_name="Data Coverage", index=False)
        correlation.to_excel(writer, sheet_name="Correlation Matrix", index=True)

        for sheet in writer.sheets.values():
            pass  # formatting applied below per sheet

        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            if sheet_name == "Holdings":
                _format_excel_sheet(ws, clean_holdings_df(holdings), {"Allocation %"}, set())
            elif sheet_name == "Scenario Summary":
                _format_excel_sheet(
                    ws,
                    scenario_summary,
                    {"Portfolio Return", "Max Drawdown", "Ann. Volatility"},
                    set(),
                )

    buffer.seek(0)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# UI renderers
# ---------------------------------------------------------------------------
def render_portfolio_inputs() -> tuple[pd.DataFrame, bool, bool, float]:
    st.subheader("Portfolio Holdings")
    edited = st.data_editor(
        DEFAULT_HOLDINGS if "holdings_df" not in st.session_state else st.session_state["holdings_df"],
        num_rows="dynamic",
        use_container_width=True,
        key="holdings_editor",
    )
    st.session_state["holdings_df"] = edited

    normalize = st.checkbox("Normalize weights to 100%", value=False)
    holdings, total, warn = validate_weights(edited, normalize=normalize)
    st.metric("Total allocation", f"{total:.2f}%")
    if warn:
        st.warning(warn)

    force_refresh = st.checkbox("Force refresh price cache", value=False)
    risk_free = st.number_input("Risk-free rate (annual, for Sharpe)", value=0.04, step=0.005, format="%.3f")

    if st.button("Load prices & run analytics", type="primary", key="run_portfolio"):
        st.session_state["portfolio_run"] = True
        st.session_state["force_refresh"] = force_refresh
        st.session_state["risk_free_rate"] = float(risk_free)
        st.session_state["holdings_normalized"] = holdings

    return holdings, force_refresh, normalize, float(risk_free)


def render_current_portfolio_breakdown() -> None:
    if "holding_summary" not in st.session_state:
        st.info("Run analysis from the **Inputs** tab first.")
        return

    # --- Asset-Level Holding Metrics ---
    st.subheader("Asset-Level Holding Metrics")
    st.caption(
        "These metrics use each holding's own full available history, "
        "so they do not change when other holdings are swapped."
    )
    hs = st.session_state["holding_summary"]
    display_hs = hs.copy()
    for col in ("Ann. Return", "Ann. Volatility", "Max Drawdown"):
        if col in display_hs.columns:
            display_hs[col] = display_hs[col].apply(lambda x: _fmt_pct(x))
    if "Latest Price" in display_hs.columns:
        display_hs["Latest Price"] = display_hs["Latest Price"].apply(
            lambda x: f"{float(x):.2f}" if pd.notna(x) and np.isfinite(float(x)) else "N/A"
        )
    st.dataframe(display_hs, use_container_width=True, hide_index=True)

    # --- Portfolio-Level Metrics ---
    st.markdown("---")
    st.subheader("Portfolio-Level Metrics")
    st.caption(
        "These metrics use the shared overlapping window across all selected holdings."
    )

    pm = st.session_state.get("port_metrics", {})
    for w in st.session_state.get("overlap_warnings", []):
        st.warning(w)

    c_dates = st.columns(3)
    c_dates[0].metric("Portfolio start date", str(pm.get("portfolio_start_date", "—")))
    c_dates[1].metric("Portfolio end date", str(pm.get("portfolio_end_date", "—")))
    c_dates[2].metric("Portfolio data points", f"{pm.get('portfolio_data_points', 0):,}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Portfolio ann. return", _fmt_pct(pm.get("portfolio_ann_return")))
    c2.metric("Portfolio ann. volatility", _fmt_pct(pm.get("portfolio_ann_volatility")))
    c3.metric("Portfolio max drawdown", _fmt_pct(pm.get("portfolio_max_drawdown")))
    c4.metric("Portfolio Sharpe", f"{pm.get('portfolio_sharpe', float('nan')):.2f}")

    st.markdown("---")
    st.subheader("Underlying holdings exposure")
    st.caption(
        "Effective allocation to each underlying stock/asset by combining your fund weights "
        "with each fund's reported holdings (FMP first, Yahoo fallback). Direct stock positions "
        "count as 100% in that ticker."
    )

    underlying_summary = st.session_state.get("underlying_summary")
    underlying_detail = st.session_state.get("underlying_detail")
    underlying_warnings = st.session_state.get("underlying_warnings", [])

    for w in underlying_warnings:
        st.warning(w)

    if underlying_summary is not None and not underlying_summary.empty:
        if len(underlying_summary) > MAX_UNDERLYING_DISPLAY:
            show_df = underlying_summary.head(MAX_UNDERLYING_DISPLAY).copy()
            other_w = float(underlying_summary.iloc[MAX_UNDERLYING_DISPLAY:]["Effective Portfolio %"].sum())
            if other_w > 0.01:
                show_df = pd.concat(
                    [
                        show_df,
                        pd.DataFrame(
                            [
                                {
                                    "Underlying": "OTHER",
                                    "Name": f"Other ({len(underlying_summary) - MAX_UNDERLYING_DISPLAY} positions)",
                                    "Effective Portfolio %": other_w,
                                    "Via Fund(s)": "",
                                }
                            ]
                        ),
                    ],
                    ignore_index=True,
                )
            st.caption(
                f"Showing top {MAX_UNDERLYING_DISPLAY} underlying positions; remainder grouped as **OTHER**."
            )
        else:
            show_df = underlying_summary.copy()

        display = show_df.copy()
        display["Effective Portfolio %"] = display["Effective Portfolio %"].apply(
            lambda x: f"{float(x):.2f}%" if pd.notna(x) else "N/A"
        )
        st.dataframe(display, use_container_width=True, hide_index=True)

        try:
            import plotly.graph_objects as go

            chart_df = underlying_summary.head(25)
            fig = go.Figure(
                go.Bar(
                    x=chart_df["Underlying"],
                    y=chart_df["Effective Portfolio %"],
                    text=[f"{v:.2f}%" for v in chart_df["Effective Portfolio %"]],
                    textposition="outside",
                )
            )
            fig.update_layout(
                title="Top underlying positions (% of portfolio)",
                xaxis_title="Underlying",
                yaxis_title="Effective portfolio %",
                height=420,
            )
            st.plotly_chart(fig, use_container_width=True)
        except ImportError:
            pass

        with st.expander("Underlying holdings detail (by fund)"):
            if underlying_detail is not None and not underlying_detail.empty:
                det = underlying_detail.copy()
                for col in ("Parent Allocation %", "Weight in Parent %", "Effective Portfolio %"):
                    if col in det.columns:
                        det[col] = det[col].apply(
                            lambda x: f"{float(x):.2f}%" if pd.notna(x) else "N/A"
                        )
                st.dataframe(det, use_container_width=True, hide_index=True)
    else:
        st.info("No underlying holdings data available for the current portfolio.")

    st.markdown("---")
    st.markdown("**Correlation matrix**")
    corr = st.session_state.get("correlation")
    if corr is not None and not corr.empty:
        st.dataframe(corr.style.format("{:.2f}"), use_container_width=True)


def _fmt_pct(x: Any) -> str:
    try:
        if pd.isna(x) or not np.isfinite(float(x)):
            return "N/A"
        return f"{float(x) * 100:.2f}%"
    except Exception:
        return "N/A"


def render_scenario_stress_tests() -> None:
    if "scenario_summary" not in st.session_state:
        st.info("Run analysis from the **Inputs** tab first.")
        return

    st.subheader("Scenario Stress Tests")
    summary = st.session_state["scenario_summary"]
    display = summary.copy()
    for col in ("Portfolio Return", "Max Drawdown", "Ann. Volatility"):
        if col in display.columns:
            display[col] = display[col].apply(lambda x: _fmt_pct(x))
    st.dataframe(display, use_container_width=True, hide_index=True)

    details = st.session_state.get("scenario_details", {})
    try:
        import plotly.graph_objects as go
    except ImportError:
        go = None

    for preset in SCENARIO_PRESETS:
        name = preset["name"]
        with st.expander(f"{name} — holding detail"):
            d = details.get(name)
            if d is not None and not d.empty:
                dd = d.copy()
                for col in ("Holding Return", "Contribution to Portfolio Return"):
                    if col in dd.columns:
                        dd[col] = dd[col].apply(lambda x: _fmt_pct(x))
                st.dataframe(dd, use_container_width=True, hide_index=True)
            row = summary[summary["Scenario"] == name]
            if not row.empty and row.iloc[0].get("Coverage Warning"):
                st.warning(str(row.iloc[0]["Coverage Warning"]))

            if go is not None:
                eq = details.get(f"{name}__equity")
                dd_curve = details.get(f"{name}__drawdown")
                contrib = details.get(f"{name}__contrib")
                if eq is not None and not eq.empty:
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=eq.index, y=eq.values, name="Portfolio equity"))
                    fig.update_layout(title=f"{name} — equity curve", height=320)
                    st.plotly_chart(fig, use_container_width=True)
                if dd_curve is not None and not dd_curve.empty:
                    fig2 = go.Figure()
                    fig2.add_trace(go.Scatter(x=dd_curve.index, y=dd_curve.values, name="Drawdown"))
                    fig2.update_layout(title=f"{name} — drawdown", height=280)
                    st.plotly_chart(fig2, use_container_width=True)
                if contrib is not None and not contrib.empty:
                    fig3 = go.Figure(go.Bar(x=contrib.index, y=contrib.values))
                    fig3.update_layout(title=f"{name} — return contribution", height=280)
                    st.plotly_chart(fig3, use_container_width=True)


def render_monte_carlo_simulation() -> None:
    if "price_panel" not in st.session_state:
        st.info("Run analysis from the **Inputs** tab first.")
        return

    st.subheader("Monte Carlo Simulation")
    st.caption(
        "Monte Carlo uses the shared overlapping historical return window across all "
        "selected holdings by default."
    )
    holdings = st.session_state.get("holdings_normalized", DEFAULT_HOLDINGS)
    panel = st.session_state["price_panel"]

    c1, c2, c3 = st.columns(3)
    with c1:
        horizon = st.selectbox("Simulation horizon", ["10 years", "30 years"], index=0)
        horizon_years = 10 if "10" in horizon else 30
    with c2:
        n_sims = st.number_input("Number of simulations", min_value=100, max_value=10000, value=1000, step=100)
    with c3:
        start_value = st.number_input("Starting portfolio value ($)", min_value=1000, value=100000, step=1000)

    c4, c5, c6 = st.columns(3)
    with c4:
        annual_contrib = st.number_input("Annual contribution ($)", value=0, step=1000)
    with c5:
        rebalance = st.selectbox("Rebalancing", ["None", "Monthly", "Quarterly", "Annually"])
    with c6:
        method = st.selectbox(
            "Return assumption",
            ["Historical mean/covariance", "Conservative adjusted returns", "User-defined expected returns"],
        )

    user_exp: dict[str, float] = {}
    if method == "User-defined expected returns":
        st.caption("Enter annual expected return per ticker (decimal, e.g. 0.07 = 7%).")
        for t in clean_holdings_df(holdings)["Ticker"]:
            user_exp[t] = st.number_input(f"{t} expected return", value=0.07, step=0.01, key=f"exp_{t}")

    if st.button("Run Monte Carlo", type="primary", key="run_mc"):
        with st.spinner("Simulating…"):
            res = run_monte_carlo_simulation(
                holdings,
                panel,
                horizon_years=horizon_years,
                n_sims=int(n_sims),
                start_value=float(start_value),
                annual_contribution=float(annual_contrib),
                rebalance=rebalance,
                method=method,
                user_expected=user_exp,
            )
        if "mc_results" not in st.session_state:
            st.session_state["mc_results"] = {}
        st.session_state["mc_results"][horizon_years] = res

    mc_all: dict[int, Any] = st.session_state.get("mc_results", {})
    for hy, res in mc_all.items():
        st.markdown(f"### {hy}-year horizon")
        if "error" in res:
            st.error(res["error"])
            continue
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Median ending", f"${res['median']:,.0f}")
        c2.metric("5th percentile", f"${res['percentiles'][5]:,.0f}")
        c3.metric("95th percentile", f"${res['percentiles'][95]:,.0f}")
        c4.metric("P(loss)", _fmt_pct(res["prob_loss"]))
        st.metric("P(doubling)", _fmt_pct(res["prob_double"]))
        st.caption(f"Worst: ${res['worst']:,.0f} | Best: ${res['best']:,.0f}")

        try:
            import plotly.graph_objects as go
        except ImportError:
            st.warning("Install plotly for Monte Carlo charts: pip install plotly>=5.20")
            continue

        days = np.arange(res["paths"].shape[1])
        tp = res["time_percentiles"]
        fig = go.Figure()
        for p, label in [(5, "5th"), (50, "Median"), (95, "95th")]:
            fig.add_trace(go.Scatter(x=days, y=tp[p], name=label))
        fig.update_layout(title=f"{hy}Y — percentile cone", height=360)
        st.plotly_chart(fig, use_container_width=True)

        sample = res["paths"][: min(30, res["n_sims"])]
        fig2 = go.Figure()
        for i in range(sample.shape[0]):
            fig2.add_trace(go.Scatter(x=days, y=sample[i], opacity=0.25, showlegend=False))
        fig2.update_layout(title=f"{hy}Y — sample paths", height=360)
        st.plotly_chart(fig2, use_container_width=True)

        fig3 = go.Figure(go.Histogram(x=res["endings"]))
        fig3.update_layout(title=f"{hy}Y — ending value distribution", height=300)
        st.plotly_chart(fig3, use_container_width=True)


def render_excel_export() -> None:
    st.subheader("Export")
    if "holding_summary" not in st.session_state:
        st.info("Run analysis from the **Inputs** tab first.")
        return
    if st.button("Prepare Excel Report", type="primary", key="prep_excel"):
        try:
            with st.spinner("Building workbook…"):
                st.session_state["excel_bytes"] = build_excel_report(
                    st.session_state.get("holdings_normalized", DEFAULT_HOLDINGS),
                    st.session_state["holding_summary"],
                    st.session_state.get("port_metrics", {}),
                    st.session_state["scenario_summary"],
                    st.session_state.get("scenario_details", {}),
                    st.session_state.get("mc_results", {}),
                    st.session_state.get("coverage", pd.DataFrame()),
                    st.session_state.get("correlation", pd.DataFrame()),
                )
            st.success("Report ready.")
        except Exception as e:
            traceback.print_exc()
            st.error(f"Export failed: {e}")

    if st.session_state.get("excel_bytes"):
        st.download_button(
            "Download Excel Report",
            data=st.session_state["excel_bytes"],
            file_name=f"portfolio_stress_{date.today().isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


def run_portfolio_analysis(
    holdings: pd.DataFrame,
    *,
    force_refresh: bool = False,
    risk_free_rate: float = 0.04,
) -> None:
    date_to = date.today()
    date_from = date(1995, 1, 1)

    histories, sources, failures, coverage = load_all_holdings(
        holdings, date_from, date_to, force_refresh=force_refresh
    )
    panel = build_price_panel(holdings, histories)
    holding_summary, corr, port_metrics, overlap_warnings = build_portfolio_summary(
        holdings, histories, sources, risk_free_rate=risk_free_rate
    )
    scenario_summary, scenario_details = run_scenario_analysis(holdings, histories, sources)
    underlying_summary, underlying_detail, underlying_warnings = build_underlying_allocation(
        holdings, force_refresh=force_refresh
    )

    st.session_state["histories"] = histories
    st.session_state["sources"] = sources
    st.session_state["coverage"] = coverage
    st.session_state["price_panel"] = panel
    st.session_state["holding_summary"] = holding_summary
    st.session_state["correlation"] = corr
    st.session_state["port_metrics"] = port_metrics
    st.session_state["scenario_summary"] = scenario_summary
    st.session_state["scenario_details"] = scenario_details
    st.session_state["overlap_warnings"] = overlap_warnings
    st.session_state["underlying_summary"] = underlying_summary
    st.session_state["underlying_detail"] = underlying_detail
    st.session_state["underlying_warnings"] = underlying_warnings
    st.session_state["holdings_normalized"] = holdings


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption(
        "Enter portfolio weights, stress-test historical regimes, and run Monte Carlo simulations. "
        f"**{SOURCE_FMP}** prices first; **{SOURCE_YAHOO}** only when FMP has no usable history."
    )

    if not fmp_api_key():
        st.error("Set **FMP_API_KEY** in `.env` to load FMP prices.")

    tab_in, tab_bd, tab_st, tab_mc, tab_ex = st.tabs(
        ["Inputs", "Portfolio Breakdown", "Stress Tests", "Monte Carlo", "Export"]
    )

    with tab_in:
        holdings, force_refresh, _norm, risk_free = render_portfolio_inputs()
        if st.session_state.pop("portfolio_run", False):
            try:
                with st.spinner("Loading prices and running analytics…"):
                    h = st.session_state.get("holdings_normalized", holdings)
                    run_portfolio_analysis(
                        h,
                        force_refresh=st.session_state.get("force_refresh", force_refresh),
                        risk_free_rate=st.session_state.get("risk_free_rate", risk_free),
                    )
                st.success("Analysis complete. Open other tabs for results.")
            except Exception as e:
                traceback.print_exc()
                st.error(f"Analysis failed: {type(e).__name__}: {e}")

    with tab_bd:
        render_current_portfolio_breakdown()

    with tab_st:
        render_scenario_stress_tests()

    with tab_mc:
        render_monte_carlo_simulation()

    with tab_ex:
        render_excel_export()


main()
