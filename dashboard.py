"""
Streamlit dashboard: sector ETF analytics (FMP dividend-adjusted prices).

- **Technology**: XLK trend/technicals, proxy-basket rotation vs XLK, vs SPY, risk, dispersion.
- **Basic Materials**: XLB (same pattern).
- **Communication Services**: XLC.
- **Consumer Cyclical**: XLY.
- **Consumer Defensive**: XLP.
- **Energy**: XLE.
- **Financial Services**: XLF.
- **Healthcare**: XLV.
- **Industrials**: XLI.
- **Real Estate**: XLRE.
- **Utilities**: XLU.

Use the **sector tabs** (horizontal control) to pick a view: the **selected** sector loads first;
other sectors are **warmed in a background thread** so switching tabs is faster once caches fill.

Install and run:
  pip install streamlit pandas openpyxl python-dotenv requests urllib3
  streamlit run dashboard.py
"""

from __future__ import annotations

import inspect
import os
import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import config
import data_loader
import dispersion_engine
import comm_rotation_engine
import consumer_cyclical_rotation_engine
import consumer_defensive_rotation_engine
import energy_rotation_engine
import financial_services_rotation_engine
import healthcare_rotation_engine
import industrials_rotation_engine
import materials_rotation_engine
import real_estate_rotation_engine
import sector_pages
import utilities_rotation_engine
import tech_rotation_engine

ROOT = Path(__file__).resolve().parent
# Works even if local `config.py` predates `DASHBOARD_CACHE_TTL_SECONDS` (pull latest or restart after editing config).
_CACHE_TTL_SECONDS: int = int(getattr(config, "DASHBOARD_CACHE_TTL_SECONDS", 3600))


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading sector dispersion…")
def _cached_sector_dispersion(api_key: str, sector: str) -> dict:
    """Session is created inside the cache (``Session`` objects are not cache-safe keys)."""
    session = data_loader.create_http_session()
    bundle_fn = dispersion_engine.run_dispersion_dashboard_bundle
    params = inspect.signature(bundle_fn).parameters
    if "sector" in params:
        return bundle_fn(session, api_key, sector=sector, force_refresh=False)
    if str(sector).strip() != "Technology":
        return {
            "ok": False,
            "error": (
                "`dispersion_engine.run_dispersion_dashboard_bundle` is missing the `sector` parameter "
                "(reload `dispersion_engine.py` from this project and restart Streamlit so multi-sector "
                "dispersion tabs can filter the universe)."
            ),
            "universe": pd.DataFrame(),
            "summary": {},
            "tables": {},
            "wide_close": pd.DataFrame(),
            "breadth_ts": pd.DataFrame(),
            "dispersion_ts": pd.DataFrame(),
            "as_of": date.today(),
        }
    return bundle_fn(session, api_key, force_refresh=False)


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Technology industry rotation…")
def _cached_tech_rotation(api_key: str) -> dict:
    """Industry proxy-basket RS vs XLK for Technology internal rotation view."""
    session = data_loader.create_http_session()
    return tech_rotation_engine.build_tech_rotation_bundle(session, api_key, force_refresh=False)


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Basic Materials industry rotation…")
def _cached_materials_rotation(api_key: str) -> dict:
    """Industry ETF RS vs XLB for Basic Materials internal rotation view."""
    session = data_loader.create_http_session()
    return materials_rotation_engine.build_materials_rotation_bundle(
        session, api_key, force_refresh=False
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Communication Services industry rotation…")
def _cached_comm_rotation(api_key: str) -> dict:
    """Industry proxy-basket RS vs XLC for Communication Services internal rotation view."""
    session = data_loader.create_http_session()
    return comm_rotation_engine.build_comm_rotation_bundle(session, api_key, force_refresh=False)


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Consumer Cyclical industry rotation…")
def _cached_consumer_cyclical_rotation(api_key: str) -> dict:
    """Industry proxy-basket RS vs XLY for Consumer Cyclical internal rotation view."""
    session = data_loader.create_http_session()
    return consumer_cyclical_rotation_engine.build_consumer_cyclical_rotation_bundle(
        session, api_key, force_refresh=False
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Consumer Defensive industry rotation…")
def _cached_consumer_defensive_rotation(api_key: str) -> dict:
    """Industry proxy-basket RS vs XLP for Consumer Defensive internal rotation view."""
    session = data_loader.create_http_session()
    return consumer_defensive_rotation_engine.build_consumer_defensive_rotation_bundle(
        session, api_key, force_refresh=False
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Energy industry rotation…")
def _cached_energy_rotation(api_key: str) -> dict:
    """Industry proxy-basket RS vs XLE for Energy internal rotation view."""
    session = data_loader.create_http_session()
    return energy_rotation_engine.build_energy_rotation_bundle(session, api_key, force_refresh=False)


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Financial Services industry rotation…")
def _cached_financial_services_rotation(api_key: str) -> dict:
    """Industry proxy-basket RS vs XLF for Financial Services internal rotation view."""
    session = data_loader.create_http_session()
    return financial_services_rotation_engine.build_financial_services_rotation_bundle(
        session, api_key, force_refresh=False
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Healthcare industry rotation…")
def _cached_healthcare_rotation(api_key: str) -> dict:
    """Industry proxy-basket RS vs XLV for Healthcare internal rotation view."""
    session = data_loader.create_http_session()
    return healthcare_rotation_engine.build_healthcare_rotation_bundle(
        session, api_key, force_refresh=False
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Industrials industry rotation…")
def _cached_industrials_rotation(api_key: str) -> dict:
    """Industry proxy-basket RS vs XLI for Industrials internal rotation view."""
    session = data_loader.create_http_session()
    return industrials_rotation_engine.build_industrials_rotation_bundle(
        session, api_key, force_refresh=False
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Real Estate industry rotation…")
def _cached_real_estate_rotation(api_key: str) -> dict:
    """Industry proxy-basket RS vs XLRE for Real Estate internal rotation view."""
    session = data_loader.create_http_session()
    return real_estate_rotation_engine.build_real_estate_rotation_bundle(
        session, api_key, force_refresh=False
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Utilities industry rotation…")
def _cached_utilities_rotation(api_key: str) -> dict:
    """Industry proxy-basket RS vs XLU for Utilities internal rotation view."""
    session = data_loader.create_http_session()
    return utilities_rotation_engine.build_utilities_rotation_bundle(
        session, api_key, force_refresh=False
    )


# Background cache warming (non-active sectors). Streamlit re-runs the script often; only one
# warmer thread at a time so we do not stack duplicate FMP work.
_WARM_THREAD: threading.Thread | None = None
_WARM_START_LOCK = threading.Lock()

_SECTOR_WARM_SPECS: tuple[tuple[str, str, Callable[[str], dict]], ...] = (
    ("Technology Sector", "Technology", _cached_tech_rotation),
    ("Basic Materials Sector", "Basic Materials", _cached_materials_rotation),
    ("Communication Services Sector", "Communication Services", _cached_comm_rotation),
    ("Consumer Cyclical Sector", "Consumer Cyclical", _cached_consumer_cyclical_rotation),
    ("Consumer Defensive Sector", "Consumer Defensive", _cached_consumer_defensive_rotation),
    ("Energy Sector", "Energy", _cached_energy_rotation),
    ("Financial Services Sector", "Financial Services", _cached_financial_services_rotation),
    ("Healthcare Sector", "Healthcare", _cached_healthcare_rotation),
    ("Industrials Sector", "Industrials", _cached_industrials_rotation),
    ("Real Estate Sector", "Real Estate", _cached_real_estate_rotation),
    ("Utilities Sector", "Utilities", _cached_utilities_rotation),
)


def _spawn_background_sector_warm(api_key: str, active_page: str) -> None:
    """
    After the active sector UI is rendered, pre-fill ``@st.cache_data`` for all other sectors.

    The selected sector is **not** included here so its work stays on the hot path only once
    (already done by the visible ``render_*`` call).
    """
    key = (api_key or "").strip()
    if not key:
        return

    def _worker() -> None:
        global _WARM_THREAD
        try:
            for page_label, fmp_sector, rot_cache in _SECTOR_WARM_SPECS:
                if page_label == active_page:
                    continue
                try:
                    _cached_sector_dispersion(key, fmp_sector)
                except Exception:
                    pass
                try:
                    rot_cache(key)
                except Exception:
                    pass
        finally:
            with _WARM_START_LOCK:
                _WARM_THREAD = None

    global _WARM_THREAD
    with _WARM_START_LOCK:
        if _WARM_THREAD is not None and _WARM_THREAD.is_alive():
            return
        _WARM_THREAD = threading.Thread(
            target=_worker,
            daemon=True,
            name="dashboard-sector-cache-warm",
        )
        _WARM_THREAD.start()


def _fmt_rel_pct(x: object) -> str:
    v = pd.to_numeric(x, errors="coerce")
    if v is None or pd.isna(v):
        return "N/A"
    return f"{float(v) * 100.0:+.2f}%"


def _fmt_vol_pct(x: object) -> str:
    """Annualized realized vol (always shown as a positive %, no leading +)."""
    v = pd.to_numeric(x, errors="coerce")
    if v is None or pd.isna(v):
        return "N/A"
    return f"{float(v) * 100.0:.2f}%"


def _dispersion_health_chart(disp_bundle: dict) -> pd.DataFrame:
    """
    Merge breadth and dispersion time series for one combined line chart (percentage points).

    Breadth DMA columns are forward-filled for display only so the 200-DMA line stays visually
    continuous when raw pct_above_200dma is missing on later dates; EW/CW σ uses unfilled inputs.
    """
    bts = disp_bundle.get("breadth_ts")
    dts = disp_bundle.get("dispersion_ts")
    if not isinstance(bts, pd.DataFrame) or bts.empty or "date" not in bts.columns:
        return pd.DataFrame()

    b = bts.copy()
    b["date"] = pd.to_datetime(b["date"], errors="coerce")
    b = b.dropna(subset=["date"]).set_index("date")[["pct_above_50dma", "pct_above_200dma"]]

    if isinstance(dts, pd.DataFrame) and not dts.empty and "date" in dts.columns:
        d = dts.copy()
        d["date"] = pd.to_datetime(d["date"], errors="coerce")
        d = d.dropna(subset=["date"]).set_index("date")[["equal_weight_std", "cap_weight_std"]]
        merged = b.join(d, how="outer").sort_index()
    else:
        merged = b.sort_index()

    chart_df = merged.copy()
    chart_df[["pct_above_50dma", "pct_above_200dma"]] = chart_df[
        ["pct_above_50dma", "pct_above_200dma"]
    ].ffill()

    out = pd.DataFrame(index=chart_df.index)
    out["Breadth 50 DMA %"] = pd.to_numeric(chart_df["pct_above_50dma"], errors="coerce") * 100.0
    out["Breadth 200 DMA %"] = pd.to_numeric(chart_df["pct_above_200dma"], errors="coerce") * 100.0
    if "equal_weight_std" in merged.columns and "cap_weight_std" in merged.columns:
        ew = pd.to_numeric(merged["equal_weight_std"], errors="coerce")
        cw = pd.to_numeric(merged["cap_weight_std"], errors="coerce")
        out["EW - CW σ Spread %"] = (ew - cw) * 100.0
    return out.sort_index()


def render_technology_sector_tab() -> None:
    """XLK trend/technicals, industry rotation vs XLK, XLK vs SPY, risk, internal dispersion."""
    st.subheader("Technology Sector Analysis")

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        st.warning(
            "FMP_API_KEY is not set (check `.env`). Live XLK / SPY sections need a key; other tabs still work."
        )
        return

    try:
        session = data_loader.create_http_session()
    except Exception as e:
        st.warning(f"Could not create HTTP session: {e}")
        return

    # --- A: XLK standalone ---
    st.subheader("Technology ETF Trend & Technicals")
    st.caption("This section analyzes XLK on its own before comparing Technology against SPY.")

    trend_detail = pd.DataFrame()
    trend_summary: dict = {}
    try:
        trend_detail, trend_summary = sector_pages.get_sector_etf_trend_data(session, api_key)
    except Exception as e:
        st.warning(f"Could not load XLK trend data: {e}")

    if trend_detail.empty:
        st.warning("XLK price history is missing or unavailable; trend & technicals section skipped.")
    else:
        as_of_t = trend_summary.get("as_of_date")
        st.markdown(f"**As of:** `{as_of_t}`")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("1W Return", _fmt_rel_pct(trend_summary.get("return_1w")))
        c2.metric("1M Return", _fmt_rel_pct(trend_summary.get("return_1m")))
        c3.metric("3M Return", _fmt_rel_pct(trend_summary.get("return_3m")))
        c4.metric("12M Skip-1M Return", _fmt_rel_pct(trend_summary.get("return_12m_skip_1m")))

        plot_df = trend_detail.dropna(subset=["dma_200"]).copy()
        if plot_df.empty:
            st.info("Not enough history yet to plot 200-DMA alongside price.")
        else:
            chart = plot_df[["date", "price", "dma_50", "dma_100", "dma_200"]].copy()
            chart["date"] = pd.to_datetime(chart["date"], errors="coerce")
            chart = chart.dropna(subset=["date"]).set_index("date")
            chart = chart.rename(
                columns={
                    "price": "XLK price",
                    "dma_50": "50 DMA",
                    "dma_100": "100 DMA",
                    "dma_200": "200 DMA",
                }
            )
            st.markdown("**XLK Price with Moving Averages**")
            st.line_chart(chart, height=460)

    st.divider()
    st.subheader("Technology Industry Rotation")
    st.caption(
        "Relative strength of equal-weight **proxy baskets** (stocks and ETFs listed per row) versus XLK. "
        "Positive values mean that basket is outperforming broad Technology over the selected window."
    )
    try:
        rot_bundle = _cached_tech_rotation(api_key)
    except Exception as e:
        rot_bundle = {"ok": False, "error": str(e)}
    if not rot_bundle.get("ok"):
        st.warning(rot_bundle.get("error") or "Technology industry rotation unavailable (check FMP key and caches).")
    else:
        st.markdown(f"**As of:** `{rot_bundle.get('as_of')}`")
        hm = rot_bundle.get("heatmap")
        if isinstance(hm, pd.DataFrame) and not hm.empty:
            # Centered diverging colors on ±30; gmap clips only for colormap, not displayed values.
            gmap_rot = hm.clip(lower=-30.0, upper=30.0)
            styled = (
                hm.style.background_gradient(
                    cmap="RdYlGn",
                    axis=None,
                    vmin=-30,
                    vmax=30,
                    gmap=gmap_rot,
                ).format("{:+.2f}%", na_rep="—")
            )
            st.dataframe(styled, use_container_width=True, hide_index=False)
            col_3m = pd.to_numeric(hm.get("3M RS %"), errors="coerce").dropna()
            if not col_3m.empty:
                mx = float(col_3m.max())
                if mx > 0:
                    top_lbl = str(col_3m.idxmax())
                    st.caption(f"Leadership: **{top_lbl}** is leading Technology over the last 3 months.")
                else:
                    st.caption("No proxy basket is outperforming XLK over the last 3 months.")
        else:
            st.info("No rotation heatmap to display.")

        with st.expander("Show detailed rotation tables"):
            met_r = rot_bundle.get("metrics")
            if isinstance(met_r, pd.DataFrame) and not met_r.empty:
                st.markdown("**Relative strength metrics**")
                st.dataframe(met_r.round(4), use_container_width=True, hide_index=True)
            px_r = rot_bundle.get("prices")
            if isinstance(px_r, pd.DataFrame) and not px_r.empty:
                st.markdown("**Latest prices (long format)**")
                st.dataframe(px_r.sort_values(["date", "symbol"], ascending=[False, True]), use_container_width=True, hide_index=True)
            rsh = rot_bundle.get("rs_ratio_history")
            if isinstance(rsh, pd.DataFrame) and not rsh.empty:
                st.markdown("**RS ratio vs XLK (level, not % change)**")
                st.dataframe(rsh.tail(500).round(6), use_container_width=True)

    st.divider()
    st.subheader("Technology vs SPY")
    st.caption(
        "XLK (Technology) vs SPY (S&P 500). Dividend-adjusted closes from FMP; cached like other price pulls."
    )

    detail = pd.DataFrame()
    summary: dict = {}
    try:
        detail, summary = sector_pages.get_sector_vs_spy_data(session, api_key)
    except Exception as e:
        st.warning(f"Could not load sector vs benchmark data: {e}")

    if detail.empty:
        st.warning(
            "Price data for XLK and/or SPY is missing or could not be aligned. "
            "Check your API key and try again after a successful `get_price_history` fetch."
        )
    else:
        as_of = summary.get("as_of_date")
        etf = str(summary.get("sector_etf", "XLK"))
        bench = str(summary.get("benchmark", "SPY"))
        col_idx_etf = f"{etf}_index"
        col_idx_bench = f"{bench}_index"

        st.markdown(f"**As of:** `{as_of}`")

        rr1w = summary.get("relative_return_1w")
        rr1 = summary.get("relative_return_1m")
        rr3 = summary.get("relative_return_3m")
        rr6 = summary.get("relative_return_6m")
        rr12s = summary.get("relative_return_12m_skip_1m")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("1W relative return vs SPY", _fmt_rel_pct(rr1w))
        c2.metric("1M relative return vs SPY", _fmt_rel_pct(rr1))
        c3.metric("3M relative return vs SPY", _fmt_rel_pct(rr3))
        c4.metric("6M relative return vs SPY", _fmt_rel_pct(rr6))
        c5.metric("12M skip-1M relative return vs SPY", _fmt_rel_pct(rr12s))

        st.caption(
            "Positive relative return means Technology is outperforming SPY over that window."
        )

        perf = detail[["date", col_idx_etf, col_idx_bench]].copy()
        perf["date"] = pd.to_datetime(perf["date"], errors="coerce")
        perf = perf.dropna(subset=["date"]).set_index("date")
        perf = perf.rename(
            columns={
                col_idx_etf: f"{etf} (normalized)",
                col_idx_bench: f"{bench} (normalized)",
            }
        )
        st.markdown("**XLK vs SPY Cumulative Performance**")
        st.line_chart(perf, height=420)

        ratio = detail[["date", "relative_strength_ratio"]].copy()
        ratio["date"] = pd.to_datetime(ratio["date"], errors="coerce")
        ratio = ratio.dropna(subset=["date"]).set_index("date")
        ratio = ratio.rename(columns={"relative_strength_ratio": f"{etf} / {bench} ratio"})
        st.markdown("**XLK / SPY Relative Strength Ratio**")
        st.line_chart(ratio, height=380)

    # --- Risk ---
    st.divider()
    st.subheader("Risk")
    st.caption("Realized volatility shows how unstable XLK has been historically.")

    risk_summary: dict = {}
    dd_ts = pd.DataFrame()
    try:
        risk_summary, _, dd_ts = sector_pages.get_sector_risk_data(session, api_key)
    except Exception as e:
        st.warning(f"Could not load XLK risk data: {e}")

    if dd_ts.empty:
        st.warning("XLK price history is missing; risk section skipped.")
    else:
        st.markdown(f"**As of:** `{risk_summary.get('as_of_date')}`")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trailing 1Y Vol", _fmt_vol_pct(risk_summary.get("trailing_1y_vol")))
        m2.metric("Trailing 3M Vol", _fmt_vol_pct(risk_summary.get("trailing_3m_vol")))
        m3.metric("Trailing 20D Vol", _fmt_vol_pct(risk_summary.get("trailing_20d_vol")))
        m4.metric("Trailing 1Y Max Drawdown", _fmt_rel_pct(risk_summary.get("trailing_1y_max_drawdown")))
        m5.metric("Current Drawdown vs 1Y High", _fmt_rel_pct(risk_summary.get("current_drawdown_1y_high")))

        st.markdown("**XLK Drawdown Over Time**")
        if dd_ts.empty:
            st.info("No drawdown series to plot.")
        else:
            dd_plot = dd_ts.copy()
            dd_plot["date"] = pd.to_datetime(dd_plot["date"], errors="coerce")
            dd_plot = dd_plot.dropna(subset=["date"]).set_index("date")
            st.line_chart(dd_plot[["drawdown"]], height=360)

    # --- Technology internal dispersion (dispersion universe; not ranking fundamentals) ---
    st.divider()
    st.subheader("Technology Internal Dispersion")
    st.caption(
        "Breadth, cross-sectional dispersion, and concentration for the US Technology **dispersion** "
        f"universe (market cap > ${config.DISPERSION_MIN_MARKET_CAP/1e9:.1f}B, avg volume > "
        f"{config.DISPERSION_MIN_AVG_VOLUME/1e3:.0f}k, price > ${config.DISPERSION_MIN_PRICE:.0f}; "
        "ETFs/funds excluded). Prices are dividend-adjusted from `data_loader.get_price_history`."
    )
    try:
        disp_bundle = _cached_sector_dispersion(api_key, "Technology")
    except Exception as e:
        disp_bundle = {"ok": False, "error": str(e)}
    if not disp_bundle.get("ok"):
        st.warning(
            disp_bundle.get("error")
            or "Dispersion analytics unavailable (check FMP key and profile-bulk access)."
        )
    else:
        summ = disp_bundle.get("summary") or {}
        tables = disp_bundle.get("tables") or {}
        as_of = disp_bundle.get("as_of")
        st.markdown(
            f"**As of:** `{as_of}` — names below are the intersection of the dispersion universe and "
            "symbols with sufficient price history for DMAs and 1M returns."
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Universe size", f"{summ.get('universe_size', 0):,}" if summ.get("universe_size") is not None else "—")
        m2.metric("% Above 50 DMA", _fmt_rel_pct(summ.get("pct_above_50dma")))
        m3.metric("% Above 200 DMA", _fmt_rel_pct(summ.get("pct_above_200dma")))
        m4.metric("Equal-weight 1M σ", _fmt_rel_pct(summ.get("equal_weight_std")))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Cap-weight 1M σ", _fmt_rel_pct(summ.get("cap_weight_std")))
        avpc = summ.get("avg_pairwise_corr")
        m6.metric(
            "Avg pairwise corr (60d)",
            f"{float(avpc):.3f}" if avpc is not None and avpc == avpc else "—",
        )
        m7.metric("Top 5 cap weight", _fmt_rel_pct(summ.get("top5_weight")))
        ew_s = summ.get("equal_weight_std")
        cw_s = summ.get("cap_weight_std")
        if ew_s is not None and cw_s is not None and ew_s == ew_s and cw_s == cw_s:
            ew_cw_spread = float(ew_s) - float(cw_s)
        else:
            ew_cw_spread = None
        m8.metric("EW - CW σ Spread", _fmt_rel_pct(ew_cw_spread))

        health = _dispersion_health_chart(disp_bundle)
        plot_cols = ["Breadth 50 DMA %", "Breadth 200 DMA %", "EW - CW σ Spread %"]
        st.markdown("**Technology Breadth & Internal Rotation**")
        if health.empty or not any(c in health.columns for c in plot_cols):
            st.info("Not enough overlapping breadth / dispersion history to plot the combined health series yet.")
        else:
            chart_df = health[[c for c in plot_cols if c in health.columns]].copy()
            st.line_chart(chart_df, height=420)
            st.caption(
                "DMA breadth lines are forward-filled for chart continuity when later dates have missing "
                "200-DMA coverage. KPI cards still use the latest raw calculated values."
            )
        st.caption(
            "Breadth measures participation across the Technology universe. A rising EW-CW σ spread means internal "
            "rotation and stock-level dispersion are increasing beneath the cap-weighted index."
        )

        with st.expander("Show detailed dispersion tables"):
            bt = tables.get("breadth_table")
            if isinstance(bt, pd.DataFrame) and not bt.empty:
                st.markdown("**Breadth**")
                st.dataframe(bt, hide_index=True, width="stretch")
            dst = tables.get("dispersion_summary_table")
            if isinstance(dst, pd.DataFrame) and not dst.empty:
                st.markdown("**Dispersion summary**")
                st.dataframe(dst, hide_index=True, width="stretch")
            ct = tables.get("concentration_table")
            if isinstance(ct, pd.DataFrame) and not ct.empty:
                st.markdown("**Concentration**")
                st.dataframe(ct, hide_index=True, width="stretch")
            top_c = tables.get("top_contributors")
            if isinstance(top_c, pd.DataFrame) and not top_c.empty:
                st.markdown("**Top contributors**")
                st.dataframe(top_c.head(50), hide_index=True, width="stretch")
            bot_c = tables.get("bottom_contributors")
            if isinstance(bot_c, pd.DataFrame) and not bot_c.empty:
                st.markdown("**Bottom contributors**")
                st.dataframe(bot_c.head(50), hide_index=True, width="stretch")
            ind_p = tables.get("industry_participation")
            if isinstance(ind_p, pd.DataFrame) and not ind_p.empty:
                st.markdown("**Industry participation**")
                st.dataframe(ind_p.round(6), hide_index=True, width="stretch")


def render_basic_materials_sector_tab() -> None:
    """XLB trend/technicals, industry rotation vs XLB, XLB vs SPY, risk, internal dispersion."""
    st.subheader("Basic Materials Sector Analysis")

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        st.warning(
            "FMP_API_KEY is not set (check `.env`). Live XLB / SPY sections need a key; other tabs still work."
        )
        return

    try:
        session = data_loader.create_http_session()
    except Exception as e:
        st.warning(f"Could not create HTTP session: {e}")
        return

    st.subheader("Basic Materials ETF Trend & Technicals")
    st.caption("This section analyzes XLB on its own before comparing Basic Materials against SPY.")

    trend_detail = pd.DataFrame()
    trend_summary: dict = {}
    try:
        trend_detail, trend_summary = sector_pages.get_sector_etf_trend_data(
            session, api_key, sector_etf="XLB"
        )
    except Exception as e:
        st.warning(f"Could not load XLB trend data: {e}")

    if trend_detail.empty:
        st.warning("XLB price history is missing or unavailable; trend & technicals section skipped.")
    else:
        as_of_t = trend_summary.get("as_of_date")
        st.markdown(f"**As of:** `{as_of_t}`")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("1W Return", _fmt_rel_pct(trend_summary.get("return_1w")))
        c2.metric("1M Return", _fmt_rel_pct(trend_summary.get("return_1m")))
        c3.metric("3M Return", _fmt_rel_pct(trend_summary.get("return_3m")))
        c4.metric("12M Skip-1M Return", _fmt_rel_pct(trend_summary.get("return_12m_skip_1m")))

        plot_df = trend_detail.dropna(subset=["dma_200"]).copy()
        if plot_df.empty:
            st.info("Not enough history yet to plot 200-DMA alongside price.")
        else:
            chart = plot_df[["date", "price", "dma_50", "dma_100", "dma_200"]].copy()
            chart["date"] = pd.to_datetime(chart["date"], errors="coerce")
            chart = chart.dropna(subset=["date"]).set_index("date")
            chart = chart.rename(
                columns={
                    "price": "XLB price",
                    "dma_50": "50 DMA",
                    "dma_100": "100 DMA",
                    "dma_200": "200 DMA",
                }
            )
            st.markdown("**XLB Price with Moving Averages**")
            st.line_chart(chart, height=460)

    st.divider()
    st.subheader("Basic Materials Industry Rotation")
    st.caption(
        "Relative strength of Basic Materials industry ETFs versus XLB. Positive values mean that industry ETF is "
        "outperforming broad Basic Materials over the selected window."
    )
    try:
        rot_bundle = _cached_materials_rotation(api_key)
    except Exception as e:
        rot_bundle = {"ok": False, "error": str(e)}
    if not rot_bundle.get("ok"):
        st.warning(
            rot_bundle.get("error")
            or "Basic Materials industry rotation unavailable (check FMP key and caches)."
        )
    else:
        st.markdown(f"**As of:** `{rot_bundle.get('as_of')}`")
        hm = rot_bundle.get("heatmap")
        if isinstance(hm, pd.DataFrame) and not hm.empty:
            gmap_rot = hm.clip(lower=-30.0, upper=30.0)
            styled = (
                hm.style.background_gradient(
                    cmap="RdYlGn",
                    axis=None,
                    vmin=-30,
                    vmax=30,
                    gmap=gmap_rot,
                ).format("{:+.2f}%", na_rep="—")
            )
            st.dataframe(styled, use_container_width=True, hide_index=False)
            col_3m = pd.to_numeric(hm.get("3M RS %"), errors="coerce").dropna()
            if not col_3m.empty:
                mx = float(col_3m.max())
                if mx > 0:
                    top_lbl = str(col_3m.idxmax())
                    st.caption(f"Leadership: **{top_lbl}** is leading Basic Materials over the last 3 months.")
                else:
                    st.caption("No industry ETF is outperforming XLB over the last 3 months.")
        else:
            st.info("No rotation heatmap to display.")

        with st.expander("Show detailed rotation tables"):
            met_r = rot_bundle.get("metrics")
            if isinstance(met_r, pd.DataFrame) and not met_r.empty:
                st.markdown("**Relative strength metrics**")
                st.dataframe(met_r.round(4), use_container_width=True, hide_index=True)
            px_r = rot_bundle.get("prices")
            if isinstance(px_r, pd.DataFrame) and not px_r.empty:
                st.markdown("**Latest prices (long format)**")
                st.dataframe(
                    px_r.sort_values(["date", "symbol"], ascending=[False, True]),
                    use_container_width=True,
                    hide_index=True,
                )
            rsh = rot_bundle.get("rs_ratio_history")
            if isinstance(rsh, pd.DataFrame) and not rsh.empty:
                st.markdown("**RS ratio vs XLB (level, not % change)**")
                st.dataframe(rsh.tail(500).round(6), use_container_width=True)

    st.divider()
    st.subheader("Basic Materials vs SPY")
    st.caption(
        "XLB (Basic Materials) vs SPY (S&P 500). Dividend-adjusted closes from FMP; cached like other price pulls."
    )

    detail = pd.DataFrame()
    summary: dict = {}
    try:
        detail, summary = sector_pages.get_sector_vs_spy_data(
            session, api_key, sector_etf="XLB", sector_name="Basic Materials"
        )
    except Exception as e:
        st.warning(f"Could not load sector vs benchmark data: {e}")

    if detail.empty:
        st.warning(
            "Price data for XLB and/or SPY is missing or could not be aligned. "
            "Check your API key and try again after a successful `get_price_history` fetch."
        )
    else:
        as_of = summary.get("as_of_date")
        etf = str(summary.get("sector_etf", "XLB"))
        bench = str(summary.get("benchmark", "SPY"))
        col_idx_etf = f"{etf}_index"
        col_idx_bench = f"{bench}_index"

        st.markdown(f"**As of:** `{as_of}`")

        rr1w = summary.get("relative_return_1w")
        rr1 = summary.get("relative_return_1m")
        rr3 = summary.get("relative_return_3m")
        rr6 = summary.get("relative_return_6m")
        rr12s = summary.get("relative_return_12m_skip_1m")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("1W relative return vs SPY", _fmt_rel_pct(rr1w))
        c2.metric("1M relative return vs SPY", _fmt_rel_pct(rr1))
        c3.metric("3M relative return vs SPY", _fmt_rel_pct(rr3))
        c4.metric("6M relative return vs SPY", _fmt_rel_pct(rr6))
        c5.metric("12M skip-1M relative return vs SPY", _fmt_rel_pct(rr12s))

        st.caption(
            "Positive relative return means Basic Materials is outperforming SPY over that window."
        )

        perf = detail[["date", col_idx_etf, col_idx_bench]].copy()
        perf["date"] = pd.to_datetime(perf["date"], errors="coerce")
        perf = perf.dropna(subset=["date"]).set_index("date")
        perf = perf.rename(
            columns={
                col_idx_etf: f"{etf} (normalized)",
                col_idx_bench: f"{bench} (normalized)",
            }
        )
        st.markdown(f"**{etf} vs SPY Cumulative Performance**")
        st.line_chart(perf, height=420)

        ratio = detail[["date", "relative_strength_ratio"]].copy()
        ratio["date"] = pd.to_datetime(ratio["date"], errors="coerce")
        ratio = ratio.dropna(subset=["date"]).set_index("date")
        ratio = ratio.rename(columns={"relative_strength_ratio": f"{etf} / {bench} ratio"})
        st.markdown(f"**{etf} / SPY Relative Strength Ratio**")
        st.line_chart(ratio, height=380)

    st.divider()
    st.subheader("Risk")
    st.caption("Realized volatility shows how unstable XLB has been historically.")

    risk_summary: dict = {}
    dd_ts = pd.DataFrame()
    try:
        risk_summary, _, dd_ts = sector_pages.get_sector_risk_data(session, api_key, sector_etf="XLB")
    except Exception as e:
        st.warning(f"Could not load XLB risk data: {e}")

    if dd_ts.empty:
        st.warning("XLB price history is missing; risk section skipped.")
    else:
        st.markdown(f"**As of:** `{risk_summary.get('as_of_date')}`")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trailing 1Y Vol", _fmt_vol_pct(risk_summary.get("trailing_1y_vol")))
        m2.metric("Trailing 3M Vol", _fmt_vol_pct(risk_summary.get("trailing_3m_vol")))
        m3.metric("Trailing 20D Vol", _fmt_vol_pct(risk_summary.get("trailing_20d_vol")))
        m4.metric("Trailing 1Y Max Drawdown", _fmt_rel_pct(risk_summary.get("trailing_1y_max_drawdown")))
        m5.metric("Current Drawdown vs 1Y High", _fmt_rel_pct(risk_summary.get("current_drawdown_1y_high")))

        st.markdown("**XLB Drawdown Over Time**")
        if dd_ts.empty:
            st.info("No drawdown series to plot.")
        else:
            dd_plot = dd_ts.copy()
            dd_plot["date"] = pd.to_datetime(dd_plot["date"], errors="coerce")
            dd_plot = dd_plot.dropna(subset=["date"]).set_index("date")
            st.line_chart(dd_plot[["drawdown"]], height=360)

    st.divider()
    st.subheader("Basic Materials Internal Dispersion")
    st.caption(
        "Breadth, cross-sectional dispersion, and concentration for the US Basic Materials **dispersion** "
        f"universe (market cap > ${config.DISPERSION_MIN_MARKET_CAP/1e9:.1f}B, avg volume > "
        f"{config.DISPERSION_MIN_AVG_VOLUME/1e3:.0f}k, price > ${config.DISPERSION_MIN_PRICE:.0f}; "
        "ETFs/funds excluded). Prices are dividend-adjusted from `data_loader.get_price_history`."
    )
    try:
        disp_bundle = _cached_sector_dispersion(api_key, "Basic Materials")
    except Exception as e:
        disp_bundle = {"ok": False, "error": str(e)}
    if not disp_bundle.get("ok"):
        st.warning(
            disp_bundle.get("error")
            or "Dispersion analytics unavailable (check FMP key and profile-bulk access)."
        )
    else:
        summ = disp_bundle.get("summary") or {}
        tables = disp_bundle.get("tables") or {}
        as_of = disp_bundle.get("as_of")
        st.markdown(
            f"**As of:** `{as_of}` — names below are the intersection of the dispersion universe and "
            "symbols with sufficient price history for DMAs and 1M returns."
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Universe size", f"{summ.get('universe_size', 0):,}" if summ.get("universe_size") is not None else "—")
        m2.metric("% Above 50 DMA", _fmt_rel_pct(summ.get("pct_above_50dma")))
        m3.metric("% Above 200 DMA", _fmt_rel_pct(summ.get("pct_above_200dma")))
        m4.metric("Equal-weight 1M σ", _fmt_rel_pct(summ.get("equal_weight_std")))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Cap-weight 1M σ", _fmt_rel_pct(summ.get("cap_weight_std")))
        avpc = summ.get("avg_pairwise_corr")
        m6.metric(
            "Avg pairwise corr (60d)",
            f"{float(avpc):.3f}" if avpc is not None and avpc == avpc else "—",
        )
        m7.metric("Top 5 cap weight", _fmt_rel_pct(summ.get("top5_weight")))
        ew_s = summ.get("equal_weight_std")
        cw_s = summ.get("cap_weight_std")
        if ew_s is not None and cw_s is not None and ew_s == ew_s and cw_s == cw_s:
            ew_cw_spread = float(ew_s) - float(cw_s)
        else:
            ew_cw_spread = None
        m8.metric("EW - CW σ Spread", _fmt_rel_pct(ew_cw_spread))

        health = _dispersion_health_chart(disp_bundle)
        plot_cols = ["Breadth 50 DMA %", "Breadth 200 DMA %", "EW - CW σ Spread %"]
        st.markdown("**Basic Materials Breadth & Internal Rotation**")
        if health.empty or not any(c in health.columns for c in plot_cols):
            st.info("Not enough overlapping breadth / dispersion history to plot the combined health series yet.")
        else:
            chart_df = health[[c for c in plot_cols if c in health.columns]].copy()
            st.line_chart(chart_df, height=420)
            st.caption(
                "DMA breadth lines are forward-filled for chart continuity when later dates have missing "
                "200-DMA coverage. KPI cards still use the latest raw calculated values."
            )
        st.caption(
            "Breadth measures participation across the Basic Materials universe. A rising EW-CW σ spread means internal "
            "rotation and stock-level dispersion are increasing beneath the cap-weighted index."
        )

        with st.expander("Show detailed dispersion tables"):
            bt = tables.get("breadth_table")
            if isinstance(bt, pd.DataFrame) and not bt.empty:
                st.markdown("**Breadth**")
                st.dataframe(bt, hide_index=True, width="stretch")
            dst = tables.get("dispersion_summary_table")
            if isinstance(dst, pd.DataFrame) and not dst.empty:
                st.markdown("**Dispersion summary**")
                st.dataframe(dst, hide_index=True, width="stretch")
            ct = tables.get("concentration_table")
            if isinstance(ct, pd.DataFrame) and not ct.empty:
                st.markdown("**Concentration**")
                st.dataframe(ct, hide_index=True, width="stretch")
            top_c = tables.get("top_contributors")
            if isinstance(top_c, pd.DataFrame) and not top_c.empty:
                st.markdown("**Top contributors**")
                st.dataframe(top_c.head(50), hide_index=True, width="stretch")
            bot_c = tables.get("bottom_contributors")
            if isinstance(bot_c, pd.DataFrame) and not bot_c.empty:
                st.markdown("**Bottom contributors**")
                st.dataframe(bot_c.head(50), hide_index=True, width="stretch")
            ind_p = tables.get("industry_participation")
            if isinstance(ind_p, pd.DataFrame) and not ind_p.empty:
                st.markdown("**Industry participation**")
                st.dataframe(ind_p.round(6), hide_index=True, width="stretch")


def render_communication_services_sector_tab() -> None:
    """XLC trend/technicals, proxy-basket rotation vs XLC, XLC vs SPY, risk, internal dispersion."""
    st.subheader("Communication Services Sector Analysis")

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        st.warning(
            "FMP_API_KEY is not set (check `.env`). Live XLC / SPY sections need a key; other tabs still work."
        )
        return

    try:
        session = data_loader.create_http_session()
    except Exception as e:
        st.warning(f"Could not create HTTP session: {e}")
        return

    st.subheader("Communication Services ETF Trend & Technicals")
    st.caption(
        "This section analyzes XLC on its own before comparing Communication Services against SPY."
    )

    trend_detail = pd.DataFrame()
    trend_summary: dict = {}
    try:
        trend_detail, trend_summary = sector_pages.get_sector_etf_trend_data(
            session, api_key, sector_etf="XLC"
        )
    except Exception as e:
        st.warning(f"Could not load XLC trend data: {e}")

    if trend_detail.empty:
        st.warning("XLC price history is missing or unavailable; trend & technicals section skipped.")
    else:
        as_of_t = trend_summary.get("as_of_date")
        st.markdown(f"**As of:** `{as_of_t}`")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("1W Return", _fmt_rel_pct(trend_summary.get("return_1w")))
        c2.metric("1M Return", _fmt_rel_pct(trend_summary.get("return_1m")))
        c3.metric("3M Return", _fmt_rel_pct(trend_summary.get("return_3m")))
        c4.metric("12M Skip-1M Return", _fmt_rel_pct(trend_summary.get("return_12m_skip_1m")))

        plot_df = trend_detail.dropna(subset=["dma_200"]).copy()
        if plot_df.empty:
            st.info("Not enough history yet to plot 200-DMA alongside price.")
        else:
            chart = plot_df[["date", "price", "dma_50", "dma_100", "dma_200"]].copy()
            chart["date"] = pd.to_datetime(chart["date"], errors="coerce")
            chart = chart.dropna(subset=["date"]).set_index("date")
            chart = chart.rename(
                columns={
                    "price": "XLC price",
                    "dma_50": "50 DMA",
                    "dma_100": "100 DMA",
                    "dma_200": "200 DMA",
                }
            )
            st.markdown("**XLC Price with Moving Averages**")
            st.line_chart(chart, height=460)

    st.divider()
    st.subheader("Communication Services Industry Rotation")
    st.caption(
        "Relative strength of equal-weight **proxy baskets** (stocks and ETFs listed per row) versus XLC. "
        "Positive values mean that basket is outperforming broad Communication Services over the selected window."
    )
    try:
        rot_bundle = _cached_comm_rotation(api_key)
    except Exception as e:
        rot_bundle = {"ok": False, "error": str(e)}
    if not rot_bundle.get("ok"):
        st.warning(
            rot_bundle.get("error")
            or "Communication Services industry rotation unavailable (check FMP key and caches)."
        )
    else:
        st.markdown(f"**As of:** `{rot_bundle.get('as_of')}`")
        hm = rot_bundle.get("heatmap")
        if isinstance(hm, pd.DataFrame) and not hm.empty:
            gmap_rot = hm.clip(lower=-30.0, upper=30.0)
            styled = (
                hm.style.background_gradient(
                    cmap="RdYlGn",
                    axis=None,
                    vmin=-30,
                    vmax=30,
                    gmap=gmap_rot,
                ).format("{:+.2f}%", na_rep="—")
            )
            st.dataframe(styled, use_container_width=True, hide_index=False)
            col_3m = pd.to_numeric(hm.get("3M RS %"), errors="coerce").dropna()
            if not col_3m.empty:
                mx = float(col_3m.max())
                if mx > 0:
                    top_lbl = str(col_3m.idxmax())
                    st.caption(
                        f"Leadership: **{top_lbl}** is leading Communication Services over the last 3 months."
                    )
                else:
                    st.caption("No proxy basket is outperforming XLC over the last 3 months.")
        else:
            st.info("No rotation heatmap to display.")

        with st.expander("Show detailed rotation tables"):
            met_r = rot_bundle.get("metrics")
            if isinstance(met_r, pd.DataFrame) and not met_r.empty:
                st.markdown("**Relative strength metrics**")
                st.dataframe(met_r.round(4), use_container_width=True, hide_index=True)
            px_r = rot_bundle.get("prices")
            if isinstance(px_r, pd.DataFrame) and not px_r.empty:
                st.markdown("**Latest prices (long format)**")
                st.dataframe(
                    px_r.sort_values(["date", "symbol"], ascending=[False, True]),
                    use_container_width=True,
                    hide_index=True,
                )
            rsh = rot_bundle.get("rs_ratio_history")
            if isinstance(rsh, pd.DataFrame) and not rsh.empty:
                st.markdown("**RS ratio vs XLC (level, not % change)**")
                st.dataframe(rsh.tail(500).round(6), use_container_width=True)

    st.divider()
    st.subheader("Communication Services vs SPY")
    st.caption(
        "XLC (Communication Services) vs SPY (S&P 500). Dividend-adjusted closes from FMP; cached like other price pulls."
    )

    detail = pd.DataFrame()
    summary: dict = {}
    try:
        detail, summary = sector_pages.get_sector_vs_spy_data(
            session, api_key, sector_etf="XLC", sector_name="Communication Services"
        )
    except Exception as e:
        st.warning(f"Could not load sector vs benchmark data: {e}")

    if detail.empty:
        st.warning(
            "Price data for XLC and/or SPY is missing or could not be aligned. "
            "Check your API key and try again after a successful `get_price_history` fetch."
        )
    else:
        as_of = summary.get("as_of_date")
        etf = str(summary.get("sector_etf", "XLC"))
        bench = str(summary.get("benchmark", "SPY"))
        col_idx_etf = f"{etf}_index"
        col_idx_bench = f"{bench}_index"

        st.markdown(f"**As of:** `{as_of}`")

        rr1w = summary.get("relative_return_1w")
        rr1 = summary.get("relative_return_1m")
        rr3 = summary.get("relative_return_3m")
        rr6 = summary.get("relative_return_6m")
        rr12s = summary.get("relative_return_12m_skip_1m")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("1W relative return vs SPY", _fmt_rel_pct(rr1w))
        c2.metric("1M relative return vs SPY", _fmt_rel_pct(rr1))
        c3.metric("3M relative return vs SPY", _fmt_rel_pct(rr3))
        c4.metric("6M relative return vs SPY", _fmt_rel_pct(rr6))
        c5.metric("12M skip-1M relative return vs SPY", _fmt_rel_pct(rr12s))

        st.caption(
            "Positive relative return means Communication Services is outperforming SPY over that window."
        )

        perf = detail[["date", col_idx_etf, col_idx_bench]].copy()
        perf["date"] = pd.to_datetime(perf["date"], errors="coerce")
        perf = perf.dropna(subset=["date"]).set_index("date")
        perf = perf.rename(
            columns={
                col_idx_etf: f"{etf} (normalized)",
                col_idx_bench: f"{bench} (normalized)",
            }
        )
        st.markdown(f"**{etf} vs SPY Cumulative Performance**")
        st.line_chart(perf, height=420)

        ratio = detail[["date", "relative_strength_ratio"]].copy()
        ratio["date"] = pd.to_datetime(ratio["date"], errors="coerce")
        ratio = ratio.dropna(subset=["date"]).set_index("date")
        ratio = ratio.rename(columns={"relative_strength_ratio": f"{etf} / {bench} ratio"})
        st.markdown(f"**{etf} / SPY Relative Strength Ratio**")
        st.line_chart(ratio, height=380)

    st.divider()
    st.subheader("Risk")
    st.caption("Realized volatility shows how unstable XLC has been historically.")

    risk_summary: dict = {}
    dd_ts = pd.DataFrame()
    try:
        risk_summary, _, dd_ts = sector_pages.get_sector_risk_data(session, api_key, sector_etf="XLC")
    except Exception as e:
        st.warning(f"Could not load XLC risk data: {e}")

    if dd_ts.empty:
        st.warning("XLC price history is missing; risk section skipped.")
    else:
        st.markdown(f"**As of:** `{risk_summary.get('as_of_date')}`")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trailing 1Y Vol", _fmt_vol_pct(risk_summary.get("trailing_1y_vol")))
        m2.metric("Trailing 3M Vol", _fmt_vol_pct(risk_summary.get("trailing_3m_vol")))
        m3.metric("Trailing 20D Vol", _fmt_vol_pct(risk_summary.get("trailing_20d_vol")))
        m4.metric("Trailing 1Y Max Drawdown", _fmt_rel_pct(risk_summary.get("trailing_1y_max_drawdown")))
        m5.metric("Current Drawdown vs 1Y High", _fmt_rel_pct(risk_summary.get("current_drawdown_1y_high")))

        st.markdown("**XLC Drawdown Over Time**")
        if dd_ts.empty:
            st.info("No drawdown series to plot.")
        else:
            dd_plot = dd_ts.copy()
            dd_plot["date"] = pd.to_datetime(dd_plot["date"], errors="coerce")
            dd_plot = dd_plot.dropna(subset=["date"]).set_index("date")
            st.line_chart(dd_plot[["drawdown"]], height=360)

    st.divider()
    st.subheader("Communication Services Internal Dispersion")
    st.caption(
        "Breadth, cross-sectional dispersion, and concentration for the US Communication Services **dispersion** "
        f"universe (market cap > ${config.DISPERSION_MIN_MARKET_CAP/1e9:.1f}B, avg volume > "
        f"{config.DISPERSION_MIN_AVG_VOLUME/1e3:.0f}k, price > ${config.DISPERSION_MIN_PRICE:.0f}; "
        "ETFs/funds excluded). Prices are dividend-adjusted from `data_loader.get_price_history`."
    )
    try:
        disp_bundle = _cached_sector_dispersion(api_key, "Communication Services")
    except Exception as e:
        disp_bundle = {"ok": False, "error": str(e)}
    if not disp_bundle.get("ok"):
        st.warning(
            disp_bundle.get("error")
            or "Dispersion analytics unavailable (check FMP key and profile-bulk access)."
        )
    else:
        summ = disp_bundle.get("summary") or {}
        tables = disp_bundle.get("tables") or {}
        as_of = disp_bundle.get("as_of")
        st.markdown(
            f"**As of:** `{as_of}` — names below are the intersection of the dispersion universe and "
            "symbols with sufficient price history for DMAs and 1M returns."
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Universe size", f"{summ.get('universe_size', 0):,}" if summ.get("universe_size") is not None else "—")
        m2.metric("% Above 50 DMA", _fmt_rel_pct(summ.get("pct_above_50dma")))
        m3.metric("% Above 200 DMA", _fmt_rel_pct(summ.get("pct_above_200dma")))
        m4.metric("Equal-weight 1M σ", _fmt_rel_pct(summ.get("equal_weight_std")))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Cap-weight 1M σ", _fmt_rel_pct(summ.get("cap_weight_std")))
        avpc = summ.get("avg_pairwise_corr")
        m6.metric(
            "Avg pairwise corr (60d)",
            f"{float(avpc):.3f}" if avpc is not None and avpc == avpc else "—",
        )
        m7.metric("Top 5 cap weight", _fmt_rel_pct(summ.get("top5_weight")))
        ew_s = summ.get("equal_weight_std")
        cw_s = summ.get("cap_weight_std")
        if ew_s is not None and cw_s is not None and ew_s == ew_s and cw_s == cw_s:
            ew_cw_spread = float(ew_s) - float(cw_s)
        else:
            ew_cw_spread = None
        m8.metric("EW - CW σ Spread", _fmt_rel_pct(ew_cw_spread))

        health = _dispersion_health_chart(disp_bundle)
        plot_cols = ["Breadth 50 DMA %", "Breadth 200 DMA %", "EW - CW σ Spread %"]
        st.markdown("**Communication Services Breadth & Internal Rotation**")
        if health.empty or not any(c in health.columns for c in plot_cols):
            st.info("Not enough overlapping breadth / dispersion history to plot the combined health series yet.")
        else:
            chart_df = health[[c for c in plot_cols if c in health.columns]].copy()
            st.line_chart(chart_df, height=420)
            st.caption(
                "DMA breadth lines are forward-filled for chart continuity when later dates have missing "
                "200-DMA coverage. KPI cards still use the latest raw calculated values."
            )
        st.caption(
            "Breadth measures participation across the Communication Services universe. A rising EW-CW σ spread "
            "means internal rotation and stock-level dispersion are increasing beneath the cap-weighted index."
        )

        with st.expander("Show detailed dispersion tables"):
            bt = tables.get("breadth_table")
            if isinstance(bt, pd.DataFrame) and not bt.empty:
                st.markdown("**Breadth**")
                st.dataframe(bt, hide_index=True, width="stretch")
            dst = tables.get("dispersion_summary_table")
            if isinstance(dst, pd.DataFrame) and not dst.empty:
                st.markdown("**Dispersion summary**")
                st.dataframe(dst, hide_index=True, width="stretch")
            ct = tables.get("concentration_table")
            if isinstance(ct, pd.DataFrame) and not ct.empty:
                st.markdown("**Concentration**")
                st.dataframe(ct, hide_index=True, width="stretch")
            top_c = tables.get("top_contributors")
            if isinstance(top_c, pd.DataFrame) and not top_c.empty:
                st.markdown("**Top contributors**")
                st.dataframe(top_c.head(50), hide_index=True, width="stretch")
            bot_c = tables.get("bottom_contributors")
            if isinstance(bot_c, pd.DataFrame) and not bot_c.empty:
                st.markdown("**Bottom contributors**")
                st.dataframe(bot_c.head(50), hide_index=True, width="stretch")
            ind_p = tables.get("industry_participation")
            if isinstance(ind_p, pd.DataFrame) and not ind_p.empty:
                st.markdown("**Industry participation**")
                st.dataframe(ind_p.round(6), hide_index=True, width="stretch")


def render_consumer_cyclical_sector_tab() -> None:
    """XLY trend/technicals, proxy baskets vs XLY, XLY vs SPY, risk, internal dispersion."""
    st.subheader("Consumer Cyclical Sector Analysis")

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        st.warning(
            "FMP_API_KEY is not set (check `.env`). Live XLY / SPY sections need a key; other tabs still work."
        )
        return

    try:
        session = data_loader.create_http_session()
    except Exception as e:
        st.warning(f"Could not create HTTP session: {e}")
        return

    st.subheader("Consumer Cyclical ETF Trend & Technicals")
    st.caption(
        "This section analyzes XLY on its own before comparing Consumer Cyclical against SPY."
    )

    trend_detail = pd.DataFrame()
    trend_summary: dict = {}
    try:
        trend_detail, trend_summary = sector_pages.get_sector_etf_trend_data(
            session, api_key, sector_etf="XLY"
        )
    except Exception as e:
        st.warning(f"Could not load XLY trend data: {e}")

    if trend_detail.empty:
        st.warning("XLY price history is missing or unavailable; trend & technicals section skipped.")
    else:
        as_of_t = trend_summary.get("as_of_date")
        st.markdown(f"**As of:** `{as_of_t}`")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("1W Return", _fmt_rel_pct(trend_summary.get("return_1w")))
        c2.metric("1M Return", _fmt_rel_pct(trend_summary.get("return_1m")))
        c3.metric("3M Return", _fmt_rel_pct(trend_summary.get("return_3m")))
        c4.metric("12M Skip-1M Return", _fmt_rel_pct(trend_summary.get("return_12m_skip_1m")))

        plot_df = trend_detail.dropna(subset=["dma_200"]).copy()
        if plot_df.empty:
            st.info("Not enough history yet to plot 200-DMA alongside price.")
        else:
            chart = plot_df[["date", "price", "dma_50", "dma_100", "dma_200"]].copy()
            chart["date"] = pd.to_datetime(chart["date"], errors="coerce")
            chart = chart.dropna(subset=["date"]).set_index("date")
            chart = chart.rename(
                columns={
                    "price": "XLY price",
                    "dma_50": "50 DMA",
                    "dma_100": "100 DMA",
                    "dma_200": "200 DMA",
                }
            )
            st.markdown("**XLY Price with Moving Averages**")
            st.line_chart(chart, height=460)

    st.divider()
    st.subheader("Consumer Cyclical Industry Rotation")
    st.caption(
        "Relative strength of equal-weight **proxy baskets** (stocks and ETFs listed per row) versus XLY. "
        "Positive values mean that basket is outperforming broad Consumer Cyclical over the selected window."
    )
    try:
        rot_bundle = _cached_consumer_cyclical_rotation(api_key)
    except Exception as e:
        rot_bundle = {"ok": False, "error": str(e)}
    if not rot_bundle.get("ok"):
        st.warning(
            rot_bundle.get("error")
            or "Consumer Cyclical industry rotation unavailable (check FMP key and caches)."
        )
    else:
        st.markdown(f"**As of:** `{rot_bundle.get('as_of')}`")
        hm = rot_bundle.get("heatmap")
        if isinstance(hm, pd.DataFrame) and not hm.empty:
            gmap_rot = hm.clip(lower=-30.0, upper=30.0)
            styled = (
                hm.style.background_gradient(
                    cmap="RdYlGn",
                    axis=None,
                    vmin=-30,
                    vmax=30,
                    gmap=gmap_rot,
                ).format("{:+.2f}%", na_rep="—")
            )
            st.dataframe(styled, use_container_width=True, hide_index=False)
            col_3m = pd.to_numeric(hm.get("3M RS %"), errors="coerce").dropna()
            if not col_3m.empty:
                mx = float(col_3m.max())
                if mx > 0:
                    top_lbl = str(col_3m.idxmax())
                    st.caption(
                        f"Leadership: **{top_lbl}** is leading Consumer Cyclical over the last 3 months."
                    )
                else:
                    st.caption("No proxy basket is outperforming XLY over the last 3 months.")
        else:
            st.info("No rotation heatmap to display.")

        with st.expander("Show detailed rotation tables"):
            met_r = rot_bundle.get("metrics")
            if isinstance(met_r, pd.DataFrame) and not met_r.empty:
                st.markdown("**Relative strength metrics**")
                st.dataframe(met_r.round(4), use_container_width=True, hide_index=True)
            px_r = rot_bundle.get("prices")
            if isinstance(px_r, pd.DataFrame) and not px_r.empty:
                st.markdown("**Latest prices (long format)**")
                st.dataframe(
                    px_r.sort_values(["date", "symbol"], ascending=[False, True]),
                    use_container_width=True,
                    hide_index=True,
                )
            rsh = rot_bundle.get("rs_ratio_history")
            if isinstance(rsh, pd.DataFrame) and not rsh.empty:
                st.markdown("**RS ratio vs XLY (level, not % change)**")
                st.dataframe(rsh.tail(500).round(6), use_container_width=True)

    st.divider()
    st.subheader("Consumer Cyclical vs SPY")
    st.caption(
        "XLY (Consumer Cyclical) vs SPY (S&P 500). Dividend-adjusted closes from FMP; cached like other price pulls."
    )

    detail = pd.DataFrame()
    summary: dict = {}
    try:
        detail, summary = sector_pages.get_sector_vs_spy_data(
            session, api_key, sector_etf="XLY", sector_name="Consumer Cyclical"
        )
    except Exception as e:
        st.warning(f"Could not load sector vs benchmark data: {e}")

    if detail.empty:
        st.warning(
            "Price data for XLY and/or SPY is missing or could not be aligned. "
            "Check your API key and try again after a successful `get_price_history` fetch."
        )
    else:
        as_of = summary.get("as_of_date")
        etf = str(summary.get("sector_etf", "XLY"))
        bench = str(summary.get("benchmark", "SPY"))
        col_idx_etf = f"{etf}_index"
        col_idx_bench = f"{bench}_index"

        st.markdown(f"**As of:** `{as_of}`")

        rr1w = summary.get("relative_return_1w")
        rr1 = summary.get("relative_return_1m")
        rr3 = summary.get("relative_return_3m")
        rr6 = summary.get("relative_return_6m")
        rr12s = summary.get("relative_return_12m_skip_1m")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("1W relative return vs SPY", _fmt_rel_pct(rr1w))
        c2.metric("1M relative return vs SPY", _fmt_rel_pct(rr1))
        c3.metric("3M relative return vs SPY", _fmt_rel_pct(rr3))
        c4.metric("6M relative return vs SPY", _fmt_rel_pct(rr6))
        c5.metric("12M skip-1M relative return vs SPY", _fmt_rel_pct(rr12s))

        st.caption(
            "Positive relative return means Consumer Cyclical is outperforming SPY over that window."
        )

        perf = detail[["date", col_idx_etf, col_idx_bench]].copy()
        perf["date"] = pd.to_datetime(perf["date"], errors="coerce")
        perf = perf.dropna(subset=["date"]).set_index("date")
        perf = perf.rename(
            columns={
                col_idx_etf: f"{etf} (normalized)",
                col_idx_bench: f"{bench} (normalized)",
            }
        )
        st.markdown(f"**{etf} vs SPY Cumulative Performance**")
        st.line_chart(perf, height=420)

        ratio = detail[["date", "relative_strength_ratio"]].copy()
        ratio["date"] = pd.to_datetime(ratio["date"], errors="coerce")
        ratio = ratio.dropna(subset=["date"]).set_index("date")
        ratio = ratio.rename(columns={"relative_strength_ratio": f"{etf} / {bench} ratio"})
        st.markdown(f"**{etf} / SPY Relative Strength Ratio**")
        st.line_chart(ratio, height=380)

    st.divider()
    st.subheader("Risk")
    st.caption("Realized volatility shows how unstable XLY has been historically.")

    risk_summary: dict = {}
    dd_ts = pd.DataFrame()
    try:
        risk_summary, _, dd_ts = sector_pages.get_sector_risk_data(session, api_key, sector_etf="XLY")
    except Exception as e:
        st.warning(f"Could not load XLY risk data: {e}")

    if dd_ts.empty:
        st.warning("XLY price history is missing; risk section skipped.")
    else:
        st.markdown(f"**As of:** `{risk_summary.get('as_of_date')}`")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trailing 1Y Vol", _fmt_vol_pct(risk_summary.get("trailing_1y_vol")))
        m2.metric("Trailing 3M Vol", _fmt_vol_pct(risk_summary.get("trailing_3m_vol")))
        m3.metric("Trailing 20D Vol", _fmt_vol_pct(risk_summary.get("trailing_20d_vol")))
        m4.metric("Trailing 1Y Max Drawdown", _fmt_rel_pct(risk_summary.get("trailing_1y_max_drawdown")))
        m5.metric("Current Drawdown vs 1Y High", _fmt_rel_pct(risk_summary.get("current_drawdown_1y_high")))

        st.markdown("**XLY Drawdown Over Time**")
        if dd_ts.empty:
            st.info("No drawdown series to plot.")
        else:
            dd_plot = dd_ts.copy()
            dd_plot["date"] = pd.to_datetime(dd_plot["date"], errors="coerce")
            dd_plot = dd_plot.dropna(subset=["date"]).set_index("date")
            st.line_chart(dd_plot[["drawdown"]], height=360)

    st.divider()
    st.subheader("Consumer Cyclical Internal Dispersion")
    st.caption(
        "Breadth, cross-sectional dispersion, and concentration for the US Consumer Cyclical **dispersion** "
        f"universe (market cap > ${config.DISPERSION_MIN_MARKET_CAP/1e9:.1f}B, avg volume > "
        f"{config.DISPERSION_MIN_AVG_VOLUME/1e3:.0f}k, price > ${config.DISPERSION_MIN_PRICE:.0f}; "
        "ETFs/funds excluded). Prices are dividend-adjusted from `data_loader.get_price_history`."
    )
    try:
        disp_bundle = _cached_sector_dispersion(api_key, "Consumer Cyclical")
    except Exception as e:
        disp_bundle = {"ok": False, "error": str(e)}
    if not disp_bundle.get("ok"):
        st.warning(
            disp_bundle.get("error")
            or "Dispersion analytics unavailable (check FMP key and profile-bulk access)."
        )
    else:
        summ = disp_bundle.get("summary") or {}
        tables = disp_bundle.get("tables") or {}
        as_of = disp_bundle.get("as_of")
        st.markdown(
            f"**As of:** `{as_of}` — names below are the intersection of the dispersion universe and "
            "symbols with sufficient price history for DMAs and 1M returns."
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Universe size", f"{summ.get('universe_size', 0):,}" if summ.get("universe_size") is not None else "—")
        m2.metric("% Above 50 DMA", _fmt_rel_pct(summ.get("pct_above_50dma")))
        m3.metric("% Above 200 DMA", _fmt_rel_pct(summ.get("pct_above_200dma")))
        m4.metric("Equal-weight 1M σ", _fmt_rel_pct(summ.get("equal_weight_std")))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Cap-weight 1M σ", _fmt_rel_pct(summ.get("cap_weight_std")))
        avpc = summ.get("avg_pairwise_corr")
        m6.metric(
            "Avg pairwise corr (60d)",
            f"{float(avpc):.3f}" if avpc is not None and avpc == avpc else "—",
        )
        m7.metric("Top 5 cap weight", _fmt_rel_pct(summ.get("top5_weight")))
        ew_s = summ.get("equal_weight_std")
        cw_s = summ.get("cap_weight_std")
        if ew_s is not None and cw_s is not None and ew_s == ew_s and cw_s == cw_s:
            ew_cw_spread = float(ew_s) - float(cw_s)
        else:
            ew_cw_spread = None
        m8.metric("EW - CW σ Spread", _fmt_rel_pct(ew_cw_spread))

        health = _dispersion_health_chart(disp_bundle)
        plot_cols = ["Breadth 50 DMA %", "Breadth 200 DMA %", "EW - CW σ Spread %"]
        st.markdown("**Consumer Cyclical Breadth & Internal Rotation**")
        if health.empty or not any(c in health.columns for c in plot_cols):
            st.info("Not enough overlapping breadth / dispersion history to plot the combined health series yet.")
        else:
            chart_df = health[[c for c in plot_cols if c in health.columns]].copy()
            st.line_chart(chart_df, height=420)
            st.caption(
                "DMA breadth lines are forward-filled for chart continuity when later dates have missing "
                "200-DMA coverage. KPI cards still use the latest raw calculated values."
            )
        st.caption(
            "Breadth measures participation across the Consumer Cyclical universe. A rising EW-CW σ spread "
            "means internal rotation and stock-level dispersion are increasing beneath the cap-weighted index."
        )

        with st.expander("Show detailed dispersion tables"):
            bt = tables.get("breadth_table")
            if isinstance(bt, pd.DataFrame) and not bt.empty:
                st.markdown("**Breadth**")
                st.dataframe(bt, hide_index=True, width="stretch")
            dst = tables.get("dispersion_summary_table")
            if isinstance(dst, pd.DataFrame) and not dst.empty:
                st.markdown("**Dispersion summary**")
                st.dataframe(dst, hide_index=True, width="stretch")
            ct = tables.get("concentration_table")
            if isinstance(ct, pd.DataFrame) and not ct.empty:
                st.markdown("**Concentration**")
                st.dataframe(ct, hide_index=True, width="stretch")
            top_c = tables.get("top_contributors")
            if isinstance(top_c, pd.DataFrame) and not top_c.empty:
                st.markdown("**Top contributors**")
                st.dataframe(top_c.head(50), hide_index=True, width="stretch")
            bot_c = tables.get("bottom_contributors")
            if isinstance(bot_c, pd.DataFrame) and not bot_c.empty:
                st.markdown("**Bottom contributors**")
                st.dataframe(bot_c.head(50), hide_index=True, width="stretch")
            ind_p = tables.get("industry_participation")
            if isinstance(ind_p, pd.DataFrame) and not ind_p.empty:
                st.markdown("**Industry participation**")
                st.dataframe(ind_p.round(6), hide_index=True, width="stretch")


def render_consumer_defensive_sector_tab() -> None:
    """XLP trend/technicals, proxy baskets vs XLP, sector vs SPY, risk, internal dispersion."""
    st.subheader("Consumer Defensive Sector Analysis")

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        st.warning(
            "FMP_API_KEY is not set (check `.env`). Live XLP / SPY sections need a key; other tabs still work."
        )
        return

    try:
        session = data_loader.create_http_session()
    except Exception as e:
        st.warning(f"Could not create HTTP session: {e}")
        return

    st.subheader("Consumer Defensive ETF Trend & Technicals")
    st.caption(
        "This section analyzes XLP on its own before comparing Consumer Defensive against SPY."
    )

    trend_detail = pd.DataFrame()
    trend_summary: dict = {}
    try:
        trend_detail, trend_summary = sector_pages.get_sector_etf_trend_data(
            session, api_key, sector_etf="XLP"
        )
    except Exception as e:
        st.warning(f"Could not load XLP trend data: {e}")

    if trend_detail.empty:
        st.warning("XLP price history is missing or unavailable; trend & technicals section skipped.")
    else:
        as_of_t = trend_summary.get("as_of_date")
        st.markdown(f"**As of:** `{as_of_t}`")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("1W Return", _fmt_rel_pct(trend_summary.get("return_1w")))
        c2.metric("1M Return", _fmt_rel_pct(trend_summary.get("return_1m")))
        c3.metric("3M Return", _fmt_rel_pct(trend_summary.get("return_3m")))
        c4.metric("12M Skip-1M Return", _fmt_rel_pct(trend_summary.get("return_12m_skip_1m")))

        plot_df = trend_detail.dropna(subset=["dma_200"]).copy()
        if plot_df.empty:
            st.info("Not enough history yet to plot 200-DMA alongside price.")
        else:
            chart = plot_df[["date", "price", "dma_50", "dma_100", "dma_200"]].copy()
            chart["date"] = pd.to_datetime(chart["date"], errors="coerce")
            chart = chart.dropna(subset=["date"]).set_index("date")
            chart = chart.rename(
                columns={
                    "price": "XLP price",
                    "dma_50": "50 DMA",
                    "dma_100": "100 DMA",
                    "dma_200": "200 DMA",
                }
            )
            st.markdown("**XLP Price with Moving Averages**")
            st.line_chart(chart, height=460)

    st.divider()
    st.subheader("Consumer Defensive Industry Rotation")
    st.caption(
        "Relative strength of equal-weight **proxy baskets** (stocks and ETFs listed per row) versus XLP. "
        "Positive values mean that basket is outperforming broad Consumer Defensive over the selected window."
    )
    try:
        rot_bundle = _cached_consumer_defensive_rotation(api_key)
    except Exception as e:
        rot_bundle = {"ok": False, "error": str(e)}
    if not rot_bundle.get("ok"):
        st.warning(
            rot_bundle.get("error")
            or "Consumer Defensive industry rotation unavailable (check FMP key and caches)."
        )
    else:
        st.markdown(f"**As of:** `{rot_bundle.get('as_of')}`")
        hm = rot_bundle.get("heatmap")
        if isinstance(hm, pd.DataFrame) and not hm.empty:
            gmap_rot = hm.clip(lower=-30.0, upper=30.0)
            styled = (
                hm.style.background_gradient(
                    cmap="RdYlGn",
                    axis=None,
                    vmin=-30,
                    vmax=30,
                    gmap=gmap_rot,
                ).format("{:+.2f}%", na_rep="—")
            )
            st.dataframe(styled, use_container_width=True, hide_index=False)
            col_3m = pd.to_numeric(hm.get("3M RS %"), errors="coerce").dropna()
            if not col_3m.empty:
                mx = float(col_3m.max())
                if mx > 0:
                    top_lbl = str(col_3m.idxmax())
                    st.caption(
                        f"Leadership: **{top_lbl}** is leading Consumer Defensive over the last 3 months."
                    )
                else:
                    st.caption("No proxy basket is outperforming XLP over the last 3 months.")
        else:
            st.info("No rotation heatmap to display.")

        with st.expander("Show detailed rotation tables"):
            met_r = rot_bundle.get("metrics")
            if isinstance(met_r, pd.DataFrame) and not met_r.empty:
                st.markdown("**Relative strength metrics**")
                st.dataframe(met_r.round(4), use_container_width=True, hide_index=True)
            px_r = rot_bundle.get("prices")
            if isinstance(px_r, pd.DataFrame) and not px_r.empty:
                st.markdown("**Latest prices (long format)**")
                st.dataframe(
                    px_r.sort_values(["date", "symbol"], ascending=[False, True]),
                    use_container_width=True,
                    hide_index=True,
                )
            rsh = rot_bundle.get("rs_ratio_history")
            if isinstance(rsh, pd.DataFrame) and not rsh.empty:
                st.markdown("**RS ratio vs XLP (level, not % change)**")
                st.dataframe(rsh.tail(500).round(6), use_container_width=True)

    st.divider()
    st.subheader("Consumer Defensive vs SPY")
    st.caption(
        "XLP (Consumer Defensive) vs SPY (S&P 500). Dividend-adjusted closes from FMP; cached like other price pulls."
    )

    detail = pd.DataFrame()
    summary: dict = {}
    try:
        detail, summary = sector_pages.get_sector_vs_spy_data(
            session, api_key, sector_etf="XLP", sector_name="Consumer Defensive"
        )
    except Exception as e:
        st.warning(f"Could not load sector vs benchmark data: {e}")

    if detail.empty:
        st.warning(
            "Price data for XLP and/or SPY is missing or could not be aligned. "
            "Check your API key and try again after a successful `get_price_history` fetch."
        )
    else:
        as_of = summary.get("as_of_date")
        etf = str(summary.get("sector_etf", "XLP"))
        bench = str(summary.get("benchmark", "SPY"))
        col_idx_etf = f"{etf}_index"
        col_idx_bench = f"{bench}_index"

        st.markdown(f"**As of:** `{as_of}`")

        rr1w = summary.get("relative_return_1w")
        rr1 = summary.get("relative_return_1m")
        rr3 = summary.get("relative_return_3m")
        rr6 = summary.get("relative_return_6m")
        rr12s = summary.get("relative_return_12m_skip_1m")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("1W relative return vs SPY", _fmt_rel_pct(rr1w))
        c2.metric("1M relative return vs SPY", _fmt_rel_pct(rr1))
        c3.metric("3M relative return vs SPY", _fmt_rel_pct(rr3))
        c4.metric("6M relative return vs SPY", _fmt_rel_pct(rr6))
        c5.metric("12M skip-1M relative return vs SPY", _fmt_rel_pct(rr12s))

        st.caption(
            "Positive relative return means Consumer Defensive is outperforming SPY over that window."
        )

        perf = detail[["date", col_idx_etf, col_idx_bench]].copy()
        perf["date"] = pd.to_datetime(perf["date"], errors="coerce")
        perf = perf.dropna(subset=["date"]).set_index("date")
        perf = perf.rename(
            columns={
                col_idx_etf: f"{etf} (normalized)",
                col_idx_bench: f"{bench} (normalized)",
            }
        )
        st.markdown(f"**{etf} vs SPY Cumulative Performance**")
        st.line_chart(perf, height=420)

        ratio = detail[["date", "relative_strength_ratio"]].copy()
        ratio["date"] = pd.to_datetime(ratio["date"], errors="coerce")
        ratio = ratio.dropna(subset=["date"]).set_index("date")
        ratio = ratio.rename(columns={"relative_strength_ratio": f"{etf} / {bench} ratio"})
        st.markdown(f"**{etf} / SPY Relative Strength Ratio**")
        st.line_chart(ratio, height=380)

    st.divider()
    st.subheader("Risk")
    st.caption("Realized volatility shows how unstable XLP has been historically.")

    risk_summary: dict = {}
    dd_ts = pd.DataFrame()
    try:
        risk_summary, _, dd_ts = sector_pages.get_sector_risk_data(session, api_key, sector_etf="XLP")
    except Exception as e:
        st.warning(f"Could not load XLP risk data: {e}")

    if dd_ts.empty:
        st.warning("XLP price history is missing; risk section skipped.")
    else:
        st.markdown(f"**As of:** `{risk_summary.get('as_of_date')}`")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trailing 1Y Vol", _fmt_vol_pct(risk_summary.get("trailing_1y_vol")))
        m2.metric("Trailing 3M Vol", _fmt_vol_pct(risk_summary.get("trailing_3m_vol")))
        m3.metric("Trailing 20D Vol", _fmt_vol_pct(risk_summary.get("trailing_20d_vol")))
        m4.metric("Trailing 1Y Max Drawdown", _fmt_rel_pct(risk_summary.get("trailing_1y_max_drawdown")))
        m5.metric("Current Drawdown vs 1Y High", _fmt_rel_pct(risk_summary.get("current_drawdown_1y_high")))

        st.markdown("**XLP Drawdown Over Time**")
        if dd_ts.empty:
            st.info("No drawdown series to plot.")
        else:
            dd_plot = dd_ts.copy()
            dd_plot["date"] = pd.to_datetime(dd_plot["date"], errors="coerce")
            dd_plot = dd_plot.dropna(subset=["date"]).set_index("date")
            st.line_chart(dd_plot[["drawdown"]], height=360)

    st.divider()
    st.subheader("Consumer Defensive Internal Dispersion")
    st.caption(
        "Breadth, cross-sectional dispersion, and concentration for the US Consumer Defensive **dispersion** "
        f"universe (market cap > ${config.DISPERSION_MIN_MARKET_CAP/1e9:.1f}B, avg volume > "
        f"{config.DISPERSION_MIN_AVG_VOLUME/1e3:.0f}k, price > ${config.DISPERSION_MIN_PRICE:.0f}; "
        "ETFs/funds excluded). Prices are dividend-adjusted from `data_loader.get_price_history`."
    )
    try:
        disp_bundle = _cached_sector_dispersion(api_key, "Consumer Defensive")
    except Exception as e:
        disp_bundle = {"ok": False, "error": str(e)}
    if not disp_bundle.get("ok"):
        st.warning(
            disp_bundle.get("error")
            or "Dispersion analytics unavailable (check FMP key and profile-bulk access)."
        )
    else:
        summ = disp_bundle.get("summary") or {}
        tables = disp_bundle.get("tables") or {}
        as_of = disp_bundle.get("as_of")
        st.markdown(
            f"**As of:** `{as_of}` — names below are the intersection of the dispersion universe and "
            "symbols with sufficient price history for DMAs and 1M returns."
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Universe size", f"{summ.get('universe_size', 0):,}" if summ.get("universe_size") is not None else "—")
        m2.metric("% Above 50 DMA", _fmt_rel_pct(summ.get("pct_above_50dma")))
        m3.metric("% Above 200 DMA", _fmt_rel_pct(summ.get("pct_above_200dma")))
        m4.metric("Equal-weight 1M σ", _fmt_rel_pct(summ.get("equal_weight_std")))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Cap-weight 1M σ", _fmt_rel_pct(summ.get("cap_weight_std")))
        avpc = summ.get("avg_pairwise_corr")
        m6.metric(
            "Avg pairwise corr (60d)",
            f"{float(avpc):.3f}" if avpc is not None and avpc == avpc else "—",
        )
        m7.metric("Top 5 cap weight", _fmt_rel_pct(summ.get("top5_weight")))
        ew_s = summ.get("equal_weight_std")
        cw_s = summ.get("cap_weight_std")
        if ew_s is not None and cw_s is not None and ew_s == ew_s and cw_s == cw_s:
            ew_cw_spread = float(ew_s) - float(cw_s)
        else:
            ew_cw_spread = None
        m8.metric("EW - CW σ Spread", _fmt_rel_pct(ew_cw_spread))

        health = _dispersion_health_chart(disp_bundle)
        plot_cols = ["Breadth 50 DMA %", "Breadth 200 DMA %", "EW - CW σ Spread %"]
        st.markdown("**Consumer Defensive Breadth & Internal Rotation**")
        if health.empty or not any(c in health.columns for c in plot_cols):
            st.info("Not enough overlapping breadth / dispersion history to plot the combined health series yet.")
        else:
            chart_df = health[[c for c in plot_cols if c in health.columns]].copy()
            st.line_chart(chart_df, height=420)
            st.caption(
                "DMA breadth lines are forward-filled for chart continuity when later dates have missing "
                "200-DMA coverage. KPI cards still use the latest raw calculated values."
            )
        st.caption(
            "Breadth measures participation across the Consumer Defensive universe. A rising EW-CW σ spread "
            "means internal rotation and stock-level dispersion are increasing beneath the cap-weighted index."
        )

        with st.expander("Show detailed dispersion tables"):
            bt = tables.get("breadth_table")
            if isinstance(bt, pd.DataFrame) and not bt.empty:
                st.markdown("**Breadth**")
                st.dataframe(bt, hide_index=True, width="stretch")
            dst = tables.get("dispersion_summary_table")
            if isinstance(dst, pd.DataFrame) and not dst.empty:
                st.markdown("**Dispersion summary**")
                st.dataframe(dst, hide_index=True, width="stretch")
            ct = tables.get("concentration_table")
            if isinstance(ct, pd.DataFrame) and not ct.empty:
                st.markdown("**Concentration**")
                st.dataframe(ct, hide_index=True, width="stretch")
            top_c = tables.get("top_contributors")
            if isinstance(top_c, pd.DataFrame) and not top_c.empty:
                st.markdown("**Top contributors**")
                st.dataframe(top_c.head(50), hide_index=True, width="stretch")
            bot_c = tables.get("bottom_contributors")
            if isinstance(bot_c, pd.DataFrame) and not bot_c.empty:
                st.markdown("**Bottom contributors**")
                st.dataframe(bot_c.head(50), hide_index=True, width="stretch")
            ind_p = tables.get("industry_participation")
            if isinstance(ind_p, pd.DataFrame) and not ind_p.empty:
                st.markdown("**Industry participation**")
                st.dataframe(ind_p.round(6), hide_index=True, width="stretch")


def render_energy_sector_tab() -> None:
    """XLE trend/technicals, proxy baskets vs XLE, sector vs SPY, risk, internal dispersion."""
    st.subheader("Energy Sector Analysis")

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        st.warning(
            "FMP_API_KEY is not set (check `.env`). Live XLE / SPY sections need a key; other tabs still work."
        )
        return

    try:
        session = data_loader.create_http_session()
    except Exception as e:
        st.warning(f"Could not create HTTP session: {e}")
        return

    st.subheader("Energy ETF Trend & Technicals")
    st.caption(
        "This section analyzes XLE on its own before comparing Energy against SPY."
    )

    trend_detail = pd.DataFrame()
    trend_summary: dict = {}
    try:
        trend_detail, trend_summary = sector_pages.get_sector_etf_trend_data(
            session, api_key, sector_etf="XLE"
        )
    except Exception as e:
        st.warning(f"Could not load XLE trend data: {e}")

    if trend_detail.empty:
        st.warning("XLE price history is missing or unavailable; trend & technicals section skipped.")
    else:
        as_of_t = trend_summary.get("as_of_date")
        st.markdown(f"**As of:** `{as_of_t}`")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("1W Return", _fmt_rel_pct(trend_summary.get("return_1w")))
        c2.metric("1M Return", _fmt_rel_pct(trend_summary.get("return_1m")))
        c3.metric("3M Return", _fmt_rel_pct(trend_summary.get("return_3m")))
        c4.metric("12M Skip-1M Return", _fmt_rel_pct(trend_summary.get("return_12m_skip_1m")))

        plot_df = trend_detail.dropna(subset=["dma_200"]).copy()
        if plot_df.empty:
            st.info("Not enough history yet to plot 200-DMA alongside price.")
        else:
            chart = plot_df[["date", "price", "dma_50", "dma_100", "dma_200"]].copy()
            chart["date"] = pd.to_datetime(chart["date"], errors="coerce")
            chart = chart.dropna(subset=["date"]).set_index("date")
            chart = chart.rename(
                columns={
                    "price": "XLE price",
                    "dma_50": "50 DMA",
                    "dma_100": "100 DMA",
                    "dma_200": "200 DMA",
                }
            )
            st.markdown("**XLE Price with Moving Averages**")
            st.line_chart(chart, height=460)

    st.divider()
    st.subheader("Energy Industry Rotation")
    st.caption(
        "Relative strength of equal-weight **proxy baskets** (stocks and ETFs listed per row) versus XLE. "
        "Positive values mean that basket is outperforming broad Energy over the selected window."
    )
    try:
        rot_bundle = _cached_energy_rotation(api_key)
    except Exception as e:
        rot_bundle = {"ok": False, "error": str(e)}
    if not rot_bundle.get("ok"):
        st.warning(
            rot_bundle.get("error")
            or "Energy industry rotation unavailable (check FMP key and caches)."
        )
    else:
        st.markdown(f"**As of:** `{rot_bundle.get('as_of')}`")
        hm = rot_bundle.get("heatmap")
        if isinstance(hm, pd.DataFrame) and not hm.empty:
            gmap_rot = hm.clip(lower=-30.0, upper=30.0)
            styled = (
                hm.style.background_gradient(
                    cmap="RdYlGn",
                    axis=None,
                    vmin=-30,
                    vmax=30,
                    gmap=gmap_rot,
                ).format("{:+.2f}%", na_rep="—")
            )
            st.dataframe(styled, use_container_width=True, hide_index=False)
            col_3m = pd.to_numeric(hm.get("3M RS %"), errors="coerce").dropna()
            if not col_3m.empty:
                mx = float(col_3m.max())
                if mx > 0:
                    top_lbl = str(col_3m.idxmax())
                    st.caption(
                        f"Leadership: **{top_lbl}** is leading Energy over the last 3 months."
                    )
                else:
                    st.caption("No proxy basket is outperforming XLE over the last 3 months.")
        else:
            st.info("No rotation heatmap to display.")

        with st.expander("Show detailed rotation tables"):
            met_r = rot_bundle.get("metrics")
            if isinstance(met_r, pd.DataFrame) and not met_r.empty:
                st.markdown("**Relative strength metrics**")
                st.dataframe(met_r.round(4), use_container_width=True, hide_index=True)
            px_r = rot_bundle.get("prices")
            if isinstance(px_r, pd.DataFrame) and not px_r.empty:
                st.markdown("**Latest prices (long format)**")
                st.dataframe(
                    px_r.sort_values(["date", "symbol"], ascending=[False, True]),
                    use_container_width=True,
                    hide_index=True,
                )
            rsh = rot_bundle.get("rs_ratio_history")
            if isinstance(rsh, pd.DataFrame) and not rsh.empty:
                st.markdown("**RS ratio vs XLE (level, not % change)**")
                st.dataframe(rsh.tail(500).round(6), use_container_width=True)

    st.divider()
    st.subheader("Energy vs SPY")
    st.caption(
        "XLE (Energy) vs SPY (S&P 500). Dividend-adjusted closes from FMP; cached like other price pulls."
    )

    detail = pd.DataFrame()
    summary: dict = {}
    try:
        detail, summary = sector_pages.get_sector_vs_spy_data(
            session, api_key, sector_etf="XLE", sector_name="Energy"
        )
    except Exception as e:
        st.warning(f"Could not load sector vs benchmark data: {e}")

    if detail.empty:
        st.warning(
            "Price data for XLE and/or SPY is missing or could not be aligned. "
            "Check your API key and try again after a successful `get_price_history` fetch."
        )
    else:
        as_of = summary.get("as_of_date")
        etf = str(summary.get("sector_etf", "XLE"))
        bench = str(summary.get("benchmark", "SPY"))
        col_idx_etf = f"{etf}_index"
        col_idx_bench = f"{bench}_index"

        st.markdown(f"**As of:** `{as_of}`")

        rr1w = summary.get("relative_return_1w")
        rr1 = summary.get("relative_return_1m")
        rr3 = summary.get("relative_return_3m")
        rr6 = summary.get("relative_return_6m")
        rr12s = summary.get("relative_return_12m_skip_1m")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("1W relative return vs SPY", _fmt_rel_pct(rr1w))
        c2.metric("1M relative return vs SPY", _fmt_rel_pct(rr1))
        c3.metric("3M relative return vs SPY", _fmt_rel_pct(rr3))
        c4.metric("6M relative return vs SPY", _fmt_rel_pct(rr6))
        c5.metric("12M skip-1M relative return vs SPY", _fmt_rel_pct(rr12s))

        st.caption(
            "Positive relative return means Energy is outperforming SPY over that window."
        )

        perf = detail[["date", col_idx_etf, col_idx_bench]].copy()
        perf["date"] = pd.to_datetime(perf["date"], errors="coerce")
        perf = perf.dropna(subset=["date"]).set_index("date")
        perf = perf.rename(
            columns={
                col_idx_etf: f"{etf} (normalized)",
                col_idx_bench: f"{bench} (normalized)",
            }
        )
        st.markdown(f"**{etf} vs SPY Cumulative Performance**")
        st.line_chart(perf, height=420)

        ratio = detail[["date", "relative_strength_ratio"]].copy()
        ratio["date"] = pd.to_datetime(ratio["date"], errors="coerce")
        ratio = ratio.dropna(subset=["date"]).set_index("date")
        ratio = ratio.rename(columns={"relative_strength_ratio": f"{etf} / {bench} ratio"})
        st.markdown(f"**{etf} / SPY Relative Strength Ratio**")
        st.line_chart(ratio, height=380)

    st.divider()
    st.subheader("Risk")
    st.caption("Realized volatility shows how unstable XLE has been historically.")

    risk_summary: dict = {}
    dd_ts = pd.DataFrame()
    try:
        risk_summary, _, dd_ts = sector_pages.get_sector_risk_data(session, api_key, sector_etf="XLE")
    except Exception as e:
        st.warning(f"Could not load XLE risk data: {e}")

    if dd_ts.empty:
        st.warning("XLE price history is missing; risk section skipped.")
    else:
        st.markdown(f"**As of:** `{risk_summary.get('as_of_date')}`")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trailing 1Y Vol", _fmt_vol_pct(risk_summary.get("trailing_1y_vol")))
        m2.metric("Trailing 3M Vol", _fmt_vol_pct(risk_summary.get("trailing_3m_vol")))
        m3.metric("Trailing 20D Vol", _fmt_vol_pct(risk_summary.get("trailing_20d_vol")))
        m4.metric("Trailing 1Y Max Drawdown", _fmt_rel_pct(risk_summary.get("trailing_1y_max_drawdown")))
        m5.metric("Current Drawdown vs 1Y High", _fmt_rel_pct(risk_summary.get("current_drawdown_1y_high")))

        st.markdown("**XLE Drawdown Over Time**")
        if dd_ts.empty:
            st.info("No drawdown series to plot.")
        else:
            dd_plot = dd_ts.copy()
            dd_plot["date"] = pd.to_datetime(dd_plot["date"], errors="coerce")
            dd_plot = dd_plot.dropna(subset=["date"]).set_index("date")
            st.line_chart(dd_plot[["drawdown"]], height=360)

    st.divider()
    st.subheader("Energy Internal Dispersion")
    st.caption(
        "Breadth, cross-sectional dispersion, and concentration for the US Energy **dispersion** "
        f"universe (market cap > ${config.DISPERSION_MIN_MARKET_CAP/1e9:.1f}B, avg volume > "
        f"{config.DISPERSION_MIN_AVG_VOLUME/1e3:.0f}k, price > ${config.DISPERSION_MIN_PRICE:.0f}; "
        "ETFs/funds excluded). Prices are dividend-adjusted from `data_loader.get_price_history`."
    )
    try:
        disp_bundle = _cached_sector_dispersion(api_key, "Energy")
    except Exception as e:
        disp_bundle = {"ok": False, "error": str(e)}
    if not disp_bundle.get("ok"):
        st.warning(
            disp_bundle.get("error")
            or "Dispersion analytics unavailable (check FMP key and profile-bulk access)."
        )
    else:
        summ = disp_bundle.get("summary") or {}
        tables = disp_bundle.get("tables") or {}
        as_of = disp_bundle.get("as_of")
        st.markdown(
            f"**As of:** `{as_of}` — names below are the intersection of the dispersion universe and "
            "symbols with sufficient price history for DMAs and 1M returns."
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Universe size", f"{summ.get('universe_size', 0):,}" if summ.get("universe_size") is not None else "—")
        m2.metric("% Above 50 DMA", _fmt_rel_pct(summ.get("pct_above_50dma")))
        m3.metric("% Above 200 DMA", _fmt_rel_pct(summ.get("pct_above_200dma")))
        m4.metric("Equal-weight 1M σ", _fmt_rel_pct(summ.get("equal_weight_std")))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Cap-weight 1M σ", _fmt_rel_pct(summ.get("cap_weight_std")))
        avpc = summ.get("avg_pairwise_corr")
        m6.metric(
            "Avg pairwise corr (60d)",
            f"{float(avpc):.3f}" if avpc is not None and avpc == avpc else "—",
        )
        m7.metric("Top 5 cap weight", _fmt_rel_pct(summ.get("top5_weight")))
        ew_s = summ.get("equal_weight_std")
        cw_s = summ.get("cap_weight_std")
        if ew_s is not None and cw_s is not None and ew_s == ew_s and cw_s == cw_s:
            ew_cw_spread = float(ew_s) - float(cw_s)
        else:
            ew_cw_spread = None
        m8.metric("EW - CW σ Spread", _fmt_rel_pct(ew_cw_spread))

        health = _dispersion_health_chart(disp_bundle)
        plot_cols = ["Breadth 50 DMA %", "Breadth 200 DMA %", "EW - CW σ Spread %"]
        st.markdown("**Energy Breadth & Internal Rotation**")
        if health.empty or not any(c in health.columns for c in plot_cols):
            st.info("Not enough overlapping breadth / dispersion history to plot the combined health series yet.")
        else:
            chart_df = health[[c for c in plot_cols if c in health.columns]].copy()
            st.line_chart(chart_df, height=420)
            st.caption(
                "DMA breadth lines are forward-filled for chart continuity when later dates have missing "
                "200-DMA coverage. KPI cards still use the latest raw calculated values."
            )
        st.caption(
            "Breadth measures participation across the Energy universe. A rising EW-CW σ spread "
            "means internal rotation and stock-level dispersion are increasing beneath the cap-weighted index."
        )

        with st.expander("Show detailed dispersion tables"):
            bt = tables.get("breadth_table")
            if isinstance(bt, pd.DataFrame) and not bt.empty:
                st.markdown("**Breadth**")
                st.dataframe(bt, hide_index=True, width="stretch")
            dst = tables.get("dispersion_summary_table")
            if isinstance(dst, pd.DataFrame) and not dst.empty:
                st.markdown("**Dispersion summary**")
                st.dataframe(dst, hide_index=True, width="stretch")
            ct = tables.get("concentration_table")
            if isinstance(ct, pd.DataFrame) and not ct.empty:
                st.markdown("**Concentration**")
                st.dataframe(ct, hide_index=True, width="stretch")
            top_c = tables.get("top_contributors")
            if isinstance(top_c, pd.DataFrame) and not top_c.empty:
                st.markdown("**Top contributors**")
                st.dataframe(top_c.head(50), hide_index=True, width="stretch")
            bot_c = tables.get("bottom_contributors")
            if isinstance(bot_c, pd.DataFrame) and not bot_c.empty:
                st.markdown("**Bottom contributors**")
                st.dataframe(bot_c.head(50), hide_index=True, width="stretch")
            ind_p = tables.get("industry_participation")
            if isinstance(ind_p, pd.DataFrame) and not ind_p.empty:
                st.markdown("**Industry participation**")
                st.dataframe(ind_p.round(6), hide_index=True, width="stretch")


def render_financial_services_sector_tab() -> None:
    """XLF trend/technicals, proxy baskets vs XLF, sector vs SPY, risk, internal dispersion."""
    st.subheader("Financial Services Sector Analysis")

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        st.warning(
            "FMP_API_KEY is not set (check `.env`). Live XLF / SPY sections need a key; other tabs still work."
        )
        return

    try:
        session = data_loader.create_http_session()
    except Exception as e:
        st.warning(f"Could not create HTTP session: {e}")
        return

    st.subheader("Financial Services ETF Trend & Technicals")
    st.caption(
        "This section analyzes XLF on its own before comparing Financial Services against SPY."
    )

    trend_detail = pd.DataFrame()
    trend_summary: dict = {}
    try:
        trend_detail, trend_summary = sector_pages.get_sector_etf_trend_data(
            session, api_key, sector_etf="XLF"
        )
    except Exception as e:
        st.warning(f"Could not load XLF trend data: {e}")

    if trend_detail.empty:
        st.warning("XLF price history is missing or unavailable; trend & technicals section skipped.")
    else:
        as_of_t = trend_summary.get("as_of_date")
        st.markdown(f"**As of:** `{as_of_t}`")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("1W Return", _fmt_rel_pct(trend_summary.get("return_1w")))
        c2.metric("1M Return", _fmt_rel_pct(trend_summary.get("return_1m")))
        c3.metric("3M Return", _fmt_rel_pct(trend_summary.get("return_3m")))
        c4.metric("12M Skip-1M Return", _fmt_rel_pct(trend_summary.get("return_12m_skip_1m")))

        plot_df = trend_detail.dropna(subset=["dma_200"]).copy()
        if plot_df.empty:
            st.info("Not enough history yet to plot 200-DMA alongside price.")
        else:
            chart = plot_df[["date", "price", "dma_50", "dma_100", "dma_200"]].copy()
            chart["date"] = pd.to_datetime(chart["date"], errors="coerce")
            chart = chart.dropna(subset=["date"]).set_index("date")
            chart = chart.rename(
                columns={
                    "price": "XLF price",
                    "dma_50": "50 DMA",
                    "dma_100": "100 DMA",
                    "dma_200": "200 DMA",
                }
            )
            st.markdown("**XLF Price with Moving Averages**")
            st.line_chart(chart, height=460)

    st.divider()
    st.subheader("Financial Services Industry Rotation")
    st.caption(
        "Relative strength of equal-weight **proxy baskets** (stocks and ETFs listed per row) versus XLF. "
        "Positive values mean that basket is outperforming broad Financial Services over the selected window."
    )
    try:
        rot_bundle = _cached_financial_services_rotation(api_key)
    except Exception as e:
        rot_bundle = {"ok": False, "error": str(e)}
    if not rot_bundle.get("ok"):
        st.warning(
            rot_bundle.get("error")
            or "Financial Services industry rotation unavailable (check FMP key and caches)."
        )
    else:
        st.markdown(f"**As of:** `{rot_bundle.get('as_of')}`")
        hm = rot_bundle.get("heatmap")
        if isinstance(hm, pd.DataFrame) and not hm.empty:
            gmap_rot = hm.clip(lower=-30.0, upper=30.0)
            styled = (
                hm.style.background_gradient(
                    cmap="RdYlGn",
                    axis=None,
                    vmin=-30,
                    vmax=30,
                    gmap=gmap_rot,
                ).format("{:+.2f}%", na_rep="—")
            )
            st.dataframe(styled, use_container_width=True, hide_index=False)
            col_3m = pd.to_numeric(hm.get("3M RS %"), errors="coerce").dropna()
            if not col_3m.empty:
                mx = float(col_3m.max())
                if mx > 0:
                    top_lbl = str(col_3m.idxmax())
                    st.caption(
                        f"Leadership: **{top_lbl}** is leading Financial Services over the last 3 months."
                    )
                else:
                    st.caption("No proxy basket is outperforming XLF over the last 3 months.")
        else:
            st.info("No rotation heatmap to display.")

        with st.expander("Show detailed rotation tables"):
            met_r = rot_bundle.get("metrics")
            if isinstance(met_r, pd.DataFrame) and not met_r.empty:
                st.markdown("**Relative strength metrics**")
                st.dataframe(met_r.round(4), use_container_width=True, hide_index=True)
            px_r = rot_bundle.get("prices")
            if isinstance(px_r, pd.DataFrame) and not px_r.empty:
                st.markdown("**Latest prices (long format)**")
                st.dataframe(
                    px_r.sort_values(["date", "symbol"], ascending=[False, True]),
                    use_container_width=True,
                    hide_index=True,
                )
            rsh = rot_bundle.get("rs_ratio_history")
            if isinstance(rsh, pd.DataFrame) and not rsh.empty:
                st.markdown("**RS ratio vs XLF (level, not % change)**")
                st.dataframe(rsh.tail(500).round(6), use_container_width=True)

    st.divider()
    st.subheader("Financial Services vs SPY")
    st.caption(
        "XLF (Financial Services) vs SPY (S&P 500). Dividend-adjusted closes from FMP; cached like other price pulls."
    )

    detail = pd.DataFrame()
    summary: dict = {}
    try:
        detail, summary = sector_pages.get_sector_vs_spy_data(
            session, api_key, sector_etf="XLF", sector_name="Financial Services"
        )
    except Exception as e:
        st.warning(f"Could not load sector vs benchmark data: {e}")

    if detail.empty:
        st.warning(
            "Price data for XLF and/or SPY is missing or could not be aligned. "
            "Check your API key and try again after a successful `get_price_history` fetch."
        )
    else:
        as_of = summary.get("as_of_date")
        etf = str(summary.get("sector_etf", "XLF"))
        bench = str(summary.get("benchmark", "SPY"))
        col_idx_etf = f"{etf}_index"
        col_idx_bench = f"{bench}_index"

        st.markdown(f"**As of:** `{as_of}`")

        rr1w = summary.get("relative_return_1w")
        rr1 = summary.get("relative_return_1m")
        rr3 = summary.get("relative_return_3m")
        rr6 = summary.get("relative_return_6m")
        rr12s = summary.get("relative_return_12m_skip_1m")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("1W relative return vs SPY", _fmt_rel_pct(rr1w))
        c2.metric("1M relative return vs SPY", _fmt_rel_pct(rr1))
        c3.metric("3M relative return vs SPY", _fmt_rel_pct(rr3))
        c4.metric("6M relative return vs SPY", _fmt_rel_pct(rr6))
        c5.metric("12M skip-1M relative return vs SPY", _fmt_rel_pct(rr12s))

        st.caption(
            "Positive relative return means Financial Services is outperforming SPY over that window."
        )

        perf = detail[["date", col_idx_etf, col_idx_bench]].copy()
        perf["date"] = pd.to_datetime(perf["date"], errors="coerce")
        perf = perf.dropna(subset=["date"]).set_index("date")
        perf = perf.rename(
            columns={
                col_idx_etf: f"{etf} (normalized)",
                col_idx_bench: f"{bench} (normalized)",
            }
        )
        st.markdown(f"**{etf} vs SPY Cumulative Performance**")
        st.line_chart(perf, height=420)

        ratio = detail[["date", "relative_strength_ratio"]].copy()
        ratio["date"] = pd.to_datetime(ratio["date"], errors="coerce")
        ratio = ratio.dropna(subset=["date"]).set_index("date")
        ratio = ratio.rename(columns={"relative_strength_ratio": f"{etf} / {bench} ratio"})
        st.markdown(f"**{etf} / SPY Relative Strength Ratio**")
        st.line_chart(ratio, height=380)

    st.divider()
    st.subheader("Risk")
    st.caption("Realized volatility shows how unstable XLF has been historically.")

    risk_summary: dict = {}
    dd_ts = pd.DataFrame()
    try:
        risk_summary, _, dd_ts = sector_pages.get_sector_risk_data(session, api_key, sector_etf="XLF")
    except Exception as e:
        st.warning(f"Could not load XLF risk data: {e}")

    if dd_ts.empty:
        st.warning("XLF price history is missing; risk section skipped.")
    else:
        st.markdown(f"**As of:** `{risk_summary.get('as_of_date')}`")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trailing 1Y Vol", _fmt_vol_pct(risk_summary.get("trailing_1y_vol")))
        m2.metric("Trailing 3M Vol", _fmt_vol_pct(risk_summary.get("trailing_3m_vol")))
        m3.metric("Trailing 20D Vol", _fmt_vol_pct(risk_summary.get("trailing_20d_vol")))
        m4.metric("Trailing 1Y Max Drawdown", _fmt_rel_pct(risk_summary.get("trailing_1y_max_drawdown")))
        m5.metric("Current Drawdown vs 1Y High", _fmt_rel_pct(risk_summary.get("current_drawdown_1y_high")))

        st.markdown("**XLF Drawdown Over Time**")
        if dd_ts.empty:
            st.info("No drawdown series to plot.")
        else:
            dd_plot = dd_ts.copy()
            dd_plot["date"] = pd.to_datetime(dd_plot["date"], errors="coerce")
            dd_plot = dd_plot.dropna(subset=["date"]).set_index("date")
            st.line_chart(dd_plot[["drawdown"]], height=360)

    st.divider()
    st.subheader("Financial Services Internal Dispersion")
    st.caption(
        "Breadth, cross-sectional dispersion, and concentration for the US Financial Services **dispersion** "
        f"universe (market cap > ${config.DISPERSION_MIN_MARKET_CAP/1e9:.1f}B, avg volume > "
        f"{config.DISPERSION_MIN_AVG_VOLUME/1e3:.0f}k, price > ${config.DISPERSION_MIN_PRICE:.0f}; "
        "ETFs/funds excluded). Prices are dividend-adjusted from `data_loader.get_price_history`."
    )
    try:
        disp_bundle = _cached_sector_dispersion(api_key, "Financial Services")
    except Exception as e:
        disp_bundle = {"ok": False, "error": str(e)}
    if not disp_bundle.get("ok"):
        st.warning(
            disp_bundle.get("error")
            or "Dispersion analytics unavailable (check FMP key and profile-bulk access)."
        )
    else:
        summ = disp_bundle.get("summary") or {}
        tables = disp_bundle.get("tables") or {}
        as_of = disp_bundle.get("as_of")
        st.markdown(
            f"**As of:** `{as_of}` — names below are the intersection of the dispersion universe and "
            "symbols with sufficient price history for DMAs and 1M returns."
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Universe size", f"{summ.get('universe_size', 0):,}" if summ.get("universe_size") is not None else "—")
        m2.metric("% Above 50 DMA", _fmt_rel_pct(summ.get("pct_above_50dma")))
        m3.metric("% Above 200 DMA", _fmt_rel_pct(summ.get("pct_above_200dma")))
        m4.metric("Equal-weight 1M σ", _fmt_rel_pct(summ.get("equal_weight_std")))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Cap-weight 1M σ", _fmt_rel_pct(summ.get("cap_weight_std")))
        avpc = summ.get("avg_pairwise_corr")
        m6.metric(
            "Avg pairwise corr (60d)",
            f"{float(avpc):.3f}" if avpc is not None and avpc == avpc else "—",
        )
        m7.metric("Top 5 cap weight", _fmt_rel_pct(summ.get("top5_weight")))
        ew_s = summ.get("equal_weight_std")
        cw_s = summ.get("cap_weight_std")
        if ew_s is not None and cw_s is not None and ew_s == ew_s and cw_s == cw_s:
            ew_cw_spread = float(ew_s) - float(cw_s)
        else:
            ew_cw_spread = None
        m8.metric("EW - CW σ Spread", _fmt_rel_pct(ew_cw_spread))

        health = _dispersion_health_chart(disp_bundle)
        plot_cols = ["Breadth 50 DMA %", "Breadth 200 DMA %", "EW - CW σ Spread %"]
        st.markdown("**Financial Services Breadth & Internal Rotation**")
        if health.empty or not any(c in health.columns for c in plot_cols):
            st.info("Not enough overlapping breadth / dispersion history to plot the combined health series yet.")
        else:
            chart_df = health[[c for c in plot_cols if c in health.columns]].copy()
            st.line_chart(chart_df, height=420)
            st.caption(
                "DMA breadth lines are forward-filled for chart continuity when later dates have missing "
                "200-DMA coverage. KPI cards still use the latest raw calculated values."
            )
        st.caption(
            "Breadth measures participation across the Financial Services universe. A rising EW-CW σ spread "
            "means internal rotation and stock-level dispersion are increasing beneath the cap-weighted index."
        )

        with st.expander("Show detailed dispersion tables"):
            bt = tables.get("breadth_table")
            if isinstance(bt, pd.DataFrame) and not bt.empty:
                st.markdown("**Breadth**")
                st.dataframe(bt, hide_index=True, width="stretch")
            dst = tables.get("dispersion_summary_table")
            if isinstance(dst, pd.DataFrame) and not dst.empty:
                st.markdown("**Dispersion summary**")
                st.dataframe(dst, hide_index=True, width="stretch")
            ct = tables.get("concentration_table")
            if isinstance(ct, pd.DataFrame) and not ct.empty:
                st.markdown("**Concentration**")
                st.dataframe(ct, hide_index=True, width="stretch")
            top_c = tables.get("top_contributors")
            if isinstance(top_c, pd.DataFrame) and not top_c.empty:
                st.markdown("**Top contributors**")
                st.dataframe(top_c.head(50), hide_index=True, width="stretch")
            bot_c = tables.get("bottom_contributors")
            if isinstance(bot_c, pd.DataFrame) and not bot_c.empty:
                st.markdown("**Bottom contributors**")
                st.dataframe(bot_c.head(50), hide_index=True, width="stretch")
            ind_p = tables.get("industry_participation")
            if isinstance(ind_p, pd.DataFrame) and not ind_p.empty:
                st.markdown("**Industry participation**")
                st.dataframe(ind_p.round(6), hide_index=True, width="stretch")


def render_healthcare_sector_tab() -> None:
    """XLV trend/technicals, proxy baskets vs XLV, sector vs SPY, risk, internal dispersion."""
    st.subheader("Healthcare Sector Analysis")

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        st.warning(
            "FMP_API_KEY is not set (check `.env`). Live XLV / SPY sections need a key; other tabs still work."
        )
        return

    try:
        session = data_loader.create_http_session()
    except Exception as e:
        st.warning(f"Could not create HTTP session: {e}")
        return

    st.subheader("Healthcare ETF Trend & Technicals")
    st.caption(
        "This section analyzes XLV on its own before comparing Healthcare against SPY."
    )

    trend_detail = pd.DataFrame()
    trend_summary: dict = {}
    try:
        trend_detail, trend_summary = sector_pages.get_sector_etf_trend_data(
            session, api_key, sector_etf="XLV"
        )
    except Exception as e:
        st.warning(f"Could not load XLV trend data: {e}")

    if trend_detail.empty:
        st.warning("XLV price history is missing or unavailable; trend & technicals section skipped.")
    else:
        as_of_t = trend_summary.get("as_of_date")
        st.markdown(f"**As of:** `{as_of_t}`")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("1W Return", _fmt_rel_pct(trend_summary.get("return_1w")))
        c2.metric("1M Return", _fmt_rel_pct(trend_summary.get("return_1m")))
        c3.metric("3M Return", _fmt_rel_pct(trend_summary.get("return_3m")))
        c4.metric("12M Skip-1M Return", _fmt_rel_pct(trend_summary.get("return_12m_skip_1m")))

        plot_df = trend_detail.dropna(subset=["dma_200"]).copy()
        if plot_df.empty:
            st.info("Not enough history yet to plot 200-DMA alongside price.")
        else:
            chart = plot_df[["date", "price", "dma_50", "dma_100", "dma_200"]].copy()
            chart["date"] = pd.to_datetime(chart["date"], errors="coerce")
            chart = chart.dropna(subset=["date"]).set_index("date")
            chart = chart.rename(
                columns={
                    "price": "XLV price",
                    "dma_50": "50 DMA",
                    "dma_100": "100 DMA",
                    "dma_200": "200 DMA",
                }
            )
            st.markdown("**XLV Price with Moving Averages**")
            st.line_chart(chart, height=460)

    st.divider()
    st.subheader("Healthcare Industry Rotation")
    st.caption(
        "Relative strength of equal-weight **proxy baskets** (stocks and ETFs listed per row) versus XLV. "
        "Positive values mean that basket is outperforming broad Healthcare over the selected window."
    )
    try:
        rot_bundle = _cached_healthcare_rotation(api_key)
    except Exception as e:
        rot_bundle = {"ok": False, "error": str(e)}
    if not rot_bundle.get("ok"):
        st.warning(
            rot_bundle.get("error")
            or "Healthcare industry rotation unavailable (check FMP key and caches)."
        )
    else:
        st.markdown(f"**As of:** `{rot_bundle.get('as_of')}`")
        hm = rot_bundle.get("heatmap")
        if isinstance(hm, pd.DataFrame) and not hm.empty:
            gmap_rot = hm.clip(lower=-30.0, upper=30.0)
            styled = (
                hm.style.background_gradient(
                    cmap="RdYlGn",
                    axis=None,
                    vmin=-30,
                    vmax=30,
                    gmap=gmap_rot,
                ).format("{:+.2f}%", na_rep="—")
            )
            st.dataframe(styled, use_container_width=True, hide_index=False)
            col_3m = pd.to_numeric(hm.get("3M RS %"), errors="coerce").dropna()
            if not col_3m.empty:
                mx = float(col_3m.max())
                if mx > 0:
                    top_lbl = str(col_3m.idxmax())
                    st.caption(
                        f"Leadership: **{top_lbl}** is leading Healthcare over the last 3 months."
                    )
                else:
                    st.caption("No proxy basket is outperforming XLV over the last 3 months.")
        else:
            st.info("No rotation heatmap to display.")

        with st.expander("Show detailed rotation tables"):
            met_r = rot_bundle.get("metrics")
            if isinstance(met_r, pd.DataFrame) and not met_r.empty:
                st.markdown("**Relative strength metrics**")
                st.dataframe(met_r.round(4), use_container_width=True, hide_index=True)
            px_r = rot_bundle.get("prices")
            if isinstance(px_r, pd.DataFrame) and not px_r.empty:
                st.markdown("**Latest prices (long format)**")
                st.dataframe(
                    px_r.sort_values(["date", "symbol"], ascending=[False, True]),
                    use_container_width=True,
                    hide_index=True,
                )
            rsh = rot_bundle.get("rs_ratio_history")
            if isinstance(rsh, pd.DataFrame) and not rsh.empty:
                st.markdown("**RS ratio vs XLV (level, not % change)**")
                st.dataframe(rsh.tail(500).round(6), use_container_width=True)

    st.divider()
    st.subheader("Healthcare vs SPY")
    st.caption(
        "XLV (Healthcare) vs SPY (S&P 500). Dividend-adjusted closes from FMP; cached like other price pulls."
    )

    detail = pd.DataFrame()
    summary: dict = {}
    try:
        detail, summary = sector_pages.get_sector_vs_spy_data(
            session, api_key, sector_etf="XLV", sector_name="Healthcare"
        )
    except Exception as e:
        st.warning(f"Could not load sector vs benchmark data: {e}")

    if detail.empty:
        st.warning(
            "Price data for XLV and/or SPY is missing or could not be aligned. "
            "Check your API key and try again after a successful `get_price_history` fetch."
        )
    else:
        as_of = summary.get("as_of_date")
        etf = str(summary.get("sector_etf", "XLV"))
        bench = str(summary.get("benchmark", "SPY"))
        col_idx_etf = f"{etf}_index"
        col_idx_bench = f"{bench}_index"

        st.markdown(f"**As of:** `{as_of}`")

        rr1w = summary.get("relative_return_1w")
        rr1 = summary.get("relative_return_1m")
        rr3 = summary.get("relative_return_3m")
        rr6 = summary.get("relative_return_6m")
        rr12s = summary.get("relative_return_12m_skip_1m")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("1W relative return vs SPY", _fmt_rel_pct(rr1w))
        c2.metric("1M relative return vs SPY", _fmt_rel_pct(rr1))
        c3.metric("3M relative return vs SPY", _fmt_rel_pct(rr3))
        c4.metric("6M relative return vs SPY", _fmt_rel_pct(rr6))
        c5.metric("12M skip-1M relative return vs SPY", _fmt_rel_pct(rr12s))

        st.caption(
            "Positive relative return means Healthcare is outperforming SPY over that window."
        )

        perf = detail[["date", col_idx_etf, col_idx_bench]].copy()
        perf["date"] = pd.to_datetime(perf["date"], errors="coerce")
        perf = perf.dropna(subset=["date"]).set_index("date")
        perf = perf.rename(
            columns={
                col_idx_etf: f"{etf} (normalized)",
                col_idx_bench: f"{bench} (normalized)",
            }
        )
        st.markdown(f"**{etf} vs SPY Cumulative Performance**")
        st.line_chart(perf, height=420)

        ratio = detail[["date", "relative_strength_ratio"]].copy()
        ratio["date"] = pd.to_datetime(ratio["date"], errors="coerce")
        ratio = ratio.dropna(subset=["date"]).set_index("date")
        ratio = ratio.rename(columns={"relative_strength_ratio": f"{etf} / {bench} ratio"})
        st.markdown(f"**{etf} / SPY Relative Strength Ratio**")
        st.line_chart(ratio, height=380)

    st.divider()
    st.subheader("Risk")
    st.caption("Realized volatility shows how unstable XLV has been historically.")

    risk_summary: dict = {}
    dd_ts = pd.DataFrame()
    try:
        risk_summary, _, dd_ts = sector_pages.get_sector_risk_data(session, api_key, sector_etf="XLV")
    except Exception as e:
        st.warning(f"Could not load XLV risk data: {e}")

    if dd_ts.empty:
        st.warning("XLV price history is missing; risk section skipped.")
    else:
        st.markdown(f"**As of:** `{risk_summary.get('as_of_date')}`")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trailing 1Y Vol", _fmt_vol_pct(risk_summary.get("trailing_1y_vol")))
        m2.metric("Trailing 3M Vol", _fmt_vol_pct(risk_summary.get("trailing_3m_vol")))
        m3.metric("Trailing 20D Vol", _fmt_vol_pct(risk_summary.get("trailing_20d_vol")))
        m4.metric("Trailing 1Y Max Drawdown", _fmt_rel_pct(risk_summary.get("trailing_1y_max_drawdown")))
        m5.metric("Current Drawdown vs 1Y High", _fmt_rel_pct(risk_summary.get("current_drawdown_1y_high")))

        st.markdown("**XLV Drawdown Over Time**")
        if dd_ts.empty:
            st.info("No drawdown series to plot.")
        else:
            dd_plot = dd_ts.copy()
            dd_plot["date"] = pd.to_datetime(dd_plot["date"], errors="coerce")
            dd_plot = dd_plot.dropna(subset=["date"]).set_index("date")
            st.line_chart(dd_plot[["drawdown"]], height=360)

    st.divider()
    st.subheader("Healthcare Internal Dispersion")
    st.caption(
        "Breadth, cross-sectional dispersion, and concentration for the US Healthcare **dispersion** "
        f"universe (market cap > ${config.DISPERSION_MIN_MARKET_CAP/1e9:.1f}B, avg volume > "
        f"{config.DISPERSION_MIN_AVG_VOLUME/1e3:.0f}k, price > ${config.DISPERSION_MIN_PRICE:.0f}; "
        "ETFs/funds excluded). Prices are dividend-adjusted from `data_loader.get_price_history`."
    )
    try:
        disp_bundle = _cached_sector_dispersion(api_key, "Healthcare")
    except Exception as e:
        disp_bundle = {"ok": False, "error": str(e)}
    if not disp_bundle.get("ok"):
        st.warning(
            disp_bundle.get("error")
            or "Dispersion analytics unavailable (check FMP key and profile-bulk access)."
        )
    else:
        summ = disp_bundle.get("summary") or {}
        tables = disp_bundle.get("tables") or {}
        as_of = disp_bundle.get("as_of")
        st.markdown(
            f"**As of:** `{as_of}` — names below are the intersection of the dispersion universe and "
            "symbols with sufficient price history for DMAs and 1M returns."
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Universe size", f"{summ.get('universe_size', 0):,}" if summ.get("universe_size") is not None else "—")
        m2.metric("% Above 50 DMA", _fmt_rel_pct(summ.get("pct_above_50dma")))
        m3.metric("% Above 200 DMA", _fmt_rel_pct(summ.get("pct_above_200dma")))
        m4.metric("Equal-weight 1M σ", _fmt_rel_pct(summ.get("equal_weight_std")))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Cap-weight 1M σ", _fmt_rel_pct(summ.get("cap_weight_std")))
        avpc = summ.get("avg_pairwise_corr")
        m6.metric(
            "Avg pairwise corr (60d)",
            f"{float(avpc):.3f}" if avpc is not None and avpc == avpc else "—",
        )
        m7.metric("Top 5 cap weight", _fmt_rel_pct(summ.get("top5_weight")))
        ew_s = summ.get("equal_weight_std")
        cw_s = summ.get("cap_weight_std")
        if ew_s is not None and cw_s is not None and ew_s == ew_s and cw_s == cw_s:
            ew_cw_spread = float(ew_s) - float(cw_s)
        else:
            ew_cw_spread = None
        m8.metric("EW - CW σ Spread", _fmt_rel_pct(ew_cw_spread))

        health = _dispersion_health_chart(disp_bundle)
        plot_cols = ["Breadth 50 DMA %", "Breadth 200 DMA %", "EW - CW σ Spread %"]
        st.markdown("**Healthcare Breadth & Internal Rotation**")
        if health.empty or not any(c in health.columns for c in plot_cols):
            st.info("Not enough overlapping breadth / dispersion history to plot the combined health series yet.")
        else:
            chart_df = health[[c for c in plot_cols if c in health.columns]].copy()
            st.line_chart(chart_df, height=420)
            st.caption(
                "DMA breadth lines are forward-filled for chart continuity when later dates have missing "
                "200-DMA coverage. KPI cards still use the latest raw calculated values."
            )
        st.caption(
            "Breadth measures participation across the Healthcare universe. A rising EW-CW σ spread "
            "means internal rotation and stock-level dispersion are increasing beneath the cap-weighted index."
        )

        with st.expander("Show detailed dispersion tables"):
            bt = tables.get("breadth_table")
            if isinstance(bt, pd.DataFrame) and not bt.empty:
                st.markdown("**Breadth**")
                st.dataframe(bt, hide_index=True, width="stretch")
            dst = tables.get("dispersion_summary_table")
            if isinstance(dst, pd.DataFrame) and not dst.empty:
                st.markdown("**Dispersion summary**")
                st.dataframe(dst, hide_index=True, width="stretch")
            ct = tables.get("concentration_table")
            if isinstance(ct, pd.DataFrame) and not ct.empty:
                st.markdown("**Concentration**")
                st.dataframe(ct, hide_index=True, width="stretch")
            top_c = tables.get("top_contributors")
            if isinstance(top_c, pd.DataFrame) and not top_c.empty:
                st.markdown("**Top contributors**")
                st.dataframe(top_c.head(50), hide_index=True, width="stretch")
            bot_c = tables.get("bottom_contributors")
            if isinstance(bot_c, pd.DataFrame) and not bot_c.empty:
                st.markdown("**Bottom contributors**")
                st.dataframe(bot_c.head(50), hide_index=True, width="stretch")
            ind_p = tables.get("industry_participation")
            if isinstance(ind_p, pd.DataFrame) and not ind_p.empty:
                st.markdown("**Industry participation**")
                st.dataframe(ind_p.round(6), hide_index=True, width="stretch")


def render_industrials_sector_tab() -> None:
    """XLI trend/technicals, proxy baskets vs XLI, sector vs SPY, risk, internal dispersion."""
    st.subheader("Industrials Sector Analysis")

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        st.warning(
            "FMP_API_KEY is not set (check `.env`). Live XLI / SPY sections need a key; other tabs still work."
        )
        return

    try:
        session = data_loader.create_http_session()
    except Exception as e:
        st.warning(f"Could not create HTTP session: {e}")
        return

    st.subheader("Industrials ETF Trend & Technicals")
    st.caption(
        "This section analyzes XLI on its own before comparing Industrials against SPY."
    )

    trend_detail = pd.DataFrame()
    trend_summary: dict = {}
    try:
        trend_detail, trend_summary = sector_pages.get_sector_etf_trend_data(
            session, api_key, sector_etf="XLI"
        )
    except Exception as e:
        st.warning(f"Could not load XLI trend data: {e}")

    if trend_detail.empty:
        st.warning("XLI price history is missing or unavailable; trend & technicals section skipped.")
    else:
        as_of_t = trend_summary.get("as_of_date")
        st.markdown(f"**As of:** `{as_of_t}`")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("1W Return", _fmt_rel_pct(trend_summary.get("return_1w")))
        c2.metric("1M Return", _fmt_rel_pct(trend_summary.get("return_1m")))
        c3.metric("3M Return", _fmt_rel_pct(trend_summary.get("return_3m")))
        c4.metric("12M Skip-1M Return", _fmt_rel_pct(trend_summary.get("return_12m_skip_1m")))

        plot_df = trend_detail.dropna(subset=["dma_200"]).copy()
        if plot_df.empty:
            st.info("Not enough history yet to plot 200-DMA alongside price.")
        else:
            chart = plot_df[["date", "price", "dma_50", "dma_100", "dma_200"]].copy()
            chart["date"] = pd.to_datetime(chart["date"], errors="coerce")
            chart = chart.dropna(subset=["date"]).set_index("date")
            chart = chart.rename(
                columns={
                    "price": "XLI price",
                    "dma_50": "50 DMA",
                    "dma_100": "100 DMA",
                    "dma_200": "200 DMA",
                }
            )
            st.markdown("**XLI Price with Moving Averages**")
            st.line_chart(chart, height=460)

    st.divider()
    st.subheader("Industrials Industry Rotation")
    st.caption(
        "Relative strength of equal-weight **proxy baskets** (stocks and ETFs listed per row) versus XLI. "
        "Positive values mean that basket is outperforming broad Industrials over the selected window."
    )
    try:
        rot_bundle = _cached_industrials_rotation(api_key)
    except Exception as e:
        rot_bundle = {"ok": False, "error": str(e)}
    if not rot_bundle.get("ok"):
        st.warning(
            rot_bundle.get("error")
            or "Industrials industry rotation unavailable (check FMP key and caches)."
        )
    else:
        st.markdown(f"**As of:** `{rot_bundle.get('as_of')}`")
        hm = rot_bundle.get("heatmap")
        if isinstance(hm, pd.DataFrame) and not hm.empty:
            gmap_rot = hm.clip(lower=-30.0, upper=30.0)
            styled = (
                hm.style.background_gradient(
                    cmap="RdYlGn",
                    axis=None,
                    vmin=-30,
                    vmax=30,
                    gmap=gmap_rot,
                ).format("{:+.2f}%", na_rep="—")
            )
            st.dataframe(styled, use_container_width=True, hide_index=False)
            col_3m = pd.to_numeric(hm.get("3M RS %"), errors="coerce").dropna()
            if not col_3m.empty:
                mx = float(col_3m.max())
                if mx > 0:
                    top_lbl = str(col_3m.idxmax())
                    st.caption(
                        f"Leadership: **{top_lbl}** is leading Industrials over the last 3 months."
                    )
                else:
                    st.caption("No proxy basket is outperforming XLI over the last 3 months.")
        else:
            st.info("No rotation heatmap to display.")

        with st.expander("Show detailed rotation tables"):
            met_r = rot_bundle.get("metrics")
            if isinstance(met_r, pd.DataFrame) and not met_r.empty:
                st.markdown("**Relative strength metrics**")
                st.dataframe(met_r.round(4), use_container_width=True, hide_index=True)
            px_r = rot_bundle.get("prices")
            if isinstance(px_r, pd.DataFrame) and not px_r.empty:
                st.markdown("**Latest prices (long format)**")
                st.dataframe(
                    px_r.sort_values(["date", "symbol"], ascending=[False, True]),
                    use_container_width=True,
                    hide_index=True,
                )
            rsh = rot_bundle.get("rs_ratio_history")
            if isinstance(rsh, pd.DataFrame) and not rsh.empty:
                st.markdown("**RS ratio vs XLI (level, not % change)**")
                st.dataframe(rsh.tail(500).round(6), use_container_width=True)

    st.divider()
    st.subheader("Industrials vs SPY")
    st.caption(
        "XLI (Industrials) vs SPY (S&P 500). Dividend-adjusted closes from FMP; cached like other price pulls."
    )

    detail = pd.DataFrame()
    summary: dict = {}
    try:
        detail, summary = sector_pages.get_sector_vs_spy_data(
            session, api_key, sector_etf="XLI", sector_name="Industrials"
        )
    except Exception as e:
        st.warning(f"Could not load sector vs benchmark data: {e}")

    if detail.empty:
        st.warning(
            "Price data for XLI and/or SPY is missing or could not be aligned. "
            "Check your API key and try again after a successful `get_price_history` fetch."
        )
    else:
        as_of = summary.get("as_of_date")
        etf = str(summary.get("sector_etf", "XLI"))
        bench = str(summary.get("benchmark", "SPY"))
        col_idx_etf = f"{etf}_index"
        col_idx_bench = f"{bench}_index"

        st.markdown(f"**As of:** `{as_of}`")

        rr1w = summary.get("relative_return_1w")
        rr1 = summary.get("relative_return_1m")
        rr3 = summary.get("relative_return_3m")
        rr6 = summary.get("relative_return_6m")
        rr12s = summary.get("relative_return_12m_skip_1m")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("1W relative return vs SPY", _fmt_rel_pct(rr1w))
        c2.metric("1M relative return vs SPY", _fmt_rel_pct(rr1))
        c3.metric("3M relative return vs SPY", _fmt_rel_pct(rr3))
        c4.metric("6M relative return vs SPY", _fmt_rel_pct(rr6))
        c5.metric("12M skip-1M relative return vs SPY", _fmt_rel_pct(rr12s))

        st.caption(
            "Positive relative return means Industrials is outperforming SPY over that window."
        )

        perf = detail[["date", col_idx_etf, col_idx_bench]].copy()
        perf["date"] = pd.to_datetime(perf["date"], errors="coerce")
        perf = perf.dropna(subset=["date"]).set_index("date")
        perf = perf.rename(
            columns={
                col_idx_etf: f"{etf} (normalized)",
                col_idx_bench: f"{bench} (normalized)",
            }
        )
        st.markdown(f"**{etf} vs SPY Cumulative Performance**")
        st.line_chart(perf, height=420)

        ratio = detail[["date", "relative_strength_ratio"]].copy()
        ratio["date"] = pd.to_datetime(ratio["date"], errors="coerce")
        ratio = ratio.dropna(subset=["date"]).set_index("date")
        ratio = ratio.rename(columns={"relative_strength_ratio": f"{etf} / {bench} ratio"})
        st.markdown(f"**{etf} / SPY Relative Strength Ratio**")
        st.line_chart(ratio, height=380)

    st.divider()
    st.subheader("Risk")
    st.caption("Realized volatility shows how unstable XLI has been historically.")

    risk_summary: dict = {}
    dd_ts = pd.DataFrame()
    try:
        risk_summary, _, dd_ts = sector_pages.get_sector_risk_data(session, api_key, sector_etf="XLI")
    except Exception as e:
        st.warning(f"Could not load XLI risk data: {e}")

    if dd_ts.empty:
        st.warning("XLI price history is missing; risk section skipped.")
    else:
        st.markdown(f"**As of:** `{risk_summary.get('as_of_date')}`")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trailing 1Y Vol", _fmt_vol_pct(risk_summary.get("trailing_1y_vol")))
        m2.metric("Trailing 3M Vol", _fmt_vol_pct(risk_summary.get("trailing_3m_vol")))
        m3.metric("Trailing 20D Vol", _fmt_vol_pct(risk_summary.get("trailing_20d_vol")))
        m4.metric("Trailing 1Y Max Drawdown", _fmt_rel_pct(risk_summary.get("trailing_1y_max_drawdown")))
        m5.metric("Current Drawdown vs 1Y High", _fmt_rel_pct(risk_summary.get("current_drawdown_1y_high")))

        st.markdown("**XLI Drawdown Over Time**")
        if dd_ts.empty:
            st.info("No drawdown series to plot.")
        else:
            dd_plot = dd_ts.copy()
            dd_plot["date"] = pd.to_datetime(dd_plot["date"], errors="coerce")
            dd_plot = dd_plot.dropna(subset=["date"]).set_index("date")
            st.line_chart(dd_plot[["drawdown"]], height=360)

    st.divider()
    st.subheader("Industrials Internal Dispersion")
    st.caption(
        "Breadth, cross-sectional dispersion, and concentration for the US Industrials **dispersion** "
        f"universe (market cap > ${config.DISPERSION_MIN_MARKET_CAP/1e9:.1f}B, avg volume > "
        f"{config.DISPERSION_MIN_AVG_VOLUME/1e3:.0f}k, price > ${config.DISPERSION_MIN_PRICE:.0f}; "
        "ETFs/funds excluded). Prices are dividend-adjusted from `data_loader.get_price_history`."
    )
    try:
        disp_bundle = _cached_sector_dispersion(api_key, "Industrials")
    except Exception as e:
        disp_bundle = {"ok": False, "error": str(e)}
    if not disp_bundle.get("ok"):
        st.warning(
            disp_bundle.get("error")
            or "Dispersion analytics unavailable (check FMP key and profile-bulk access)."
        )
    else:
        summ = disp_bundle.get("summary") or {}
        tables = disp_bundle.get("tables") or {}
        as_of = disp_bundle.get("as_of")
        st.markdown(
            f"**As of:** `{as_of}` — names below are the intersection of the dispersion universe and "
            "symbols with sufficient price history for DMAs and 1M returns."
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Universe size", f"{summ.get('universe_size', 0):,}" if summ.get("universe_size") is not None else "—")
        m2.metric("% Above 50 DMA", _fmt_rel_pct(summ.get("pct_above_50dma")))
        m3.metric("% Above 200 DMA", _fmt_rel_pct(summ.get("pct_above_200dma")))
        m4.metric("Equal-weight 1M σ", _fmt_rel_pct(summ.get("equal_weight_std")))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Cap-weight 1M σ", _fmt_rel_pct(summ.get("cap_weight_std")))
        avpc = summ.get("avg_pairwise_corr")
        m6.metric(
            "Avg pairwise corr (60d)",
            f"{float(avpc):.3f}" if avpc is not None and avpc == avpc else "—",
        )
        m7.metric("Top 5 cap weight", _fmt_rel_pct(summ.get("top5_weight")))
        ew_s = summ.get("equal_weight_std")
        cw_s = summ.get("cap_weight_std")
        if ew_s is not None and cw_s is not None and ew_s == ew_s and cw_s == cw_s:
            ew_cw_spread = float(ew_s) - float(cw_s)
        else:
            ew_cw_spread = None
        m8.metric("EW - CW σ Spread", _fmt_rel_pct(ew_cw_spread))

        health = _dispersion_health_chart(disp_bundle)
        plot_cols = ["Breadth 50 DMA %", "Breadth 200 DMA %", "EW - CW σ Spread %"]
        st.markdown("**Industrials Breadth & Internal Rotation**")
        if health.empty or not any(c in health.columns for c in plot_cols):
            st.info("Not enough overlapping breadth / dispersion history to plot the combined health series yet.")
        else:
            chart_df = health[[c for c in plot_cols if c in health.columns]].copy()
            st.line_chart(chart_df, height=420)
            st.caption(
                "DMA breadth lines are forward-filled for chart continuity when later dates have missing "
                "200-DMA coverage. KPI cards still use the latest raw calculated values."
            )
        st.caption(
            "Breadth measures participation across the Industrials universe. A rising EW-CW σ spread "
            "means internal rotation and stock-level dispersion are increasing beneath the cap-weighted index."
        )

        with st.expander("Show detailed dispersion tables"):
            bt = tables.get("breadth_table")
            if isinstance(bt, pd.DataFrame) and not bt.empty:
                st.markdown("**Breadth**")
                st.dataframe(bt, hide_index=True, width="stretch")
            dst = tables.get("dispersion_summary_table")
            if isinstance(dst, pd.DataFrame) and not dst.empty:
                st.markdown("**Dispersion summary**")
                st.dataframe(dst, hide_index=True, width="stretch")
            ct = tables.get("concentration_table")
            if isinstance(ct, pd.DataFrame) and not ct.empty:
                st.markdown("**Concentration**")
                st.dataframe(ct, hide_index=True, width="stretch")
            top_c = tables.get("top_contributors")
            if isinstance(top_c, pd.DataFrame) and not top_c.empty:
                st.markdown("**Top contributors**")
                st.dataframe(top_c.head(50), hide_index=True, width="stretch")
            bot_c = tables.get("bottom_contributors")
            if isinstance(bot_c, pd.DataFrame) and not bot_c.empty:
                st.markdown("**Bottom contributors**")
                st.dataframe(bot_c.head(50), hide_index=True, width="stretch")
            ind_p = tables.get("industry_participation")
            if isinstance(ind_p, pd.DataFrame) and not ind_p.empty:
                st.markdown("**Industry participation**")
                st.dataframe(ind_p.round(6), hide_index=True, width="stretch")


def render_real_estate_sector_tab() -> None:
    """XLRE trend/technicals, proxy baskets vs XLRE, sector vs SPY, risk, internal dispersion."""
    st.subheader("Real Estate Sector Analysis")

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        st.warning(
            "FMP_API_KEY is not set (check `.env`). Live XLRE / SPY sections need a key; other tabs still work."
        )
        return

    try:
        session = data_loader.create_http_session()
    except Exception as e:
        st.warning(f"Could not create HTTP session: {e}")
        return

    st.subheader("Real Estate ETF Trend & Technicals")
    st.caption(
        "This section analyzes XLRE on its own before comparing Real Estate against SPY."
    )

    trend_detail = pd.DataFrame()
    trend_summary: dict = {}
    try:
        trend_detail, trend_summary = sector_pages.get_sector_etf_trend_data(
            session, api_key, sector_etf="XLRE"
        )
    except Exception as e:
        st.warning(f"Could not load XLRE trend data: {e}")

    if trend_detail.empty:
        st.warning("XLRE price history is missing or unavailable; trend & technicals section skipped.")
    else:
        as_of_t = trend_summary.get("as_of_date")
        st.markdown(f"**As of:** `{as_of_t}`")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("1W Return", _fmt_rel_pct(trend_summary.get("return_1w")))
        c2.metric("1M Return", _fmt_rel_pct(trend_summary.get("return_1m")))
        c3.metric("3M Return", _fmt_rel_pct(trend_summary.get("return_3m")))
        c4.metric("12M Skip-1M Return", _fmt_rel_pct(trend_summary.get("return_12m_skip_1m")))

        plot_df = trend_detail.dropna(subset=["dma_200"]).copy()
        if plot_df.empty:
            st.info("Not enough history yet to plot 200-DMA alongside price.")
        else:
            chart = plot_df[["date", "price", "dma_50", "dma_100", "dma_200"]].copy()
            chart["date"] = pd.to_datetime(chart["date"], errors="coerce")
            chart = chart.dropna(subset=["date"]).set_index("date")
            chart = chart.rename(
                columns={
                    "price": "XLRE price",
                    "dma_50": "50 DMA",
                    "dma_100": "100 DMA",
                    "dma_200": "200 DMA",
                }
            )
            st.markdown("**XLRE Price with Moving Averages**")
            st.line_chart(chart, height=460)

    st.divider()
    st.subheader("Real Estate Industry Rotation")
    st.caption(
        "Relative strength of equal-weight **proxy baskets** (REITs and ETFs listed per row) versus XLRE. "
        "Positive values mean that basket is outperforming broad Real Estate over the selected window."
    )
    try:
        rot_bundle = _cached_real_estate_rotation(api_key)
    except Exception as e:
        rot_bundle = {"ok": False, "error": str(e)}
    if not rot_bundle.get("ok"):
        st.warning(
            rot_bundle.get("error")
            or "Real Estate industry rotation unavailable (check FMP key and caches)."
        )
    else:
        st.markdown(f"**As of:** `{rot_bundle.get('as_of')}`")
        hm = rot_bundle.get("heatmap")
        if isinstance(hm, pd.DataFrame) and not hm.empty:
            gmap_rot = hm.clip(lower=-30.0, upper=30.0)
            styled = (
                hm.style.background_gradient(
                    cmap="RdYlGn",
                    axis=None,
                    vmin=-30,
                    vmax=30,
                    gmap=gmap_rot,
                ).format("{:+.2f}%", na_rep="—")
            )
            st.dataframe(styled, use_container_width=True, hide_index=False)
            col_3m = pd.to_numeric(hm.get("3M RS %"), errors="coerce").dropna()
            if not col_3m.empty:
                mx = float(col_3m.max())
                if mx > 0:
                    top_lbl = str(col_3m.idxmax())
                    st.caption(
                        f"Leadership: **{top_lbl}** is leading Real Estate over the last 3 months."
                    )
                else:
                    st.caption("No proxy basket is outperforming XLRE over the last 3 months.")
        else:
            st.info("No rotation heatmap to display.")

        with st.expander("Show detailed rotation tables"):
            met_r = rot_bundle.get("metrics")
            if isinstance(met_r, pd.DataFrame) and not met_r.empty:
                st.markdown("**Relative strength metrics**")
                st.dataframe(met_r.round(4), use_container_width=True, hide_index=True)
            px_r = rot_bundle.get("prices")
            if isinstance(px_r, pd.DataFrame) and not px_r.empty:
                st.markdown("**Latest prices (long format)**")
                st.dataframe(
                    px_r.sort_values(["date", "symbol"], ascending=[False, True]),
                    use_container_width=True,
                    hide_index=True,
                )
            rsh = rot_bundle.get("rs_ratio_history")
            if isinstance(rsh, pd.DataFrame) and not rsh.empty:
                st.markdown("**RS ratio vs XLRE (level, not % change)**")
                st.dataframe(rsh.tail(500).round(6), use_container_width=True)

    st.divider()
    st.subheader("Real Estate vs SPY")
    st.caption(
        "XLRE (Real Estate) vs SPY (S&P 500). Dividend-adjusted closes from FMP; cached like other price pulls."
    )

    detail = pd.DataFrame()
    summary: dict = {}
    try:
        detail, summary = sector_pages.get_sector_vs_spy_data(
            session, api_key, sector_etf="XLRE", sector_name="Real Estate"
        )
    except Exception as e:
        st.warning(f"Could not load sector vs benchmark data: {e}")

    if detail.empty:
        st.warning(
            "Price data for XLRE and/or SPY is missing or could not be aligned. "
            "Check your API key and try again after a successful `get_price_history` fetch."
        )
    else:
        as_of = summary.get("as_of_date")
        etf = str(summary.get("sector_etf", "XLRE"))
        bench = str(summary.get("benchmark", "SPY"))
        col_idx_etf = f"{etf}_index"
        col_idx_bench = f"{bench}_index"

        st.markdown(f"**As of:** `{as_of}`")

        rr1w = summary.get("relative_return_1w")
        rr1 = summary.get("relative_return_1m")
        rr3 = summary.get("relative_return_3m")
        rr6 = summary.get("relative_return_6m")
        rr12s = summary.get("relative_return_12m_skip_1m")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("1W relative return vs SPY", _fmt_rel_pct(rr1w))
        c2.metric("1M relative return vs SPY", _fmt_rel_pct(rr1))
        c3.metric("3M relative return vs SPY", _fmt_rel_pct(rr3))
        c4.metric("6M relative return vs SPY", _fmt_rel_pct(rr6))
        c5.metric("12M skip-1M relative return vs SPY", _fmt_rel_pct(rr12s))

        st.caption(
            "Positive relative return means Real Estate is outperforming SPY over that window."
        )

        perf = detail[["date", col_idx_etf, col_idx_bench]].copy()
        perf["date"] = pd.to_datetime(perf["date"], errors="coerce")
        perf = perf.dropna(subset=["date"]).set_index("date")
        perf = perf.rename(
            columns={
                col_idx_etf: f"{etf} (normalized)",
                col_idx_bench: f"{bench} (normalized)",
            }
        )
        st.markdown(f"**{etf} vs SPY Cumulative Performance**")
        st.line_chart(perf, height=420)

        ratio = detail[["date", "relative_strength_ratio"]].copy()
        ratio["date"] = pd.to_datetime(ratio["date"], errors="coerce")
        ratio = ratio.dropna(subset=["date"]).set_index("date")
        ratio = ratio.rename(columns={"relative_strength_ratio": f"{etf} / {bench} ratio"})
        st.markdown(f"**{etf} / SPY Relative Strength Ratio**")
        st.line_chart(ratio, height=380)

    st.divider()
    st.subheader("Risk")
    st.caption("Realized volatility shows how unstable XLRE has been historically.")

    risk_summary: dict = {}
    dd_ts = pd.DataFrame()
    try:
        risk_summary, _, dd_ts = sector_pages.get_sector_risk_data(session, api_key, sector_etf="XLRE")
    except Exception as e:
        st.warning(f"Could not load XLRE risk data: {e}")

    if dd_ts.empty:
        st.warning("XLRE price history is missing; risk section skipped.")
    else:
        st.markdown(f"**As of:** `{risk_summary.get('as_of_date')}`")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trailing 1Y Vol", _fmt_vol_pct(risk_summary.get("trailing_1y_vol")))
        m2.metric("Trailing 3M Vol", _fmt_vol_pct(risk_summary.get("trailing_3m_vol")))
        m3.metric("Trailing 20D Vol", _fmt_vol_pct(risk_summary.get("trailing_20d_vol")))
        m4.metric("Trailing 1Y Max Drawdown", _fmt_rel_pct(risk_summary.get("trailing_1y_max_drawdown")))
        m5.metric("Current Drawdown vs 1Y High", _fmt_rel_pct(risk_summary.get("current_drawdown_1y_high")))

        st.markdown("**XLRE Drawdown Over Time**")
        if dd_ts.empty:
            st.info("No drawdown series to plot.")
        else:
            dd_plot = dd_ts.copy()
            dd_plot["date"] = pd.to_datetime(dd_plot["date"], errors="coerce")
            dd_plot = dd_plot.dropna(subset=["date"]).set_index("date")
            st.line_chart(dd_plot[["drawdown"]], height=360)

    st.divider()
    st.subheader("Real Estate Internal Dispersion")
    st.caption(
        "Breadth, cross-sectional dispersion, and concentration for the US Real Estate **dispersion** "
        f"universe (market cap > ${config.DISPERSION_MIN_MARKET_CAP/1e9:.1f}B, avg volume > "
        f"{config.DISPERSION_MIN_AVG_VOLUME/1e3:.0f}k, price > ${config.DISPERSION_MIN_PRICE:.0f}; "
        "ETFs/funds excluded). Prices are dividend-adjusted from `data_loader.get_price_history`."
    )
    try:
        disp_bundle = _cached_sector_dispersion(api_key, "Real Estate")
    except Exception as e:
        disp_bundle = {"ok": False, "error": str(e)}
    if not disp_bundle.get("ok"):
        st.warning(
            disp_bundle.get("error")
            or "Dispersion analytics unavailable (check FMP key and profile-bulk access)."
        )
    else:
        summ = disp_bundle.get("summary") or {}
        tables = disp_bundle.get("tables") or {}
        as_of = disp_bundle.get("as_of")
        st.markdown(
            f"**As of:** `{as_of}` — names below are the intersection of the dispersion universe and "
            "symbols with sufficient price history for DMAs and 1M returns."
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Universe size", f"{summ.get('universe_size', 0):,}" if summ.get("universe_size") is not None else "—")
        m2.metric("% Above 50 DMA", _fmt_rel_pct(summ.get("pct_above_50dma")))
        m3.metric("% Above 200 DMA", _fmt_rel_pct(summ.get("pct_above_200dma")))
        m4.metric("Equal-weight 1M σ", _fmt_rel_pct(summ.get("equal_weight_std")))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Cap-weight 1M σ", _fmt_rel_pct(summ.get("cap_weight_std")))
        avpc = summ.get("avg_pairwise_corr")
        m6.metric(
            "Avg pairwise corr (60d)",
            f"{float(avpc):.3f}" if avpc is not None and avpc == avpc else "—",
        )
        m7.metric("Top 5 cap weight", _fmt_rel_pct(summ.get("top5_weight")))
        ew_s = summ.get("equal_weight_std")
        cw_s = summ.get("cap_weight_std")
        if ew_s is not None and cw_s is not None and ew_s == ew_s and cw_s == cw_s:
            ew_cw_spread = float(ew_s) - float(cw_s)
        else:
            ew_cw_spread = None
        m8.metric("EW - CW σ Spread", _fmt_rel_pct(ew_cw_spread))

        health = _dispersion_health_chart(disp_bundle)
        plot_cols = ["Breadth 50 DMA %", "Breadth 200 DMA %", "EW - CW σ Spread %"]
        st.markdown("**Real Estate Breadth & Internal Rotation**")
        if health.empty or not any(c in health.columns for c in plot_cols):
            st.info("Not enough overlapping breadth / dispersion history to plot the combined health series yet.")
        else:
            chart_df = health[[c for c in plot_cols if c in health.columns]].copy()
            st.line_chart(chart_df, height=420)
            st.caption(
                "DMA breadth lines are forward-filled for chart continuity when later dates have missing "
                "200-DMA coverage. KPI cards still use the latest raw calculated values."
            )
        st.caption(
            "Breadth measures participation across the Real Estate universe. A rising EW-CW σ spread "
            "means internal rotation and stock-level dispersion are increasing beneath the cap-weighted index."
        )

        with st.expander("Show detailed dispersion tables"):
            bt = tables.get("breadth_table")
            if isinstance(bt, pd.DataFrame) and not bt.empty:
                st.markdown("**Breadth**")
                st.dataframe(bt, hide_index=True, width="stretch")
            dst = tables.get("dispersion_summary_table")
            if isinstance(dst, pd.DataFrame) and not dst.empty:
                st.markdown("**Dispersion summary**")
                st.dataframe(dst, hide_index=True, width="stretch")
            ct = tables.get("concentration_table")
            if isinstance(ct, pd.DataFrame) and not ct.empty:
                st.markdown("**Concentration**")
                st.dataframe(ct, hide_index=True, width="stretch")
            top_c = tables.get("top_contributors")
            if isinstance(top_c, pd.DataFrame) and not top_c.empty:
                st.markdown("**Top contributors**")
                st.dataframe(top_c.head(50), hide_index=True, width="stretch")
            bot_c = tables.get("bottom_contributors")
            if isinstance(bot_c, pd.DataFrame) and not bot_c.empty:
                st.markdown("**Bottom contributors**")
                st.dataframe(bot_c.head(50), hide_index=True, width="stretch")
            ind_p = tables.get("industry_participation")
            if isinstance(ind_p, pd.DataFrame) and not ind_p.empty:
                st.markdown("**Industry participation**")
                st.dataframe(ind_p.round(6), hide_index=True, width="stretch")


def render_utilities_sector_tab() -> None:
    """XLU trend/technicals, proxy baskets vs XLU, sector vs SPY, risk, internal dispersion."""
    st.subheader("Utilities Sector Analysis")

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        st.warning(
            "FMP_API_KEY is not set (check `.env`). Live XLU / SPY sections need a key; other tabs still work."
        )
        return

    try:
        session = data_loader.create_http_session()
    except Exception as e:
        st.warning(f"Could not create HTTP session: {e}")
        return

    st.subheader("Utilities ETF Trend & Technicals")
    st.caption(
        "This section analyzes XLU on its own before comparing Utilities against SPY."
    )

    trend_detail = pd.DataFrame()
    trend_summary: dict = {}
    try:
        trend_detail, trend_summary = sector_pages.get_sector_etf_trend_data(
            session, api_key, sector_etf="XLU"
        )
    except Exception as e:
        st.warning(f"Could not load XLU trend data: {e}")

    if trend_detail.empty:
        st.warning("XLU price history is missing or unavailable; trend & technicals section skipped.")
    else:
        as_of_t = trend_summary.get("as_of_date")
        st.markdown(f"**As of:** `{as_of_t}`")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("1W Return", _fmt_rel_pct(trend_summary.get("return_1w")))
        c2.metric("1M Return", _fmt_rel_pct(trend_summary.get("return_1m")))
        c3.metric("3M Return", _fmt_rel_pct(trend_summary.get("return_3m")))
        c4.metric("12M Skip-1M Return", _fmt_rel_pct(trend_summary.get("return_12m_skip_1m")))

        plot_df = trend_detail.dropna(subset=["dma_200"]).copy()
        if plot_df.empty:
            st.info("Not enough history yet to plot 200-DMA alongside price.")
        else:
            chart = plot_df[["date", "price", "dma_50", "dma_100", "dma_200"]].copy()
            chart["date"] = pd.to_datetime(chart["date"], errors="coerce")
            chart = chart.dropna(subset=["date"]).set_index("date")
            chart = chart.rename(
                columns={
                    "price": "XLU price",
                    "dma_50": "50 DMA",
                    "dma_100": "100 DMA",
                    "dma_200": "200 DMA",
                }
            )
            st.markdown("**XLU Price with Moving Averages**")
            st.line_chart(chart, height=460)

    st.divider()
    st.subheader("Utilities Industry Rotation")
    st.caption(
        "Relative strength of equal-weight **proxy baskets** (stocks and ETFs listed per row) versus XLU. "
        "Positive values mean that basket is outperforming broad Utilities over the selected window."
    )
    try:
        rot_bundle = _cached_utilities_rotation(api_key)
    except Exception as e:
        rot_bundle = {"ok": False, "error": str(e)}
    if not rot_bundle.get("ok"):
        st.warning(
            rot_bundle.get("error")
            or "Utilities industry rotation unavailable (check FMP key and caches)."
        )
    else:
        st.markdown(f"**As of:** `{rot_bundle.get('as_of')}`")
        hm = rot_bundle.get("heatmap")
        if isinstance(hm, pd.DataFrame) and not hm.empty:
            gmap_rot = hm.clip(lower=-30.0, upper=30.0)
            styled = (
                hm.style.background_gradient(
                    cmap="RdYlGn",
                    axis=None,
                    vmin=-30,
                    vmax=30,
                    gmap=gmap_rot,
                ).format("{:+.2f}%", na_rep="—")
            )
            st.dataframe(styled, use_container_width=True, hide_index=False)
            col_3m = pd.to_numeric(hm.get("3M RS %"), errors="coerce").dropna()
            if not col_3m.empty:
                mx = float(col_3m.max())
                if mx > 0:
                    top_lbl = str(col_3m.idxmax())
                    st.caption(
                        f"Leadership: **{top_lbl}** is leading Utilities over the last 3 months."
                    )
                else:
                    st.caption("No proxy basket is outperforming XLU over the last 3 months.")
        else:
            st.info("No rotation heatmap to display.")

        with st.expander("Show detailed rotation tables"):
            met_r = rot_bundle.get("metrics")
            if isinstance(met_r, pd.DataFrame) and not met_r.empty:
                st.markdown("**Relative strength metrics**")
                st.dataframe(met_r.round(4), use_container_width=True, hide_index=True)
            px_r = rot_bundle.get("prices")
            if isinstance(px_r, pd.DataFrame) and not px_r.empty:
                st.markdown("**Latest prices (long format)**")
                st.dataframe(
                    px_r.sort_values(["date", "symbol"], ascending=[False, True]),
                    use_container_width=True,
                    hide_index=True,
                )
            rsh = rot_bundle.get("rs_ratio_history")
            if isinstance(rsh, pd.DataFrame) and not rsh.empty:
                st.markdown("**RS ratio vs XLU (level, not % change)**")
                st.dataframe(rsh.tail(500).round(6), use_container_width=True)

    st.divider()
    st.subheader("Utilities vs SPY")
    st.caption(
        "XLU (Utilities) vs SPY (S&P 500). Dividend-adjusted closes from FMP; cached like other price pulls."
    )

    detail = pd.DataFrame()
    summary: dict = {}
    try:
        detail, summary = sector_pages.get_sector_vs_spy_data(
            session, api_key, sector_etf="XLU", sector_name="Utilities"
        )
    except Exception as e:
        st.warning(f"Could not load sector vs benchmark data: {e}")

    if detail.empty:
        st.warning(
            "Price data for XLU and/or SPY is missing or could not be aligned. "
            "Check your API key and try again after a successful `get_price_history` fetch."
        )
    else:
        as_of = summary.get("as_of_date")
        etf = str(summary.get("sector_etf", "XLU"))
        bench = str(summary.get("benchmark", "SPY"))
        col_idx_etf = f"{etf}_index"
        col_idx_bench = f"{bench}_index"

        st.markdown(f"**As of:** `{as_of}`")

        rr1w = summary.get("relative_return_1w")
        rr1 = summary.get("relative_return_1m")
        rr3 = summary.get("relative_return_3m")
        rr6 = summary.get("relative_return_6m")
        rr12s = summary.get("relative_return_12m_skip_1m")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("1W relative return vs SPY", _fmt_rel_pct(rr1w))
        c2.metric("1M relative return vs SPY", _fmt_rel_pct(rr1))
        c3.metric("3M relative return vs SPY", _fmt_rel_pct(rr3))
        c4.metric("6M relative return vs SPY", _fmt_rel_pct(rr6))
        c5.metric("12M skip-1M relative return vs SPY", _fmt_rel_pct(rr12s))

        st.caption(
            "Positive relative return means Utilities is outperforming SPY over that window."
        )

        perf = detail[["date", col_idx_etf, col_idx_bench]].copy()
        perf["date"] = pd.to_datetime(perf["date"], errors="coerce")
        perf = perf.dropna(subset=["date"]).set_index("date")
        perf = perf.rename(
            columns={
                col_idx_etf: f"{etf} (normalized)",
                col_idx_bench: f"{bench} (normalized)",
            }
        )
        st.markdown(f"**{etf} vs SPY Cumulative Performance**")
        st.line_chart(perf, height=420)

        ratio = detail[["date", "relative_strength_ratio"]].copy()
        ratio["date"] = pd.to_datetime(ratio["date"], errors="coerce")
        ratio = ratio.dropna(subset=["date"]).set_index("date")
        ratio = ratio.rename(columns={"relative_strength_ratio": f"{etf} / {bench} ratio"})
        st.markdown(f"**{etf} / SPY Relative Strength Ratio**")
        st.line_chart(ratio, height=380)

    st.divider()
    st.subheader("Risk")
    st.caption("Realized volatility shows how unstable XLU has been historically.")

    risk_summary: dict = {}
    dd_ts = pd.DataFrame()
    try:
        risk_summary, _, dd_ts = sector_pages.get_sector_risk_data(session, api_key, sector_etf="XLU")
    except Exception as e:
        st.warning(f"Could not load XLU risk data: {e}")

    if dd_ts.empty:
        st.warning("XLU price history is missing; risk section skipped.")
    else:
        st.markdown(f"**As of:** `{risk_summary.get('as_of_date')}`")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trailing 1Y Vol", _fmt_vol_pct(risk_summary.get("trailing_1y_vol")))
        m2.metric("Trailing 3M Vol", _fmt_vol_pct(risk_summary.get("trailing_3m_vol")))
        m3.metric("Trailing 20D Vol", _fmt_vol_pct(risk_summary.get("trailing_20d_vol")))
        m4.metric("Trailing 1Y Max Drawdown", _fmt_rel_pct(risk_summary.get("trailing_1y_max_drawdown")))
        m5.metric("Current Drawdown vs 1Y High", _fmt_rel_pct(risk_summary.get("current_drawdown_1y_high")))

        st.markdown("**XLU Drawdown Over Time**")
        if dd_ts.empty:
            st.info("No drawdown series to plot.")
        else:
            dd_plot = dd_ts.copy()
            dd_plot["date"] = pd.to_datetime(dd_plot["date"], errors="coerce")
            dd_plot = dd_plot.dropna(subset=["date"]).set_index("date")
            st.line_chart(dd_plot[["drawdown"]], height=360)

    st.divider()
    st.subheader("Utilities Internal Dispersion")
    st.caption(
        "Breadth, cross-sectional dispersion, and concentration for the US Utilities **dispersion** "
        f"universe (market cap > ${config.DISPERSION_MIN_MARKET_CAP/1e9:.1f}B, avg volume > "
        f"{config.DISPERSION_MIN_AVG_VOLUME/1e3:.0f}k, price > ${config.DISPERSION_MIN_PRICE:.0f}; "
        "ETFs/funds excluded). Prices are dividend-adjusted from `data_loader.get_price_history`."
    )
    try:
        disp_bundle = _cached_sector_dispersion(api_key, "Utilities")
    except Exception as e:
        disp_bundle = {"ok": False, "error": str(e)}
    if not disp_bundle.get("ok"):
        st.warning(
            disp_bundle.get("error")
            or "Dispersion analytics unavailable (check FMP key and profile-bulk access)."
        )
    else:
        summ = disp_bundle.get("summary") or {}
        tables = disp_bundle.get("tables") or {}
        as_of = disp_bundle.get("as_of")
        st.markdown(
            f"**As of:** `{as_of}` — names below are the intersection of the dispersion universe and "
            "symbols with sufficient price history for DMAs and 1M returns."
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Universe size", f"{summ.get('universe_size', 0):,}" if summ.get("universe_size") is not None else "—")
        m2.metric("% Above 50 DMA", _fmt_rel_pct(summ.get("pct_above_50dma")))
        m3.metric("% Above 200 DMA", _fmt_rel_pct(summ.get("pct_above_200dma")))
        m4.metric("Equal-weight 1M σ", _fmt_rel_pct(summ.get("equal_weight_std")))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Cap-weight 1M σ", _fmt_rel_pct(summ.get("cap_weight_std")))
        avpc = summ.get("avg_pairwise_corr")
        m6.metric(
            "Avg pairwise corr (60d)",
            f"{float(avpc):.3f}" if avpc is not None and avpc == avpc else "—",
        )
        m7.metric("Top 5 cap weight", _fmt_rel_pct(summ.get("top5_weight")))
        ew_s = summ.get("equal_weight_std")
        cw_s = summ.get("cap_weight_std")
        if ew_s is not None and cw_s is not None and ew_s == ew_s and cw_s == cw_s:
            ew_cw_spread = float(ew_s) - float(cw_s)
        else:
            ew_cw_spread = None
        m8.metric("EW - CW σ Spread", _fmt_rel_pct(ew_cw_spread))

        health = _dispersion_health_chart(disp_bundle)
        plot_cols = ["Breadth 50 DMA %", "Breadth 200 DMA %", "EW - CW σ Spread %"]
        st.markdown("**Utilities Breadth & Internal Rotation**")
        if health.empty or not any(c in health.columns for c in plot_cols):
            st.info("Not enough overlapping breadth / dispersion history to plot the combined health series yet.")
        else:
            chart_df = health[[c for c in plot_cols if c in health.columns]].copy()
            st.line_chart(chart_df, height=420)
            st.caption(
                "DMA breadth lines are forward-filled for chart continuity when later dates have missing "
                "200-DMA coverage. KPI cards still use the latest raw calculated values."
            )
        st.caption(
            "Breadth measures participation across the Utilities universe. A rising EW-CW σ spread "
            "means internal rotation and stock-level dispersion are increasing beneath the cap-weighted index."
        )

        with st.expander("Show detailed dispersion tables"):
            bt = tables.get("breadth_table")
            if isinstance(bt, pd.DataFrame) and not bt.empty:
                st.markdown("**Breadth**")
                st.dataframe(bt, hide_index=True, width="stretch")
            dst = tables.get("dispersion_summary_table")
            if isinstance(dst, pd.DataFrame) and not dst.empty:
                st.markdown("**Dispersion summary**")
                st.dataframe(dst, hide_index=True, width="stretch")
            ct = tables.get("concentration_table")
            if isinstance(ct, pd.DataFrame) and not ct.empty:
                st.markdown("**Concentration**")
                st.dataframe(ct, hide_index=True, width="stretch")
            top_c = tables.get("top_contributors")
            if isinstance(top_c, pd.DataFrame) and not top_c.empty:
                st.markdown("**Top contributors**")
                st.dataframe(top_c.head(50), hide_index=True, width="stretch")
            bot_c = tables.get("bottom_contributors")
            if isinstance(bot_c, pd.DataFrame) and not bot_c.empty:
                st.markdown("**Bottom contributors**")
                st.dataframe(bot_c.head(50), hide_index=True, width="stretch")
            ind_p = tables.get("industry_participation")
            if isinstance(ind_p, pd.DataFrame) and not ind_p.empty:
                st.markdown("**Industry participation**")
                st.dataframe(ind_p.round(6), hide_index=True, width="stretch")


def main() -> None:
    st.set_page_config(page_title="FMP Screener", layout="wide")
    st.title("FMP screener")
    st.caption(
        "Sector ETF dashboards (FMP). **Tabs:** the sector you pick runs first; other sectors preload "
        f"in the background. Cached bundles TTL {_CACHE_TTL_SECONDS}s (`@st.cache_data`)."
    )

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()

    sector_options = (
        "Technology Sector",
        "Basic Materials Sector",
        "Communication Services Sector",
        "Consumer Cyclical Sector",
        "Consumer Defensive Sector",
        "Energy Sector",
        "Financial Services Sector",
        "Healthcare Sector",
        "Industrials Sector",
        "Real Estate Sector",
        "Utilities Sector",
    )
    st.markdown("##### Sectors")
    page = st.radio(
        "Active sector",
        options=sector_options,
        horizontal=True,
        key="fmp_dashboard_sector_tab",
        label_visibility="collapsed",
        help="Selected sector loads immediately; others warm in a daemon thread when an API key is set.",
    )

    if page == "Technology Sector":
        render_technology_sector_tab()
    elif page == "Basic Materials Sector":
        render_basic_materials_sector_tab()
    elif page == "Communication Services Sector":
        render_communication_services_sector_tab()
    elif page == "Consumer Cyclical Sector":
        render_consumer_cyclical_sector_tab()
    elif page == "Consumer Defensive Sector":
        render_consumer_defensive_sector_tab()
    elif page == "Energy Sector":
        render_energy_sector_tab()
    elif page == "Financial Services Sector":
        render_financial_services_sector_tab()
    elif page == "Healthcare Sector":
        render_healthcare_sector_tab()
    elif page == "Industrials Sector":
        render_industrials_sector_tab()
    elif page == "Real Estate Sector":
        render_real_estate_sector_tab()
    elif page == "Utilities Sector":
        render_utilities_sector_tab()

    if api_key:
        _spawn_background_sector_warm(api_key, page)


if __name__ == "__main__":
    main()
