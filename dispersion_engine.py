"""
Sector internal dispersion, breadth, concentration, and participation (default: Technology).

Uses FMP `profile-bulk` (via `tech_universe`) for the **dispersion universe** and
`data_loader.get_price_histories_long` for dividend-adjusted closes. No fundamentals.

Thresholds and exchange lists live in ``config`` (``DISPERSION_*``).

Designed to be called from the Streamlit dashboard with ``@st.cache_data`` around
``run_dispersion_dashboard_bundle(..., sector=...)``.

Performance: ``tech_universe.fetch_profile_bulk_all`` disk-caches the full FMP ``profile-bulk``
concat (see ``config.PROFILE_BULK_CACHE_*``). Price pulls use ``DISPERSION_PRICE_HISTORY_CAL_DAYS``.
Rolling dispersion σ uses a vectorized return panel; optional ``DISPERSION_TS_STRIDE`` subsamples dates.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

import config
import data_loader
import tech_universe


# ---------------------------------------------------------------------------
# 1) Universe
# ---------------------------------------------------------------------------
def build_dispersion_universe(
    session: Any,
    api_key: str,
    *,
    sector: str = "Technology",
    force_refresh_profiles: bool = False,
) -> pd.DataFrame:
    """
    US stocks in the given FMP ``sector`` from profile-bulk with dispersion filters
    (not the multifactor fundamental universe).
    """
    prof = tech_universe.fetch_profile_bulk_all(
        session, api_key, force_refresh=force_refresh_profiles
    )
    if prof.empty:
        return pd.DataFrame(
            columns=[
                "symbol",
                "companyName",
                "sector",
                "industry",
                "marketCap",
                "volume",
                "exchangeShortName",
            ]
        )

    df = prof.copy()
    df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()
    df = tech_universe._normalize_exchange_column(df)

    sec = str(sector).strip()
    if "sector" in df.columns:
        df = df[df["sector"].astype(str).str.strip() == sec]
    else:
        return pd.DataFrame()

    if "country" in df.columns:
        df = df[df["country"].astype(str).str.upper() == "US"]

    if "isActivelyTrading" in df.columns:
        df = df[tech_universe._truthy_mask(df["isActivelyTrading"])]

    if "isEtf" in df.columns:
        df = df[~tech_universe._truthy_mask(df["isEtf"])]
    if "isFund" in df.columns:
        df = df[~tech_universe._truthy_mask(df["isFund"])]

    if "exchangeShortName" in df.columns:
        df = df[df["exchangeShortName"].isin(config.DISPERSION_ALLOWED_EXCHANGES)]

    df["marketCap"] = pd.to_numeric(df.get("marketCap"), errors="coerce")
    df["averageVolume"] = pd.to_numeric(df.get("averageVolume"), errors="coerce")
    df["price"] = pd.to_numeric(df.get("price"), errors="coerce")

    df = df[df["marketCap"] > float(config.DISPERSION_MIN_MARKET_CAP)]
    df = df[df["averageVolume"] > float(config.DISPERSION_MIN_AVG_VOLUME)]
    df = df[df["price"] > float(config.DISPERSION_MIN_PRICE)]

    if "symbol" in df.columns:
        df = df[~df["symbol"].astype(str).map(data_loader._looks_like_warrant_or_right)]
    if "industry" in df.columns:
        df = df[~df["industry"].map(data_loader._looks_like_warrant_industry)]
    if "symbol" in df.columns:
        df = df[~df["symbol"].astype(str).map(data_loader._looks_like_preferred_or_unit)]

    df = df.dropna(subset=["symbol"])
    df = df.drop_duplicates(subset=["symbol"], keep="last")

    out = pd.DataFrame()
    out["symbol"] = df["symbol"]
    if "companyName" in df.columns:
        out["companyName"] = df["companyName"]
    else:
        out["companyName"] = ""
    out["sector"] = df["sector"] if "sector" in df.columns else sec
    out["industry"] = df.get("industry")
    out["marketCap"] = df["marketCap"]
    out["volume"] = df["averageVolume"]
    out["exchangeShortName"] = df["exchangeShortName"]
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2) Prices
# ---------------------------------------------------------------------------
def get_price_histories(
    session: Any,
    api_key: str,
    symbols: list[str],
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Long-format dividend-adjusted prices: columns `date`, `symbol`, `adjClose`.
    """
    end = date_to or date.today()
    start = date_from or (end - timedelta(days=config.PRICE_HISTORY_LOOKBACK_DAYS))
    syms = [str(s).upper().strip() for s in symbols if str(s).strip()]
    return data_loader.get_price_histories_long(
        session, api_key, syms, start, end, force_refresh=force_refresh
    )


def prices_to_wide_close(long_px: pd.DataFrame) -> pd.DataFrame:
    """Pivot to dates × symbols adjusted closes."""
    if long_px.empty:
        return pd.DataFrame()
    return (
        long_px.pivot(index="date", columns="symbol", values="adjClose")
        .sort_index()
        .astype(float)
    )


# ---------------------------------------------------------------------------
# 3) Breadth
# ---------------------------------------------------------------------------
def calculate_breadth_metrics(
    wide_close: pd.DataFrame,
) -> dict[str, Any]:
    """
    Latest breadth: % above 50/200 DMA with raw counts.

    Returns keys: pct_above_50dma, pct_above_200dma, count_above_50dma,
    count_above_200dma, count_valid_50dma, count_valid_200dma.
    """
    out: dict[str, Any] = {
        "pct_above_50dma": float("nan"),
        "pct_above_200dma": float("nan"),
        "count_above_50dma": 0,
        "count_above_200dma": 0,
        "count_valid_50dma": 0,
        "count_valid_200dma": 0,
    }
    if wide_close.empty or len(wide_close) < 200:
        return out

    close = wide_close
    dma50 = close.rolling(50, min_periods=50).mean()
    dma200 = close.rolling(200, min_periods=200).mean()
    last = close.iloc[-1]
    d50 = dma50.iloc[-1]
    d200 = dma200.iloc[-1]

    v50 = last.notna() & d50.notna()
    v200 = last.notna() & d200.notna()
    a50 = (last > d50) & v50
    a200 = (last > d200) & v200

    n50 = int(v50.sum())
    n200 = int(v200.sum())
    out["count_valid_50dma"] = n50
    out["count_valid_200dma"] = n200
    out["count_above_50dma"] = int(a50.sum())
    out["count_above_200dma"] = int(a200.sum())
    if n50:
        out["pct_above_50dma"] = float(a50.sum() / n50)
    if n200:
        out["pct_above_200dma"] = float(a200.sum() / n200)
    return out


def breadth_time_series(wide_close: pd.DataFrame) -> pd.DataFrame:
    """Daily pct above 50 DMA and 200 DMA across the panel."""
    if wide_close.empty or len(wide_close) < 200:
        return pd.DataFrame(columns=["date", "pct_above_50dma", "pct_above_200dma"])
    close = wide_close
    dma50 = close.rolling(50, min_periods=50).mean()
    dma200 = close.rolling(200, min_periods=200).mean()
    valid50 = close.notna() & dma50.notna()
    valid200 = close.notna() & dma200.notna()
    pct50 = ((close > dma50) & valid50).sum(axis=1) / valid50.sum(axis=1).replace(0, np.nan)
    pct200 = ((close > dma200) & valid200).sum(axis=1) / valid200.sum(axis=1).replace(0, np.nan)
    start = wide_close.index.max() - timedelta(days=config.DISPERSION_CHART_LOOKBACK_DAYS)
    ts = pd.DataFrame({"date": wide_close.index, "pct_above_50dma": pct50.values, "pct_above_200dma": pct200.values})
    ts = ts[ts["date"] >= start]
    return ts.dropna(subset=["pct_above_50dma", "pct_above_200dma"], how="all").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 4) Dispersion
# ---------------------------------------------------------------------------
def _return_1m(close: pd.DataFrame, n: int) -> pd.Series:
    """Last-row ~1M simple return per symbol: P_t / P_{t-n} - 1."""
    if close.empty or len(close) < n + 1:
        return pd.Series(dtype=float)
    return close.iloc[-1] / close.iloc[-(n + 1)] - 1.0


def _cap_weights(mc: pd.Series) -> pd.Series:
    s = pd.to_numeric(mc, errors="coerce").clip(lower=0)
    tot = s.sum()
    if tot <= 0 or math.isnan(tot):
        return pd.Series(0.0, index=s.index)
    return s / tot


def _cap_weighted_return_std_row(r: np.ndarray, w: np.ndarray, *, min_n: int = 5) -> float:
    """Std of 1M returns with cap-weights ``w`` (same length as ``r``); NaNs ignored."""
    m = np.isfinite(r) & np.isfinite(w) & (w > 0)
    k = int(m.sum())
    if k < min_n:
        return float("nan")
    rw = r[m].astype(float, copy=False)
    ww = w[m].astype(float, copy=False)
    s = float(ww.sum())
    if s <= 0:
        return float("nan")
    ww = ww / s
    mu = float(np.dot(ww, rw))
    var = float(np.dot(ww, (rw - mu) ** 2))
    if var < 0 or math.isnan(var):
        return float("nan")
    return math.sqrt(var)


def calculate_dispersion_metrics(
    wide_close: pd.DataFrame,
    universe: pd.DataFrame,
    *,
    return_days: int | None = None,
    corr_window: int | None = None,
) -> dict[str, Any]:
    """
    Cross-sectional dispersion snapshot (latest date) plus optional series prep.
    """
    rd = return_days or int(config.DISPERSION_RETURN_TRADING_DAYS)
    cw = corr_window or int(config.DISPERSION_CORR_TRADING_DAYS)
    out: dict[str, Any] = {
        "equal_weight_std": float("nan"),
        "cap_weight_std": float("nan"),
        "avg_pairwise_corr": float("nan"),
        "median_return_1m": float("nan"),
        "return_spread": float("nan"),
    }
    if wide_close.empty or len(wide_close) < max(rd + 2, cw + 2):
        return out

    close = wide_close
    r1 = _return_1m(close, rd)
    r1 = r1.replace([np.inf, -np.inf], np.nan).dropna()
    if r1.empty:
        return out

    out["equal_weight_std"] = float(r1.std(ddof=1)) if len(r1) > 1 else float("nan")
    out["median_return_1m"] = float(r1.median())

    # Decile spread (equal-weight)
    q_hi = r1.quantile(0.9)
    q_lo = r1.quantile(0.1)
    top = r1[r1 >= q_hi]
    bot = r1[r1 <= q_lo]
    if len(top) and len(bot):
        out["return_spread"] = float(top.mean() - bot.mean())

    # Cap-weighted std of 1M returns
    mc = universe.set_index("symbol")["marketCap"].reindex(r1.index)
    w = _cap_weights(mc)
    mask = (w > 0) & (r1.notna())
    if mask.sum() > 1:
        rw = r1[mask]
        ww = w[mask]
        ww = ww / ww.sum()
        mu = float((ww * rw).sum())
        var = float((ww * (rw - mu) ** 2).sum())
        out["cap_weight_std"] = math.sqrt(var) if var >= 0 else float("nan")

    # Average pairwise correlation of daily returns (last `cw` rows)
    rets = close.pct_change().iloc[-cw:].dropna(how="all", axis=0)
    rets = rets.dropna(axis=1, thresh=max(10, int(cw * 0.6)))
    if rets.shape[1] >= 2 and len(rets) >= 10:
        cm = rets.corr()
        tri = np.triu_indices_from(cm.values, k=1)
        vals = cm.values[tri]
        vals = vals[~np.isnan(vals)]
        if len(vals):
            out["avg_pairwise_corr"] = float(np.mean(vals))

    return out


def dispersion_time_series(wide_close: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    """Rolling cross-sectional std of ~1M returns (equal- and cap-weighted)."""
    rd = int(config.DISPERSION_RETURN_TRADING_DAYS)
    stride = max(1, int(getattr(config, "DISPERSION_TS_STRIDE", 1)))

    if wide_close.empty or len(wide_close) < rd + 5:
        return pd.DataFrame(columns=["date", "equal_weight_std", "cap_weight_std"])

    close = wide_close
    # Vectorized rolling ~1M simple return per date × symbol (avoids O(days²) window copies).
    r_panel = close / close.shift(rd) - 1.0
    r_panel = r_panel.iloc[rd + 1 :]
    if r_panel.empty:
        return pd.DataFrame(columns=["date", "equal_weight_std", "cap_weight_std"])
    if stride > 1:
        r_panel = r_panel.iloc[::stride]

    cnt = r_panel.count(axis=1)
    ew = r_panel.std(axis=1, ddof=1)
    ew = ew.where(cnt >= 5, np.nan)

    mc = universe.set_index("symbol")["marketCap"]
    w_series = _cap_weights(mc.reindex(close.columns)).reindex(close.columns).fillna(0.0)
    w_arr = w_series.to_numpy(dtype=float)
    mat = r_panel.to_numpy(dtype=float)
    cw = np.empty(len(r_panel), dtype=float)
    for i in range(len(r_panel)):
        cw[i] = _cap_weighted_return_std_row(mat[i], w_arr)

    ts = pd.DataFrame(
        {"date": r_panel.index, "equal_weight_std": ew.to_numpy(), "cap_weight_std": cw}
    ).dropna(subset=["equal_weight_std", "cap_weight_std"], how="all")
    start = ts["date"].max() - timedelta(days=config.DISPERSION_CHART_LOOKBACK_DAYS) if len(ts) else None
    if start is not None:
        ts = ts[ts["date"] >= start]
    return ts.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5) Concentration
# ---------------------------------------------------------------------------
def calculate_concentration_metrics(universe: pd.DataFrame, ret_1m: pd.Series) -> dict[str, Any]:
    """Top weights, HHI, and cap-weighted return contribution from top 5 names."""
    out: dict[str, Any] = {
        "top5_weight": float("nan"),
        "top10_weight": float("nan"),
        "top5_return_contribution": float("nan"),
        "hhi": float("nan"),
    }
    if universe.empty or "marketCap" not in universe.columns or "symbol" not in universe.columns:
        return out
    mc = pd.to_numeric(universe.set_index("symbol")["marketCap"], errors="coerce").clip(lower=0)
    tot = float(mc.sum())
    if tot <= 0:
        return out
    w = mc / tot
    w_sorted = w.sort_values(ascending=False)
    out["top5_weight"] = float(w_sorted.head(5).sum())
    out["top10_weight"] = float(w_sorted.head(10).sum())
    out["hhi"] = float((w ** 2).sum())

    top5_syms = w_sorted.head(5).index.tolist()
    contrib = 0.0
    for sym in top5_syms:
        wi = float(w_sorted.loc[sym]) if sym in w_sorted.index else 0.0
        ri = float(ret_1m.get(sym, float("nan")))
        if wi > 0 and ri == ri:
            contrib += wi * ri
    out["top5_return_contribution"] = float(contrib)
    return out


# ---------------------------------------------------------------------------
# 6) Summary + contributor tables
# ---------------------------------------------------------------------------
def _dma_flags(wide_close: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Latest above-50 and above-200 flags per symbol (boolean Series)."""
    if wide_close.empty or len(wide_close) < 200:
        return pd.Series(dtype=bool), pd.Series(dtype=bool)
    close = wide_close
    d50 = close.rolling(50, min_periods=50).mean().iloc[-1]
    d200 = close.rolling(200, min_periods=200).mean().iloc[-1]
    last = close.iloc[-1]
    v50 = last.notna() & d50.notna()
    v200 = last.notna() & d200.notna()
    a50 = (last > d50) & v50
    a200 = (last > d200) & v200
    return a50.fillna(False), a200.fillna(False)


def build_contributor_tables(
    universe: pd.DataFrame,
    wide_close: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Top / bottom by cap-weighted contribution (weight × 1M return)."""
    cols = [
        "symbol",
        "companyName",
        "marketCap",
        "return_1m",
        "contribution_to_sector_return",
        "above_50dma",
        "above_200dma",
    ]
    if universe.empty or wide_close.empty:
        empty = pd.DataFrame(columns=cols)
        return empty, empty

    rd = int(config.DISPERSION_RETURN_TRADING_DAYS)
    r1 = _return_1m(wide_close, rd)
    mc = universe.set_index("symbol")["marketCap"]
    w = _cap_weights(mc.reindex(r1.index)).reindex(r1.index)
    contrib = w * r1

    a50, a200 = _dma_flags(wide_close)
    names = universe.set_index("symbol")["companyName"]

    rows = []
    for sym in r1.index:
        if sym not in wide_close.columns:
            continue
        rows.append(
            {
                "symbol": sym,
                "companyName": names.get(sym, ""),
                "marketCap": float(mc.get(sym, float("nan"))) if sym in mc.index else float("nan"),
                "return_1m": float(r1.get(sym, float("nan"))),
                "contribution_to_sector_return": float(contrib.get(sym, float("nan"))),
                "above_50dma": bool(a50.get(sym, False)),
                "above_200dma": bool(a200.get(sym, False)),
            }
        )
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["return_1m"], how="all")
    top = df.sort_values("contribution_to_sector_return", ascending=False, na_position="last")
    bot = df.sort_values("contribution_to_sector_return", ascending=True, na_position="last")
    return top.reset_index(drop=True), bot.reset_index(drop=True)


def build_industry_participation(
    universe: pd.DataFrame,
    wide_close: pd.DataFrame,
) -> pd.DataFrame:
    """Per FMP industry (within the sector universe): counts, avg 1M return, breadth, EW/CW returns."""
    rd = int(config.DISPERSION_RETURN_TRADING_DAYS)
    if universe.empty or wide_close.empty or len(wide_close) < max(rd + 2, 200):
        return pd.DataFrame(
            columns=[
                "industry",
                "company_count",
                "avg_return_1m",
                "pct_above_50dma",
                "pct_above_200dma",
                "equal_weight_return_1m",
                "cap_weight_return_1m",
            ]
        )

    close = wide_close
    dma50 = close.rolling(50, min_periods=50).mean().iloc[-1]
    dma200 = close.rolling(200, min_periods=200).mean().iloc[-1]
    last = close.iloc[-1]
    r1 = _return_1m(close, rd)
    mc = universe.set_index("symbol")["marketCap"]

    parts: list[dict[str, Any]] = []
    for ind, grp in universe.groupby("industry", dropna=False):
        syms = [
            str(s)
            for s in grp["symbol"]
            if s in wide_close.columns and s in r1.index and pd.notna(r1.get(s, float("nan")))
        ]
        if not syms:
            continue
        r_sub = r1.loc[syms]
        ew = float(r_sub.mean())
        mloc = mc.reindex(syms)
        w = _cap_weights(mloc)
        cw = float((w * r_sub).sum()) if float(w.sum()) > 0 else float("nan")

        val50 = above50 = val200 = above200 = 0
        for s in syms:
            if s not in wide_close.columns:
                continue
            if wide_close[s].iloc[-50:].notna().sum() >= 50 and pd.notna(last.get(s)) and pd.notna(dma50.get(s)):
                val50 += 1
                if float(last[s]) > float(dma50[s]):
                    above50 += 1
            if wide_close[s].iloc[-200:].notna().sum() >= 200 and pd.notna(last.get(s)) and pd.notna(dma200.get(s)):
                val200 += 1
                if float(last[s]) > float(dma200[s]):
                    above200 += 1

        ind_label = "" if ind is None or (isinstance(ind, float) and pd.isna(ind)) else str(ind)
        parts.append(
            {
                "industry": ind_label,
                "company_count": len(syms),
                "avg_return_1m": float(r_sub.mean()),
                "pct_above_50dma": float(above50 / val50) if val50 else float("nan"),
                "pct_above_200dma": float(above200 / val200) if val200 else float("nan"),
                "equal_weight_return_1m": ew,
                "cap_weight_return_1m": cw,
            }
        )
    return pd.DataFrame(parts).sort_values("industry").reset_index(drop=True)


def build_dispersion_summary(
    universe: pd.DataFrame,
    wide_close: pd.DataFrame,
) -> dict[str, Any]:
    """Single dict combining breadth, dispersion, and concentration (snapshot)."""
    breadth = calculate_breadth_metrics(wide_close)
    disp = calculate_dispersion_metrics(wide_close, universe)
    r1 = _return_1m(wide_close, int(config.DISPERSION_RETURN_TRADING_DAYS))
    conc = calculate_concentration_metrics(universe, r1)

    return {
        "universe_size": int(len(universe)),
        "pct_above_50dma": breadth.get("pct_above_50dma"),
        "pct_above_200dma": breadth.get("pct_above_200dma"),
        "equal_weight_std": disp.get("equal_weight_std"),
        "cap_weight_std": disp.get("cap_weight_std"),
        "avg_pairwise_corr": disp.get("avg_pairwise_corr"),
        "median_return_1m": disp.get("median_return_1m"),
        "return_spread": disp.get("return_spread"),
        "top5_weight": conc.get("top5_weight"),
        "top10_weight": conc.get("top10_weight"),
        "top5_return_contribution": conc.get("top5_return_contribution"),
        "hhi": conc.get("hhi"),
    }


def assemble_dispersion_tables(
    universe: pd.DataFrame,
    wide_close: pd.DataFrame,
    summary: dict[str, Any],
) -> dict[str, pd.DataFrame]:
    """Summary / breadth / concentration one-row tables plus contributor and industry tables."""
    breadth = calculate_breadth_metrics(wide_close)
    breadth_row = {
        "pct_above_50dma": breadth.get("pct_above_50dma"),
        "pct_above_200dma": breadth.get("pct_above_200dma"),
        "count_above_50dma": breadth.get("count_above_50dma"),
        "count_above_200dma": breadth.get("count_above_200dma"),
        "count_valid_50dma": breadth.get("count_valid_50dma"),
        "count_valid_200dma": breadth.get("count_valid_200dma"),
    }
    r1 = _return_1m(wide_close, int(config.DISPERSION_RETURN_TRADING_DAYS))
    conc = calculate_concentration_metrics(universe, r1)
    conc_row = {**conc, "universe_size": len(universe)}

    top, bot = build_contributor_tables(universe, wide_close)
    ind = build_industry_participation(universe, wide_close)

    return {
        "dispersion_summary_table": pd.DataFrame([summary]),
        "breadth_table": pd.DataFrame([breadth_row]),
        "concentration_table": pd.DataFrame([conc_row]),
        "top_contributors": top,
        "bottom_contributors": bot,
        "industry_participation": ind,
    }


def run_dispersion_dashboard_bundle(
    session: Any,
    api_key: str,
    *,
    sector: str = "Technology",
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    End-to-end bundle for the dashboard: universe, prices, metrics, charts data, tables.
    """
    uni = build_dispersion_universe(
        session, api_key, sector=sector, force_refresh_profiles=force_refresh
    )
    if uni.empty:
        return {
            "ok": False,
            "error": "Empty dispersion universe (check FMP profile-bulk and filters).",
            "universe": uni,
            "summary": {},
            "tables": {},
            "wide_close": pd.DataFrame(),
            "breadth_ts": pd.DataFrame(),
            "dispersion_ts": pd.DataFrame(),
            "as_of": date.today(),
        }

    syms = uni["symbol"].astype(str).tolist()
    end = date.today()
    hist_days = int(
        getattr(
            config,
            "DISPERSION_PRICE_HISTORY_CAL_DAYS",
            config.DISPERSION_CHART_LOOKBACK_DAYS + config.DISPERSION_DMA_WARMUP_DAYS,
        )
    )
    start = end - timedelta(days=hist_days)
    long_px = get_price_histories(
        session,
        api_key,
        syms,
        date_from=start,
        date_to=end,
        force_refresh=force_refresh,
    )
    wide = prices_to_wide_close(long_px)
    have = [s for s in syms if s in wide.columns]
    uni_f = uni[uni["symbol"].isin(have)].copy()
    wide_f = wide[[c for c in wide.columns if c in have]]

    summ = build_dispersion_summary(uni_f, wide_f)
    tables = assemble_dispersion_tables(uni_f, wide_f, summ)

    return {
        "ok": True,
        "error": None,
        "universe": uni_f,
        "summary": summ,
        "tables": tables,
        "wide_close": wide_f,
        "breadth_ts": breadth_time_series(wide_f),
        "dispersion_ts": dispersion_time_series(wide_f, uni_f),
        "as_of": wide_f.index.max().date() if len(wide_f) else date.today(),
    }
