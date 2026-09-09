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
from market_intelligence.overview import build_what_changed
from market_intelligence.sector_mapping import CANONICAL_SECTORS
from market_intelligence.ui import (
    age_text,
    compact_as_of,
    fmt,
    fmt_signed,
    freshness_chip,
    heatmap_legend,
    history_chart,
    implied_prior_yield,
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
PRIMARY_MACRO = ("growth", "labor", "inflation", "liquidity")
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


def display_cell(value: Any) -> str:
    """Render a table cell as text so Streamlit/pyarrow never mixes ints with '—'."""
    if value is None or value == "":
        return "—"
    return str(value)


def _transform_text(entry: dict[str, Any] | None) -> str:
    if not entry:
        return "—"
    text = fmt_signed(entry.get("value"), entry.get("units")) if entry.get("units") in {"bps", "pct", "pp", "fraction"} else fmt(entry.get("value"), entry.get("units"))
    if entry.get("status") not in (None, "OK"):
        text += " ({0})".format(entry.get("status").lower().replace("_", " "))
    return text


def _worst_freshness(health: list[dict[str, Any]]) -> str | None:
    statuses = [str(row.get("freshness_status") or "").upper() for row in health]
    if "STALE" in statuses:
        return "STALE"
    if "UNKNOWN" in statuses or not statuses:
        return "UNKNOWN"
    if "FRESH" in statuses:
        return "FRESH"
    return statuses[0] if statuses else None


def _material_warning(health: list[dict[str, Any]], extra: list[str] | None = None) -> str | None:
    notes = list(extra or [])
    stale = [row for row in health if str(row.get("freshness_status") or "").upper() == "STALE"]
    failed = [row for row in health if str(row.get("transport_status") or "").upper() in {"FAILED", "METADATA_REJECTED"}]
    if stale:
        notes.append("{0} source(s) are stale relative to their release cadence.".format(len(stale)))
    if failed:
        notes.append("{0} source(s) failed the last retrieval. Last valid stored values are shown.".format(len(failed)))
    return " ".join(notes) if notes else None


def _open_page(path: str, label: str) -> None:
    try:
        st.page_link(path, label=label)
    except Exception:  # noqa: BLE001
        st.caption(label)


# ---- Overview ---------------------------------------------------------------------------

def render_market_pulse() -> None:
    health = load_or_stop("source_health")
    rates = load_or_stop("rates_context")
    credit = load_or_stop("credit_context")
    sectors = load_or_stop("sectors_context")
    macro = load_or_stop("macro_context")
    order_flow = load_or_stop("order_flow_overview")
    as_of, freshness = compact_as_of(
        [row.get("latest_observation_date") for row in health] + [row.get("as_of") for row in (sectors.get("datasets") or {}).get("ETF_RS_VS_SPY") or []],
        freshness=_worst_freshness(health),
    )
    page_header(
        "Overview",
        "What changed across sectors, rates, credit, the economy, and reported bond activity.",
        as_of=as_of,
        freshness=freshness,
        warning=_material_warning(health),
    )
    st.caption("Overnight quotes unavailable: no live quote source is configured. Prior-session closes are not labeled as overnight.")

    changed = build_what_changed(rates=rates, credit=credit, sectors=sectors, macro=macro, order_flow=order_flow)
    st.subheader("What changed")
    if changed:
        st.dataframe(pd.DataFrame([{"Area": row["area"], "Change": row["text"], "Period": row["period"]} for row in changed]), use_container_width=True, hide_index=True)
    else:
        st.info("No stored observations yet.")

    curve = [row for row in rates.get("curve", []) if row.get("tenor") in {"2Y", "10Y", "30Y"} and row.get("yield_pct") is not None]
    buckets = [row for row in (credit.get("buckets") or []) if row.get("bucket") in {"ig_broad", "hy_broad"}]
    rs_rows = (sectors.get("datasets") or {}).get("ETF_RS_VS_SPY") or []
    headline_cols = st.columns(4)
    if curve:
        ten = next((row for row in curve if row["tenor"] == "10Y"), curve[0])
        headline_cols[0].metric("10Y yield", fmt(ten.get("yield_pct"), "pct"), fmt_signed(ten.get("chg_prev_bps"), "bps") if ten.get("chg_prev_bps") is not None else None)
    if buckets:
        ig = next((row for row in buckets if row["bucket"] == "ig_broad"), buckets[0])
        headline_cols[1].metric("IG OAS", fmt(ig.get("oas_bps"), "bps").replace("+", ""), fmt_signed(ig.get("change_1d_bps"), "bps") if ig.get("change_1d_bps") is not None else None)
    if rs_rows:
        ranked = sorted(rs_rows, key=lambda row: ((row.get("metrics") or {}).get("rs_chg_1m") is None, -((row.get("metrics") or {}).get("rs_chg_1m") or 0)))
        lead = ranked[0]
        headline_cols[2].metric("Sector lead (1M RS)", str(lead.get("sector_key") or "—"), fmt_signed((lead.get("metrics") or {}).get("rs_chg_1m"), "fraction") if (lead.get("metrics") or {}).get("rs_chg_1m") is not None else None)
    breadth = next((row for row in ((order_flow.get("breadth") or {}).get("rows") or []) if (row.get("product_category") or "").lower() == "all securities"), None)
    if breadth:
        headline_cols[3].metric("Bond activity (volume)", fmt(breadth.get("total_volume"), None), fmt_signed(breadth.get("volume_change"), None) if breadth.get("volume_change") is not None else None)

    st.subheader("Sector leadership and weakness")
    if rs_rows:
        frame = pd.DataFrame(
            [{"Sector": row["sector_key"], "ETF proxy": row["instrument_id"], "As of": row["as_of"], "1W RS": (row["metrics"] or {}).get("rs_chg_1w"), "1M RS": (row["metrics"] or {}).get("rs_chg_1m"), "3M RS": (row["metrics"] or {}).get("rs_chg_3m"), "1M return": (row["metrics"] or {}).get("ret_1m")} for row in rs_rows]
        ).sort_values("1M RS", ascending=False, na_position="last")
        st.dataframe(styled_heatmap(frame, ["1W RS", "1M RS", "3M RS", "1M return"]), use_container_width=True, hide_index=True)
        heatmap_legend()
        st.caption("Relative strength is the change in the ETF/SPY adjusted-close ratio, not an arithmetic excess return. ETF proxy, not a constituent aggregate.")
        _open_page("pages/14_Sector_Rotation_V2.py", "Open Sectors")
    else:
        st.info("No sector snapshots stored.")

    st.subheader("Treasury yields")
    if curve:
        cols = st.columns(len(curve))
        for i, row in enumerate(curve):
            cols[i].metric("{0}".format(row["tenor"]), fmt(row.get("yield_pct"), "pct"), fmt_signed(row.get("chg_prev_bps"), "bps") if row.get("chg_prev_bps") is not None else None)
        _open_page("pages/12_Rates_Curve.py", "Open Rates")
    else:
        st.info("No Treasury curve data stored.")

    st.subheader("Credit spreads")
    if buckets:
        cols = st.columns(len(buckets))
        for i, row in enumerate(buckets):
            cols[i].metric(row["label"], fmt(row.get("oas_bps"), "bps").replace("+", ""), fmt_signed(row.get("change_1d_bps"), "bps") if row.get("change_1d_bps") is not None else None)
        st.caption(credit.get("attribution") or "")
        _open_page("pages/13_Credit_Overview.py", "Open Credit")
    else:
        st.info("No credit index snapshots stored.")

    st.subheader("Economy")
    cats = macro.get("categories") or {}
    quick = []
    for cat in PRIMARY_MACRO:
        for block in cats.get(cat, []):
            transforms = block.get("transforms") or {}
            headline = transforms.get("yoy_pct") or transforms.get("qoq_saar_pct") or transforms.get("mom_change") or transforms.get("chg_4w") or transforms.get("wow_change")
            kind = next((TRANSFORM_LABELS.get(key) for key in ("yoy_pct", "qoq_saar_pct", "mom_change", "chg_4w", "wow_change") if key in transforms), "—")
            quick.append({"Group": CATEGORY_TITLES[cat], "Series": block.get("label"), "Latest": fmt(block["latest"].get("value"), None), "Change": _transform_text(headline), "Change kind": kind, "Observation": block["latest"].get("observation_date")})
    if quick:
        st.dataframe(pd.DataFrame(quick), use_container_width=True, hide_index=True)
        _open_page("pages/11_Macro_Overview.py", "Open Macro")
    else:
        st.info("No macro observations stored.")

    st.subheader("Bond trading activity")
    if breadth:
        st.caption("Reported TRACE activity, not a live order book.")
        cols = st.columns(3)
        cols[0].metric("Reported volume", fmt(breadth.get("total_volume"), None), fmt_signed(breadth.get("volume_change"), None) if breadth.get("volume_change") is not None else None)
        cols[1].metric("Trade count", fmt(breadth.get("total_trades"), None), fmt_signed(breadth.get("trade_count_change"), None) if breadth.get("trade_count_change") is not None else None)
        cols[2].metric("Session", str(breadth.get("observation_date") or "—"))
        capped = order_flow.get("capped_volume") or {}
        if not capped.get("headline_eligible"):
            st.caption(capped.get("identity_note") or "Capped-volume figures are withheld from headlines until reporting-period identity is validated.")
        _open_page("pages/18_Order_Flow.py", "Open Order Flow")
    else:
        st.info("No corporate-bond activity aggregates stored.")


# ---- Macro ---------------------------------------------------------------------------

def render_macro_overview() -> None:
    macro = load_or_stop("macro_context")
    cats = macro.get("categories") or {}
    dates = [block.get("latest", {}).get("observation_date") for blocks in cats.values() for block in blocks]
    page_header(
        "Macro",
        "Growth, labor, inflation, and liquidity. Observation dates are the period being measured, not the retrieval time.",
        as_of=compact_as_of(dates)[0],
    )
    if not cats:
        st.info("No macro observations stored yet.")
        return
    if macro.get("series_without_data"):
        with st.expander("Catalog series without stored observations"):
            st.write(", ".join(macro["series_without_data"]))

    chosen_default = None
    for cat in PRIMARY_MACRO:
        blocks = cats.get(cat) or []
        if not blocks:
            continue
        st.subheader(CATEGORY_TITLES[cat])
        rows = []
        for block in blocks:
            transforms = block.get("transforms") or {}
            chosen_default = chosen_default or block["series_id"]
            row = {
                "Series": block.get("label") or block["series_id"],
                "Latest": fmt(block["latest"].get("value"), None),
                "Units": block["latest"].get("units") or block.get("catalog_units"),
                "Observation": block["latest"].get("observation_date"),
                "Change": _transform_text(transforms.get("yoy_pct") or transforms.get("qoq_saar_pct") or transforms.get("mom_change") or transforms.get("chg_4w") or transforms.get("wow_change") or transforms.get("chg_prev")),
            }
            rows.append(row)
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        quarantined = [block["series_id"] for block in blocks if block.get("publication_status") and block.get("publication_status") != "PUBLISHED"]
        if quarantined:
            st.warning("The latest retrieval for {0} was quarantined; last validated values remain.".format(", ".join(quarantined)))
        if cat == "liquidity":
            st.caption("Balances differ in dating and scale. Display conversions keep provider units in storage; no composite liquidity score is computed.")
        if cat == "inflation":
            st.caption("YoY = 100·(I_t/I_{t−12} − 1); 3M annualized = 100·((I_t/I_{t−3})⁴ − 1). Exact calendar alignment, no forward fill.")

    with st.expander("Policy rates and Treasury catalog"):
        extra_rows = []
        for cat in ("policy", "rates"):
            for block in cats.get(cat) or []:
                extra_rows.append({"Group": CATEGORY_TITLES[cat], "Series": block.get("label"), "Latest": fmt(block["latest"].get("value"), None), "Observation": block["latest"].get("observation_date")})
        if extra_rows:
            st.dataframe(pd.DataFrame(extra_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("No additional policy or Treasury catalog rows on this page. See Rates.")

    with st.expander("Series definitions and transforms"):
        detail = []
        for cat, blocks in cats.items():
            for block in blocks:
                transforms = block.get("transforms") or {}
                row = {"Series": block.get("label"), "ID": block["series_id"], "Freq": block.get("frequency"), "SA": block.get("seasonal_adjustment"), "Vintage": block.get("vintage_kind")}
                for key, label in TRANSFORM_LABELS.items():
                    if key in transforms:
                        row[label] = _transform_text(transforms[key])
                detail.append(row)
        if detail:
            st.dataframe(pd.DataFrame(detail), use_container_width=True, hide_index=True)

    st.subheader("History")
    all_ids = sorted({block["series_id"] for blocks in cats.values() for block in blocks})
    if all_ids:
        chosen = st.selectbox("Series", all_ids, index=all_ids.index(chosen_default) if chosen_default in all_ids else 0, format_func=lambda series_id: "{0} — {1}".format(series_id, CATALOG_BY_ID[series_id].label if series_id in CATALOG_BY_ID else series_id))
        history = load_or_stop("observation_history", chosen)
        spec = CATALOG_BY_ID.get(chosen)
        history_chart(history, x="observation_date", y="value", title="{0}".format(spec.label if spec else chosen), units=(spec.expected_units_contains[0] if spec and spec.expected_units_contains else None))


# ---- Rates ---------------------------------------------------------------------------

def render_rates_curve() -> None:
    rates = load_or_stop("rates_context")
    curve = rates.get("curve") or []
    present = [row for row in curve if row.get("yield_pct") is not None]
    page_header(
        "Rates",
        "Treasury curve in percent; changes in basis points.",
        as_of=compact_as_of([row.get("observation_date") for row in present])[0],
        warning="Tenors have different observation dates: {0}.".format(", ".join(rates.get("curve_observation_dates") or [])) if rates.get("curve_dates_mixed") else None,
    )
    if not present:
        st.info("No Treasury curve observations stored.")
        return
    frame = pd.DataFrame(present)
    frame["tenor_order"] = frame["tenor"].map({tenor: i for i, tenor in enumerate(CURVE_TENORS)})
    frame = frame.sort_values("tenor_order")
    compare = st.radio("Compare with", ["None", "Prior session", "1 week", "1 month"], horizontal=True, key="rates_compare")
    change_key = {"Prior session": "chg_prev_bps", "1 week": "chg_1w_bps", "1 month": "chg_1m_bps"}.get(compare)
    try:
        import plotly.graph_objects as go

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=frame["tenor"], y=frame["yield_pct"], mode="lines+markers", name="Latest yield (%)"))
        if change_key:
            prior = [implied_prior_yield(row.get("yield_pct"), row.get(change_key)) for row in frame.to_dict("records")]
            if any(value is not None for value in prior):
                fig.add_trace(go.Scatter(x=frame["tenor"], y=prior, mode="lines+markers", name=compare, line=dict(dash="dash")))
        fig.update_layout(height=360, margin=dict(l=10, r=10, t=30, b=10), yaxis_title="percent", legend=dict(orientation="h"))
        st.plotly_chart(fig, use_container_width=True)
    except ImportError:  # pragma: no cover
        st.line_chart(frame.set_index("tenor")["yield_pct"])

    headline = [row for row in present if row["tenor"] in {"2Y", "10Y", "30Y"}]
    cols = st.columns(max(1, len(headline)))
    for i, row in enumerate(headline):
        cols[i].metric("{0}".format(row["tenor"]), fmt(row.get("yield_pct"), "pct"), fmt_signed(row.get("chg_prev_bps"), "bps") if row.get("chg_prev_bps") is not None else None)

    slopes = rates.get("slopes") or {}
    slope_2s10s = slopes.get("2s10s") or slopes.get("2Y10Y") or next(iter(slopes.values()), None)
    if slope_2s10s:
        st.metric("2s10s slope", _transform_text(slope_2s10s), help="Long minus short tenor on a common observation date, in basis points.")

    with st.expander("Tenor table and other slopes"):
        st.dataframe(
            pd.DataFrame([{"Tenor": row["tenor"], "Yield (%)": fmt(row["yield_pct"], "pct"), "Observation": row["observation_date"], "vs prior (bps)": fmt_signed(row.get("chg_prev_bps"), "bps"), "1W (bps)": fmt_signed(row.get("chg_1w_bps"), "bps"), "1M (bps)": fmt_signed(row.get("chg_1m_bps"), "bps"), "3M (bps)": fmt_signed(row.get("chg_3m_bps"), "bps")} for row in present]),
            use_container_width=True,
            hide_index=True,
        )
        missing_tenors = [row["tenor"] for row in curve if row.get("yield_pct") is None]
        if missing_tenors:
            st.caption("Tenors without data (not extrapolated): {0}".format(", ".join(missing_tenors)))
        slope_cols = st.columns(max(1, len(slopes)))
        for i, (name, entry) in enumerate(slopes.items()):
            slope_cols[i % len(slope_cols)].metric(name, _transform_text(entry))

    for title, key in (("Real yields (TIPS)", "real_yields"), ("Inflation compensation (market-implied, not survey)", "inflation_compensation"), ("Policy rates", "policy")):
        blocks = rates.get(key) or []
        if not blocks:
            continue
        with st.expander(title):
            st.dataframe(pd.DataFrame([{"Series": block.get("label"), "Level (%)": fmt(block["latest"].get("value"), "pct"), "Observation": block["latest"].get("observation_date"), "vs prior (bps)": _transform_text((block.get("transforms") or {}).get("chg_prev_bps")), "1W (bps)": _transform_text((block.get("transforms") or {}).get("chg_1w_bps")), "1M (bps)": _transform_text((block.get("transforms") or {}).get("chg_1m_bps"))} for block in blocks]), use_container_width=True, hide_index=True)
    st.caption(rates.get("units_note") or "")


# ---- Credit ---------------------------------------------------------------------------

def render_credit_overview() -> None:
    credit = load_or_stop("credit_context")
    buckets = credit.get("buckets") or []
    page_header(
        "Credit",
        "ICE BofA option-adjusted spreads. Internal view; redistribution restricted.",
        as_of=compact_as_of([row.get("as_of") for row in buckets])[0],
    )
    if not buckets:
        st.info("No credit index snapshots stored.")
        return
    broad = [row for row in buckets if row["bucket"] in {"ig_broad", "hy_broad"}]
    cols = st.columns(max(1, len(broad)))
    for i, row in enumerate(broad):
        cols[i].metric(row["label"], "{0:.0f} bps".format(row["oas_bps"]) if row.get("oas_bps") is not None else "—", fmt_signed(row.get("change_1d_bps"), "bps") if row.get("change_1d_bps") is not None else None)
    st.caption(credit.get("attribution") or "")

    rating = [row for row in buckets if row["bucket"] not in {"ig_broad", "hy_broad"}]
    if rating:
        st.subheader("Rating buckets")
        st.dataframe(
            pd.DataFrame([{"Bucket": row["label"], "As of": row["as_of"], "OAS (bps)": fmt(row["oas_bps"], None, digits=0), "1D": fmt_signed(row["change_1d_bps"], "bps"), "1W": fmt_signed(row["change_1w_bps"], "bps"), "1M": fmt_signed(row["change_1m_bps"], "bps")} for row in rating]),
            use_container_width=True,
            hide_index=True,
        )

    ids = [row["series_id"] for row in buckets]
    chosen = st.selectbox("History", ids, format_func=lambda series_id: CATALOG_BY_ID[series_id].label if series_id in CATALOG_BY_ID else series_id)
    history = load_or_stop("metric_history", "{0}.oas_bps".format(chosen))
    history_chart(history, x="as_of", y="value", title="{0} OAS (bps)".format(CATALOG_BY_ID[chosen].label if chosen in CATALOG_BY_ID else chosen), units="bps")

    with st.expander("Percentiles, z-scores, and history windows"):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Bucket": row["label"],
                        "Window": row["percentile_window"] or "—",
                        "Percentile": fmt(row["percentile"], "pctile") if row.get("window_observations") else "—",
                        "Z-score": fmt(row["zscore"], None) if row.get("window_observations") else "—",
                        "Observations": row["window_observations"] or "—",
                        "History from": row["history_first_date"] or "—",
                        "History": row["history_status"],
                    }
                    for row in buckets
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(credit.get("coverage_note") or "")
        st.caption("Percentiles and z-scores appear only when the labeled lookback has enough stored history.")


# ---- Sectors ---------------------------------------------------------------------------

def render_sector_rotation_v2() -> None:
    sectors = load_or_stop("sectors_context")
    datasets = sectors.get("datasets") or {}
    rs_rows = datasets.get("ETF_RS_VS_SPY") or []
    page_header(
        "Sectors",
        "Sector comparison from stored snapshots. ETF proxies versus SPY unless another benchmark is selected.",
        fred=False,
        as_of=compact_as_of([row.get("as_of") for row in rs_rows])[0],
    )
    if not datasets:
        st.info("No sector snapshots stored.")
        return
    pit = load_or_stop("pit_sector_context")
    if not pit.get("available"):
        st.caption("Point-in-time constituent internals are not ingested. Current-universe breadth below is a snapshot, not PIT.")

    if rs_rows:
        benchmarks = sorted({row.get("benchmark") for row in rs_rows if row.get("benchmark")})
        benchmark = st.selectbox("Benchmark", benchmarks) if len(benchmarks) > 1 else (benchmarks[0] if benchmarks else "SPY")
        rows = [row for row in rs_rows if row.get("benchmark") == benchmark]
        metric = st.radio("Heatmap metric", ["Relative strength vs benchmark", "Absolute ETF return"], horizontal=True, key="sector_metric")
        as_ofs = sorted({row["as_of"] for row in rows if row.get("as_of")})
        basis = rows[0].get("return_basis") if rows else "—"
        st.caption("Benchmark {0} · as of {1} · {2} · ETF proxy, not a constituent aggregate.".format(benchmark, ", ".join(as_ofs), basis))
        if metric.startswith("Relative"):
            frame = pd.DataFrame([{"Sector": row["sector_key"], "Kind": row["entity_kind"], "ETF": row["instrument_id"], "1W": (row["metrics"] or {}).get("rs_chg_1w"), "1M": (row["metrics"] or {}).get("rs_chg_1m"), "3M": (row["metrics"] or {}).get("rs_chg_3m"), "6M": (row["metrics"] or {}).get("rs_chg_6m"), "12M": (row["metrics"] or {}).get("rs_chg_12m")} for row in rows])
            value_cols = ["1W", "1M", "3M", "6M", "12M"]
            st.caption("Relative strength = change in the ETF/benchmark adjusted-close ratio.")
        else:
            frame = pd.DataFrame([{"Sector": row["sector_key"], "Kind": row["entity_kind"], "ETF": row["instrument_id"], "1M": (row["metrics"] or {}).get("ret_1m"), "3M": (row["metrics"] or {}).get("ret_3m"), "12M": (row["metrics"] or {}).get("ret_12m")} for row in rows])
            value_cols = ["1M", "3M", "12M"]
            st.caption("Absolute ETF returns on adjusted close. Not equal-weight or cap-weight constituent portfolios.")
        order = {name: i for i, name in enumerate(CANONICAL_SECTORS)}
        frame["_o"] = frame["Sector"].map(lambda name: order.get(name, 99))
        frame = frame.sort_values(["_o", "Sector"]).drop(columns="_o")
        st.dataframe(styled_heatmap(frame, value_cols), use_container_width=True, hide_index=True)
        heatmap_legend()

        names = [row["sector_key"] for row in rows]
        chosen = st.selectbox("Sector drilldown", names, key="sector_drilldown")
        selected = next((row for row in rows if row["sector_key"] == chosen), rows[0])
        metrics = selected.get("metrics") or {}
        cols = st.columns(4)
        cols[0].metric("1M RS vs {0}".format(benchmark), fmt_signed(metrics.get("rs_chg_1m"), "fraction") if metrics.get("rs_chg_1m") is not None else "—")
        cols[1].metric("1M ETF return", fmt_signed(metrics.get("ret_1m"), "fraction") if metrics.get("ret_1m") is not None else "—")
        cols[2].metric("vs 50DMA", fmt_signed(metrics.get("pct_vs_50dma"), "fraction") if metrics.get("pct_vs_50dma") is not None else "—")
        cols[3].metric("vs 200DMA", fmt_signed(metrics.get("pct_vs_200dma"), "fraction") if metrics.get("pct_vs_200dma") is not None else "—")
        stale = (selected.get("coverage") or {}).get("price_status")
        if stale == "STALE":
            st.warning("{0} last price predates the bundle as-of; windowed metrics stay blank rather than being relabelled current.".format(selected.get("instrument_id")))

        with st.expander("ETF trend and risk"):
            trend = pd.DataFrame(
                [
                    {
                        "Sector": row["sector_key"],
                        "ETF": row["instrument_id"],
                        "1M return": (row["metrics"] or {}).get("ret_1m"),
                        "3M return": (row["metrics"] or {}).get("ret_3m"),
                        "12M return": (row["metrics"] or {}).get("ret_12m"),
                        "vs 50DMA": (row["metrics"] or {}).get("pct_vs_50dma"),
                        "vs 200DMA": (row["metrics"] or {}).get("pct_vs_200dma"),
                    }
                    for row in rows
                ]
            )
            st.dataframe(styled_heatmap(trend, ["1M return", "3M return", "12M return", "vs 50DMA", "vs 200DMA"]), use_container_width=True, hide_index=True)
            st.caption("ETF returns on FMP adjusted close. Windows use the bundle session calendar; a metric is blank unless its full window is present.")

    disp = datasets.get("CONSTITUENT_DISPERSION") or []
    if disp:
        st.caption("Current-universe breadth is available (CURRENT_UNIVERSE_CONTEXT_ONLY, not point-in-time).")
        with st.expander("Current-universe breadth (not point-in-time)"):
            st.warning("CURRENT_UNIVERSE_CONTEXT_ONLY: constituent metrics use the current FMP profile universe and current market caps. Research-ineligible; not point-in-time.")
            st.dataframe(
                pd.DataFrame([{"Sector": row["sector_key"], "As of": row["as_of"], "Universe": (row.get("coverage") or {}).get("universe_size"), "% > 50DMA": fmt((row["metrics"] or {}).get("pct_above_50dma"), "fraction").replace("+", ""), "% > 200DMA": fmt((row["metrics"] or {}).get("pct_above_200dma"), "fraction").replace("+", ""), "EW std": fmt((row["metrics"] or {}).get("equal_weight_std"), None, digits=3), "CW std": fmt((row["metrics"] or {}).get("cap_weight_std"), None, digits=3), "Top5 weight": fmt((row["metrics"] or {}).get("top5_weight"), "fraction").replace("+", "")} for row in disp]),
                use_container_width=True,
                hide_index=True,
            )

    if st.toggle("Industry tables", value=False, key="sector_industries"):
        industries = load_or_stop("industries_context")
        ind_datasets = industries.get("datasets") or {}
        if not ind_datasets:
            st.info("No industry snapshots stored.")
        else:
            dataset = st.selectbox("Industry dataset", sorted(ind_datasets))
            parents = sorted(ind_datasets[dataset])
            parent = st.selectbox("Sector / theme", parents)
            items = ind_datasets[dataset][parent]
            if dataset in {"INDUSTRY_RS_VS_SECTOR_ETF", "THEME_RS"}:
                frame = pd.DataFrame([{"Industry": row["industry_key"], "ETF": row["instrument_id"], "As of": row["as_of"], "Benchmark": row["benchmark"], "1W RS": (row["metrics"] or {}).get("rs_chg_1w"), "1M RS": (row["metrics"] or {}).get("rs_chg_1m"), "3M RS": (row["metrics"] or {}).get("rs_chg_3m")} for row in items])
                st.dataframe(styled_heatmap(frame, ["1W RS", "1M RS", "3M RS"]), use_container_width=True, hide_index=True)
                st.caption("Industry ETF baskets are proxies, not exact mutually exclusive classifications.")
            else:
                frame = pd.DataFrame([{"Industry": row["industry_key"], "As of": row["as_of"], "Companies": (row["metrics"] or {}).get("company_count"), "EW 1M": (row["metrics"] or {}).get("equal_weight_return_1m"), "CW 1M (current caps)": (row["metrics"] or {}).get("cap_weight_return_1m")} for row in items])
                st.dataframe(styled_heatmap(frame, ["EW 1M", "CW 1M (current caps)"]), use_container_width=True, hide_index=True)
            heatmap_legend()


# ---- PIT / methodology ------------------------------------------------------------------

def render_pit_sector_internals() -> None:
    ctx = load_or_stop("pit_sector_context")
    page_header("Sector methodology", "Point-in-time sector internals when an artifact has been ingested. Pre-holdout only.", fred=False)
    if not ctx.get("available"):
        reason = ctx.get("reason") or "NO_ARTIFACT_INGESTED"
        if reason == "MIGRATION_PENDING":
            st.info("No PIT sector views yet. Nothing is fabricated.")
        else:
            st.info("No point-in-time sector internals have been ingested.")
        return
    latest = ctx.get("latest") or []
    artifacts = ctx.get("artifacts") or []
    provenances = sorted({str(row.get("provenance")) for row in latest})
    if any(value not in {"REAL_QC", "LOCAL_LICENSED", "REAL_HISTORICAL_PRE_2025"} for value in provenances):
        st.warning("Provenance {0}: research_eligible = FALSE. Synthetic/test artifacts are never research evidence; they demonstrate the consumer path only.".format(", ".join(provenances)))
    k = latest[0].get("return_sessions") if latest else None
    frame = pd.DataFrame(
        [
            {
                "Sector": row.get("sector"),
                "Decision date": row.get("decision_date"),
                "Members": row.get("constituent_count"),
                "% > 50d": row.get("pct_above_50d"),
                "% > 200d": row.get("pct_above_200d"),
                "EW {0}d".format(k): row.get("ew_return"),
                "CW {0}d".format(k): row.get("cw_return"),
                "Held EW {0}d".format(k): row.get("held_ew_return"),
            }
            for row in latest
        ]
    )
    value_cols = ["% > 50d", "% > 200d", "EW {0}d".format(k), "CW {0}d".format(k), "Held EW {0}d".format(k)]
    st.dataframe(styled_heatmap(frame, value_cols), use_container_width=True, hide_index=True)
    heatmap_legend()
    with st.expander("Method, coverage statuses, and ingested artifacts"):
        st.caption("Method {0} · latest decision date {1} · holdout boundary {2}.".format(", ".join(sorted({str(row.get("method_version")) for row in latest})), max(str(row.get("decision_date")) for row in latest), ", ".join(sorted({str(item.get("effective_holdout_start")) for item in artifacts if item.get("effective_holdout_start")}) or [])))
        st.caption("Breadth uses decision-time members. CW uses point-in-time caps and is never substituted with current caps. research_eligible = FALSE unless provenance is a real licensed/QC artifact.")
        if artifacts:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "SHA-256": str(item.get("artifact_sha256"))[:16] + "…",
                            "Method": item.get("method_version"),
                            "Provenance": item.get("provenance"),
                            "Research eligible": "yes" if item.get("research_eligible") else "no",
                            "Window": "{0} → {1}".format(item.get("window_start"), item.get("window_end")),
                            "Boundary": item.get("effective_holdout_start"),
                            "Ingested": age_text(item.get("ingested_at")),
                        }
                        for item in artifacts
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )


# ---- Data Health -----------------------------------------------------------------------

def render_data_health() -> None:
    health = load_or_stop("source_health")
    runs = load_or_stop("recent_runs", 200)
    ctx = load_or_stop("data_health_context")
    page_header("Data Health", "Actionable exceptions first. Healthy sources are summarized compactly.", fred=False)
    stale = [row for row in health if str(row.get("freshness_status") or "").upper() == "STALE"]
    failed = [row for row in health if str(row.get("transport_status") or "").upper() in {"FAILED", "METADATA_REJECTED", "PARTIAL"}]
    if not health:
        st.info("No sources registered yet.")
    if stale or failed:
        st.subheader("Needs attention")
        problem = stale + [row for row in failed if row not in stale]
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Source": row.get("provider") or row.get("source_id"),
                        "Dataset": row.get("freshness_dataset") or row.get("dataset"),
                        "Status": row.get("freshness_status") or row.get("transport_status"),
                        "Latest observation": row.get("latest_observation_date") or "—",
                        "Last success": age_text(row.get("last_success_at")),
                        "Detail": (row.get("last_error_redacted") or "")[:120] or "—",
                    }
                    for row in problem
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    healthy = [row for row in health if row not in stale and row not in failed]
    if healthy:
        st.subheader("Healthy sources")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Source": row.get("provider") or row.get("source_id"),
                        "Dataset": row.get("freshness_dataset") or row.get("dataset"),
                        "Latest observation": row.get("latest_observation_date") or "—",
                        "Last success": age_text(row.get("last_success_at")),
                        "Freshness": freshness_chip(row.get("freshness_status")),
                    }
                    for row in healthy
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )

    collectors = load_or_stop("ibkr_collector_status")
    quotes = load_or_stop("ibkr_quotes_latest")
    st.subheader("Windows collector (IBKR)")
    if not collectors:
        st.info("No collector heartbeat has been received. FRED and FINRA do not depend on this laptop.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Collector": row.get("collector_id"),
                        "Observed": row.get("observed_state"),
                        "Heartbeat age (s)": row.get("heartbeat_age_seconds"),
                        "Last quote": age_text(row.get("last_quote_at")),
                        "Last ingest": age_text(row.get("last_ingest_ok_at")),
                        "Delivery error": (row.get("last_delivery_error_redacted") or "")[:80] or "—",
                    }
                    for row in collectors
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
        st.caption("If the Windows laptop sleeps, TWS stops sending heartbeats and the server marks the collector offline. That is not a FINRA or FRED backend failure.")
    if quotes:
        with st.expander("Latest stored IBKR quotes"):
            st.dataframe(pd.DataFrame([{"Instrument": row.get("display_name") or row.get("instrument_id"), "Bid": row.get("bid"), "Ask": row.get("ask"), "Last": row.get("last_price"), "Status": row.get("quote_status") or "—", "Received": age_text(row.get("retrieved_at"))} for row in quotes]), use_container_width=True, hide_index=True)
    elif collectors:
        st.caption("Collector registered, but no quotes have been persisted yet.")

    quarantine = ctx.get("quarantine") or []
    finra_quarantine = ctx.get("finra_quarantine") or []
    if quarantine or finra_quarantine:
        with st.expander("Quarantined rows"):
            if quarantine:
                st.dataframe(pd.DataFrame(quarantine), use_container_width=True, hide_index=True)
            if finra_quarantine:
                st.dataframe(pd.DataFrame(finra_quarantine), use_container_width=True, hide_index=True)

    with st.expander("Ingestion diagnostics"):
        if runs:
            st.dataframe(pd.DataFrame([{"Run": row["run_id"], "Source": row["source_id"], "Dataset": row["dataset"], "Status": row["status"], "Received": row["rows_received"], "Inserted": row["rows_inserted"], "Revised": row["rows_revised"], "Rejected": row["rows_rejected"], "Error": (row["error_redacted"] or "")[:80]} for row in runs]), use_container_width=True, hide_index=True)
        detail = pd.DataFrame(
            [
                {
                    "Source": row.get("source_id"),
                    "Dataset": row.get("freshness_dataset") or row.get("dataset"),
                    "Transport": transport_chip(row.get("transport_status")),
                    "Metadata": row.get("metadata_status") or "—",
                    "Cadence": row.get("dataset_cadence") or "—",
                    "Age (d)": row.get("age_days"),
                    "Error": (row.get("last_error_redacted") or "")[:80],
                }
                for row in health
            ]
        )
        if not detail.empty:
            st.dataframe(detail, use_container_width=True, hide_index=True)

    strategies = load_or_stop("strategies_context")
    rows = strategies.get("strategies") or []
    with st.expander("Research delivery"):
        if rows:
            st.dataframe(pd.DataFrame([{"Strategy": row["strategy_id"], "Research": row["research_status"], "Economic gate": row["economic_gate"], "Delivery": row["delivery_status"]} for row in rows]), use_container_width=True, hide_index=True)
        else:
            st.caption("No research runs in PostgreSQL.")


# ---- Morning Brief ---------------------------------------------------------------------

def render_morning_context() -> None:
    snapshot = load_or_stop("morning_latest")
    index = load_or_stop("morning_index", 20)
    page_header("Morning Brief", "Deterministic snapshot of stored analytics. No generated commentary.")
    if not snapshot:
        st.info("No morning brief has been published.")
        return
    body = snapshot.get("snapshot_json") or {}
    from market_intelligence.read_models import snapshot_age

    age = snapshot_age(snapshot)
    cols = st.columns(3)
    cols[0].metric("Generated", str(snapshot["generated_at"])[:19])
    cols[1].metric("Completeness", snapshot["completeness"])
    cols[2].metric("Age", "{0} ({1} h)".format(age.get("snapshot_age_status"), age.get("age_hours")))
    if age.get("snapshot_age_status") == "STALE":
        st.warning("This brief is stale for delivery. Captured values are unchanged; see Data Health.")
    quality = snapshot.get("quality_status") or "OK"
    if quality != "OK":
        st.warning("Quality flag: {0} — {1}".format(quality, snapshot.get("quality_note") or ""))

    status = body.get("sections_status") or {}
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Section": name.replace("_", " ").title(),
                    "Status": row.get("status"),
                    "Latest observation": row.get("latest_observation_date") or "—",
                    "Freshness": ((row.get("captured_freshness") or {}).get("status") or "—") if isinstance(row, dict) else "—",
                    "Note": row.get("reason") or "",
                }
                for name, row in status.items()
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    sections = body.get("sections") or {}
    market = (sections.get("market") or {}).get("data") or {}
    if market:
        st.subheader("Market")
        st.caption(market.get("overnight_quotes", {}).get("reason", "") or "Overnight quotes unavailable.")
        lead = market.get("sector_leadership_rs_vs_spy") or market.get("sector_leadership_1m_rs_vs_spy") or []
        if lead:
            lead = sorted(lead, key=lambda row: (row.get("rs_chg_1m") is None, -(row.get("rs_chg_1m") or 0)))
            st.dataframe(styled_heatmap(pd.DataFrame([{"Sector": row["sector_key"], "ETF": row["instrument_id"], "As of": row["as_of"], "1W RS": row["rs_chg_1w"], "1M RS": row["rs_chg_1m"], "3M RS": row["rs_chg_3m"], "1M return": row["ret_1m"]} for row in lead]), ["1W RS", "1M RS", "3M RS", "1M return"]), use_container_width=True, hide_index=True)
    rates = (sections.get("rates") or {}).get("data") or {}
    if rates.get("curve"):
        st.subheader("Rates")
        st.dataframe(pd.DataFrame([{"Tenor": row["tenor"], "Yield (%)": fmt(row["yield_pct"], "pct"), "Observation": row["observation_date"], "vs prior (bps)": fmt_signed(row.get("chg_prev_bps"), "bps")} for row in rates["curve"] if row.get("yield_pct") is not None]), use_container_width=True, hide_index=True)
    credit = (sections.get("credit") or {}).get("data") or {}
    if credit.get("buckets"):
        st.subheader("Credit")
        st.dataframe(pd.DataFrame([{"Bucket": row["label"], "As of": row["as_of"], "OAS (bps)": fmt(row["oas_bps"], None, digits=0), "1D": fmt_signed(row["change_1d_bps"], "bps")} for row in credit["buckets"] if row.get("bucket") in {"ig_broad", "hy_broad"} or row.get("oas_bps") is not None][:8]), use_container_width=True, hide_index=True)
    flow = (sections.get("order_flow") or {}).get("data") or {}
    if flow:
        st.subheader("Order Flow")
        st.caption("Reported TRACE activity, not a live order book.")
        breadth = flow.get("breadth_latest") or []
        headline = next((row for row in breadth if (row.get("product_category") or "").lower() == "all securities"), None)
        if headline:
            cols = st.columns(3)
            cols[0].metric("Reported volume", fmt(headline.get("total_volume"), None), fmt_signed(headline.get("volume_change"), None) if headline.get("volume_change") is not None else None)
            cols[1].metric("Trade count", fmt(headline.get("total_trades"), None), fmt_signed(headline.get("trade_count_change"), None) if headline.get("trade_count_change") is not None else None)
            cols[2].metric("Session", str(headline.get("observation_date") or "—"))
        capped = flow.get("capped_volume") or {}
        if capped and not capped.get("headline_eligible"):
            st.caption(capped.get("identity_note") or "Capped-volume headlines are withheld until identity is validated.")

    ideas = load_or_stop("research_ideas") or []
    if ideas:
        with st.expander("Research idea registry"):
            st.dataframe(pd.DataFrame([{"Idea": row["idea_id"], "Title": row["title"], "State": row["current_state"], "Economic gate": row.get("economic_gate") or "—"} for row in ideas]), use_container_width=True, hide_index=True)
    with st.expander("Technical snapshot (SHA-256, schema, JSON)"):
        st.caption("SHA-256: `{0}` · content SHA-256: `{1}` · schema {2}.".format(snapshot["snapshot_sha256"], snapshot.get("content_sha256") or "n/a", snapshot["schema_version"]))
        st.code(strict_dumps(body, indent=2), language="json")
        st.dataframe(pd.DataFrame(index), use_container_width=True, hide_index=True)


# ---- Order Flow ------------------------------------------------------------------------

def order_flow_coverage_frame(coverage: list[dict[str, Any]]) -> pd.DataFrame:
    """Coverage table with uniform text columns (mixed HTTP int/null breaks Streamlit/pyarrow)."""
    return pd.DataFrame(
        [
            {
                "Provider": "FINRA",
                "Dataset": row.get("dataset"),
                "Group": row.get("group_name"),
                "Kind": "aggregate" if row.get("dataset") != "TRACE_INDIVIDUAL_TRANSACTIONS" else "individual trades",
                "Capability": display_cell(row.get("capability_status")),
                "HTTP": display_cell(row.get("http_status")),
                "Probe records": display_cell(row.get("probe_record_count")),
                "Latest obs": display_cell(row.get("ingest_latest_observation_date") or row.get("probe_latest_observation_date")),
                "Last retrieval": age_text(row.get("ingest_last_success_at") or row.get("probe_last_success_at")),
                "Last probe": age_text(row.get("last_probe_at")),
                "Transport": transport_chip(row.get("transport_status")),
                "Freshness": freshness_chip(row.get("freshness_status")),
                "Cadence": display_cell(row.get("dataset_cadence")),
                "Access": display_cell(row.get("source_access_status")),
            }
            for row in coverage
        ]
    )


def render_order_flow() -> None:
    ctx = load_or_stop("order_flow_context")
    breadth = ctx.get("breadth") or {}
    rows = breadth.get("rows") or []
    page_header(
        "Order Flow",
        "Corporate Bond Trading Activity — reported TRACE aggregates, not a live order book.",
        fred=False,
        as_of=str(breadth.get("latest_observation_date") or "—") if breadth.get("latest_observation_date") else None,
    )
    st.caption(ctx.get("coverage_explanation") or "These are reported activity measures, not a live order book.")
    st.caption(ctx.get("attribution") or "")

    if rows:
        headline = next((row for row in rows if (row.get("product_category") or "").lower() == "all securities"), rows[0])
        cols = st.columns(4)
        cols[0].metric("Latest session", str(headline.get("observation_date") or "—"))
        cols[1].metric("Reported volume", fmt(headline.get("total_volume"), None), fmt_signed(headline.get("volume_change"), None) if headline.get("volume_change") is not None else None)
        cols[2].metric("Trade count", fmt(headline.get("total_trades"), None), fmt_signed(headline.get("trade_count_change"), None) if headline.get("trade_count_change") is not None else None)
        adv, dec = headline.get("advances"), headline.get("declines")
        cols[3].metric("Advances − declines", fmt(None if adv is None or dec is None else adv - dec, None))
        history = ctx.get("history") or []
        volume_rows = []
        trade_rows = []
        for item in history:
            metrics = item.get("metrics_json") or {}
            if isinstance(metrics, str):
                import json as _json

                metrics = _json.loads(metrics)
            if (item.get("category_key") or "").lower() != "all securities":
                continue
            volume_rows.append({"observation_date": item.get("observation_date"), "reported_volume": metrics.get("totalVolume")})
            trade_rows.append({"observation_date": item.get("observation_date"), "trade_count": metrics.get("totalTrades")})
        if volume_rows:
            history_chart(volume_rows, x="observation_date", y="reported_volume", title="Reported volume", units="source units")
        if trade_rows:
            history_chart(trade_rows, x="observation_date", y="trade_count", title="Trade count", units="trades")
    else:
        st.info("No corporate market-breadth aggregates stored.")

    sentiment = ctx.get("sentiment") or {}
    net = sentiment.get("customer_net")
    if net:
        st.metric("Customer net volume (dealer-reported)", fmt(net.get("customer_net_volume"), None), help=net.get("perspective"))
        st.caption(net.get("perspective") or "")

    with st.expander("Category tables"):
        if rows:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Category": row.get("product_category"),
                            "Observation": row.get("observation_date"),
                            "Volume": fmt(row.get("total_volume"), None),
                            "Δ volume": fmt_signed(row.get("volume_change"), None) if row.get("volume_change") is not None else "—",
                            "Trades": fmt(row.get("total_trades"), None),
                            "Advances": fmt(row.get("advances"), None),
                            "Declines": fmt(row.get("declines"), None),
                        }
                        for row in rows
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            st.caption(breadth.get("overlap_note") or "")
        sent_rows = sentiment.get("rows") or []
        if sent_rows:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Trade type": row.get("trade_type"),
                            "Product category": row.get("product_category"),
                            "Observation": row.get("observation_date"),
                            "Volume": fmt(row.get("total_volume"), None),
                            "Trades": fmt(row.get("total_trades"), None),
                        }
                        for row in sent_rows
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

    capped = ctx.get("capped_volume") or {}
    cap_rows = capped.get("rows") or []
    with st.expander("Capped / reported volume"):
        if not capped.get("headline_eligible"):
            st.warning(capped.get("identity_note") or "Capped-volume identity is not validated for headlines.")
        if cap_rows:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Grade": row.get("grade_code"),
                            "144A": row.get("rule_144a_flag"),
                            "Reporting period": row.get("reporting_period") or "—",
                            "Publication date": row.get("observation_date"),
                            "Trade count": fmt(row.get("total_trade_count"), None),
                            "Capped volume qty": fmt(row.get("total_volume_quantity"), None),
                        }
                        for row in cap_rows
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            st.caption(capped.get("capped_note") or "")
        else:
            st.caption("No capped-volume aggregates stored.")

    individual = ctx.get("individual_trades") or {}
    if not individual.get("available"):
        st.caption("Individual TRACE transactions are not available through this Query API entitlement.")
    coverage = list(ctx.get("coverage") or [])
    with st.expander("Dataset coverage and limitations"):
        if not coverage:
            coverage = [{"dataset": "FINRA Query API", "group_name": "fixedIncomeMarket", "capability_status": "NEVER_ATTEMPTED", "coverage_note": "Backend has not recorded a FINRA probe yet."}]
        st.dataframe(order_flow_coverage_frame(coverage), use_container_width=True, hide_index=True)
        notes = [row.get("coverage_note") for row in coverage if row.get("coverage_note")]
        if notes:
            st.caption(notes[0])


__all__ = [
    "display_cell",
    "order_flow_coverage_frame",
    "render_credit_overview",
    "render_data_health",
    "render_macro_overview",
    "render_market_pulse",
    "render_morning_context",
    "render_order_flow",
    "render_pit_sector_internals",
    "render_rates_curve",
    "render_sector_rotation_v2",
]
