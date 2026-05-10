"""
Sector return construction:

- Equal-weight: average of constituent *daily* simple returns
- Cap-weight: daily re-normalized weights over names with non-missing returns
- ETF proxy: dividend-adjusted ETF daily returns

Then we compare breadth (EW vs CW) and tracking (CW vs ETF).
"""

from __future__ import annotations

import sys
from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd
import requests

import config
import data_loader
import metrics

# Require a few names so one mega-cap doesn't dominate statistics visually.
MIN_NAMES_PER_SECTOR: int = 3


def _cap_weight_row(row: pd.Series, weights: pd.Series) -> float:
    """One-day cap-weighted return with renormalization over non-NaN names."""
    w = weights.reindex(row.index).astype(float)
    r = row.astype(float)
    mask = r.notna() & w.notna() & (w > 0)
    if int(mask.sum()) == 0:
        return float("nan")
    w2 = w[mask].to_numpy(dtype=float)
    r2 = r[mask].to_numpy(dtype=float)
    w2 = w2 / float(np.sum(w2))
    return float(np.dot(w2, r2))


def _build_returns_map(
    session: requests.Session,
    api_key: str,
    symbols: Iterable[str],
    date_from: date,
    date_to: date,
    *,
    force_refresh: bool,
) -> dict[str, pd.DataFrame]:
    """symbol -> DataFrame(date, ret_decimal)."""
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        px = data_loader.get_price_history(session, api_key, sym, date_from, date_to, force_refresh=force_refresh)
        if px.empty:
            continue
        r = metrics.calculate_returns(px, price_col="adjClose")
        out[sym] = pd.DataFrame({"date": px["date"], "ret": r})
    return out


def _sector_composite(
    sector: str,
    tickers: list[str],
    caps: pd.Series,
    ret_map: dict[str, pd.DataFrame],
) -> pd.DataFrame | None:
    """Daily equal-weight and cap-weight sector returns."""
    frames = []
    for t in tickers:
        if t not in ret_map:
            continue
        df = ret_map[t][["date", "ret"]].rename(columns={"ret": t})
        frames.append(df)

    if len(frames) < MIN_NAMES_PER_SECTOR:
        print(
            f"  warning: {sector} skipped (only {len(frames)} usable tickers with prices).",
            file=sys.stderr,
        )
        return None

    merged = frames[0]
    for nxt in frames[1:]:
        merged = pd.merge(merged, nxt, on="date", how="outer")

    merged = merged.sort_values("date")
    tick_cols = [c for c in merged.columns if c != "date"]

    # Equal weight across available names each day
    ew = merged[tick_cols].mean(axis=1, skipna=True)

    w = caps.reindex(tick_cols).astype(float)
    cw = merged[tick_cols].apply(lambda row: _cap_weight_row(row, w), axis=1)

    out = pd.DataFrame({"date": merged["date"], "ew": ew, "cw": cw}).dropna(subset=["date"])
    return out


def _slice_by_window(dates: pd.Series, rets: pd.Series, window: str, as_of: pd.Timestamp) -> pd.Series:
    df = pd.DataFrame({"d": pd.to_datetime(dates, errors="coerce"), "r": rets}).dropna().sort_values("d")
    if df.empty:
        return pd.Series(dtype=float)
    if window == "1W":
        return df["r"].tail(config.TRADING_DAYS_1W)
    if window == "1M":
        return df["r"].tail(config.TRADING_DAYS_1M)
    if window == "YTD":
        y0 = pd.Timestamp(int(as_of.year), 1, 1)
        return df.loc[df["d"] >= y0, "r"]
    raise ValueError(f"Unknown window: {window}")


def _window_total_pct(dates: pd.Series, rets: pd.Series, window: str, as_of: pd.Timestamp) -> float:
    s = _slice_by_window(dates, rets, window, as_of)
    return float(metrics.cumulative_return(s))


def primary_etf_column(windows: tuple[str, ...]) -> str:
    """Pick a stable ETF performance column for sorting / rankings."""
    if "YTD" in windows:
        return "etf_YTD_pct"
    return f"etf_{windows[-1]}_pct"


def primary_breadth_column(windows: tuple[str, ...]) -> str:
    if "YTD" in windows:
        return "breadth_spread_YTD_pct"
    return f"breadth_spread_{windows[-1]}_pct"


def build_sector_outputs(
    session: requests.Session,
    api_key: str,
    universe: pd.DataFrame,
    etf_prices: dict[str, pd.DataFrame],
    *,
    windows: tuple[str, ...],
    date_from: date,
    date_to: date,
    force_refresh: bool,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """
    Returns:
      summary table (one row per sector)
      diagnostics: sector -> composite panel (date, ew, cw, etf)
    """
    symbols = universe["symbol"].astype(str).str.upper().unique().tolist()
    print(f"[model] Building return panel for {len(symbols)} symbols...", flush=True)
    ret_map = _build_returns_map(session, api_key, symbols, date_from, date_to, force_refresh=force_refresh)

    caps = universe.set_index("symbol")["marketCap"].astype(float)

    panels: dict[str, pd.DataFrame] = {}
    rows: list[dict] = []

    for sector, etf_sym in config.SECTOR_ETF_MAP.items():
        tickers = universe.loc[universe["sector"] == sector, "symbol"].astype(str).str.upper().tolist()
        comp = _sector_composite(sector, tickers, caps, ret_map)
        if comp is None:
            continue

        etf_df = etf_prices.get(sector, pd.DataFrame())
        if etf_df.empty:
            print(f"  warning: missing ETF prices for {sector} ({etf_sym})", file=sys.stderr)
            continue

        etf_ret = metrics.calculate_returns(etf_df, price_col="adjClose")
        etf_part = pd.DataFrame({"date": etf_df["date"], "etf": etf_ret})

        panel = pd.merge(comp, etf_part, on="date", how="inner").sort_values("date")
        panel = panel.dropna(subset=["ew", "cw", "etf"], how="any")
        if panel.empty:
            print(f"  warning: empty merged panel for {sector}", file=sys.stderr)
            continue

        as_of = pd.to_datetime(panel["date"].max())

        rec: dict = {"sector": sector, "etf_ticker": etf_sym, "as_of": as_of.date().isoformat()}
        for w in windows:
            rec[f"equal_weight_{w}_pct"] = _window_total_pct(panel["date"], panel["ew"], w, as_of)
            rec[f"cap_weight_{w}_pct"] = _window_total_pct(panel["date"], panel["cw"], w, as_of)
            rec[f"etf_{w}_pct"] = _window_total_pct(panel["date"], panel["etf"], w, as_of)

        for w in windows:
            ew = rec[f"equal_weight_{w}_pct"]
            cw = rec[f"cap_weight_{w}_pct"]
            etf = rec[f"etf_{w}_pct"]
            rec[f"breadth_spread_{w}_pct"] = ew - cw  # breadth vs concentration
            rec[f"etf_spread_{w}_pct"] = cw - etf  # cap basket vs ETF

        # YTD Sharpe on equal-weight daily series (bonus diagnostics)
        ytd_mask = pd.to_datetime(panel["date"]) >= pd.Timestamp(int(as_of.year), 1, 1)
        ytd_ew = panel.loc[ytd_mask, "ew"]
        rec["ew_ytd_sharpe"] = metrics.sharpe_ratio(ytd_ew, config.RISK_FREE_RATE)

        rows.append(rec)
        panels[sector] = panel

    summary = pd.DataFrame(rows)
    if not summary.empty:
        sort_col = primary_etf_column(windows)
        summary = summary.sort_values(sort_col, ascending=False, na_position="last").reset_index(drop=True)
    return summary, panels


def rank_sectors(summary: pd.DataFrame, windows: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    """Small helper tables for pretty printing."""
    if summary.empty:
        return {"top_etf": summary, "bottom_etf": summary, "breadth": summary}

    etf_col = primary_etf_column(windows)
    breadth_col = primary_breadth_column(windows)

    top = summary.nlargest(3, etf_col, keep="all")
    bottom = summary.nsmallest(3, etf_col, keep="all")
    breadth = summary.sort_values(breadth_col, ascending=False).head(5)
    return {"top_etf": top, "bottom_etf": bottom, "breadth": breadth}
