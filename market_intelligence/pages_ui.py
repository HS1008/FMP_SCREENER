"""Renderers for the DB-only Market Intelligence pages (Streamlit).

Each ``render_*`` reads through :func:`market_intelligence.ui.load_or_stop` (read-only
role, cached) and renders populated / empty / stale / unconfigured / partial states without
fabricating numbers. Missing values render as "—".
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from market_intelligence.catalog import CATALOG_BY_ID, CURVE_TENORS
from market_intelligence.nulls import strict_dumps
from market_intelligence.sector_mapping import CANONICAL_SECTORS
from market_intelligence.ui import (
    age_text,
    fmt,
    fmt_signed,
    freshness_chip,
    heatmap_legend,
    history_chart,
    load_or_stop,
    page_header,
    styled_heatmap,
    transport_chip,
)

CATEGORY_TITLES = {
    "growth": "Growth",
    "labor": "Labor",
    "inflation": "Inflation",
    "policy": "Policy rates",
    "rates": "Rates",
    "liquidity": "Liquidity",
    "credit": "Credit",
}
TRANSFORM_LABELS = {
    "yoy_pct": "YoY",
    "ann3m_pct": "3M annualized",
    "ann6m_pct": "6M annualized",
    "mom_pct": "MoM",
    "qoq_saar_pct": "QoQ SAAR",
    "mom_change": "MoM change",
    "mom_change_pp": "MoM change (pp)",
    "yoy_change_pp": "YoY change (pp)",
    "wow_change": "WoW change",
    "chg_4w": "4W change",
    "avg_4w": "4W average",
    "chg_prev": "vs prior obs",
    "chg_1w": "1W change",
    "chg_prev_bps": "vs prior session",
    "chg_1w_bps": "1W",
    "chg_1m_bps": "1M",
    "chg_3m_bps": "3M",
    "level_pct": "Level",
    "oas_bps": "OAS",
}


def _transform_text(entry: dict[str, Any] | None) -> str:
    if not entry:
        return "—"
    text = fmt_signed(entry.get("value"), entry.get("units")) if entry.get("units") in {"bps", "pct", "pp", "fraction"} else fmt(entry.get("value"), entry.get("units"))
    if entry.get("status") not in (None, "OK"):
        text += " ({0})".format(entry.get("status").lower().replace("_", " "))
    return text


def _sources_summary(health: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for h in health:
        rows.append(
            {
                "Source": h.get("source_id"),
                "Dataset": h.get("freshness_dataset") or h.get("dataset"),
                "Enabled": "yes" if h.get("enabled") else "no",
                "Access": h.get("access_status"),
                "Cadence": h.get("dataset_cadence") or "—",
                "Transport": transport_chip(h.get("transport_status")),
                "Freshness (now)": freshness_chip(h.get("freshness_status")),
                "Latest obs": h.get("latest_observation_date") or "—",
                "Last success": age_text(h.get("last_success_at")),
            }
        )
    return pd.DataFrame(rows)


# ---- Market Pulse ---------------------------------------------------------------------------

def render_market_pulse() -> None:
    page_header("Market Pulse", "Source health plus the latest available macro, rates, credit and sector changes. PostgreSQL read-only; no provider calls.")
    health = load_or_stop("source_health")
    rates = load_or_stop("rates_context")
    credit = load_or_stop("credit_context")
    sectors = load_or_stop("sectors_context")
    macro = load_or_stop("macro_context")

    st.subheader("Source health")
    top_level = [h for h in health if h.get("freshness_dataset") in (None, "fred_series_observations", "precomputed_sector_bundles", h.get("dataset"))]
    if not top_level:
        st.info("No sources registered. Run `python -m jobs.market_intelligence_refresh --all-configured` on the backend.")
    else:
        st.dataframe(_sources_summary(top_level), use_container_width=True, hide_index=True)

    st.subheader("Overnight")
    st.info("Overnight quotes unavailable: no live quote source is configured. Prior-session closes are not labeled as overnight.")

    st.subheader("Rates")
    curve = [c for c in rates.get("curve", []) if c.get("yield_pct") is not None]
    if not curve:
        st.info("No Treasury curve data stored.")
    else:
        cols = st.columns(min(4, len(curve)))
        for i, c in enumerate([x for x in curve if x["tenor"] in {"3M", "2Y", "10Y", "30Y"}][:4]):
            with cols[i % len(cols)]:
                st.metric("{0} Treasury ({1})".format(c["tenor"], c["observation_date"]), fmt(c["yield_pct"], "pct"), fmt_signed(c.get("chg_prev_bps"), "bps") if c.get("chg_prev_bps") is not None else None, help="Yield in percent; delta vs prior session in basis points.")
        slopes = rates.get("slopes") or {}
        slope_cols = st.columns(4)
        for i, (name, entry) in enumerate(slopes.items()):
            with slope_cols[i % 4]:
                st.metric("Slope {0}".format(name.replace("Y", "Y-", 1) if name[0].isdigit() else name), _transform_text(entry), help="Long minus short tenor on a common observation date, in bps.")
        if rates.get("curve_dates_mixed"):
            st.warning("Curve tenors carry different observation dates: {0}".format(", ".join(rates.get("curve_observation_dates", []))))

    st.subheader("Credit (internal view)")
    buckets = credit.get("buckets") or []
    if not buckets:
        st.info("No credit index snapshots stored.")
    else:
        broad = [b for b in buckets if b["bucket"] in {"ig_broad", "hy_broad"}]
        cols = st.columns(max(1, len(broad)))
        for i, b in enumerate(broad):
            with cols[i]:
                st.metric("{0} ({1})".format(b["label"], b["as_of"]), fmt(b["oas_bps"], "bps").replace("+", ""), fmt_signed(b.get("change_1d_bps"), "bps") if b.get("change_1d_bps") is not None else None, help="OAS in bps; 1D change only when the prior observation is one session earlier.")
        st.caption(credit.get("attribution") or "")

    st.subheader("Inflation & labor")
    cats = macro.get("categories") or {}
    quick = []
    for cat in ("inflation", "labor", "growth"):
        for block in cats.get(cat, []):
            transforms = block.get("transforms") or {}
            headline = transforms.get("yoy_pct") or transforms.get("mom_change") or transforms.get("qoq_saar_pct") or transforms.get("wow_change")
            quick.append({"Series": block.get("label"), "Latest": fmt(block["latest"].get("value"), None), "Units": block["latest"].get("units"), "Obs date": block["latest"].get("observation_date"), "Headline": _transform_text(headline), "Headline kind": next((TRANSFORM_LABELS.get(k) for k in ("yoy_pct", "mom_change", "qoq_saar_pct", "wow_change") if k in transforms), "—")})
    if quick:
        st.dataframe(pd.DataFrame(quick), use_container_width=True, hide_index=True)
    else:
        st.info("No macro observations stored.")

    st.subheader("Sector leadership (1M relative strength vs SPY)")
    rs_rows = (sectors.get("datasets") or {}).get("ETF_RS_VS_SPY") or []
    if not rs_rows:
        st.info("No sector rotation snapshots stored (legacy bridge not run).")
    else:
        frame = pd.DataFrame(
            [{"Sector / theme": r["sector_key"], "ETF": r["instrument_id"], "As of": r["as_of"], "1W RS": (r["metrics"] or {}).get("rs_chg_1w"), "1M RS": (r["metrics"] or {}).get("rs_chg_1m"), "3M RS": (r["metrics"] or {}).get("rs_chg_3m"), "1M return": (r["metrics"] or {}).get("ret_1m")} for r in rs_rows]
        ).sort_values("1M RS", ascending=False, na_position="last")
        st.dataframe(styled_heatmap(frame, ["1W RS", "1M RS", "3M RS", "1M return"]), use_container_width=True, hide_index=True)
        heatmap_legend()
        st.caption("Relative strength = change in the ETF/SPY adjusted-close ratio (not an arithmetic excess return). Source: FMP legacy bundles.")


# ---- Macro Overview ---------------------------------------------------------------------------

def render_macro_overview() -> None:
    page_header("Macro Overview", "Growth, labor, inflation, policy, liquidity and credit from canonical PostgreSQL. Latest values, versioned transforms, history.")
    macro = load_or_stop("macro_context")
    credit = load_or_stop("credit_context")
    cats = macro.get("categories") or {}
    if not cats:
        st.info("No FRED observations stored yet. Configure `FRED_API_KEY` on the backend and run `python -m jobs.market_intelligence_refresh --fred --build-analytics`.")
        return
    missing = macro.get("series_without_data") or []
    if missing:
        st.warning("{0} catalog series have no stored data: {1}".format(len(missing), ", ".join(missing[:12]) + (" …" if len(missing) > 12 else "")))
    for cat in ("growth", "labor", "inflation", "policy", "rates", "liquidity"):
        blocks = cats.get(cat) or []
        if not blocks:
            continue
        st.subheader(CATEGORY_TITLES.get(cat, cat.title()))
        rows = []
        for block in blocks:
            transforms = block.get("transforms") or {}
            row = {"Series": block.get("label") or block["series_id"], "ID": block["series_id"], "Latest": fmt(block["latest"].get("value"), None), "Units": block["latest"].get("units") or block.get("catalog_units"), "Obs date": block["latest"].get("observation_date"), "Freq": block.get("frequency"), "SA": block.get("seasonal_adjustment"), "Aggregation": block.get("aggregation") or "—", "Vintage": block.get("vintage_kind")}
            display = transforms.get("level_display")
            if display is not None and display.get("value") is not None:
                row["Display"] = "{0} {1}".format(fmt(display.get("value"), None), display.get("units") or "")
            for key in ("yoy_pct", "ann3m_pct", "ann6m_pct", "mom_pct", "qoq_saar_pct", "mom_change", "mom_change_pp", "yoy_change_pp", "wow_change", "chg_4w", "avg_4w", "chg_prev_bps", "chg_1w_bps", "chg_1m_bps", "chg_3m_bps"):
                if key in transforms:
                    row[TRANSFORM_LABELS.get(key, key)] = _transform_text(transforms[key])
            if block.get("publication_status") and block.get("publication_status") != "PUBLISHED":
                row["Publication"] = "{0} ({1})".format(block.get("publication_status"), block.get("metadata_status"))
            rows.append(row)
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        quarantined = [b["series_id"] for b in blocks if b.get("publication_status") and b.get("publication_status") != "PUBLISHED"]
        if quarantined:
            st.warning("Metadata gate: {0} show the last validated data only; the latest retrieval was quarantined (see Data Health).".format(", ".join(quarantined)))
        if cat == "liquidity":
            st.caption("Balances differ in dating (Wednesday levels for WALCL vs week averages ending Wednesday for WTREGEN/WRESBAL vs daily RRPONTSYD) and scale (provider units are millions or billions; 'Display' is an explicit versioned conversion, the stored level keeps provider units); no composite liquidity score is computed.")
        if cat == "inflation":
            st.caption("YoY = 100·(I_t/I_{t−12} − 1); 3M annualized = 100·((I_t/I_{t−3})⁴ − 1); 6M annualized = 100·((I_t/I_{t−6})² − 1); exact calendar alignment, no forward fill.")
    buckets = credit.get("buckets") or []
    if buckets:
        st.subheader("Credit")
        st.dataframe(pd.DataFrame([{"Bucket": b["label"], "As of": b["as_of"], "OAS (bps)": fmt(b["oas_bps"], None, digits=0), "1D": fmt_signed(b["change_1d_bps"], "bps"), "1W": fmt_signed(b["change_1w_bps"], "bps"), "1M": fmt_signed(b["change_1m_bps"], "bps"), "Window": b["percentile_window"] or "—", "Percentile": fmt(b["percentile"], "pctile"), "History": b["history_status"]} for b in buckets]), use_container_width=True, hide_index=True)
        st.caption(credit.get("attribution") or "")

    st.subheader("History")
    all_ids = sorted({b["series_id"] for blocks in cats.values() for b in blocks})
    chosen = st.selectbox("Series", all_ids, index=all_ids.index("CPIAUCSL") if "CPIAUCSL" in all_ids else 0, format_func=lambda s: "{0} — {1}".format(s, CATALOG_BY_ID[s].label if s in CATALOG_BY_ID else s))
    history = load_or_stop("observation_history", chosen)
    spec = CATALOG_BY_ID.get(chosen)
    history_chart(history, x="observation_date", y="value", title="{0} ({1})".format(spec.label if spec else chosen, chosen), units=(spec.expected_units_contains[0] if spec and spec.expected_units_contains else None))
    if spec:
        st.caption("Source: {0} · {1}".format(spec.source_url, spec.notes or ""))


# ---- Rates & Curve ---------------------------------------------------------------------------

def render_rates_curve() -> None:
    page_header("Rates & Curve", "Treasury nominal curve, real yields, inflation compensation and slopes. Yields in percent; changes in basis points.")
    rates = load_or_stop("rates_context")
    curve = rates.get("curve") or []
    present = [c for c in curve if c.get("yield_pct") is not None]
    if not present:
        st.info("No Treasury curve observations stored. Run the FRED refresh on the backend.")
        return
    if rates.get("curve_dates_mixed"):
        st.warning("Tenors have different latest observation dates ({0}); the curve is shown per tenor date, not mixed.".format(", ".join(rates["curve_observation_dates"])))
    frame = pd.DataFrame(present)
    frame["tenor_order"] = frame["tenor"].map({t: i for i, t in enumerate(CURVE_TENORS)})
    frame = frame.sort_values("tenor_order")
    try:
        import plotly.graph_objects as go

        fig = go.Figure(go.Scatter(x=frame["tenor"], y=frame["yield_pct"], mode="lines+markers", name="Yield (%)"))
        fig.update_layout(height=340, margin=dict(l=10, r=10, t=30, b=10), yaxis_title="percent")
        st.plotly_chart(fig, use_container_width=True)
    except ImportError:  # pragma: no cover
        st.line_chart(frame.set_index("tenor")["yield_pct"])
    table = pd.DataFrame(
        [{"Tenor": c["tenor"], "Yield (%)": fmt(c["yield_pct"], "pct"), "Obs date": c["observation_date"], "vs prior (bps)": fmt_signed(c.get("chg_prev_bps"), "bps"), "1W (bps)": fmt_signed(c.get("chg_1w_bps"), "bps"), "1M (bps)": fmt_signed(c.get("chg_1m_bps"), "bps"), "3M (bps)": fmt_signed(c.get("chg_3m_bps"), "bps")} for c in present]
    )
    st.dataframe(table, use_container_width=True, hide_index=True)
    missing_tenors = [c["tenor"] for c in curve if c.get("yield_pct") is None]
    if missing_tenors:
        st.caption("Tenors without data (not extrapolated): {0}".format(", ".join(missing_tenors)))

    st.subheader("Slopes")
    slopes = rates.get("slopes") or {}
    cols = st.columns(4)
    for i, (name, entry) in enumerate(slopes.items()):
        with cols[i % 4]:
            st.metric(name, _transform_text(entry), help="Long minus short on a common observation date; missing legs reported, never mixed dates.")
            if entry and entry.get("comparison", {}).get("missing_legs"):
                st.caption("Missing legs: {0}".format(", ".join(entry["comparison"]["missing_legs"])))

    for title, key in (("Real yields (TIPS)", "real_yields"), ("Inflation compensation (breakevens — market-implied, not survey)", "inflation_compensation"), ("Policy rates", "policy")):
        blocks = rates.get(key) or []
        if not blocks:
            continue
        st.subheader(title)
        st.dataframe(pd.DataFrame([{"Series": b.get("label"), "ID": b["series_id"], "Level (%)": fmt(b["latest"].get("value"), "pct"), "Obs date": b["latest"].get("observation_date"), "vs prior (bps)": _transform_text((b.get("transforms") or {}).get("chg_prev_bps")), "1W (bps)": _transform_text((b.get("transforms") or {}).get("chg_1w_bps")), "1M (bps)": _transform_text((b.get("transforms") or {}).get("chg_1m_bps"))} for b in blocks]), use_container_width=True, hide_index=True)
    st.caption(rates.get("units_note") or "")


# ---- Credit Overview -------------------------------------------------------------------------

def render_credit_overview() -> None:
    page_header("Credit Overview", "ICE BofA option-adjusted spreads via FRED: IG/HY and rating buckets, changes, and available-window distributions. Internal view; redistribution restricted.")
    credit = load_or_stop("credit_context")
    buckets = credit.get("buckets") or []
    if not buckets:
        st.info("No credit index snapshots stored. Run the FRED refresh with analytics on the backend.")
        return
    broad = [b for b in buckets if b["bucket"] in {"ig_broad", "hy_broad"}]
    cols = st.columns(max(1, len(broad)))
    for i, b in enumerate(broad):
        with cols[i]:
            st.metric("{0} ({1})".format(b["label"], b["as_of"]), "{0:.0f} bps".format(b["oas_bps"]) if b.get("oas_bps") is not None else "—", fmt_signed(b.get("change_1d_bps"), "bps") if b.get("change_1d_bps") is not None else None)
    st.dataframe(
        pd.DataFrame([{"Bucket": b["label"], "Series": b["series_id"], "As of": b["as_of"], "OAS (bps)": fmt(b["oas_bps"], None, digits=0), "1D": fmt_signed(b["change_1d_bps"], "bps"), "1W": fmt_signed(b["change_1w_bps"], "bps"), "1M": fmt_signed(b["change_1m_bps"], "bps"), "3M": fmt_signed(b["change_3m_bps"], "bps"), "Window": b["percentile_window"] or "—", "Percentile": fmt(b["percentile"], "pctile"), "Z-score": fmt(b["zscore"], None), "Obs in window": b["window_observations"], "History from": b["history_first_date"], "History": b["history_status"]} for b in buckets]),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(credit.get("coverage_note") or "")
    st.caption(credit.get("attribution") or "")
    st.subheader("History")
    ids = [b["series_id"] for b in buckets]
    chosen = st.selectbox("Series", ids, format_func=lambda s: CATALOG_BY_ID[s].label if s in CATALOG_BY_ID else s)
    history = load_or_stop("metric_history", "{0}.oas_bps".format(chosen))
    history_chart(history, x="as_of", y="value", title="{0} OAS (bps)".format(CATALOG_BY_ID[chosen].label if chosen in CATALOG_BY_ID else chosen), units="bps")


# ---- Sector Rotation V2 ----------------------------------------------------------------------

def render_sector_rotation_v2() -> None:
    page_header("Sector Rotation V2", "Source-aware sector heatmap from canonical snapshots (legacy FMP bridge today; QC diagnostics when published). No live provider calls.", fred=False)
    sectors = load_or_stop("sectors_context")
    datasets = sectors.get("datasets") or {}
    if not datasets:
        st.info("No sector snapshots stored. Run `python -m jobs.ingest_legacy_sector_precomputed` on the backend after the nightly bundles exist.")
        return
    rs_rows = datasets.get("ETF_RS_VS_SPY") or []
    if rs_rows:
        benchmarks = sorted({r.get("benchmark") for r in rs_rows if r.get("benchmark")})
        benchmark = st.selectbox("Benchmark (stored, compatible metrics only)", benchmarks) if len(benchmarks) > 1 else (benchmarks[0] if benchmarks else None)
        rows = [r for r in rs_rows if r.get("benchmark") == benchmark]
        as_ofs = sorted({r["as_of"] for r in rows if r.get("as_of")})
        st.caption("Relative strength vs {0} · as of {1} · basis: {2} · values are fractions shown as percent".format(benchmark, ", ".join(as_ofs), rows[0].get("return_basis") if rows else "—"))
        frame = pd.DataFrame(
            [{"Sector / theme": r["sector_key"], "Kind": r["entity_kind"], "ETF": r["instrument_id"], "1W RS": (r["metrics"] or {}).get("rs_chg_1w"), "1M RS": (r["metrics"] or {}).get("rs_chg_1m"), "3M RS": (r["metrics"] or {}).get("rs_chg_3m"), "6M RS": (r["metrics"] or {}).get("rs_chg_6m"), "12M RS": (r["metrics"] or {}).get("rs_chg_12m"), "RS vs 50DMA": (r["metrics"] or {}).get("rs_vs_50dma"), "RS vs 200DMA": (r["metrics"] or {}).get("rs_vs_200dma")} for r in rows]
        )
        order = {s: i for i, s in enumerate(CANONICAL_SECTORS)}
        frame["_o"] = frame["Sector / theme"].map(lambda s: order.get(s, 99))
        frame = frame.sort_values(["_o", "Sector / theme"]).drop(columns="_o")
        value_cols = ["1W RS", "1M RS", "3M RS", "6M RS", "12M RS", "RS vs 50DMA", "RS vs 200DMA"]
        st.dataframe(styled_heatmap(frame, value_cols), use_container_width=True, hide_index=True)
        heatmap_legend()
        st.subheader("ETF trend & risk (from bundle prices)")
        trend = pd.DataFrame(
            [
                {
                    "Sector / theme": r["sector_key"],
                    "ETF": r["instrument_id"],
                    "1M return": (r["metrics"] or {}).get("ret_1m"),
                    "3M return": (r["metrics"] or {}).get("ret_3m"),
                    "12M return": (r["metrics"] or {}).get("ret_12m"),
                    "vs 50DMA": (r["metrics"] or {}).get("pct_vs_50dma"),
                    "vs 200DMA": (r["metrics"] or {}).get("pct_vs_200dma"),
                    "Vol 63d (ann)": (r["metrics"] or {}).get("vol_63d_ann"),
                    "Max DD 252d": (r["metrics"] or {}).get("max_drawdown_252d"),
                    "DD coverage": ((r.get("coverage") or {}).get("price_metrics") or {}).get("max_drawdown_252d"),
                    "Price status": (r.get("coverage") or {}).get("price_status"),
                    "Last price": (r.get("coverage") or {}).get("last_price_date"),
                }
                for r in rows
            ]
        )
        st.dataframe(styled_heatmap(trend, ["1M return", "3M return", "12M return", "vs 50DMA", "vs 200DMA"]), use_container_width=True, hide_index=True)
        stale = [r["instrument_id"] for r in rows if (r.get("coverage") or {}).get("price_status") == "STALE"]
        if stale:
            st.warning("Stale instruments (last price before the bundle as_of): {0}. Their windowed metrics are NULL rather than relabelled current.".format(", ".join(str(s) for s in stale)))
        st.caption("ETF returns on FMP adjusted close (dividend-adjusted, price-ratio basis); these are ETF returns, not constituent portfolio returns. Windows are counted on the bundle session calendar; a metric is blank unless its full window is present (PARTIAL = a few NULL sessions inside the window).")
    disp = datasets.get("CONSTITUENT_DISPERSION") or []
    if disp:
        st.subheader("Breadth, dispersion & concentration (current universe, context only)")
        st.warning("Constituent metrics use the FMP profile-bulk *current* universe and current market caps: CURRENT_UNIVERSE_CONTEXT_ONLY, research-ineligible, not point-in-time.")
        st.dataframe(
            pd.DataFrame([{"Sector": r["sector_key"], "As of": r["as_of"], "Universe": (r.get("coverage") or {}).get("universe_size"), "Valid 200DMA": (r.get("coverage") or {}).get("count_valid_200dma"), "Stale constituents": (r.get("coverage") or {}).get("stale_constituents"), "Denominator": (r.get("coverage") or {}).get("denominator_status"), "% > 50DMA": fmt((r["metrics"] or {}).get("pct_above_50dma"), "fraction").replace("+", ""), "% > 200DMA": fmt((r["metrics"] or {}).get("pct_above_200dma"), "fraction").replace("+", ""), "EW std": fmt((r["metrics"] or {}).get("equal_weight_std"), None, digits=3), "CW std": fmt((r["metrics"] or {}).get("cap_weight_std"), None, digits=3), "Median 1M ret": fmt_signed((r["metrics"] or {}).get("median_return_1m"), "fraction"), "Top5 weight": fmt((r["metrics"] or {}).get("top5_weight"), "fraction").replace("+", ""), "HHI": fmt((r["metrics"] or {}).get("hhi"), None, digits=3)} for r in disp]),
            use_container_width=True,
            hide_index=True,
        )
    st.subheader("Industry detail")
    industries = load_or_stop("industries_context")
    ind_datasets = industries.get("datasets") or {}
    if not ind_datasets:
        st.info("No industry snapshots stored.")
        return
    ds = st.selectbox("Industry dataset", sorted(ind_datasets))
    parents = sorted(ind_datasets[ds])
    parent = st.selectbox("Sector / theme", parents)
    items = ind_datasets[ds][parent]
    if ds in {"INDUSTRY_RS_VS_SECTOR_ETF", "THEME_RS"}:
        frame = pd.DataFrame([{"Industry": r["industry_key"], "ETF": r["instrument_id"], "As of": r["as_of"], "Benchmark": r["benchmark"], "1W RS": (r["metrics"] or {}).get("rs_chg_1w"), "1M RS": (r["metrics"] or {}).get("rs_chg_1m"), "3M RS": (r["metrics"] or {}).get("rs_chg_3m"), "RS vs 50DMA": (r["metrics"] or {}).get("rs_vs_50dma"), "RS vs 200DMA": (r["metrics"] or {}).get("rs_vs_200dma")} for r in items])
        st.dataframe(styled_heatmap(frame, ["1W RS", "1M RS", "3M RS", "RS vs 50DMA", "RS vs 200DMA"]), use_container_width=True, hide_index=True)
        st.caption("Industry ETF baskets are proxies, not exact mutually exclusive classifications.")
    else:
        frame = pd.DataFrame([{"Industry": r["industry_key"], "As of": r["as_of"], "Companies": (r["metrics"] or {}).get("company_count"), "EW 1M": (r["metrics"] or {}).get("equal_weight_return_1m"), "CW 1M (current caps)": (r["metrics"] or {}).get("cap_weight_return_1m"), "% > 50DMA": (r["metrics"] or {}).get("pct_above_50dma"), "% > 200DMA": (r["metrics"] or {}).get("pct_above_200dma")} for r in items])
        st.dataframe(styled_heatmap(frame, ["EW 1M", "CW 1M (current caps)"]), use_container_width=True, hide_index=True)
    heatmap_legend()


# ---- PIT Sector Internals ---------------------------------------------------------------------

def render_pit_sector_internals() -> None:
    page_header("PIT Sector Internals", "Decision-time sector aggregates from hash-verified sector_internals_v1 artifacts (isolated quant-strategies producer). Pre-holdout only. No live provider calls, no constituent data.", fred=False)
    ctx = load_or_stop("pit_sector_context")
    if not ctx.get("available"):
        reason = ctx.get("reason") or "NO_ARTIFACT_INGESTED"
        if reason == "MIGRATION_PENDING":
            st.info("No PIT sector views yet (migration 014 pending). Nothing is fabricated.")
        else:
            st.info("No sector_internals_v1 artifact ingested. Build one locally with the quant-strategies producer, then run `python -m jobs.ingest_pit_sector_internals --artifact <file>` on the backend.")
        return
    latest = ctx.get("latest") or []
    artifacts = ctx.get("artifacts") or []
    provenances = sorted({str(r.get("provenance")) for r in latest})
    if any(p not in {"REAL_QC", "LOCAL_LICENSED", "REAL_HISTORICAL_PRE_2025"} for p in provenances):
        st.warning("Provenance {0}: research_eligible = FALSE. Synthetic/test artifacts are never research evidence; they demonstrate the consumer path only.".format(", ".join(provenances)))
    boundaries = sorted({str(a.get("effective_holdout_start")) for a in artifacts if a.get("effective_holdout_start")})
    st.caption("Method {0} · latest decision date {1} · effective holdout boundary {2} · every stored row is dated strictly before the boundary.".format(", ".join(sorted({str(r.get("method_version")) for r in latest})), max(str(r.get("decision_date")) for r in latest), ", ".join(boundaries) or "—"))
    k = latest[0].get("return_sessions") if latest else None
    frame = pd.DataFrame(
        [
            {
                "Sector": r.get("sector"),
                "Decision date": r.get("decision_date"),
                "Members": r.get("constituent_count"),
                "Priced": r.get("priced_count"),
                "% > 20d": r.get("pct_above_20d"),
                "% > 50d": r.get("pct_above_50d"),
                "% > 100d": r.get("pct_above_100d"),
                "% > 200d": r.get("pct_above_200d"),
                "Median {0}d".format(k): r.get("median_return"),
                "EW {0}d".format(k): r.get("ew_return"),
                "CW {0}d".format(k): r.get("cw_return"),
                "EW-CW": r.get("ew_minus_cw"),
                "Held EW {0}d".format(k): r.get("held_ew_return"),
                "Dispersion": fmt(r.get("dispersion"), None, digits=4),
                "Return n": r.get("return_denominator"),
                "CW status": r.get("cw_status"),
                "Held status": r.get("held_status"),
                "HHI": fmt(r.get("hhi_cap"), None, digits=3),
                "Top5 share": fmt(r.get("top5_cap_share"), "fraction").replace("+", ""),
                "Conc. status": r.get("concentration_status"),
                "Rev": r.get("revision_seq"),
            }
            for r in latest
        ]
    )
    value_cols = ["% > 20d", "% > 50d", "% > 100d", "% > 200d", "Median {0}d".format(k), "EW {0}d".format(k), "CW {0}d".format(k), "EW-CW", "Held EW {0}d".format(k)]
    st.dataframe(styled_heatmap(frame, value_cols), use_container_width=True, hide_index=True)
    heatmap_legend()
    st.caption("Breadth = share of decision-time members above their own W-session simple moving average (denominator = members with full W-session history). Trailing returns are statistics of *current* decision-time members; 'Held EW' is the equal-weight portfolio formed at d-K from members known then (reclassification/universe exit = last still-member close; inferred delisting without declared proceeds NULLs the held return). CW uses point-in-time caps at the window start and is NULL when cap coverage is insufficient; it is never substituted with current caps.")
    st.subheader("History (stored rows, current revisions)")
    history = ctx.get("history") or {}
    if history:
        sector = st.selectbox("Sector", sorted(history))
        rows = history.get(sector) or []
        metric = st.selectbox("Metric", ["pct_above_50d", "pct_above_200d", "median_return", "ew_return", "cw_return", "held_ew_return", "dispersion", "hhi_cap"])
        history_chart(rows, x="decision_date", y=metric, title="{0} · {1}".format(sector, metric), units="fraction")
        st.caption("{0} stored decision dates for {1}; NULL points are gaps in coverage (insufficient history or cap coverage), never zeros.".format(len(rows), sector))
    st.subheader("Ingested artifacts")
    if artifacts:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "SHA-256": str(a.get("artifact_sha256"))[:16] + "…",
                        "Method": a.get("method_version"),
                        "Provenance": a.get("provenance"),
                        "Research eligible": "yes" if a.get("research_eligible") else "no",
                        "Idea": a.get("contract_idea_id") or "—",
                        "Window": "{0} → {1}".format(a.get("window_start"), a.get("window_end")),
                        "Sessions": a.get("sessions"),
                        "Rows": a.get("row_count"),
                        "Boundary": a.get("effective_holdout_start"),
                        "Ingested": age_text(a.get("ingested_at")),
                    }
                    for a in artifacts
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    st.caption(ctx.get("note") or "")


# ---- Data Health -----------------------------------------------------------------------------

def render_data_health() -> None:
    page_header("Data Health", "Operational status of every source and dataset: configuration, transport, freshness, counts and recent ingestion runs. Not an investment signal.", fred=False)
    health = load_or_stop("source_health")
    runs = load_or_stop("recent_runs", 200)
    if not health:
        st.info("No sources registered yet. The first `jobs.market_intelligence_refresh` run registers sources (disabled sources appear as explicit skips).")
    else:
        frame = pd.DataFrame(
            [
                {
                    "Source": h.get("source_id"),
                    "Provider": h.get("provider"),
                    "Dataset": h.get("freshness_dataset") or h.get("dataset"),
                    "Enabled": "yes" if h.get("enabled") else "no",
                    "Access": h.get("access_status"),
                    "Cadence": h.get("dataset_cadence") or "—",
                    "Transport": transport_chip(h.get("transport_status")),
                    "Metadata": h.get("metadata_status") or "—",
                    "Freshness (now)": freshness_chip(h.get("freshness_status")),
                    "Freshness (at ingest)": freshness_chip(h.get("stored_freshness_status")),
                    "Latest obs": h.get("latest_observation_date") or "—",
                    "Age (d)": h.get("age_days"),
                    "Tolerance (d)": h.get("tolerance_days"),
                    "Stale after": h.get("stale_after_estimate") or "—",
                    "Obs retrieved": age_text(h.get("latest_observation_retrieved_at")),
                    "Last attempt": age_text(h.get("last_attempt_at")),
                    "Last success": age_text(h.get("last_success_at")),
                    "Error": (h.get("last_error_redacted") or "")[:80],
                    "Export scope": h.get("usage_scope"),
                }
                for h in health
            ]
        )
        st.dataframe(frame, use_container_width=True, hide_index=True)
        evaluated_on = next((h.get("evaluated_on") for h in health if h.get("evaluated_on")), None)
        policy = next((h.get("freshness_policy_version") for h in health if h.get("freshness_policy_version")), None)
        st.caption(
            "Transport (did the last retrieval succeed?), metadata (did provider units/frequency/identity validate?) and freshness (is the latest observation within the cadence tolerance?) are tracked separately. "
            "Freshness (now) is re-evaluated against the database clock on {0} under {1}; it decays even when no job runs. 'Stale after' is the tolerance bound, not an official release date. "
            "A successful retrieval of old data is not fresh; a failed or metadata-rejected retrieval does not erase last valid data.".format(evaluated_on or "today", policy or "the freshness policy")
        )
    st.subheader("IBKR Windows collector")
    collectors = load_or_stop("ibkr_collector_status")
    quotes = load_or_stop("ibkr_quotes_latest")
    if not collectors:
        st.info("No collector heartbeat has been received. Closing TWS preserves stored quotes; this page will show COLLECTOR_OFFLINE once a heartbeat goes stale.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Collector": c.get("collector_id"),
                        "Reported": c.get("reported_state"),
                        "Observed (server clock)": c.get("observed_state"),
                        "Heartbeat age (s)": c.get("heartbeat_age_seconds"),
                        "Last heartbeat": age_text(c.get("last_heartbeat_at")),
                        "Last TWS connect": age_text(c.get("last_tws_connect_at")),
                        "Last quote": age_text(c.get("last_quote_at")),
                        "Last DB ingest": age_text(c.get("last_ingest_ok_at")),
                        "Market data": c.get("market_data_type") or "UNAVAILABLE",
                        "Delivery error": (c.get("last_delivery_error_redacted") or "")[:80],
                    }
                    for c in collectors
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
        st.caption("Observed state uses the database clock. If heartbeats stop (collector offline, sleep, or crash), the server marks COLLECTOR_OFFLINE; the collector cannot report its own outage.")
    if quotes:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Instrument": q.get("display_name") or q.get("instrument_id"),
                        "conId": q.get("con_id"),
                        "Currency": q.get("currency") or "—",
                        "Bid": q.get("bid"),
                        "Ask": q.get("ask"),
                        "Last": q.get("last_price"),
                        "Close": q.get("close_price"),
                        "Type": q.get("market_data_type") or "—",
                        "Quote time": q.get("quote_ts") or "—",
                        "Received": age_text(q.get("retrieved_at")),
                        "Status": q.get("quote_status") or "—",
                    }
                    for q in quotes
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
        st.caption("Missing bid/ask/last remain empty (NULL). Closing TWS does not delete these rows; age is shown from stored timestamps.")
    elif collectors:
        st.info("Collector registered, but no IBKR quotes have been persisted yet.")
    quarantine = load_or_stop("data_health_context").get("quarantine") or []
    if quarantine:
        st.subheader("Quarantined observations (metadata gate / future dates)")
        st.dataframe(pd.DataFrame(quarantine), use_container_width=True, hide_index=True)
        st.caption("Rows retrieved under failed metadata validation or with impossible dates are kept here for diagnosis and are never promoted to current observations or metrics. Last valid published data remains visible on the other pages.")
    st.subheader("Recent ingestion runs (30 days)")
    if not runs:
        st.info("No ingestion runs recorded.")
    else:
        st.dataframe(pd.DataFrame([{"Run": r["run_id"], "Source": r["source_id"], "Dataset": r["dataset"], "Status": r["status"], "Started": r["started_at"], "Finished": r["finished_at"], "Received": r["rows_received"], "Inserted": r["rows_inserted"], "Revised": r["rows_revised"], "Unchanged": r["rows_unchanged"], "Rejected": r["rows_rejected"], "Retries": r["retry_count"], "Dry run": r["dry_run"], "Error": (r["error_redacted"] or "")[:80]} for r in runs]), use_container_width=True, hide_index=True)
    st.subheader("Research delivery")
    strategies = load_or_stop("strategies_context")
    rows = strategies.get("strategies") or []
    if rows:
        st.dataframe(pd.DataFrame([{"Strategy": s["strategy_id"], "Kind": s["research_kind"], "Research": s["research_status"], "Economic gate": s["economic_gate"], "Promotion": s["promotion_gate"], "Holdout": s["holdout_status"], "Delivery": s["delivery_status"], "Last seen": s["last_seen_at"]} for s in rows]), use_container_width=True, hide_index=True)
    else:
        st.info("No research runs in PostgreSQL.")


# ---- Morning Context ------------------------------------------------------------------------

def render_morning_context() -> None:
    page_header("Morning Context", "Latest published morning_context snapshot: human-readable sections plus the deterministic JSON and its identity. Built by the backend (current-only; no historical reconstruction); no LLM.", fred=True)
    snapshot = load_or_stop("morning_latest")
    index = load_or_stop("morning_index", 20)
    if not snapshot:
        st.info("No morning context snapshot has been published. Run `python -m jobs.build_morning_context` on the backend.")
        return
    body = snapshot.get("snapshot_json") or {}
    cols = st.columns(4)
    cols[0].metric("Snapshot", snapshot["snapshot_id"])
    cols[1].metric("Generated", str(snapshot["generated_at"])[:19])
    cols[2].metric("Cutoff", str(snapshot["cutoff_at"])[:19])
    cols[3].metric("Completeness", snapshot["completeness"])
    from market_intelligence.read_models import snapshot_age

    age = snapshot_age(snapshot)
    quality = snapshot.get("quality_status") or "OK"
    st.caption(
        "SHA-256: `{0}` · content SHA-256: `{1}` · schema {2} · quality {3} · snapshot age {4} ({5} h; aging after {6} h, stale after {7} h). "
        "generated_at is builder wall-clock; cutoff_at is the DB capture time (REPEATABLE READ); every metric keeps its own observation date. "
        "Captured health inside the body is frozen at capture; the age shown here is evaluated now.".format(
            snapshot["snapshot_sha256"], snapshot.get("content_sha256") or "n/a", snapshot["schema_version"], quality, age.get("snapshot_age_status"), age.get("age_hours"), age.get("aging_after_hours"), age.get("stale_after_hours")
        )
    )
    if age.get("snapshot_age_status") == "STALE":
        st.warning("The latest published snapshot is stale for delivery purposes (no newer snapshot has been published). Captured values are unchanged; check Data Health and the backend timer.")
    if quality != "OK":
        st.warning("Quality flag on this snapshot: {0} — {1}".format(quality, snapshot.get("quality_note") or ""))
    status = body.get("sections_status") or {}
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Section": k,
                    "Status": v.get("status"),
                    "Latest obs": v.get("latest_observation_date") or "—",
                    "Captured freshness": ((v.get("captured_freshness") or {}).get("status") or "—") if isinstance(v, dict) else "—",
                    "Cadence": ((v.get("captured_freshness") or {}).get("cadence") or "—") if isinstance(v, dict) else "—",
                    "Reason": v.get("reason") or "",
                }
                for k, v in status.items()
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    captured = body.get("captured_health") or {}
    if captured.get("quarantined_series") or captured.get("stale_sources") or captured.get("failed_transport"):
        st.caption("Captured health at cutoff: {0} stale source(s), {1} failed/rejected transport(s), quarantined series: {2}.".format(len(captured.get("stale_sources") or []), len(captured.get("failed_transport") or []), ", ".join(captured.get("quarantined_series") or []) or "none"))
    sections = body.get("sections") or {}
    market = (sections.get("market") or {}).get("data") or {}
    if market:
        st.subheader("Market")
        st.write(market.get("overnight_quotes", {}).get("reason", ""))
        lead = market.get("sector_leadership_rs_vs_spy") or market.get("sector_leadership_1m_rs_vs_spy") or []
        if lead:
            lead = sorted(lead, key=lambda r: (r.get("rs_chg_1m") is None, -(r.get("rs_chg_1m") or 0)))  # ranked locally; the body stores identity order
        if lead:
            st.dataframe(styled_heatmap(pd.DataFrame([{"Sector / theme": r["sector_key"], "ETF": r["instrument_id"], "As of": r["as_of"], "1W RS": r["rs_chg_1w"], "1M RS": r["rs_chg_1m"], "3M RS": r["rs_chg_3m"], "1M return": r["ret_1m"]} for r in lead]), ["1W RS", "1M RS", "3M RS", "1M return"]), use_container_width=True, hide_index=True)
    rates = (sections.get("rates") or {}).get("data") or {}
    if rates.get("curve"):
        st.subheader("Rates")
        st.dataframe(pd.DataFrame([{"Tenor": c["tenor"], "Yield (%)": fmt(c["yield_pct"], "pct"), "Obs date": c["observation_date"], "vs prior (bps)": fmt_signed(c.get("chg_prev_bps"), "bps"), "1M (bps)": fmt_signed(c.get("chg_1m_bps"), "bps")} for c in rates["curve"] if c.get("yield_pct") is not None]), use_container_width=True, hide_index=True)
    credit = (sections.get("credit") or {}).get("data") or {}
    if credit.get("buckets"):
        st.subheader("Credit (internal)")
        st.dataframe(pd.DataFrame([{"Bucket": b["label"], "As of": b["as_of"], "OAS (bps)": fmt(b["oas_bps"], None, digits=0), "1D": fmt_signed(b["change_1d_bps"], "bps"), "1M": fmt_signed(b["change_1m_bps"], "bps"), "Percentile": fmt(b["percentile"], "pctile"), "Window": b["percentile_window"] or "—"} for b in credit["buckets"]]), use_container_width=True, hide_index=True)
    strategies = (sections.get("strategy_monitor_summary") or {}).get("data") or {}
    if strategies.get("strategies"):
        st.subheader("Strategy Monitor summary")
        st.dataframe(pd.DataFrame([{"Strategy": s["strategy_id"], "Research": s["research_status"], "Economic gate": s["economic_gate"], "Promotion": s["promotion_gate"], "Delivery": s["delivery_status"]} for s in strategies["strategies"]]), use_container_width=True, hide_index=True)
    ideas = load_or_stop("research_ideas") or []
    st.subheader("Research ideas (registry)")
    if ideas:
        st.dataframe(pd.DataFrame([{"Idea": i["idea_id"], "Title": i["title"], "Type": i["research_type"], "State": i["current_state"], "Version": i["current_version"], "Execution support": i.get("execution_support") or "—", "Spec": i.get("spec_completeness") or "—", "Missing": ", ".join(i.get("missing_fields") or []) or "—", "Holdout from": i.get("effective_holdout_start") or "—", "Economic gate": i.get("economic_gate") or "—", "Spec hash": (i.get("current_spec_hash") or "")[:12], "Updated": i["updated_at"]} for i in ideas]), use_container_width=True, hide_index=True)
        st.caption("Approval requires a COMPLETE frozen spec; the registry never supplies acceptance thresholds (economic gate NOT_DEFINED unless a human recorded them). MANUAL_SPEC_REQUIRED means an engine exists but a human must author the StrategySpec.")
        st.caption("Ideas are dry-run only: approval binds to the exact spec hash; nothing is backtested or deployed from this page.")
    else:
        st.info("No research ideas registered. Register with `python -m jobs.research_ideas register --spec idea.json --actor <you>`.")
    with st.expander("Deterministic JSON (internal, unfiltered)"):
        st.code(strict_dumps(body, indent=2), language="json")
    with st.expander("Snapshot history"):
        st.dataframe(pd.DataFrame(index), use_container_width=True, hide_index=True)


__all__ = [
    "render_credit_overview",
    "render_data_health",
    "render_macro_overview",
    "render_market_pulse",
    "render_morning_context",
    "render_rates_curve",
    "render_sector_rotation_v2",
]
