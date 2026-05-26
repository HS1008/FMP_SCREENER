"""
IPP & Power Market Dashboard — standalone Streamlit app.

Tracks spark spreads, implied heat rates, gas basis, and power-market
conditions for IPPs and power generators.  Includes a secondary stock
watchlist section.

Run:
  streamlit run power_producer_watchlist.py
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import config
import data_loader
from data_sources import eia_wholesale

try:
    import plotly.graph_objects as go

    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

# =========================================================================
# Constants
# =========================================================================
WATCHLIST_CSV = config.PROJECT_ROOT / "data" / "power_producer_watchlist.csv"
HUB_MAP_CSV = config.PROJECT_ROOT / "data" / "power_market_hub_map.csv"

RETURN_LOOKBACKS: dict[str, int] = {"1D": 1, "1W": 5, "1M": 21, "3M": 63, "6M": 126}
SKIP_1M_BARS = 21
SKIP_12M_BARS = 252

MOMENTUM_WEIGHTS: dict[str, float] = {
    "1M": 0.20,
    "3M": 0.30,
    "6M": 0.30,
    "12M Skip 1M": 0.20,
}
RETURN_COLS = ["1D", "1W", "1M", "3M", "6M", "12M Skip 1M"]
PRICE_LOOKBACK_CALENDAR_DAYS = 400

HEAT_RATE_DEFAULT = 7.2
HEAT_RATE_MIN = 6.5
HEAT_RATE_MAX = 10.5

REGIME_LABELS: list[tuple[float, str]] = [
    (75, "Bullish / tight power market"),
    (55, "Improving"),
    (40, "Neutral"),
    (0, "Weak / margin pressure"),
]

LOOKBACK_OPTIONS: dict[str, int] = {
    "1M": 30,
    "3M": 90,
    "6M": 180,
    "1Y": 365,
    "Max": 99999,
}


# =========================================================================
# Stock Watchlist Functions (preserved)
# =========================================================================
def load_watchlist() -> pd.DataFrame:
    if not WATCHLIST_CSV.is_file():
        return pd.DataFrame()
    df = pd.read_csv(WATCHLIST_CSV, dtype=str).fillna("")
    df.columns = df.columns.str.strip()
    df["ticker"] = df["ticker"].str.strip().str.upper()
    return df


def _load_api_key() -> str:
    load_dotenv(config.PROJECT_ROOT / ".env")
    key = (os.getenv("FMP_API_KEY") or "").strip()
    if not key:
        st.warning(
            "FMP_API_KEY is missing. Add it to `.env` or Streamlit secrets "
            "to load stock price data."
        )
    return key


@st.cache_data(ttl=900, show_spinner="Fetching FMP prices…")
def fetch_price_history(ticker: str, date_from: date, date_to: date) -> pd.Series:
    load_dotenv(config.PROJECT_ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        return pd.Series(dtype=float, name=ticker)
    session = data_loader.create_http_session()
    try:
        hist = data_loader.get_price_history(
            session, api_key, ticker, date_from, date_to
        )
    except Exception:
        return pd.Series(dtype=float, name=ticker)
    if hist is None or hist.empty:
        return pd.Series(dtype=float, name=ticker)
    col = data_loader.pick_price_column(hist)
    idx = pd.to_datetime(hist["date"], errors="coerce")
    vals = pd.to_numeric(hist[col], errors="coerce")
    s = pd.Series(vals.values, index=idx, name=ticker).dropna().sort_index()
    return s[~s.index.duplicated(keep="last")]


def _safe_return(prices: pd.Series, end_offset: int, start_offset: int) -> float:
    n = len(prices)
    end_idx = n - end_offset
    start_idx = n - start_offset
    if start_idx < 0 or end_idx < 0 or start_idx >= n or end_idx >= n:
        return float("nan")
    p_end = float(prices.iloc[end_idx])
    p_start = float(prices.iloc[start_idx])
    if p_start <= 0 or not np.isfinite(p_start) or not np.isfinite(p_end):
        return float("nan")
    return p_end / p_start - 1.0


def calculate_return_metrics(prices: pd.Series) -> dict[str, float]:
    out: dict[str, float] = {}
    if prices is None or len(prices) < 2:
        for c in RETURN_COLS:
            out[c] = float("nan")
        return out
    for label, bars in RETURN_LOOKBACKS.items():
        out[label] = _safe_return(prices, end_offset=1, start_offset=bars + 1)
    out["12M Skip 1M"] = _safe_return(
        prices, end_offset=SKIP_1M_BARS + 1, start_offset=SKIP_12M_BARS + 1
    )
    return out


def calculate_momentum_score(row: dict[str, Any]) -> float:
    total_weight = 0.0
    weighted_sum = 0.0
    for col, w in MOMENTUM_WEIGHTS.items():
        val = row.get(col, float("nan"))
        if pd.notna(val) and np.isfinite(float(val)):
            weighted_sum += float(val) * w
            total_weight += w
    if total_weight <= 0:
        return float("nan")
    return weighted_sum / total_weight


def build_stock_table(watchlist: pd.DataFrame, date_to: date) -> pd.DataFrame:
    date_from = date_to - timedelta(days=PRICE_LOOKBACK_CALENDAR_DAYS)
    rows: list[dict[str, Any]] = []
    for _, wrow in watchlist.iterrows():
        ticker = str(wrow["ticker"]).strip().upper()
        base = wrow.to_dict()
        prices = fetch_price_history(ticker, date_from, date_to)
        metrics = calculate_return_metrics(prices)
        base.update(metrics)
        base["Momentum Score"] = calculate_momentum_score(metrics)
        rows.append(base)
    return pd.DataFrame(rows)


# =========================================================================
# Formatting helpers
# =========================================================================
def _fmt_pct(x: Any) -> str:
    try:
        if pd.isna(x) or not np.isfinite(float(x)):
            return "N/A"
        return f"{float(x) * 100:.2f}%"
    except Exception:
        return "N/A"


def _fmt_dollar(x: Any, decimals: int = 2) -> str:
    try:
        if pd.isna(x) or not np.isfinite(float(x)):
            return "N/A"
        return f"${float(x):,.{decimals}f}"
    except Exception:
        return "N/A"


def _fmt_num(x: Any, decimals: int = 2) -> str:
    try:
        if pd.isna(x) or not np.isfinite(float(x)):
            return "N/A"
        return f"{float(x):,.{decimals}f}"
    except Exception:
        return "N/A"


def _stock_display_columns() -> list[str]:
    return [
        "ticker", "company", "type",
        "main_power_source", "secondary_power_source", "region_market",
    ] + RETURN_COLS + ["Momentum Score", "notes"]


def _stock_rename_map() -> dict[str, str]:
    return {
        "ticker": "Ticker",
        "company": "Company",
        "type": "Type",
        "main_power_source": "Main Power Source",
        "secondary_power_source": "Secondary Power Source",
        "region_market": "Region / Market",
        "notes": "Notes",
    }


def format_stock_table(df: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in _stock_display_columns() if c in df.columns]
    out = df[cols].copy().rename(columns=_stock_rename_map())
    for c in RETURN_COLS:
        if c in out.columns:
            out[c] = out[c].apply(_fmt_pct)
    if "Momentum Score" in out.columns:
        out["Momentum Score"] = out["Momentum Score"].apply(_fmt_pct)
    return out


# =========================================================================
# Hub Map & Market Data
# =========================================================================
def load_hub_map() -> pd.DataFrame:
    if not HUB_MAP_CSV.is_file():
        st.error(f"Hub map CSV not found: `{HUB_MAP_CSV}`")
        return pd.DataFrame()
    df = pd.read_csv(HUB_MAP_CSV, dtype=str).fillna("")
    df.columns = df.columns.str.strip().str.lower()
    return df


def load_market_data(
    force_refresh: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, bool]:
    return eia_wholesale.load_cached_or_fetch_eia_data(force_refresh=force_refresh)


# =========================================================================
# Market Calculations
# =========================================================================
def calculate_spark_spreads(df: pd.DataFrame, heat_rate: float) -> pd.DataFrame:
    out = df.copy()
    out["spark_spread"] = out["power_price"] - out["gas_price"] * heat_rate
    out["implied_heat_rate"] = np.where(
        out["gas_price"] > 0, out["power_price"] / out["gas_price"], np.nan
    )
    out["gas_basis"] = np.where(
        out["henry_hub_price"].notna(),
        out["gas_price"] - out["henry_hub_price"],
        np.nan,
    )
    return out


def _trend_change(series: pd.Series, bars: int) -> float:
    s = series.dropna()
    if len(s) <= bars:
        return float("nan")
    return float(s.iloc[-1] - s.iloc[-(bars + 1)])


def _pct_rank(series: pd.Series, lookback: int = 252) -> float:
    recent = series.dropna().tail(lookback)
    if len(recent) < 2:
        return float("nan")
    return float((recent <= recent.iloc[-1]).mean())


def calculate_market_score(row: dict[str, Any]) -> float:
    """Weighted score 0-100.  Higher = more bullish for generators."""
    components: list[tuple[float, float]] = []

    ss_pct = row.get("SS Pctile")
    if pd.notna(ss_pct) and np.isfinite(float(ss_pct)):
        components.append((float(ss_pct) * 100, 0.35))

    ss_1m = row.get("SS 1M Chg")
    if pd.notna(ss_1m) and np.isfinite(float(ss_1m)):
        components.append((max(0, min(100, 50 + float(ss_1m) * 5)), 0.25))

    ihr_1m = row.get("IHR 1M Chg")
    if pd.notna(ihr_1m) and np.isfinite(float(ihr_1m)):
        components.append((max(0, min(100, 50 + float(ihr_1m) * 10)), 0.15))

    pp_1m = row.get("PP 1M Chg")
    if pd.notna(pp_1m) and np.isfinite(float(pp_1m)):
        components.append((max(0, min(100, 50 + float(pp_1m) * 3)), 0.15))

    gb = row.get("GB Latest")
    if pd.notna(gb) and np.isfinite(float(gb)):
        gb_favorable = max(0, min(100, 100 - abs(float(gb)) * 20))
        components.append((gb_favorable, 0.10))

    if not components:
        return float("nan")
    total_w = sum(w for _, w in components)
    if total_w <= 0:
        return float("nan")
    return sum(v * w for v, w in components) / total_w


def regime_label(score: float) -> str:
    try:
        if pd.isna(score) or not np.isfinite(score):
            return "N/A"
    except (TypeError, ValueError):
        return "N/A"
    for threshold, label in REGIME_LABELS:
        if score >= threshold:
            return label
    return "N/A"


def build_spark_spread_summary(market_df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for region in market_df["region"].unique():
        rdf = market_df[market_df["region"] == region].sort_values("date")
        if rdf.empty:
            continue
        latest = rdf.iloc[-1]
        row: dict[str, Any] = {
            "Region": region,
            "ISO": latest["iso"],
            "Power Hub": latest["power_hub"],
            "Gas Hub": latest["gas_hub"],
            "Power Price": latest["power_price"],
            "Gas Price": latest["gas_price"],
            "Spark Spread": latest["spark_spread"],
            "Implied Heat Rate": latest["implied_heat_rate"],
            "Gas Basis": latest.get("gas_basis", np.nan),
        }
        ss = rdf["spark_spread"]
        row["SS 1D Chg"] = _trend_change(ss, 1)
        row["SS 1W Chg"] = _trend_change(ss, 5)
        row["SS 1M Chg"] = _trend_change(ss, 21)
        row["SS Pctile"] = _pct_rank(ss)

        row["PP 1M Chg"] = _trend_change(rdf["power_price"], 21)
        row["IHR 1M Chg"] = _trend_change(rdf["implied_heat_rate"], 21)
        row["GB Latest"] = float(latest.get("gas_basis", np.nan))

        row["Market Score"] = calculate_market_score(row)
        row["Regime"] = regime_label(row["Market Score"])
        rows.append(row)
    return pd.DataFrame(rows)


# =========================================================================
# Rendering — Market Controls
# =========================================================================
def render_market_controls(hub_map: pd.DataFrame) -> dict[str, Any]:
    st.sidebar.header("Market Controls")

    heat_rate = st.sidebar.slider(
        "Heat rate assumption (MMBtu/MWh)",
        min_value=HEAT_RATE_MIN,
        max_value=HEAT_RATE_MAX,
        value=HEAT_RATE_DEFAULT,
        step=0.1,
    )

    all_regions = sorted(hub_map["region"].unique().tolist())
    selected_regions = st.sidebar.multiselect(
        "Regions / hubs", all_regions, default=all_regions
    )

    lookback_label = st.sidebar.selectbox(
        "Chart lookback", list(LOOKBACK_OPTIONS.keys()), index=3
    )
    lookback_days = LOOKBACK_OPTIONS[lookback_label]

    force_refresh = st.sidebar.button("Force Refresh")

    return {
        "heat_rate": heat_rate,
        "regions": selected_regions,
        "lookback_days": lookback_days,
        "force_refresh": force_refresh,
    }


# =========================================================================
# Rendering — Market Overview Cards
# =========================================================================
def render_market_overview_cards(summary: pd.DataFrame) -> None:
    st.subheader("Power Market Overview")
    if summary.empty:
        st.info("No market data available.")
        return

    cols = st.columns(3)

    ss = summary["Spark Spread"].dropna()
    if not ss.empty:
        best = summary.loc[ss.idxmax()]
        cols[0].metric(
            "Best Spark Spread",
            _fmt_dollar(best["Spark Spread"]),
            delta=best["Region"],
        )
    else:
        cols[0].metric("Best Spark Spread", "N/A")

    if not ss.empty:
        worst = summary.loc[ss.idxmin()]
        cols[1].metric(
            "Worst Spark Spread",
            _fmt_dollar(worst["Spark Spread"]),
            delta=worst["Region"],
        )
    else:
        cols[1].metric("Worst Spark Spread", "N/A")

    ss_1m = summary["SS 1M Chg"].dropna()
    if not ss_1m.empty:
        best_1m = summary.loc[ss_1m.idxmax()]
        cols[2].metric(
            "Biggest 1M SS Improvement",
            _fmt_dollar(best_1m["SS 1M Chg"]),
            delta=best_1m["Region"],
        )
    else:
        cols[2].metric("Biggest 1M SS Improvement", "N/A")

    cols2 = st.columns(3)

    ihr = summary["Implied Heat Rate"].dropna()
    if not ihr.empty:
        best_ihr = summary.loc[ihr.idxmax()]
        cols2[0].metric(
            "Highest Implied Heat Rate",
            _fmt_num(best_ihr["Implied Heat Rate"], 1) + " MMBtu/MWh",
            delta=best_ihr["Region"],
        )
    else:
        cols2[0].metric("Highest Implied Heat Rate", "N/A")

    gb = summary["Gas Basis"].dropna()
    if not gb.empty:
        worst_gb = summary.loc[gb.abs().idxmax()]
        cols2[1].metric(
            "Biggest Gas Basis Pressure",
            _fmt_dollar(worst_gb["Gas Basis"]),
            delta=worst_gb["Region"],
        )
    else:
        cols2[1].metric("Biggest Gas Basis Pressure", "N/A")

    ms = summary["Market Score"].dropna()
    if not ms.empty:
        best_ms = summary.loc[ms.idxmax()]
        cols2[2].metric(
            "Highest Market Score",
            _fmt_num(best_ms["Market Score"], 0),
            delta=f"{best_ms['Region']} — {best_ms['Regime']}",
        )
    else:
        cols2[2].metric("Highest Market Score", "N/A")


# =========================================================================
# Rendering — Spark Spread Monitor Table
# =========================================================================
def render_spark_spread_monitor(summary: pd.DataFrame) -> None:
    st.subheader("Spark Spread Monitor")
    if summary.empty:
        st.info("No data.")
        return

    display = summary.copy()
    for col, fn in [
        ("Power Price", _fmt_dollar),
        ("Gas Price", _fmt_dollar),
        ("Spark Spread", _fmt_dollar),
        ("SS 1W Chg", _fmt_dollar),
        ("SS 1M Chg", _fmt_dollar),
        ("Gas Basis", _fmt_dollar),
    ]:
        if col in display.columns:
            display[col] = display[col].apply(fn)
    if "Implied Heat Rate" in display.columns:
        display["Implied Heat Rate"] = display["Implied Heat Rate"].apply(
            lambda x: _fmt_num(x, 1)
        )
    if "SS Pctile" in display.columns:
        display["SS Pctile"] = display["SS Pctile"].apply(
            lambda x: _fmt_pct(x) if pd.notna(x) else "N/A"
        )
    if "Market Score" in display.columns:
        display["Market Score"] = display["Market Score"].apply(
            lambda x: _fmt_num(x, 0)
        )

    show_cols = [
        "Region", "ISO", "Power Hub", "Gas Hub",
        "Power Price", "Gas Price", "Spark Spread",
        "SS 1W Chg", "SS 1M Chg", "SS Pctile",
        "Implied Heat Rate", "Gas Basis",
        "Market Score", "Regime",
    ]
    show_cols = [c for c in show_cols if c in display.columns]
    st.dataframe(display[show_cols], use_container_width=True, hide_index=True)


# =========================================================================
# Rendering — Spark Spread Trend Chart
# =========================================================================
def render_spark_spread_chart(
    market_df: pd.DataFrame, regions: list[str], lookback_days: int
) -> None:
    st.subheader("Spark Spread Trend")
    if not HAS_PLOTLY or market_df.empty:
        if not HAS_PLOTLY:
            st.warning("Install plotly for charts.")
        return
    cutoff = market_df["date"].max() - pd.Timedelta(days=lookback_days)
    plot_df = market_df[market_df["date"] >= cutoff]

    fig = go.Figure()
    for region in regions:
        rdf = plot_df[plot_df["region"] == region].sort_values("date")
        if rdf.empty:
            continue
        fig.add_trace(
            go.Scatter(x=rdf["date"], y=rdf["spark_spread"], mode="lines", name=region)
        )
        if len(rdf) >= 30:
            ma = rdf["spark_spread"].rolling(30, min_periods=15).mean()
            fig.add_trace(
                go.Scatter(
                    x=rdf["date"],
                    y=ma,
                    mode="lines",
                    name=f"{region} 30d MA",
                    line=dict(dash="dot"),
                    showlegend=False,
                )
            )
    fig.update_layout(
        yaxis_title="Spark Spread ($/MWh)",
        hovermode="x unified",
        height=400,
        margin=dict(l=50, r=20, t=30, b=40),
    )
    st.plotly_chart(fig, use_container_width=True)


# =========================================================================
# Rendering — Implied Heat Rate Chart
# =========================================================================
def render_implied_heat_rate_chart(
    market_df: pd.DataFrame, regions: list[str], lookback_days: int
) -> None:
    st.subheader("Implied Heat Rate Trend")
    if not HAS_PLOTLY or market_df.empty:
        return
    cutoff = market_df["date"].max() - pd.Timedelta(days=lookback_days)
    plot_df = market_df[market_df["date"] >= cutoff]

    fig = go.Figure()
    for region in regions:
        rdf = plot_df[plot_df["region"] == region].sort_values("date")
        if rdf.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=rdf["date"], y=rdf["implied_heat_rate"], mode="lines", name=region
            )
        )
    fig.update_layout(
        yaxis_title="Implied Heat Rate (MMBtu/MWh)",
        hovermode="x unified",
        height=400,
        margin=dict(l=50, r=20, t=30, b=40),
    )
    st.plotly_chart(fig, use_container_width=True)


# =========================================================================
# Rendering — Power Price vs Fuel Cost
# =========================================================================
def render_power_vs_fuel_cost_chart(
    market_df: pd.DataFrame, heat_rate: float, lookback_days: int
) -> None:
    st.subheader("Power Price vs Fuel Cost")
    if not HAS_PLOTLY or market_df.empty:
        return
    regions = sorted(market_df["region"].unique())
    selected = st.selectbox("Select hub pair", regions, key="pv_fuel_region")

    rdf = market_df[market_df["region"] == selected].sort_values("date")
    cutoff = rdf["date"].max() - pd.Timedelta(days=lookback_days)
    rdf = rdf[rdf["date"] >= cutoff]
    if rdf.empty:
        st.info("No data for selected hub.")
        return

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=rdf["date"], y=rdf["power_price"], mode="lines", name="Power Price ($/MWh)"
        )
    )
    fuel_cost = rdf["gas_price"] * heat_rate
    fig.add_trace(
        go.Scatter(
            x=rdf["date"],
            y=fuel_cost,
            mode="lines",
            name=f"Fuel Cost (gas × {heat_rate:.1f})",
        )
    )
    fig.update_layout(
        yaxis_title="$/MWh",
        hovermode="x unified",
        height=400,
        margin=dict(l=50, r=20, t=30, b=40),
    )
    st.plotly_chart(fig, use_container_width=True)


# =========================================================================
# Rendering — Gas Basis Monitor
# =========================================================================
def render_gas_basis_monitor(market_df: pd.DataFrame) -> None:
    st.subheader("Gas Basis Monitor")
    if market_df.empty:
        return
    rows: list[dict[str, Any]] = []
    for region in market_df["region"].unique():
        rdf = market_df[market_df["region"] == region].sort_values("date")
        if rdf.empty:
            continue
        latest = rdf.iloc[-1]
        gb = latest.get("gas_basis", np.nan)
        gb_series = rdf["gas_basis"].dropna()
        gb_1m = _trend_change(gb_series, 21) if len(gb_series) > 21 else np.nan

        if pd.isna(gb) or not np.isfinite(float(gb)):
            interp = "N/A"
        elif gb > 2.0:
            interp = "High fuel cost pressure"
        elif gb > 1.0:
            interp = "Elevated basis"
        elif gb > 0.3:
            interp = "Moderate basis"
        elif gb > -0.3:
            interp = "Near parity"
        else:
            interp = "Regional discount"

        rows.append(
            {
                "Region": region,
                "Gas Hub": latest["gas_hub"],
                "Gas Price": _fmt_dollar(latest["gas_price"]),
                "Henry Hub": _fmt_dollar(latest.get("henry_hub_price", np.nan)),
                "Gas Basis": _fmt_dollar(gb),
                "1M Basis Chg": _fmt_dollar(gb_1m),
                "Interpretation": interp,
            }
        )
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# =========================================================================
# Rendering — Stock Watchlist (secondary section)
# =========================================================================
def render_stock_watchlist() -> None:
    st.subheader("Power Producer Stock Watchlist")
    st.caption(
        "Stock performance is shown as a secondary layer. "
        "The main dashboard above tracks the underlying power-market setup."
    )
    _load_api_key()
    watchlist = load_watchlist()
    if watchlist.empty:
        st.info("No watchlist data available.")
        return
    today = date.today()
    df = build_stock_table(watchlist, today)
    if df.empty:
        st.info("No stock data returned.")
        return
    st.dataframe(format_stock_table(df), use_container_width=True, hide_index=True)


# =========================================================================
# Rendering — Data Notes
# =========================================================================
def render_data_notes() -> None:
    st.info(
        "**Data Notes**\n\n"
        "- EIA wholesale data is not real-time and may update with a lag.\n"
        "- Spark spread is a simplified margin proxy, not company-level EBITDA.\n"
        "- Actual company exposure depends on plant mix, region, hedges, "
        "contracts, and retail operations.\n"
        "- 12M Skip 1M stock return excludes the most recent month.\n"
        "- This dashboard intentionally excludes valuation metrics."
    )


# =========================================================================
# Main Dashboard
# =========================================================================
def render_dashboard() -> None:
    try:
        st.set_page_config(
            page_title="IPP & Power Market Dashboard", layout="wide"
        )
    except Exception:
        pass

    st.title("IPP & Power Market Dashboard")
    st.caption(
        "Track spark spreads, implied heat rates, gas basis, and power-market "
        "conditions for IPPs and power generators."
    )

    hub_map = load_hub_map()
    if hub_map.empty:
        st.stop()

    controls = render_market_controls(hub_map)
    heat_rate: float = controls["heat_rate"]
    selected_regions: list[str] = controls["regions"]
    lookback_days: int = controls["lookback_days"]
    force_refresh: bool = controls["force_refresh"]

    power_df, gas_df, is_sample = load_market_data(force_refresh)
    if is_sample:
        st.warning(
            "Using **sample data** for demonstration. Place real EIA data "
            "files in `data/eia_wholesale/` (as `power_prices.csv` and "
            "`gas_prices.csv` with columns: date, hub, price) for live "
            "market data."
        )

    merged = eia_wholesale.merge_power_gas_hubs(hub_map, power_df, gas_df)
    if merged.empty:
        st.error("No market data after merging hub pairs.")
        st.stop()

    market_df = calculate_spark_spreads(merged, heat_rate)

    if selected_regions:
        market_df = market_df[market_df["region"].isin(selected_regions)]
    if market_df.empty:
        st.warning("No data for selected regions.")
        st.stop()

    summary = build_spark_spread_summary(market_df)

    # ---- sections ----
    render_market_overview_cards(summary)

    st.markdown("---")
    render_spark_spread_monitor(summary)

    st.markdown("---")
    c1, c2 = st.columns(2)
    with c1:
        render_spark_spread_chart(market_df, selected_regions, lookback_days)
    with c2:
        render_implied_heat_rate_chart(market_df, selected_regions, lookback_days)

    st.markdown("---")
    render_power_vs_fuel_cost_chart(market_df, heat_rate, lookback_days)

    st.markdown("---")
    render_gas_basis_monitor(market_df)

    st.markdown("---")
    render_stock_watchlist()

    st.markdown("---")
    render_data_notes()


if __name__ == "__main__":
    render_dashboard()
