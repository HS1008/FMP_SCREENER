"""Renderers for the DB-only Market Intelligence pages (Streamlit).

Each ``render_*`` reads through :func:`market_intelligence.ui.load_or_stop` (read-only
role, cached) and renders populated / empty / stale / unconfigured / partial states without
fabricating numbers. Missing values render as "—".
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from market_intelligence.bond_ladder import LadderBond, aggregate_ladder, theoretical_rungs
from market_intelligence.bond_tax import ASSET_CORPORATE, ASSET_MUNI, ASSET_TREASURY, BondTaxInputs, TaxAssumptions, compare_three, muni_treasury_ratio
from market_intelligence.bonds import interpolate_par_yield
from market_intelligence.catalog import CATALOG_BY_ID, CURVE_TENORS
from market_intelligence.freshness import is_current_status
from market_intelligence.nulls import strict_dumps
from market_intelligence.overview import build_session_changes, build_what_changed
from market_intelligence.page_registry import PAGE_BY_ROUTE, navigation_active, registered_page
from market_intelligence.quote_status import derive_quote_status, exception_note, overview_caption
from market_intelligence.read_models import term_structure_display_rows
from market_intelligence.surface_status import worst_surface_status
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
    "commodities": "Commodities",
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


def _actionable_health(health: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop FRED DGS/DFII fallbacks when the Treasury primary curve is current."""
    treasury_ok = any(
        str(row.get("source_id") or "") == "TREASURY" and is_current_status(row.get("freshness_status"))
        for row in health
    )
    out = []
    for row in health:
        dataset = str(row.get("freshness_dataset") or row.get("dataset") or "")
        if treasury_ok and str(row.get("source_id") or "") == "FRED" and (
            dataset.startswith("series:DGS") or dataset.startswith("series:DFII")
        ):
            continue
        out.append(row)
    return out


def _worst_freshness(health: list[dict[str, Any]]) -> str | None:
    return worst_surface_status(_actionable_health(health))


def _material_warning(health: list[dict[str, Any]], extra: list[str] | None = None) -> str | None:
    notes = list(extra or [])
    stale = [row for row in _actionable_health(health) if str(row.get("freshness_status") or "").upper() in {"STALE", "STALE_INGESTION"}]
    failed = [row for row in _actionable_health(health) if str(row.get("transport_status") or "").upper() in {"FAILED", "METADATA_REJECTED"}]
    if stale:
        notes.append("{0} source(s) are stale relative to their release cadence.".format(len(stale)))
    if failed:
        notes.append("{0} source(s) failed the last retrieval. Last valid stored values are shown.".format(len(failed)))
    return " ".join(notes) if notes else None


def open_registered_page(route_id: str, label: str) -> None:
    """Link using the same registry as ``st.navigation``. No silent caption fallback.

    Isolated wrapper AppTests (no navigation) omit the link rather than pretending
    a broken path worked.
    """
    spec = PAGE_BY_ROUTE[route_id]
    page = registered_page(route_id)
    if page is not None:
        st.page_link(page, label=label)
        return
    if navigation_active():
        target = spec.file_path or spec.url_path
        st.page_link(target, label=label)
        return
    # Standalone ``pages/*.py`` render: the production entry point is dashboard.py.
    _ = spec


# ---- Overview ---------------------------------------------------------------------------

def _fmt_or_dash(value: Any, units: str | None = None) -> str:
    if value is None:
        return "—"
    return fmt(value, units)


def _curve_yield_map(curve_rows: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in curve_rows:
        tenor = row.get("tenor")
        series_id = CURVE_TENORS.get(str(tenor)) if tenor is not None else None
        if series_id and row.get("yield_pct") is not None:
            out[series_id] = float(row["yield_pct"])
    return out


def _render_commodities_panel(cats: dict[str, Any], *, heading: str = "Commodities") -> None:
    blocks = cats.get("commodities") or []
    if not blocks:
        return
    if heading:
        st.subheader(heading)
    st.caption("FRED/EIA and IMF levels. Missing observations stay missing. Not a futures curve.")
    rows = []
    for block in blocks:
        transforms = block.get("transforms") or {}
        headline = transforms.get("chg_prev") or transforms.get("mom_pct") or transforms.get("yoy_pct")
        rows.append(
            {
                "Series": block.get("label") or block.get("series_id"),
                "Latest": fmt(block["latest"].get("value"), None),
                "Units": block["latest"].get("units") or block.get("catalog_units"),
                "Change": _transform_text(headline),
                "Observation": block["latest"].get("observation_date"),
                "Source": "FRED",
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _render_volatility_panel(ctx: dict[str, Any] | None) -> None:
    st.subheader("Options and volatility")
    st.caption("Stored PostgreSQL option snapshots only. Delay label is provider-specific (not hardcoded Cboe). Not a live quote. GEX is an OI-derived gamma-exposure proxy (estimated, CALL_PLUS_PUT_MINUS_V1), not observed dealer inventory. IBKR OPRA is gated until the TWS API delivers NBBO.")
    if not ctx or (not ctx.get("symbols") and not ctx.get("vix")):
        st.info(ctx.get("reason") if ctx else "Options schema is not available. This optional source is not a platform outage.")
        return
    symbols = ctx.get("symbols") or []
    if symbols:
        frame = pd.DataFrame(
            [
                {
                    "Underlying": row.get("underlying_symbol"),
                    "Session": row.get("session_date") or "unknown",
                    "As-of": str(row.get("observation_time") or row.get("observation_date") or "unknown"),
                    "Delay": row.get("delay_label") or "—",
                    "30D ATM IV": _fmt_or_dash(row.get("iv_30d"), "pct") if row.get("iv_30d") is not None else "—",
                    "25Δ skew (vol pts)": _fmt_or_dash(row.get("selected_skew_25d"), None) if row.get("selected_skew_25d") is not None else "—",
                    "P/C OI": _fmt_or_dash(row.get("oi_put_call"), None) if row.get("oi_put_call") is not None else "—",
                    "Estimated GEX proxy (signed)": _fmt_or_dash((row.get("gex") or {}).get("signed_net"), None),
                    "Largest gamma conc.": ((row.get("gex") or {}).get("largest_gamma_concentration") or {}).get("strike") or "—",
                }
                for row in symbols
            ]
        )
        st.dataframe(frame, use_container_width=True, hide_index=True)
        with st.expander("Term structure and methodology"):
            for row in symbols:
                st.caption("{0} session {1} · {2}".format(row.get("underlying_symbol"), row.get("session_date") or "unknown", row.get("delay_label")))
                term = term_structure_display_rows(row.get("atm_term_structure") or [])
                if term:
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "Expiry": p.get("expiration"),
                                    "DTE (session)": p.get("dte_session"),
                                    "Spot ATM IV": _fmt_or_dash(p.get("atm_iv"), "pct") if p.get("atm_iv") is not None else "—",
                                    "Call IV": _fmt_or_dash(p.get("call_iv"), "pct") if p.get("call_iv") is not None else "—",
                                    "Put IV": _fmt_or_dash(p.get("put_iv"), "pct") if p.get("put_iv") is not None else "—",
                                    "One-sided": "yes" if p.get("one_sided") else "no",
                                }
                                for p in term
                            ]
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )
                details = load_or_stop("options_chain_details", row.get("underlying_symbol"), limit=40)
                if details:
                    st.caption("Bounded chain sample (not the full chain).")
                    st.dataframe(pd.DataFrame([{"Contract": d.get("contract_symbol"), "Exp": d.get("expiration"), "K": d.get("strike"), "CP": d.get("call_put"), "OI": d.get("open_interest"), "IV": d.get("implied_volatility")} for d in details]), use_container_width=True, hide_index=True)
    vix = ctx.get("vix")
    if vix:
        cols = st.columns(4)
        cols[0].metric("VX M1", _fmt_or_dash((vix.get("m1") or {}).get("price"), None), (vix.get("m1") or {}).get("expiration"))
        cols[1].metric("VX M2", _fmt_or_dash((vix.get("m2") or {}).get("price"), None), (vix.get("m2") or {}).get("expiration"))
        cols[2].metric("M2−M1", _fmt_or_dash(vix.get("m2_minus_m1_points"), None))
        cols[3].metric("Front shape", str(vix.get("front_shape") or "—"))
        st.caption("VX_EOD 4 p.m. ET levels on {0}. Not official settlement. Not live quotes. Front-curve shape only.".format(vix.get("observation_date") or "unknown date"))
        points = vix.get("points") or []
        if points:
            st.dataframe(pd.DataFrame([{"Expiry": p.get("expiration"), "Precision": p.get("precision"), "Price": p.get("price")} for p in points]), use_container_width=True, hide_index=True)


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
    collectors = load_or_stop("ibkr_collector_status")
    quotes = load_or_stop("ibkr_quotes_latest")
    quote_state = derive_quote_status(collectors=collectors, quotes=quotes)
    st.caption(overview_caption(quote_state))

    session = build_session_changes(rates=rates, credit=credit, sectors=sectors, order_flow=order_flow)
    st.subheader("Day to day")
    st.caption("Prior-session moves only. Macro releases use their own publication lag and are listed under What changed.")
    if session:
        st.dataframe(pd.DataFrame([{"Area": row["area"], "Change": row["text"], "Period": row["period"]} for row in session]), use_container_width=True, hide_index=True)
    else:
        st.info("No prior-session changes are stored yet. Sector 1-day ETF returns appear after the next snapshot that includes ret_1d.")

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
        day_ranked = [row for row in rs_rows if (row.get("metrics") or {}).get("ret_1d") is not None]
        if day_ranked:
            day_ranked = sorted(day_ranked, key=lambda row: -((row.get("metrics") or {}).get("ret_1d") or 0))
            lead = day_ranked[0]
            headline_cols[2].metric("Sector lead (1D)", str(lead.get("sector_key") or "—"), fmt_signed((lead.get("metrics") or {}).get("ret_1d"), "fraction") if (lead.get("metrics") or {}).get("ret_1d") is not None else None)
        else:
            ranked = sorted(rs_rows, key=lambda row: ((row.get("metrics") or {}).get("rs_chg_1m") is None, -((row.get("metrics") or {}).get("rs_chg_1m") or 0)))
            lead = ranked[0]
            headline_cols[2].metric("Sector lead (1M RS)", str(lead.get("sector_key") or "—"), fmt_signed((lead.get("metrics") or {}).get("rs_chg_1m"), "fraction") if (lead.get("metrics") or {}).get("rs_chg_1m") is not None else None)
    breadth = next((row for row in ((order_flow.get("breadth") or {}).get("rows") or []) if (row.get("product_category") or "").lower() == "all securities"), None)
    if breadth:
        headline_cols[3].metric("Bond activity (volume)", fmt(breadth.get("total_volume"), None), fmt_signed(breadth.get("volume_change"), None) if breadth.get("volume_change") is not None else None)

    options_vol = load_or_stop("options_volatility_context")
    _render_volatility_panel(options_vol)

    st.subheader("Sector leadership and weakness")
    if rs_rows:
        frame = pd.DataFrame(
            [{"Sector": row["sector_key"], "ETF proxy": row["instrument_id"], "As of": row["as_of"], "Source": row.get("source_id"), "1D return": (row["metrics"] or {}).get("ret_1d"), "1D RS": (row["metrics"] or {}).get("rs_chg_1d"), "1W RS": (row["metrics"] or {}).get("rs_chg_1w"), "1M RS": (row["metrics"] or {}).get("rs_chg_1m"), "3M RS": (row["metrics"] or {}).get("rs_chg_3m"), "1M return": (row["metrics"] or {}).get("ret_1m")} for row in rs_rows]
        ).sort_values("1M RS", ascending=False, na_position="last")
        heat_cols = [name for name in ("1D return", "1D RS", "1W RS", "1M RS", "3M RS", "1M return") if name in frame.columns]
        st.dataframe(styled_heatmap(frame, heat_cols), use_container_width=True, hide_index=True)
        heatmap_legend()
        st.caption("1D return and 1D RS use the last completed aligned session. RS is the change in the ETF/SPY adjusted-close ratio (not arithmetic excess). Missing values are blank, not zero. Source and as-of are per row.")
        open_registered_page("sectors", "Open Sectors")
    else:
        st.info("No sector snapshots stored.")

    st.subheader("Treasury yields")
    if curve:
        cols = st.columns(len(curve))
        for i, row in enumerate(curve):
            cols[i].metric("{0}".format(row["tenor"]), fmt(row.get("yield_pct"), "pct"), fmt_signed(row.get("chg_prev_bps"), "bps") if row.get("chg_prev_bps") is not None else None)
        open_registered_page("rates", "Open Rates")
    else:
        st.info("No Treasury curve data stored.")

    st.subheader("Credit spreads")
    if buckets:
        cols = st.columns(len(buckets))
        for i, row in enumerate(buckets):
            cols[i].metric(row["label"], fmt(row.get("oas_bps"), "bps").replace("+", ""), fmt_signed(row.get("change_1d_bps"), "bps") if row.get("change_1d_bps") is not None else None)
        st.caption(credit.get("attribution") or "")
        open_registered_page("credit", "Open Credit")
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
        open_registered_page("macro", "Open Macro")
    else:
        st.info("No macro observations stored.")

    _render_commodities_panel(cats)
    open_registered_page("commodities", "Open Commodities")

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
        open_registered_page("order_flow", "Open Order Flow")
        open_registered_page("fixed_income", "Open Fixed Income")
    else:
        st.info("No corporate-bond activity aggregates stored.")
        open_registered_page("fixed_income", "Open Fixed Income")


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

    _render_commodities_panel(cats, heading="Commodities")
    open_registered_page("commodities", "Open Commodities")

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
    st.caption(
        "Complete curve date {0} · sources {1}{2}".format(
            rates.get("complete_curve_date") or "—",
            ", ".join(rates.get("source_ids") or []) or "—",
            " · FRED fallback" if rates.get("fallback") else "",
        )
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
            pd.DataFrame([{"Tenor": row["tenor"], "Yield (%)": fmt(row["yield_pct"], "pct"), "Observation": row["observation_date"], "Source": row.get("source_id"), "Fallback": row.get("fallback"), "vs prior (bps)": fmt_signed(row.get("chg_prev_bps"), "bps"), "1W (bps)": fmt_signed(row.get("chg_1w_bps"), "bps"), "1M (bps)": fmt_signed(row.get("chg_1m_bps"), "bps"), "3M (bps)": fmt_signed(row.get("chg_3m_bps"), "bps")} for row in present]),
            use_container_width=True,
            hide_index=True,
        )
        missing_tenors = [row["tenor"] for row in curve if row.get("yield_pct") is None]
        if missing_tenors:
            st.caption("Tenors without data (not extrapolated): {0}".format(", ".join(missing_tenors)))
        slope_cols = st.columns(max(1, len(slopes)))
        for i, (name, entry) in enumerate(slopes.items()):
            slope_cols[i % len(slope_cols)].metric(name, _transform_text(entry))
        partial = rates.get("partial_newer") or []
        if partial:
            st.caption("Newer incomplete observations (not mixed into the complete curve):")
            st.dataframe(
                pd.DataFrame([{"Tenor": row["tenor"], "Yield (%)": fmt(row.get("yield_pct"), "pct"), "Observation": row.get("observation_date"), "Source": row.get("source_id")} for row in partial]),
                use_container_width=True,
                hide_index=True,
            )
        derived = rates.get("derived_nominal_minus_real") or []
        if derived:
            st.caption("Derived nominal minus real (same date only). Distinct from published breakevens.")
            st.dataframe(pd.DataFrame(derived), use_container_width=True, hide_index=True)

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
            frame = pd.DataFrame([{"Sector": row["sector_key"], "Kind": row["entity_kind"], "ETF": row["instrument_id"], "As of": row["as_of"], "1D": (row["metrics"] or {}).get("rs_chg_1d"), "1W": (row["metrics"] or {}).get("rs_chg_1w"), "1M": (row["metrics"] or {}).get("rs_chg_1m"), "3M": (row["metrics"] or {}).get("rs_chg_3m"), "6M": (row["metrics"] or {}).get("rs_chg_6m"), "12M": (row["metrics"] or {}).get("rs_chg_12m")} for row in rows])
            value_cols = ["1D", "1W", "1M", "3M", "6M", "12M"]
            st.caption("Relative strength = change in the ETF/benchmark adjusted-close ratio on aligned sessions. 1D is side-by-side with longer horizons.")
        else:
            frame = pd.DataFrame([{"Sector": row["sector_key"], "Kind": row["entity_kind"], "ETF": row["instrument_id"], "As of": row["as_of"], "1D": (row["metrics"] or {}).get("ret_1d"), "1W": (row["metrics"] or {}).get("ret_1w"), "1M": (row["metrics"] or {}).get("ret_1m"), "3M": (row["metrics"] or {}).get("ret_3m"), "12M": (row["metrics"] or {}).get("ret_12m")} for row in rows])
            value_cols = ["1D", "1W", "1M", "3M", "12M"]
            st.caption("Absolute ETF price returns on adjusted close. Missing values are blank, not zero. Dividend treatment is not proven from the vendor name alone.")
        order = {name: i for i, name in enumerate(CANONICAL_SECTORS)}
        frame["_o"] = frame["Sector"].map(lambda name: order.get(name, 99))
        frame = frame.sort_values(["_o", "Sector"]).drop(columns="_o")
        st.dataframe(styled_heatmap(frame, value_cols), use_container_width=True, hide_index=True)
        heatmap_legend()

        names = [row["sector_key"] for row in rows]
        chosen = st.selectbox("Sector drilldown", names, key="sector_drilldown")
        selected = next((row for row in rows if row["sector_key"] == chosen), rows[0])
        metrics = selected.get("metrics") or {}
        cols = st.columns(6)
        cols[0].metric("1D ETF return", fmt_signed(metrics.get("ret_1d"), "fraction") if metrics.get("ret_1d") is not None else "—")
        cols[1].metric("1D RS vs {0}".format(benchmark), fmt_signed(metrics.get("rs_chg_1d"), "fraction") if metrics.get("rs_chg_1d") is not None else "—")
        cols[2].metric("1M RS vs {0}".format(benchmark), fmt_signed(metrics.get("rs_chg_1m"), "fraction") if metrics.get("rs_chg_1m") is not None else "—")
        cols[3].metric("1M ETF return", fmt_signed(metrics.get("ret_1m"), "fraction") if metrics.get("ret_1m") is not None else "—")
        cols[4].metric("vs 50DMA", fmt_signed(metrics.get("pct_vs_50dma"), "fraction") if metrics.get("pct_vs_50dma") is not None else "—")
        cols[5].metric("vs 200DMA", fmt_signed(metrics.get("pct_vs_200dma"), "fraction") if metrics.get("pct_vs_200dma") is not None else "—")
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
            st.caption("ETF price returns on the stored adjustment basis. Windows use the session calendar; a metric is blank unless its full window is present.")

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

    st.subheader("Industry and custom subgroups")
    industries = load_or_stop("industries_context")
    ind_datasets = industries.get("datasets") or {}
    if not ind_datasets:
        st.info("No industry or subgroup snapshots stored. A meaningful subgroup is shown as unavailable rather than guessed.")
    else:
        dataset = st.selectbox("Industry dataset", sorted(ind_datasets), key="industry_dataset")
        parents = sorted(ind_datasets[dataset])
        parent = st.selectbox("Sector / theme", parents, key="industry_parent")
        items = ind_datasets[dataset][parent]
        if dataset == "SUBGROUP_UNAVAILABLE":
            st.info("No curated subgroup is defined for this sector. Coverage is not guessed.")
            frame = pd.DataFrame([{"Sector": row.get("parent_sector_key") or parent, "Group": row["industry_key"], "As of": row["as_of"], "Status": (row.get("coverage") or {}).get("status") or "UNAVAILABLE"} for row in items])
            st.dataframe(frame, use_container_width=True, hide_index=True)
        elif dataset in {"INDUSTRY_RS_VS_SECTOR_ETF", "THEME_RS"}:
            frame = pd.DataFrame([{"Group": row["industry_key"], "Id": row["instrument_id"], "As of": row["as_of"], "Benchmark": row["benchmark"], "1D ret": (row["metrics"] or {}).get("ret_1d"), "1D RS": (row["metrics"] or {}).get("rs_chg_1d"), "1W RS": (row["metrics"] or {}).get("rs_chg_1w"), "1M RS": (row["metrics"] or {}).get("rs_chg_1m"), "3M RS": (row["metrics"] or {}).get("rs_chg_3m"), "Coverage": (row.get("coverage") or {}).get("coverage")} for row in items])
            st.dataframe(styled_heatmap(frame, ["1D ret", "1D RS", "1W RS", "1M RS", "3M RS"]), use_container_width=True, hide_index=True)
            st.caption("Custom baskets are daily-rebalanced equal-dollar return indexes (current-context membership, not PIT). SMH/XSD are ETF comparisons, not exclusive subindustries. XLK remains the Technology comparison.")
            chosen_g = st.selectbox("Group membership", [row["industry_key"] for row in items], key="industry_member")
            detail = next((row for row in items if row["industry_key"] == chosen_g), items[0])
            cov = detail.get("coverage") or {}
            st.caption("Members: {0} · used: {1} · missing: {2} · method: {3}".format(
                ", ".join(cov.get("membership") or []) or "—",
                ", ".join(cov.get("members_used") or []) or "—",
                ", ".join(cov.get("members_missing") or []) or "—",
                cov.get("weighting") or detail.get("return_basis") or "—",
            ))
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
    stale = [row for row in health if str(row.get("freshness_status") or "").upper() in {"STALE", "STALE_INGESTION"} and not row.get("retired_optional")]
    failed = [row for row in health if str(row.get("transport_status") or "").upper() in {"FAILED", "METADATA_REJECTED", "PARTIAL"} and not row.get("retired_optional")]
    gated = [row for row in health if row.get("retired_optional") or row.get("optional_disabled")]
    if not health:
        st.info("No sources registered yet.")
    if gated:
        st.subheader("Disabled or rights-gated")
        st.caption("Licensing and configuration gates are not platform outages. RIGHTS_PENDING / AGREEMENT_REQUIRED are not FAILED.")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Source": row.get("source_id"),
                        "Provider": row.get("provider") or "—",
                        "Product": row.get("dataset") or row.get("freshness_dataset") or "—",
                        "State": row.get("policy_status") or row.get("access_status") or "DISABLED",
                        "Rights / access": row.get("access_status") or "—",
                        "Collection": "on" if row.get("enabled") else "off",
                        "Export": row.get("usage_scope") or "—",
                        "Cadence": row.get("dataset_cadence") or row.get("expected_cadence") or "—",
                        "Stale after (days)": display_cell(row.get("tolerance_days")),
                        "Last attempt": age_text(row.get("last_attempt_at")),
                        "Last success": age_text(row.get("last_success_at")),
                        "Latest observation": row.get("latest_observation_date") or "—",
                        "Latest failure": (row.get("last_error_redacted") or "—"),
                        "Why": exception_note(row),
                    }
                    for row in gated
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    if stale or failed:
        st.subheader("Needs attention")
        st.caption("STALE_INGESTION means our pipeline is behind the provider. HEALTHY_PUBLICATION_LAG / CURRENT_TO_SOURCE means the provider has not published a newer print.")
        problem = stale + [row for row in failed if row not in stale]
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Source": row.get("provider") or row.get("source_id"),
                        "Dataset": row.get("freshness_dataset") or row.get("dataset"),
                        "Freshness": row.get("health_label") or row.get("freshness_status") or "—",
                        "Transport": row.get("transport_status") or "—",
                        "Latest observation": row.get("latest_observation_date") or "—",
                        "Provider latest": row.get("provider_latest_observation_date") or "—",
                        "Last success": age_text(row.get("last_success_at")),
                        "Why it looks like this": exception_note(row),
                    }
                    for row in problem
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    healthy = [row for row in health if row not in stale and row not in failed and row not in gated]
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
                        "Freshness": freshness_chip(row.get("health_label") or row.get("freshness_status")),
                    }
                    for row in healthy
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )

    collectors = load_or_stop("ibkr_collector_status")
    quotes = load_or_stop("ibkr_quotes_latest")
    quote_state = derive_quote_status(collectors=collectors, quotes=quotes)
    st.subheader("Windows collector (IBKR)")
    st.caption(overview_caption(quote_state))
    if not collectors:
        st.info("No collector heartbeat has been received. FRED and FINRA do not depend on this laptop.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Collector": row.get("collector_id"),
                        "Observed": row.get("observed_state"),
                        "Heartbeat age (s)": display_cell(row.get("heartbeat_age_seconds")),
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

    options_health = ctx.get("options_volatility") or load_or_stop("options_volatility_context")
    with st.expander("OpenBB / Cboe options and VX_EOD"):
        st.caption("Optional delayed/EOD source. Disabled or rights-gated is not a platform outage.")
        if options_health.get("symbols") or options_health.get("vix"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Symbol": row.get("underlying_symbol"),
                            "Session": row.get("session_date") or "unknown",
                            "Delay": row.get("delay_label"),
                            "Contracts": row.get("contract_count"),
                            "Snapshot": row.get("snapshot_id"),
                        }
                        for row in (options_health.get("symbols") or [])
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            attempts = options_health.get("last_attempts") or []
            if attempts:
                st.caption("Latest acquisition attempts (including failures). Last valid snapshot is unchanged by a failed attempt.")
                st.dataframe(pd.DataFrame(attempts), use_container_width=True, hide_index=True)
        else:
            st.info(options_health.get("reason") or "No published options/VIX snapshots.")

    with st.expander("IBKR options and storage rights"):
        st.caption("OPRA L1 in Client Portal is not treated as TWS API entitlement. Frozen Type 2 on 2026-09-14 still returned 354 / no NBBO.")
        st.info("IBKR_OPTIONS = PROVIDER_SUPPORT_REQUIRED. IBKR_OPTIONS_STORAGE = RIGHTS_PENDING. Collection remains off.")

    with st.expander("Fixed-income source gates"):
        st.caption("Treasury, FINRA aggregates, credit OAS, munis, and IBKR bonds are independent. A blocked muni feed is not a failed fixed-income workspace.")
        fi_ids = {
            "TREASURY",
            "FRED",
            "FINRA_QUERY",
            "FINRA_TRACE",
            "IBKR_CORPORATE_BONDS",
            "IBKR_MUNICIPAL_BONDS",
            "MSRB_EMMA",
            "CFTC_COT",
            "EIA_ENERGY",
        }
        fi_rows = [row for row in health if str(row.get("source_id") or "") in fi_ids]
        if fi_rows:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Source": row.get("source_id"),
                            "State": row.get("policy_status") or row.get("access_status") or "—",
                            "Freshness": row.get("freshness_status") or "—",
                            "Latest observation": row.get("latest_observation_date") or "—",
                            "Why": exception_note(row),
                        }
                        for row in fi_rows
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("Registry rows appear after the next refresh upsert.")

    quarantine = ctx.get("quarantine") or []
    finra_quarantine = ctx.get("finra_quarantine") or []
    if quarantine or finra_quarantine:
        with st.expander("Quarantined rows"):
            if quarantine:
                st.dataframe(pd.DataFrame(quarantine), use_container_width=True, hide_index=True)
            if finra_quarantine:
                st.dataframe(pd.DataFrame(finra_quarantine), use_container_width=True, hide_index=True)

    ops = load_or_stop("ops_status")
    if ops:
        with st.expander("Platform ops summary"):
            st.caption("Engineering detail stays on Data Health. Main pages show only current / delayed / stale / unavailable / blocked.")
            st.write(
                {
                    "sources": ops.get("source_count"),
                    "stale": ops.get("stale_count"),
                    "failed_transport": ops.get("failed_transport_count"),
                    "last_successful_refresh": ops.get("last_successful_refresh"),
                    "research_runs": ops.get("research_run_count"),
                    "latest_research_update": ops.get("latest_research_update"),
                    "holdout_accessed_runs": ops.get("holdout_accessed_runs"),
                    "migrations": ops.get("migration_count"),
                    "migrations_missing_checksum": ops.get("migrations_missing_checksum"),
                    "ibkr_oldest_heartbeat_age_seconds": ops.get("ibkr_oldest_heartbeat_age_seconds"),
                    "ibkr_quote_count": ops.get("ibkr_quote_count"),
                    "deploy_git_sha": ops.get("deploy_git_sha"),
                    "dashboard_readonly_proven": ops.get("dashboard_readonly_proven"),
                    "dashboard_streamlit_readonly": ops.get("dashboard_streamlit_readonly"),
                    "systemd_still_git_pull": ops.get("systemd_still_git_pull"),
                    "systemd_cutover_proven": ops.get("systemd_cutover_proven"),
                    "immutable_current_present": ops.get("immutable_current_present"),
                    "csfml_v1_label_integrity": ops.get("csfml_v1_label_integrity"),
                    "csfml_v1_rerun_authorized": ops.get("csfml_v1_rerun_authorized"),
                    "csfml_v1_live_present": ops.get("csfml_v1_live_present"),
                    "csfml_v1_live_identity_ok": ops.get("csfml_v1_live_identity_ok"),
                    "tlt_v0_live_present": ops.get("tlt_v0_live_present"),
                    "tlt_v0_live_identity_ok": ops.get("tlt_v0_live_identity_ok"),
                    "stage1_live_present": ops.get("stage1_live_present"),
                    "stage1_live_identity_ok": ops.get("stage1_live_identity_ok"),
                    "deploy_identity_recorded_at": ops.get("deploy_identity_recorded_at"),
                }
            )

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
                    "Age (d)": display_cell(row.get("age_days")),
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
    options_section = (sections.get("options_volatility") or {}).get("data") or {}
    if options_section:
        st.subheader("Options and volatility")
        st.caption(options_section.get("delay_note") or "Cboe delayed / EOD stored snapshot.")
        rows = options_section.get("symbols") or []
        if rows:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Underlying": row.get("underlying_symbol"),
                            "Session": row.get("session_date") or "unknown",
                            "30D ATM IV": row.get("iv_30d"),
                            "25Δ skew": row.get("selected_skew_25d"),
                            "P/C OI": row.get("oi_put_call"),
                            "Estimated GEX proxy": (row.get("gex") or {}).get("signed_net"),
                        }
                        for row in rows
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
        vix = options_section.get("vix") or {}
        if vix:
            st.caption("VX front {0} on {1}".format(vix.get("front_shape") or "—", vix.get("observation_date") or "unknown"))
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


def _tax_assumptions_from_ui(prefix: str) -> TaxAssumptions:
    st.caption("Tax calculations are estimates. Actual treatment depends on jurisdiction and facts. Not tax or legal advice.")
    cols = st.columns(5)
    federal = cols[0].number_input("Federal rate", min_value=0.0, max_value=0.99, value=0.24, step=0.01, format="%.3f", key=prefix + "_fed")
    state = cols[1].number_input("State rate", min_value=0.0, max_value=0.99, value=0.05, step=0.01, format="%.3f", key=prefix + "_state")
    local = cols[2].number_input("Local rate", min_value=0.0, max_value=0.99, value=0.00, step=0.01, format="%.3f", key=prefix + "_local")
    niit = cols[3].number_input("NIIT rate", min_value=0.0, max_value=0.99, value=0.038, step=0.001, format="%.3f", key=prefix + "_niit")
    override_on = cols[4].checkbox("Use combined override", value=False, key=prefix + "_ov_on")
    override = st.number_input("Combined ordinary override", min_value=0.0, max_value=0.99, value=0.35, step=0.01, format="%.3f", key=prefix + "_ov") if override_on else None
    flags = st.columns(4)
    in_state = flags[0].checkbox("Muni is in-state", value=True, key=prefix + "_instate")
    amt = flags[1].checkbox("AMT / private-activity", value=False, key=prefix + "_amt")
    residence = flags[2].text_input("Residence state", value="", key=prefix + "_res")
    issuer = flags[3].text_input("Muni issuer state", value="", key=prefix + "_iss")
    return TaxAssumptions(
        federal_rate=federal,
        state_rate=state,
        local_rate=local,
        niit_rate=niit,
        combined_override=override,
        muni_in_state=in_state,
        muni_amt_or_pab=amt,
        residence_state=residence or None,
        issuer_state=issuer or None,
    )


def render_fixed_income() -> None:
    rates = load_or_stop("rates_context")
    credit = load_or_stop("credit_context")
    order_flow = load_or_stop("order_flow_overview")
    health = load_or_stop("source_health")
    page_header(
        "Fixed Income",
        "Treasuries, credit, TRACE activity, tax-aware comparison, and an analytical ladder. Streamlit does not place orders or fetch live brokers.",
        as_of=compact_as_of([row.get("observation_date") for row in (rates.get("curve") or [])])[0],
    )
    st.dataframe(
        pd.DataFrame(
            [
                {"Rule": "Yield is not guaranteed total return", "Applies": "all comparisons"},
                {"Rule": "Missing tax inputs or yields stay missing", "Applies": "after-tax / TEY figures"},
                {"Rule": "Callable comparisons prefer YTW when provided", "Applies": "muni / corporate"},
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    fi_ids = {
        "TREASURY",
        "FRED",
        "FINRA_QUERY",
        "FINRA_TRACE",
        "IBKR_CORPORATE_BONDS",
        "IBKR_MUNICIPAL_BONDS",
        "MSRB_EMMA",
    }
    fi_health = [row for row in health if str(row.get("source_id") or "") in fi_ids]
    if fi_health:
        st.caption("Fixed-income source states are independent. A blocked muni feed does not fail Treasuries or TRACE aggregates.")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Source": row.get("source_id"),
                        "State": row.get("policy_status") or row.get("access_status") or "—",
                        "Freshness": row.get("freshness_status") or "—",
                        "Latest observation": row.get("latest_observation_date") or "—",
                        "Why": exception_note(row),
                    }
                    for row in fi_health
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    tab_overview, tab_corporates, tab_munis, tab_rv, tab_ladder = st.tabs(["Overview", "Corporates / TRACE", "Municipals", "Relative value", "Ladder builder"])
    curve = [row for row in (rates.get("curve") or []) if row.get("yield_pct") is not None]
    buckets = credit.get("buckets") or []
    with tab_overview:
        st.subheader("Treasury curve")
        if curve:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Tenor": row.get("tenor"),
                            "Yield (%)": fmt(row.get("yield_pct"), "pct"),
                            "vs prior (bps)": fmt_signed(row.get("chg_prev_bps"), "bps"),
                            "Source": row.get("source_id"),
                        }
                        for row in curve
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            open_registered_page("rates", "Open Rates")
        else:
            st.info("No Treasury curve is stored.")
        st.subheader("Credit spreads")
        if buckets:
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Bucket": row.get("label"), "OAS (bps)": fmt(row.get("oas_bps"), None, digits=0), "1D": fmt_signed(row.get("change_1d_bps"), "bps")}
                        for row in buckets
                        if row.get("bucket") in {"ig_broad", "hy_broad", "aaa", "bbb"}
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            open_registered_page("credit", "Open Credit")
        else:
            st.info("No ICE BofA OAS snapshots are stored.")
        st.subheader("Muni / Treasury ratios")
        st.caption("Market-level ratios require a municipal curve. None is configured, so this panel stays empty rather than using a single bond.")
        st.info("MSRB/EMMA municipal curve is NOT_CONFIGURED. Ratios are not computed from a random CUSIP.")
        activity = (order_flow.get("breadth") or {}).get("rows") or []
        if activity:
            st.subheader("Corporate market activity")
            st.caption("FINRA Query aggregates, not individual TRACE prints.")
            st.dataframe(pd.DataFrame(activity), use_container_width=True, hide_index=True)
            open_registered_page("order_flow", "Open Order Flow")
    with tab_corporates:
        st.caption(
            "Primary transaction data is FINRA Query API aggregates. Individual TRACE prints remain ENTITLEMENT_REQUIRED. "
            "IBKR corporate CUSIP→conId resolution is proven; live quotes need market-data entitlement and PostgreSQL archival is RIGHTS_PENDING."
        )
        activity_corp = (order_flow.get("breadth") or {}).get("rows") or []
        if activity_corp:
            st.dataframe(pd.DataFrame(activity_corp), use_container_width=True, hide_index=True)
        else:
            st.info("No FINRA aggregates stored.")
        open_registered_page("order_flow", "Open Order Flow")
        st.subheader("Individual corporates")
        st.info("No canonical individual corporate inventory is ingested. Use Relative value for manual comparison.")
    with tab_munis:
        st.caption("Municipal TRACE-style data is a different system from FINRA TRACE. MSRB/EMMA is NOT_CONFIGURED. Do not infer liquidity.")
        st.info("Browse/screener filters stay hidden until a municipal security source is configured.")
        st.markdown(
            "\n".join(
                [
                    "- Issuer, state, CUSIP, coupon, maturity, YTM/YTW, call, rating, AMT/PAB, and trades stay **missing** until a permitted source exists.",
                    "- Manual entry on Relative value still works.",
                ]
            )
        )
    with tab_rv:
        assumptions = _tax_assumptions_from_ui("rv")
        ten_row = next((row for row in curve if row.get("tenor") == "10Y"), None)
        treasury_default = float(ten_row["yield_pct"]) if ten_row and ten_row.get("yield_pct") is not None else 4.0
        ig = next((row for row in buckets if row.get("bucket") == "ig_broad"), None)
        if ig is not None and ig.get("oas_bps") is not None:
            corp_default = treasury_default + float(ig["oas_bps"]) / 100.0
        else:
            corp_default = 5.0
        amount = st.number_input("Investment amount", min_value=0.0, value=100000.0, step=1000.0, key="rv_amount")
        mat_cols = st.columns(2)
        muni_mat = mat_cols[0].number_input("Muni / corporate maturity (years)", min_value=0.0, max_value=40.0, value=10.0, step=0.5, key="rv_mat")
        matched = interpolate_par_yield(_curve_yield_map(curve), float(muni_mat)) if curve else None
        if matched is not None:
            treasury_default = float(matched)
            st.caption("Treasury yield default is the interpolated par curve at {0:.1f}y.".format(float(muni_mat)))
        else:
            st.caption("No interpolated Treasury at that maturity. Using the stored 10Y or a labelled 4% default.")
        cols = st.columns(3)
        muni_y = cols[0].number_input("Muni yield %", min_value=-10.0, max_value=50.0, value=3.50, step=0.05, key="rv_muni_y")
        treas_y = cols[1].number_input("Treasury yield %", min_value=-10.0, max_value=50.0, value=float(treasury_default), step=0.05, key="rv_treas_y")
        corp_y = cols[2].number_input("Corporate yield %", min_value=-10.0, max_value=50.0, value=float(corp_default), step=0.05, key="rv_corp_y")
        more = st.columns(6)
        muni_d = more[0].number_input("Muni duration", min_value=0.0, value=7.0, step=0.1, key="rv_muni_d")
        treas_d = more[1].number_input("Treasury duration", min_value=0.0, value=7.0, step=0.1, key="rv_treas_d")
        corp_d = more[2].number_input("Corporate duration", min_value=0.0, value=7.0, step=0.1, key="rv_corp_d")
        muni_call = more[3].checkbox("Muni callable", value=False, key="rv_muni_call")
        muni_ytw = more[4].number_input("Muni YTW %", min_value=-10.0, max_value=50.0, value=3.20, step=0.05, key="rv_muni_ytw")
        corp_rating = more[5].text_input("Corporate rating", value="BBB", key="rv_corp_rating")
        comparison = compare_three(
            BondTaxInputs(ASSET_MUNI, muni_y, ytw_pct=muni_ytw if muni_call else None, callable=muni_call, duration=muni_d or None, maturity_years=muni_mat or None),
            BondTaxInputs(ASSET_TREASURY, treas_y, duration=treas_d or None, maturity_years=muni_mat or None),
            BondTaxInputs(ASSET_CORPORATE, corp_y, duration=corp_d or None, rating=corp_rating or None, maturity_years=muni_mat or None),
            assumptions,
            investment_amount=amount,
        )
        results = comparison["results"]
        pretax_muni_spread = _fmt_or_dash((muni_y - treas_y) if muni_y is not None else None, None)
        pretax_corp_spread = _fmt_or_dash((corp_y - treas_y) if corp_y is not None else None, None)
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        " ": "Pretax yield",
                        "Muni": _fmt_or_dash(results["muni"]["pretax_yield_pct"], "pct"),
                        "Treasury": _fmt_or_dash(results["treasury"]["pretax_yield_pct"], "pct"),
                        "Corporate": _fmt_or_dash(results["corporate"]["pretax_yield_pct"], "pct"),
                    },
                    {
                        " ": "After-tax yield",
                        "Muni": _fmt_or_dash(results["muni"]["after_tax_yield_pct"], "pct"),
                        "Treasury": _fmt_or_dash(results["treasury"]["after_tax_yield_pct"], "pct"),
                        "Corporate": _fmt_or_dash(results["corporate"]["after_tax_yield_pct"], "pct"),
                    },
                    {
                        " ": "Taxable-equivalent vs corporate",
                        "Muni": _fmt_or_dash(results["muni"]["taxable_equivalent_yield_pct"], "pct"),
                        "Treasury": "—",
                        "Corporate": _fmt_or_dash(results["corporate"]["pretax_yield_pct"], "pct"),
                    },
                    {
                        " ": "Gross interest",
                        "Muni": _fmt_or_dash(comparison["income"]["muni"]["gross_interest"], None),
                        "Treasury": _fmt_or_dash(comparison["income"]["treasury"]["gross_interest"], None),
                        "Corporate": _fmt_or_dash(comparison["income"]["corporate"]["gross_interest"], None),
                    },
                    {
                        " ": "Federal tax (ex-NIIT)",
                        "Muni": _fmt_or_dash(comparison["income"]["muni"]["federal_tax"], None),
                        "Treasury": _fmt_or_dash(comparison["income"]["treasury"]["federal_tax"], None),
                        "Corporate": _fmt_or_dash(comparison["income"]["corporate"]["federal_tax"], None),
                    },
                    {
                        " ": "State / local tax",
                        "Muni": _fmt_or_dash(comparison["income"]["muni"]["state_local_tax"], None),
                        "Treasury": _fmt_or_dash(comparison["income"]["treasury"]["state_local_tax"], None),
                        "Corporate": _fmt_or_dash(comparison["income"]["corporate"]["state_local_tax"], None),
                    },
                    {
                        " ": "After-tax income",
                        "Muni": _fmt_or_dash(comparison["income"]["muni"]["after_tax_income"], None),
                        "Treasury": _fmt_or_dash(comparison["income"]["treasury"]["after_tax_income"], None),
                        "Corporate": _fmt_or_dash(comparison["income"]["corporate"]["after_tax_income"], None),
                    },
                    {
                        " ": "After-tax yield / duration",
                        "Muni": _fmt_or_dash(comparison["risk_context"]["muni_after_tax_per_duration"], None),
                        "Treasury": _fmt_or_dash(comparison["risk_context"]["treasury_after_tax_per_duration"], None),
                        "Corporate": _fmt_or_dash(comparison["risk_context"]["corporate_after_tax_per_duration"], None),
                    },
                    {
                        " ": "After-tax spread vs Treasury",
                        "Muni": _fmt_or_dash(comparison["spreads"]["muni_minus_treasury_after_tax"], None),
                        "Treasury": "—",
                        "Corporate": _fmt_or_dash(comparison["spreads"]["corporate_minus_treasury_after_tax"], None),
                    },
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
        st.caption("Break-even Treasury yield for the muni: {0}".format(_fmt_or_dash(results["muni"]["break_even_treasury_yield_pct"], "pct")))
        st.caption("Pretax nominal spreads vs the Treasury input: muni {0}, corporate {1}.".format(pretax_muni_spread, pretax_corp_spread))
        ratio = muni_treasury_ratio(muni_y, treas_y)
        st.caption("This pair's muni/Treasury ratio is {0}. That is a pair ratio, not a market curve ratio.".format(_fmt_or_dash(ratio, None)))
        for note in comparison["disclaimer"]:
            st.caption(note)
    with tab_ladder:
        st.caption("Analytical only. Does not place orders or rebalance accounts.")
        assumptions = _tax_assumptions_from_ui("ladder")
        cols = st.columns(5)
        total = cols[0].number_input("Total investment", min_value=0.0, value=300000.0, step=10000.0, key="ladder_total")
        start_y = cols[1].number_input("First rung (years)", min_value=1, max_value=40, value=1, key="ladder_start")
        end_y = cols[2].number_input("Last rung (years)", min_value=1, max_value=40, value=10, key="ladder_end")
        step_y = cols[3].number_input("Interval (years)", min_value=1, max_value=10, value=1, key="ladder_step")
        klass = cols[4].selectbox("Asset class", ["treasury", "muni", "corporate"], key="ladder_klass")
        yld = st.number_input("Assumed rung yield %", min_value=-10.0, max_value=50.0, value=4.0, step=0.05, key="ladder_yld")
        if st.checkbox("Build theoretical ladder", value=False, key="ladder_go") and total > 0 and end_y >= start_y:
            rungs = theoretical_rungs(
                total_investment=total,
                start_year=int(start_y),
                end_year=int(end_y),
                interval_years=int(step_y),
                yield_pct=yld,
                asset_class=klass,
            )
            out = aggregate_ladder(rungs, assumptions)
            st.dataframe(pd.DataFrame(out["maturity_schedule"]), use_container_width=True, hide_index=True)
            metrics = st.columns(5)
            metrics[0].metric("Wtd pretax yield", _fmt_or_dash(out.get("weighted_average_yield_pct"), "pct"))
            metrics[1].metric("Wtd after-tax yield", _fmt_or_dash(out.get("weighted_average_after_tax_yield_pct"), "pct"))
            metrics[2].metric("Wtd maturity", _fmt_or_dash(out.get("weighted_average_maturity"), None))
            metrics[3].metric("Est. coupon income", _fmt_or_dash(out.get("estimated_coupon_income"), None))
            metrics[4].metric("Est. after-tax income", _fmt_or_dash(out.get("estimated_after_tax_income"), None))
            st.caption("This theoretical ladder assumes equal principal and a single yield. Real CUSIPs, calls, and credit differ.")
        st.subheader("Optional held-bond list")
        st.caption("Paste is not supported. Add rows for a concentration check.")
        extra = []
        if st.checkbox("Include one sample callable muni rung", value=False):
            extra.append(LadderBond("Sample muni", "muni", 2030, 25000, 3.2, 3.2, ytw_pct=2.8, callable=True, rating="AA", state="NY", issuer="Sample"))
        if extra:
            mixed = extra
            mixed_out = aggregate_ladder(mixed, assumptions)
            st.write({"call_exposure_share": mixed_out.get("call_exposure_share"), "issuer_concentration": mixed_out.get("issuer_concentration")})


def render_commodities() -> None:
    macro = load_or_stop("macro_context")
    health = load_or_stop("source_health")
    cats = macro.get("categories") or {}
    blocks = cats.get("commodities") or []
    dates = [block.get("latest", {}).get("observation_date") for block in blocks]
    page_header(
        "Commodities",
        "Stored energy and metal levels from PostgreSQL. Not a futures curve and not a live quote.",
        as_of=compact_as_of(dates)[0],
    )
    st.caption("Gold (LBMA daily) was removed from FRED in 2022. No substitute gold price is invented.")
    st.dataframe(
        pd.DataFrame(
            [
                {"Coverage": "WTI spot", "Source": "FRED / EIA (DCOILWTICO)", "Notes": "daily dollars per barrel"},
                {"Coverage": "Henry Hub natural gas", "Source": "FRED / EIA (DHHNGSP)", "Notes": "daily dollars per MMBtu"},
                {"Coverage": "Global copper", "Source": "FRED / IMF (PCOPPUSDM)", "Notes": "monthly USD per metric ton"},
                {"Coverage": "Gold spot", "Source": "FRED LBMA daily", "Notes": "unavailable — IBA/LBMA series were removed from FRED in 2022; no substitute is invented"},
                {"Coverage": "EIA inventories / production", "Source": "EIA_ENERGY", "Notes": "weekly stocks/storage when the official EIA Open Data key is set; otherwise signup at https://www.eia.gov/opendata/"},
                {"Coverage": "CFTC positioning", "Source": "CFTC_COT", "Notes": "public weekly COT watchlist; no API key"},
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    _render_commodities_panel(cats, heading="Stored levels")
    cot = load_or_stop("cftc_context")
    if cot.get("rows"):
        st.subheader("CFTC positioning")
        st.caption(cot.get("attribution") or "CFTC public COT. Futures-only watchlist.")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Market": row.get("market"),
                        "Report date": row.get("report_date"),
                        "Open interest": row.get("open_interest"),
                        "Noncomm long": row.get("noncomm_long"),
                        "Noncomm short": row.get("noncomm_short"),
                        "Noncomm net": row.get("noncomm_net"),
                        "Comm long": row.get("comm_long"),
                        "Comm short": row.get("comm_short"),
                    }
                    for row in cot["rows"]
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    eia = load_or_stop("eia_context")
    if eia.get("rows"):
        st.subheader("EIA weekly energy")
        st.caption(eia.get("attribution") or "EIA Open Data.")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Series": row.get("series_id"),
                        "Observation": row.get("observation_date"),
                        "Value": row.get("value"),
                        "Units": row.get("units") or "—",
                    }
                    for row in eia["rows"]
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    if not blocks and not cot.get("rows") and not eia.get("rows"):
        st.info("No commodity observations stored.")
    gate_ids = {"CFTC_COT", "EIA_ENERGY", "FRED"}
    gate_rows = [row for row in health if str(row.get("source_id") or "") in gate_ids]
    if gate_rows:
        st.subheader("Source gates")
        st.caption("A blocked EIA or CFTC feed does not fail the FRED energy/metal series that are already stored.")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Source": row.get("source_id"),
                        "State": row.get("policy_status") or row.get("access_status") or "—",
                        "Freshness": row.get("freshness_status") or "—",
                        "Latest observation": row.get("latest_observation_date") or "—",
                        "Why": exception_note(row),
                    }
                    for row in gate_rows
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    open_registered_page("macro", "Open Macro")


__all__ = [
    "display_cell",
    "order_flow_coverage_frame",
    "render_commodities",
    "render_credit_overview",
    "render_data_health",
    "render_fixed_income",
    "render_macro_overview",
    "render_market_pulse",
    "render_morning_context",
    "render_order_flow",
    "render_pit_sector_internals",
    "render_rates_curve",
    "render_sector_rotation_v2",
]
