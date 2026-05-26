"""
Power Producer Watchlist — standalone Streamlit dashboard.

Tracks IPPs / power producers by generation source and stock momentum.
Reuses the project's FMP data_loader for price history and caching.

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WATCHLIST_CSV = config.PROJECT_ROOT / "data" / "power_producer_watchlist.csv"

RETURN_LOOKBACKS: dict[str, int] = {
    "1D": 1,
    "1W": 5,
    "1M": 21,
    "3M": 63,
    "6M": 126,
}
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


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_watchlist() -> pd.DataFrame:
    """Read the power-producer CSV universe."""
    if not WATCHLIST_CSV.is_file():
        st.error(f"Watchlist CSV not found: `{WATCHLIST_CSV}`")
        return pd.DataFrame()
    df = pd.read_csv(WATCHLIST_CSV, dtype=str).fillna("")
    df.columns = df.columns.str.strip()
    df["ticker"] = df["ticker"].str.strip().str.upper()
    return df


def _load_api_key() -> str:
    """Return the FMP API key or empty string (with a visible Streamlit warning)."""
    load_dotenv(config.PROJECT_ROOT / ".env")
    key = (os.getenv("FMP_API_KEY") or "").strip()
    if not key:
        st.warning(
            "FMP_API_KEY is missing. Add it to `.env` or Streamlit secrets to load price data."
        )
    return key


@st.cache_data(ttl=900, show_spinner="Fetching FMP prices…")
def fetch_price_history(ticker: str, date_from: date, date_to: date) -> pd.Series:
    """Daily adjusted-close series for one ticker (cached 15 min)."""
    load_dotenv(config.PROJECT_ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        return pd.Series(dtype=float, name=ticker)
    session = data_loader.create_http_session()
    try:
        hist = data_loader.get_price_history(session, api_key, ticker, date_from, date_to)
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
    """
    Return from ``prices[-start_offset]`` to ``prices[-end_offset]``.

    Offsets are in trading-day bars from the end (1-based).
    ``end_offset=1`` means latest bar; ``start_offset=22`` means 22 bars back.
    """
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
    """All return columns from a daily adjusted-close series."""
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
    """Weighted momentum score; re-normalizes if some components are missing."""
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


def build_full_table(watchlist: pd.DataFrame, date_to: date) -> pd.DataFrame:
    """Fetch prices and compute returns for every ticker in the watchlist."""
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


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _fmt_pct(x: Any) -> str:
    try:
        if pd.isna(x) or not np.isfinite(float(x)):
            return "N/A"
        return f"{float(x) * 100:.2f}%"
    except Exception:
        return "N/A"


def _fmt_pct_num(x: Any) -> float | None:
    """Return as percentage float (e.g. 0.05 → 5.0) for heatmap values."""
    try:
        if pd.isna(x) or not np.isfinite(float(x)):
            return None
        return float(x) * 100.0
    except Exception:
        return None


def _display_columns() -> list[str]:
    return [
        "ticker",
        "company",
        "type",
        "main_power_source",
        "secondary_power_source",
        "region_market",
    ] + RETURN_COLS + [
        "Momentum Score",
        "notes",
    ]


def _rename_map() -> dict[str, str]:
    return {
        "ticker": "Ticker",
        "company": "Company",
        "type": "Type",
        "main_power_source": "Main Power Source",
        "secondary_power_source": "Secondary Power Source",
        "region_market": "Region / Market",
        "notes": "Notes",
    }


def format_table(df: pd.DataFrame) -> pd.DataFrame:
    """Human-readable display copy."""
    cols = [c for c in _display_columns() if c in df.columns]
    out = df[cols].copy().rename(columns=_rename_map())
    for c in RETURN_COLS:
        if c in out.columns:
            out[c] = out[c].apply(_fmt_pct)
    if "Momentum Score" in out.columns:
        out["Momentum Score"] = out["Momentum Score"].apply(_fmt_pct)
    return out


# ---------------------------------------------------------------------------
# Heatmap
# ---------------------------------------------------------------------------
def build_heatmap(df: pd.DataFrame) -> None:
    """Plotly heatmap: tickers × return columns, red/yellow/green."""
    try:
        import plotly.graph_objects as go
    except ImportError:
        st.warning("Install plotly for the heatmap: `pip install plotly>=5.20`")
        return

    tickers = df["ticker"].tolist()
    z: list[list[float | None]] = []
    for _, row in df.iterrows():
        z.append([_fmt_pct_num(row.get(c)) for c in RETURN_COLS])

    fig = go.Figure(
        go.Heatmap(
            z=z,
            x=RETURN_COLS,
            y=tickers,
            colorscale=[
                [0.0, "#d32f2f"],
                [0.4, "#ffeb3b"],
                [1.0, "#388e3c"],
            ],
            zmid=0,
            text=[[f"{v:.2f}%" if v is not None else "N/A" for v in row] for row in z],
            texttemplate="%{text}",
            hovertemplate="Ticker: %{y}<br>Period: %{x}<br>Return: %{text}<extra></extra>",
            colorbar=dict(title="Return %"),
        )
    )
    fig.update_layout(
        title="Return Heatmap",
        xaxis_title="Period",
        yaxis_title="Ticker",
        yaxis=dict(autorange="reversed"),
        height=max(340, 50 * len(tickers)),
        margin=dict(l=100),
    )
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Group summary
# ---------------------------------------------------------------------------
def build_group_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Average returns and momentum by Type."""
    metric_cols = ["1M", "3M", "6M", "12M Skip 1M", "Momentum Score"]
    present = [c for c in metric_cols if c in df.columns]
    if not present:
        return pd.DataFrame()
    grouped = df.groupby("type", dropna=False)[present].apply(
        lambda g: g.apply(lambda s: s.dropna().mean())
    )
    grouped["Companies"] = df.groupby("type")["ticker"].count()
    grouped = grouped.reset_index().rename(columns={"type": "Type"})
    for c in present:
        grouped[c] = grouped[c].apply(_fmt_pct)
    grouped = grouped.rename(
        columns={
            "1M": "Avg 1M",
            "3M": "Avg 3M",
            "6M": "Avg 6M",
            "12M Skip 1M": "Avg 12M Skip 1M",
            "Momentum Score": "Avg Momentum",
        }
    )
    return grouped


# ---------------------------------------------------------------------------
# Main dashboard renderer
# ---------------------------------------------------------------------------
def render_dashboard() -> None:
    st.set_page_config(page_title="Power Producer Watchlist", layout="wide")
    st.title("Power Producer Watchlist")
    st.caption(
        "Track IPPs, nuclear-heavy generators, renewable IPPs, and utility comps by "
        "generation source and momentum."
    )
    st.markdown(
        "This dashboard compares public power producers by generation source and momentum. "
        "It is meant to identify which power themes are being rewarded by the market."
    )

    _load_api_key()

    watchlist = load_watchlist()
    if watchlist.empty:
        st.stop()

    today = date.today()
    df = build_full_table(watchlist, today)
    if df.empty:
        st.error("No data returned for any ticker.")
        st.stop()

    # ---- sidebar filters ----
    st.sidebar.header("Filters")

    all_types = sorted(df["type"].dropna().unique().tolist())
    sel_types = st.sidebar.multiselect("Type", all_types, default=all_types)

    all_sources = sorted(
        {s.strip() for vals in df["main_power_source"].dropna() for s in str(vals).split(";")}
    )
    sel_sources = st.sidebar.multiselect("Main power source", all_sources, default=all_sources)

    all_regions = sorted(
        {r.strip() for vals in df["region_market"].dropna() for r in str(vals).split(";")}
    )
    sel_regions = st.sidebar.multiselect("Region / market", all_regions, default=all_regions)

    sel_tickers = st.sidebar.multiselect(
        "Tickers", df["ticker"].tolist(), default=df["ticker"].tolist()
    )

    sort_options = RETURN_COLS + ["Momentum Score"]
    sort_by = st.sidebar.selectbox("Sort by", sort_options, index=sort_options.index("Momentum Score"))

    # ---- apply filters ----
    mask = df["type"].isin(sel_types) & df["ticker"].isin(sel_tickers)
    mask &= df["main_power_source"].apply(
        lambda v: any(s.strip() in sel_sources for s in str(v).split(";"))
    )
    mask &= df["region_market"].apply(
        lambda v: any(r.strip() in sel_regions for r in str(v).split(";"))
    )
    filtered = df.loc[mask].copy()

    if filtered.empty:
        st.warning("No tickers match current filters.")
        st.stop()

    filtered = filtered.sort_values(sort_by, ascending=False, na_position="last")

    # ---- summary cards ----
    st.subheader("Top performers")
    card_cols = st.columns(4)
    for i, (label, col_name) in enumerate(
        [("Best 1M", "1M"), ("Best 3M", "3M"), ("Best 6M", "6M"), ("Best 12M Skip 1M", "12M Skip 1M")]
    ):
        vals = filtered[col_name].dropna()
        if vals.empty:
            card_cols[i].metric(label, "N/A")
            continue
        best_idx = vals.idxmax()
        best_row = filtered.loc[best_idx]
        card_cols[i].metric(
            label,
            _fmt_pct(best_row[col_name]),
            delta=f"{best_row['ticker']}  —  {best_row['company']}",
        )

    # ---- main table ----
    st.subheader("Watchlist")
    st.dataframe(format_table(filtered), use_container_width=True, hide_index=True)

    # ---- heatmap ----
    st.subheader("Return heatmap")
    build_heatmap(filtered)

    # ---- group summary ----
    st.subheader("Group summary by type")
    group = build_group_summary(filtered)
    if not group.empty:
        st.dataframe(group, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    render_dashboard()
