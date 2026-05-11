"""
Streamlit UI for a single sector tab: ETF trend, rotation vs sector ETF, vs SPY, risk, dispersion.

Used only by ``dashboard.py``; keeps the main dashboard file small.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import config
import data_loader
import dispersion_engine
import rotation_price_batch
import sector_pages
import spy_sector_rotation_engine

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class SectorTabSpec:
    """One row in the sector radio + matching FMP dispersion sector name."""

    page_radio_label: str
    display: str
    etf: str
    fmp_sector: str
    rotation_cache: Callable[[str, str], dict]
    rotation_instruments: str = "stocks and ETFs listed per row"


def _fmt_rel_pct(x: object) -> str:
    v = pd.to_numeric(x, errors="coerce")
    if v is None or pd.isna(v):
        return "N/A"
    return f"{float(v) * 100.0:+.2f}%"


def _fmt_vol_pct(x: object) -> str:
    v = pd.to_numeric(x, errors="coerce")
    if v is None or pd.isna(v):
        return "N/A"
    return f"{float(v) * 100.0:.2f}%"


def _dispersion_health_chart(disp_bundle: dict) -> pd.DataFrame:
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


def render_sector_tab(
    spec: SectorTabSpec,
    *,
    cached_sector_dispersion: Callable[[str, str, str], dict],
) -> None:
    """
    Render one sector: trend, rotation, vs SPY, risk, dispersion.

    ``cached_sector_dispersion`` is the dashboard's ``@st.cache_data`` wrapper
    (signature ``(api_key, fmp_sector_name, data_revision) -> bundle``).
    """
    etf = spec.etf
    label = spec.display
    fmp = spec.fmp_sector

    st.subheader(f"{label} Sector Analysis")

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        st.warning(
            f"FMP_API_KEY is not set (check `.env`). Live {etf} / SPY sections need a key; other tabs still work."
        )
        return

    try:
        session = data_loader.create_http_session()
    except Exception as e:
        st.warning(f"Could not create HTTP session: {e}")
        return

    # Rotation runs in a background thread while ETF trend is rendering.
    _rot_box: dict[str, object] = {}
    rot_fp = data_loader.price_cache_fingerprint(rotation_price_batch.dashboard_rotation_symbols())

    def _run_rotation() -> None:
        try:
            _rot_box["bundle"] = spec.rotation_cache(api_key, rot_fp)
        except Exception as e:  # pragma: no cover - network
            _rot_box["bundle"] = {"ok": False, "error": str(e)}

    # --- ETF trend ---
    st.subheader(f"{label} ETF Trend & Technicals")
    st.caption(f"This section analyzes {etf} on its own before comparing {label} against SPY.")

    trend_detail = pd.DataFrame()
    trend_summary: dict = {}
    try:
        trend_detail, trend_summary = sector_pages.get_sector_etf_trend_data(
            session, api_key, sector_etf=etf
        )
    except Exception as e:
        st.warning(f"Could not load {etf} trend data: {e}")

    if trend_detail.empty:
        st.warning(f"{etf} price history is missing or unavailable; trend & technicals section skipped.")
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
                    "price": f"{etf} price",
                    "dma_50": "50 DMA",
                    "dma_100": "100 DMA",
                    "dma_200": "200 DMA",
                }
            )
            st.markdown(f"**{etf} Price with Moving Averages**")
            st.line_chart(chart, height=460)

    _thr_rot = threading.Thread(target=_run_rotation, name="sector-rotation", daemon=True)
    _thr_rot.start()

    st.divider()
    st.subheader(f"{label} Industry Rotation")
    st.caption(
        f"Relative strength of equal-weight **proxy baskets** ({spec.rotation_instruments}) versus {etf}. "
        f"Positive values mean that basket is outperforming broad {label} over the selected window."
    )
    _thr_rot.join()

    rot_bundle = _rot_box.get(
        "bundle",
        {"ok": False, "error": "Industry rotation did not complete."},
    )
    if isinstance(rot_bundle, dict) and not rot_bundle.get("ok"):
        st.warning(
            rot_bundle.get("error") or f"{label} industry rotation unavailable (check FMP key and caches)."
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
                    st.caption(f"Leadership: **{top_lbl}** is leading {label} over the last 3 months.")
                else:
                    st.caption(f"No proxy basket is outperforming {etf} over the last 3 months.")
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
                st.markdown(f"**RS ratio vs {etf} (level, not % change)**")
                st.dataframe(rsh.tail(500).round(6), use_container_width=True)

    st.divider()
    st.subheader(f"{label} vs SPY")
    st.caption(
        f"{etf} ({label}) vs SPY (S&P 500). Dividend-adjusted closes from FMP; cached like other price pulls."
    )

    detail = pd.DataFrame()
    summary: dict = {}
    try:
        detail, summary = sector_pages.get_sector_vs_spy_data(
            session, api_key, sector_etf=etf, sector_name=fmp
        )
    except Exception as e:
        st.warning(f"Could not load sector vs benchmark data: {e}")

    if detail.empty:
        st.warning(
            f"Price data for {etf} and/or SPY is missing or could not be aligned. "
            "Check your API key and try again after a successful `get_price_history` fetch."
        )
    else:
        as_of = summary.get("as_of_date")
        etf_sym = str(summary.get("sector_etf", etf))
        bench = str(summary.get("benchmark", "SPY"))
        col_idx_etf = f"{etf_sym}_index"
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

        st.caption(f"Positive relative return means {label} is outperforming SPY over that window.")

        perf = detail[["date", col_idx_etf, col_idx_bench]].copy()
        perf["date"] = pd.to_datetime(perf["date"], errors="coerce")
        perf = perf.dropna(subset=["date"]).set_index("date")
        perf = perf.rename(
            columns={
                col_idx_etf: f"{etf_sym} (normalized)",
                col_idx_bench: f"{bench} (normalized)",
            }
        )
        st.markdown(f"**{etf_sym} vs SPY Cumulative Performance**")
        st.line_chart(perf, height=420)

        ratio = detail[["date", "relative_strength_ratio"]].copy()
        ratio["date"] = pd.to_datetime(ratio["date"], errors="coerce")
        ratio = ratio.dropna(subset=["date"]).set_index("date")
        ratio = ratio.rename(columns={"relative_strength_ratio": f"{etf_sym} / {bench} ratio"})
        st.markdown(f"**{etf_sym} / SPY Relative Strength Ratio**")
        st.line_chart(ratio, height=380)

    st.divider()
    st.subheader("Risk")
    st.caption(f"Realized volatility shows how unstable {etf} has been historically.")

    risk_summary: dict = {}
    dd_ts = pd.DataFrame()
    try:
        risk_summary, _, dd_ts = sector_pages.get_sector_risk_data(session, api_key, sector_etf=etf)
    except Exception as e:
        st.warning(f"Could not load {etf} risk data: {e}")

    if dd_ts.empty:
        st.warning(f"{etf} price history is missing; risk section skipped.")
    else:
        st.markdown(f"**As of:** `{risk_summary.get('as_of_date')}`")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trailing 1Y Vol", _fmt_vol_pct(risk_summary.get("trailing_1y_vol")))
        m2.metric("Trailing 3M Vol", _fmt_vol_pct(risk_summary.get("trailing_3m_vol")))
        m3.metric("Trailing 20D Vol", _fmt_vol_pct(risk_summary.get("trailing_20d_vol")))
        m4.metric("Trailing 1Y Max Drawdown", _fmt_rel_pct(risk_summary.get("trailing_1y_max_drawdown")))
        m5.metric("Current Drawdown vs 1Y High", _fmt_rel_pct(risk_summary.get("current_drawdown_1y_high")))

        st.markdown(f"**{etf} Drawdown Over Time**")
        dd_plot = dd_ts.copy()
        dd_plot["date"] = pd.to_datetime(dd_plot["date"], errors="coerce")
        dd_plot = dd_plot.dropna(subset=["date"]).set_index("date")
        st.line_chart(dd_plot[["drawdown"]], height=360)

    st.divider()
    st.subheader(f"{label} Internal Dispersion")
    st.caption(
        f"Breadth, cross-sectional dispersion, and concentration for the US {label} **dispersion** "
        f"universe (market cap > ${config.DISPERSION_MIN_MARKET_CAP/1e9:.1f}B, avg volume > "
        f"{config.DISPERSION_MIN_AVG_VOLUME/1e3:.0f}k, price > ${config.DISPERSION_MIN_PRICE:.0f}; "
        "ETFs/funds excluded). Prices are dividend-adjusted from `data_loader.get_price_history`."
    )
    disp_state_key = f"dispersion_loaded__{spec.page_radio_label}"
    if st.button("Load Internal Dispersion", key=f"load_dispersion_btn__{spec.page_radio_label}"):
        st.session_state[disp_state_key] = True
    if not bool(st.session_state.get(disp_state_key, False)):
        st.info("Internal dispersion is lazy-loaded. Click **Load Internal Dispersion** to run it.")
        return

    try:
        uni = dispersion_engine.build_dispersion_universe(session, api_key, sector=fmp, force_refresh_profiles=False)
        disp_syms = uni["symbol"].astype(str).tolist() if not uni.empty else []
        disp_rev = data_loader.dispersion_bundle_cache_revision(fmp, disp_syms)
        disp_bundle = cached_sector_dispersion(api_key, fmp, disp_rev)
    except Exception as e:  # pragma: no cover - network
        disp_bundle = {"ok": False, "error": str(e)}
    if not disp_bundle.get("ok"):
        st.warning(
            disp_bundle.get("error")
            or "Dispersion analytics unavailable (check FMP key and profile-bulk access)."
        )
    else:
        summ = disp_bundle.get("summary") or {}
        tables = disp_bundle.get("tables") or {}
        as_of_d = disp_bundle.get("as_of")
        st.markdown(
            f"**As of:** `{as_of_d}` — names below are the intersection of the dispersion universe and "
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
        st.markdown(f"**{label} Breadth & Internal Rotation**")
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
            f"Breadth measures participation across the {label} universe. A rising EW-CW σ spread means internal "
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


def render_spy_benchmark_tab(
    *,
    cached_spy_sector_rotation: Callable[[str, str], dict],
) -> None:
    """
    SPY as the headline ETF: trend, sector ETFs vs SPY rotation heatmap, RSP vs SPY, SPY risk.

    No internal dispersion block.
    """
    etf = "SPY"
    label = "S&P 500"

    st.subheader(f"{label} ({etf}) overview")

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        st.warning(
            "FMP_API_KEY is not set (check `.env`). Live SPY / RSP / sector ETF sections need a key."
        )
        return

    try:
        session = data_loader.create_http_session()
    except Exception as e:
        st.warning(f"Could not create HTTP session: {e}")
        return

    _rot_box: dict[str, object] = {}
    rot_fp = data_loader.price_cache_fingerprint(spy_sector_rotation_engine.all_rotation_symbols())

    def _run_rotation() -> None:
        try:
            _rot_box["bundle"] = cached_spy_sector_rotation(api_key, rot_fp)
        except Exception as e:  # pragma: no cover - network
            _rot_box["bundle"] = {"ok": False, "error": str(e)}

    # --- SPY trend ---
    st.subheader("SPY trend & technicals")
    st.caption("Cap-weight S&P 500 proxy (SPY), dividend-adjusted.")

    trend_detail = pd.DataFrame()
    trend_summary: dict = {}
    try:
        trend_detail, trend_summary = sector_pages.get_sector_etf_trend_data(
            session, api_key, sector_etf=etf
        )
    except Exception as e:
        st.warning(f"Could not load {etf} trend data: {e}")

    if trend_detail.empty:
        st.warning(f"{etf} price history is missing or unavailable; trend section skipped.")
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
                    "price": f"{etf} price",
                    "dma_50": "50 DMA",
                    "dma_100": "100 DMA",
                    "dma_200": "200 DMA",
                }
            )
            st.markdown(f"**{etf} price with moving averages**")
            st.line_chart(chart, height=460)

    _thr_rot = threading.Thread(target=_run_rotation, name="spy-sector-rotation", daemon=True)
    _thr_rot.start()

    st.divider()
    st.subheader("Sector rotation vs SPY")
    st.caption(
        "Each row is a sector ETF from the dashboard map; values are RS vs SPY (rebased ratio) over "
        "the window—same convention as industry rotation heatmaps. Positive means that sector ETF "
        "outperformed SPY."
    )
    _thr_rot.join()

    rot_bundle = _rot_box.get(
        "bundle",
        {"ok": False, "error": "Sector rotation did not complete."},
    )
    if isinstance(rot_bundle, dict) and not rot_bundle.get("ok"):
        st.warning(rot_bundle.get("error") or "Sector rotation unavailable (check FMP key and caches).")
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
        else:
            st.info("No sector rotation heatmap to display.")

        with st.expander("Show sector rotation detail"):
            met_r = rot_bundle.get("metrics")
            if isinstance(met_r, pd.DataFrame) and not met_r.empty:
                st.markdown("**RS metrics (decimals)**")
                st.dataframe(met_r.round(4), use_container_width=True, hide_index=True)
            px_r = rot_bundle.get("prices")
            if isinstance(px_r, pd.DataFrame) and not px_r.empty:
                st.markdown("**Prices (long format)**")
                st.dataframe(
                    px_r.sort_values(["date", "symbol"], ascending=[False, True]),
                    use_container_width=True,
                    hide_index=True,
                )
            rsh = rot_bundle.get("rs_ratio_history")
            if isinstance(rsh, pd.DataFrame) and not rsh.empty:
                st.markdown("**RS ratio vs SPY (level)**")
                st.dataframe(rsh.tail(500).round(6), use_container_width=True)

    st.divider()
    st.subheader("RSP vs SPY")
    st.caption(
        "Equal-weight S&P 500 (RSP) vs cap-weight SPY. Dividend-adjusted closes; relative metrics are "
        "RSP return minus SPY return over each window."
    )

    detail = pd.DataFrame()
    summary: dict = {}
    try:
        detail, summary = sector_pages.get_sector_vs_spy_data(
            session, api_key, sector_etf="RSP", benchmark="SPY", sector_name="Equal-weight S&P 500"
        )
    except Exception as e:
        st.warning(f"Could not load RSP vs SPY data: {e}")

    if detail.empty:
        st.warning("Price data for RSP and/or SPY is missing or could not be aligned.")
    else:
        as_of = summary.get("as_of_date")
        etf_sym = str(summary.get("sector_etf", "RSP"))
        bench = str(summary.get("benchmark", "SPY"))
        col_idx_etf = f"{etf_sym}_index"
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

        st.caption("Positive relative return means RSP outperformed SPY over that window.")

        perf = detail[["date", col_idx_etf, col_idx_bench]].copy()
        perf["date"] = pd.to_datetime(perf["date"], errors="coerce")
        perf = perf.dropna(subset=["date"]).set_index("date")
        perf = perf.rename(
            columns={
                col_idx_etf: f"{etf_sym} (normalized)",
                col_idx_bench: f"{bench} (normalized)",
            }
        )
        st.markdown("**RSP vs SPY cumulative performance**")
        st.line_chart(perf, height=420)

        ratio = detail[["date", "relative_strength_ratio"]].copy()
        ratio["date"] = pd.to_datetime(ratio["date"], errors="coerce")
        ratio = ratio.dropna(subset=["date"]).set_index("date")
        ratio = ratio.rename(columns={"relative_strength_ratio": f"{etf_sym} / {bench} ratio"})
        st.markdown("**RSP / SPY relative strength ratio**")
        st.line_chart(ratio, height=380)

    st.divider()
    st.subheader("SPY risk")
    st.caption("Realized volatility and drawdowns for SPY.")

    risk_summary: dict = {}
    dd_ts = pd.DataFrame()
    try:
        risk_summary, _, dd_ts = sector_pages.get_sector_risk_data(session, api_key, sector_etf=etf)
    except Exception as e:
        st.warning(f"Could not load {etf} risk data: {e}")

    if dd_ts.empty:
        st.warning(f"{etf} price history is missing; risk section skipped.")
    else:
        st.markdown(f"**As of:** `{risk_summary.get('as_of_date')}`")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trailing 1Y Vol", _fmt_vol_pct(risk_summary.get("trailing_1y_vol")))
        m2.metric("Trailing 3M Vol", _fmt_vol_pct(risk_summary.get("trailing_3m_vol")))
        m3.metric("Trailing 20D Vol", _fmt_vol_pct(risk_summary.get("trailing_20d_vol")))
        m4.metric("Trailing 1Y Max Drawdown", _fmt_rel_pct(risk_summary.get("trailing_1y_max_drawdown")))
        m5.metric("Current Drawdown vs 1Y High", _fmt_rel_pct(risk_summary.get("current_drawdown_1y_high")))

        st.markdown(f"**{etf} drawdown over time**")
        dd_plot = dd_ts.copy()
        dd_plot["date"] = pd.to_datetime(dd_plot["date"], errors="coerce")
        dd_plot = dd_plot.dropna(subset=["date"]).set_index("date")
        st.line_chart(dd_plot[["drawdown"]], height=360)
