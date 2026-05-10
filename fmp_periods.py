"""Compound sector returns over calendar / trading windows from daily % changes."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd


def compound_daily_pct(returns_pct: pd.Series) -> float:
    """FMP daily values are in percent points (e.g. 0.76 == +0.76%)."""
    r = pd.to_numeric(returns_pct, errors="coerce").dropna()
    if r.empty:
        return float("nan")
    return float((np.prod(1.0 + r / 100.0) - 1.0) * 100.0)


def build_sector_period_summary(hist: pd.DataFrame, change_col: str) -> pd.DataFrame:
    """
    One row per sector: 1W (5 trading days), 1M (21 trading days), YTD, 1Y (calendar).
    """
    if hist.empty or change_col not in hist.columns:
        return pd.DataFrame()

    df = hist.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df[change_col] = pd.to_numeric(df[change_col], errors="coerce")
    df = df.dropna(subset=[change_col])

    as_of = df["date"].max()
    if pd.isna(as_of):
        return pd.DataFrame()

    ytd_start = pd.Timestamp(date(as_of.year, 1, 1))
    one_year_start = as_of - pd.Timedelta(days=365)

    rows: list[dict] = []
    for sector, g in df.groupby("sector", sort=True):
        g = g.sort_values("date")
        ex = g["exchange"].iloc[-1] if "exchange" in g.columns and g["exchange"].notna().any() else None

        last5 = g.tail(5)[change_col]
        last21 = g.tail(21)[change_col]
        ytd = g.loc[g["date"] >= ytd_start, change_col]
        one_y = g.loc[g["date"] >= one_year_start, change_col]

        rows.append(
            {
                "sector": sector,
                "exchange": ex,
                "as_of": as_of.normalize(),
                "1W_%": compound_daily_pct(last5),
                "1M_%": compound_daily_pct(last21),
                "YTD_%": compound_daily_pct(ytd),
                "1Y_%": compound_daily_pct(one_y),
            }
        )

    out = pd.DataFrame(rows)
    return out.sort_values("sector").reset_index(drop=True)


def history_fetch_date_range(as_of: date) -> tuple[date, date]:
    """Wide enough `from` for ~1Y back plus full YTD."""
    from_d = min(date(as_of.year, 1, 1), as_of - timedelta(days=420))
    return from_d, as_of


def build_etf_proxy_period_summary(etf_hist: pd.DataFrame) -> pd.DataFrame:
    """
    Same windows as FMP sector summary: 1W (5 sessions), 1M (21), YTD, 1Y,
    from daily close returns (pct_change * 100), compounded like FMP daily series.
    """
    if etf_hist.empty or "close" not in etf_hist.columns:
        return pd.DataFrame()

    df = etf_hist.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])

    as_of = df["date"].max()
    if pd.isna(as_of):
        return pd.DataFrame()

    ytd_start = pd.Timestamp(date(as_of.year, 1, 1))
    one_year_start = as_of - pd.Timedelta(days=365)

    rows: list[dict] = []
    for sector, g in df.groupby("sector", sort=True):
        g = g.sort_values("date")
        sym = g["symbol"].iloc[-1] if "symbol" in g.columns and g["symbol"].notna().any() else None
        daily_pct = g["close"].pct_change() * 100.0
        g2 = g.assign(daily_pct=daily_pct).dropna(subset=["daily_pct"])

        last5 = g2["daily_pct"].tail(5)
        last21 = g2["daily_pct"].tail(21)
        ytd = g2.loc[g2["date"] >= ytd_start, "daily_pct"]
        one_y = g2.loc[g2["date"] >= one_year_start, "daily_pct"]

        rows.append(
            {
                "sector": sector,
                "etf_proxy": sym,
                "as_of": as_of.normalize(),
                "1W_%": compound_daily_pct(last5),
                "1M_%": compound_daily_pct(last21),
                "YTD_%": compound_daily_pct(ytd),
                "1Y_%": compound_daily_pct(one_y),
            }
        )

    out = pd.DataFrame(rows)
    return out.sort_values("sector").reset_index(drop=True)


def merge_fmp_and_etf_periods(fmp: pd.DataFrame, etf: pd.DataFrame) -> pd.DataFrame:
    """Side-by-side FMP vs ETF proxy with differences (FMP - ETF), same horizons."""
    if fmp.empty or etf.empty:
        return pd.DataFrame()

    fcols = ["sector", "1W_%", "1M_%", "YTD_%", "1Y_%"]
    if "exchange" in fmp.columns:
        fcols.insert(1, "exchange")
    f = fmp[[c for c in fcols if c in fmp.columns]].copy()
    if "as_of" in fmp.columns:
        f["as_of_FMP"] = fmp["as_of"]

    e = etf[["sector", "etf_proxy", "1W_%", "1M_%", "YTD_%", "1Y_%"]].copy()
    if "as_of" in etf.columns:
        e["as_of_ETF"] = etf["as_of"]

    f = f.rename(
        columns={"1W_%": "1W_FMP_%", "1M_%": "1M_FMP_%", "YTD_%": "YTD_FMP_%", "1Y_%": "1Y_FMP_%"}
    )
    e = e.rename(
        columns={"1W_%": "1W_ETF_%", "1M_%": "1M_ETF_%", "YTD_%": "YTD_ETF_%", "1Y_%": "1Y_ETF_%"}
    )

    m = f.merge(e, on="sector", how="outer")
    for w in ("1W", "1M", "YTD", "1Y"):
        cf, ce = f"{w}_FMP_%", f"{w}_ETF_%"
        m[f"{w}_diff_FMP_minus_ETF_%"] = pd.to_numeric(m[cf], errors="coerce") - pd.to_numeric(
            m[ce], errors="coerce"
        )
    return m.sort_values("sector").reset_index(drop=True)
