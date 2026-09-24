"""Renderers for the DB-only Market Intelligence pages (Streamlit).

Each ``render_*`` reads through :func:`market_intelligence.ui.load_or_stop` (read-only
role, cached) and renders populated / empty / stale / unconfigured / partial states without
fabricating numbers. Missing values render as "—".
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import streamlit as st

from market_intelligence.bond_ladder import LadderBond, aggregate_ladder, theoretical_rungs
from market_intelligence.bond_tax import ASSET_CORPORATE, ASSET_MUNI, ASSET_TREASURY, BondTaxInputs, TaxAssumptions, compare_three, muni_treasury_ratio
from market_intelligence.bonds import interpolate_par_yield
from market_intelligence.catalog import CATALOG_BY_ID, CURVE_TENORS
from market_intelligence.curve_compare import (
    AFTER_CURRENT_MESSAGE,
    COMPARE_CUSTOM,
    COMPARE_NONE,
    COMPARE_OPTIONS,
    NO_CURVE_MESSAGE,
    REASON_AFTER_CURRENT,
    comparison_target,
    format_curve_date,
    parse_curve_date,
    source_display,
)
from market_intelligence.freshness import is_current_status
from market_intelligence.nulls import strict_dumps
from market_intelligence.page_registry import PAGE_BY_ROUTE, navigation_active, registered_page
from market_intelligence.quote_status import derive_quote_status, exception_note, overview_caption
from market_intelligence.read_models import term_structure_display_rows
from market_intelligence.signals import build_what_matters, credit_sector_coverage
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
    load_optional,
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


def _material_warning(health: list[dict[str, Any]], extra: list[str] | None = None, *, displayed_dates: list[Any] | None = None) -> str | None:
    """One concise notice when a problem affects an important displayed signal."""
    notes = list(extra or [])
    actionable = _actionable_health(health)
    # Prefer rows whose latest observation feeds the overview dates we actually show.
    relevant = actionable
    if displayed_dates:
        shown = {str(value)[:10] for value in displayed_dates if value}
        dated = [
            row
            for row in actionable
            if str(row.get("latest_observation_date") or "")[:10] in shown
            or str(row.get("source_id") or "") in {"TREASURY", "FRED", "FINRA_QUERY", "ICE", "SECTOR"}
        ]
        if dated:
            relevant = dated
    stale = [row for row in relevant if str(row.get("freshness_status") or "").upper() in {"STALE", "STALE_INGESTION"}]
    failed = [row for row in relevant if str(row.get("transport_status") or "").upper() in {"FAILED", "METADATA_REJECTED"}]
    if stale:
        notes.append("Some displayed market data is stale relative to its release cadence; last valid stored values are shown.")
    elif failed:
        notes.append("A source used on this page failed its last retrieval; last valid stored values are shown.")
    return " ".join(notes[:1]) if notes else None


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


def _optional_data(result: dict[str, Any]) -> Any:
    return result.get("data")


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



def _cboe_metric_row(latest: list[dict[str, Any]], metric_id: str) -> dict[str, Any] | None:
    for row in latest:
        if row.get("metric_id") == metric_id:
            return row
    return None


def _cboe_ok_value(row: dict[str, Any] | None) -> Any:
    if not row or row.get("status") != "OK":
        return None
    return row.get("value")


def _render_cboe_core_panel(core: dict[str, Any] | None) -> None:
    st.subheader("Cboe core volatility (LiveVol)")
    st.caption(
        "Direct LiveVol All Access metrics stored in PostgreSQL. Provider observation time and ingestion time are separate. "
        "VIX index term structure is not a VIX futures curve and is never labeled contango or backwardation. Not a trading recommendation."
    )
    if not core or not core.get("available"):
        st.info((core or {}).get("reason") or "Cboe core volatility views are not published yet.")
        return
    latest = list(core.get("latest") or [])
    history = list(core.get("history") or [])
    health = list(core.get("health") or [])
    if not latest:
        st.info("No LiveVol core metrics stored yet.")
        return
    stale = [row for row in health if str(row.get("freshness_status") or "").upper() == "STALE"]
    failed = [row for row in health if str(row.get("transport_status") or "").upper() in {"FAILED", "AUTH_FAILED", "ENTITLEMENT_REQUIRED", "TRIAL_LIMIT", "RATE_LIMITED", "UNAVAILABLE", "PARTIAL"}]
    if stale or failed:
        st.warning("Stored LiveVol snapshot is not a live quote. See Data Health for auth, trial, or publication lag.")
    vix = _cboe_metric_row(latest, "VIX_SPOT")
    skew = _cboe_metric_row(latest, "SPX_25D_SKEW")
    spread = _cboe_metric_row(latest, "SPX_IV30_MINUS_SPX_RV20") or _cboe_metric_row(latest, "VIX_MINUS_SPX_RV20")
    slope = _cboe_metric_row(latest, "VIX_INDEX_FRONT_TO_BACK")
    detail = (vix or {}).get("detail_json") or {}
    if isinstance(detail, str):
        import json as _json
        detail = _json.loads(detail)
    observed = (vix or {}).get("provider_observation_ts") or (detail or {}).get("observation_ts")
    ingested = (vix or {}).get("ingested_at")
    st.caption("Provider observation: {0}".format(observed or "unavailable"))
    st.caption("Ingested: {0}".format(ingested or "unavailable"))
    curve_state = (slope or {}).get("detail_json") or {}
    if isinstance(curve_state, str):
        import json as _json
        curve_state = _json.loads(curve_state)
    state_name = curve_state.get("curve_state") if isinstance(curve_state, dict) else None
    state_label = {"upward_sloping": "Upward-sloping", "downward_sloping": "Downward-sloping", "flat": "Flat"}.get(state_name, "Unavailable")
    cols = st.columns(4)
    cols[0].metric("VIX", fmt(_cboe_ok_value(vix), "vol_points"), fmt_signed((detail or {}).get("change_1d"), None) if _cboe_ok_value(vix) is not None and (detail or {}).get("change_1d") is not None else None)
    cols[1].metric("25Δ SPX skew", fmt(_cboe_ok_value(skew), "vol_points"))
    spread_label = "IV − RV"
    if spread and spread.get("metric_id") == "VIX_MINUS_SPX_RV20" and spread.get("status") == "OK":
        spread_label = "VIX − RV20"
    elif spread and spread.get("metric_id") == "SPX_IV30_MINUS_SPX_RV20" and spread.get("status") == "OK":
        spread_label = "IV30 − RV20"
    cols[2].metric(spread_label, fmt(_cboe_ok_value(spread), "vol_points"))
    cols[3].metric("VIX index curve", state_label if _cboe_ok_value(slope) is not None else "Unavailable")
    st.markdown("**VIX index term structure**")
    st.caption("Volatility-index tenors (VIX9D, VIX, VIX3M, VIX6M, VIX1Y). Distinct from the VX futures curve below.")
    tenor_ids = {"9D": "VIX_9D", "1M": "VIX_1M", "3M": "VIX_3M", "6M": "VIX_6M", "1Y": "VIX_1Y"}
    curve_rows = []
    for tenor, metric_id in tenor_ids.items():
        row = _cboe_metric_row(latest, metric_id)
        level = _cboe_ok_value(row)
        if level is None:
            continue
        curve_rows.append({"tenor": tenor, "implied_vol": float(level)})
    if len(curve_rows) >= 2:
        st.line_chart(pd.DataFrame(curve_rows).set_index("tenor"))
        if vix and vix.get("as_of"):
            st.caption("Observation date {0}.".format(vix.get("as_of")))
    else:
        st.info("No VIX index term structure stored.")
    st.markdown("**25Δ SPX skew**")
    put = _cboe_metric_row(latest, "SPX_25D_PUT_IV")
    call = _cboe_metric_row(latest, "SPX_25D_CALL_IV")
    skew_detail = (skew or {}).get("detail_json") or {}
    if isinstance(skew_detail, str):
        import json as _json
        skew_detail = _json.loads(skew_detail)
    if _cboe_ok_value(skew) is None:
        reason = (skew_detail or {}).get("reason") if isinstance(skew_detail, dict) else None
        st.info("25Δ SPX skew is unavailable{0}.".format("" if not reason else " ({0})".format(reason)))
    else:
        st.caption("Downside puts are priced {0} vol points above equivalent calls.".format(fmt(_cboe_ok_value(skew), "vol_points")))
        put_ev = (skew_detail or {}).get("put") or {}
        call_ev = (skew_detail or {}).get("call") or {}
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Put IV": fmt(_cboe_ok_value(put), "vol_points"),
                        "Call IV": fmt(_cboe_ok_value(call), "vol_points"),
                        "Skew": fmt(_cboe_ok_value(skew), "vol_points"),
                        "Expiry": (skew_detail or {}).get("expiry") or "—",
                        "DTE": (skew_detail or {}).get("dte") if (skew_detail or {}).get("dte") is not None else "—",
                        "Put strike": put_ev.get("strike") if put_ev else "—",
                        "Call strike": call_ev.get("strike") if call_ev else "—",
                        "Put Δ": put_ev.get("delta") if put_ev else "—",
                        "Call Δ": call_ev.get("delta") if call_ev else "—",
                        "Underlying": (skew_detail or {}).get("underlying_level") if (skew_detail or {}).get("underlying_level") is not None else "—",
                        "Method": (skew_detail or {}).get("method") or "—",
                        "Status": (skew or {}).get("status") or "—",
                    }
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    st.markdown("**Implied minus realized**")
    iv_row = _cboe_metric_row(latest, "SPX_IV30") or _cboe_metric_row(latest, "VIX_SPOT")
    rv_row = _cboe_metric_row(latest, "SPX_REALIZED_VOL_20D")
    if _cboe_ok_value(spread) is None:
        st.info("IV minus realized volatility is unavailable.")
    else:
        construction = spread.get("metric_id")
        if construction == "SPX_IV30_MINUS_SPX_RV20":
            st.caption("30-day average SPX implied volatility is {0} vol points versus 20-day SPX realized volatility. This is not VIX.".format(fmt(_cboe_ok_value(spread), "vol_points")))
        else:
            st.caption("VIX is {0} vol points versus 20-day SPX realized volatility. This is not ATM implied volatility.".format(fmt(_cboe_ok_value(spread), "vol_points")))
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Implied": fmt(_cboe_ok_value(iv_row), "vol_points"),
                        "RV20": fmt(_cboe_ok_value(rv_row), "vol_points"),
                        "Spread": fmt(_cboe_ok_value(spread), "vol_points"),
                        "Construction": construction,
                    }
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    spread_id = spread.get("metric_id") if spread else "SPX_IV30_MINUS_SPX_RV20"
    spread_history = [row for row in history if row.get("metric_id") == spread_id and row.get("value") is not None]
    if spread_history:
        history_chart(spread_history, x="as_of", y="value", title="IV minus realized", units="vol points")
    vix_history = [row for row in history if row.get("metric_id") == "VIX_SPOT" and row.get("value") is not None]
    if vix_history:
        history_chart(vix_history, x="as_of", y="value", title="VIX", units="vol points")


def _render_volatility_panel(ctx: dict[str, Any] | None) -> None:
    _render_cboe_core_panel((ctx or {}).get("cboe_core"))
    st.subheader("Stored option chains and VX futures")
    st.caption("OpenBB/Cboe delayed chain snapshots and VX futures EOD when published. Delay label is provider-specific. GEX is an OI-derived gamma-exposure proxy (estimated, CALL_PLUS_PUT_MINUS_V1), not observed dealer inventory. IBKR OPRA is gated until the TWS API delivers NBBO. Distinct from the LiveVol VIX index term structure above.")
    has_openbb = bool(ctx and (ctx.get("symbols") or ctx.get("vix")))
    has_core = bool(ctx and (ctx.get("cboe_core") or {}).get("latest"))
    if not has_openbb and not has_core:
        st.info(ctx.get("reason") if ctx else "Options schema is not available. This optional source is not a platform outage.")
        open_registered_page("data_health", "Open Data Health")
        return
    if not has_openbb:
        st.info("No OpenBB chain or VX futures snapshots stored. LiveVol core metrics above may still be available.")
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
                details = load_optional("options_chain_details", row.get("underlying_symbol"), limit=40, default=[])
                chain = details.get("data") or []
                if chain:
                    st.caption("Bounded chain sample (not the full chain).")
                    st.dataframe(pd.DataFrame([{"Contract": d.get("contract_symbol"), "Exp": d.get("expiration"), "K": d.get("strike"), "CP": d.get("call_put"), "OI": d.get("open_interest"), "IV": d.get("implied_volatility")} for d in chain]), use_container_width=True, hide_index=True)
    vix = ctx.get("vix")
    if vix:
        cols = st.columns(4)
        cols[0].metric("VX M1", _fmt_or_dash((vix.get("m1") or {}).get("price"), None), (vix.get("m1") or {}).get("expiration"))
        cols[1].metric("VX M2", _fmt_or_dash((vix.get("m2") or {}).get("price"), None), (vix.get("m2") or {}).get("expiration"))
        cols[2].metric("M2−M1", _fmt_or_dash(vix.get("m2_minus_m1_points"), None))
        cols[3].metric("Front shape", str(vix.get("front_shape") or "—"))
        st.caption("VIX FUTURES TERM STRUCTURE — VX_EOD 4 p.m. ET levels on {0}. Not official settlement. Not live quotes. Front-curve shape only. Contango/backwardation language applies only to this futures curve, not to the VIX index tenors above.".format(vix.get("observation_date") or "unknown date"))
        points = vix.get("points") or []
        if points:
            st.dataframe(pd.DataFrame([{"Expiry": p.get("expiration"), "Precision": p.get("precision"), "Price": p.get("price")} for p in points]), use_container_width=True, hide_index=True)


def render_options_volatility() -> None:
    page_header(
        "Options & Volatility",
        "LiveVol core regime metrics plus stored OpenBB option chains and VX futures. Spot VIX index levels are distinct from a VIX futures curve.",
        fred=False,
    )
    result = load_optional("options_volatility_context", default={})
    ctx = result.get("data") or {}
    if not result.get("available"):
        st.info("Options read model is unavailable ({0}). This optional category is not a platform outage.".format(result.get("error") or "query failed"))
        open_registered_page("data_health", "Open Data Health")
        return
    _render_volatility_panel(ctx)
    open_registered_page("options", "Refresh Options & Volatility")
    open_registered_page("data_health", "Open Data Health")


def _overview_displayed_dates(
    *,
    rates: dict[str, Any],
    credit: dict[str, Any],
    sectors: dict[str, Any],
    order_flow: dict[str, Any],
) -> list[Any]:
    dates: list[Any] = []
    for row in rates.get("curve") or []:
        dates.append(row.get("observation_date"))
    for row in credit.get("buckets") or []:
        dates.append(row.get("as_of"))
    for row in (sectors.get("datasets") or {}).get("ETF_RS_VS_SPY") or []:
        dates.append(row.get("as_of"))
    for row in (order_flow.get("breadth") or {}).get("rows") or []:
        dates.append(row.get("observation_date"))
    return dates


def _render_overview_cards(
    *,
    rates: dict[str, Any],
    credit: dict[str, Any],
    sectors: dict[str, Any],
    macro: dict[str, Any],
    order_flow: dict[str, Any],
    options: dict[str, Any] | None,
    horizon: str,
) -> None:
    cards: list[dict[str, Any]] = []
    rs_rows = (sectors.get("datasets") or {}).get("ETF_RS_VS_SPY") or []
    metric = {"1D": "ret_1d", "1W": "ret_1w", "1M": "ret_1m"}.get(horizon, "ret_1d")
    day_ranked = [row for row in rs_rows if (row.get("metrics") or {}).get(metric) is not None]
    if day_ranked:
        day_ranked = sorted(day_ranked, key=lambda row: -((row.get("metrics") or {}).get(metric) or 0))
        lead = day_ranked[0]
        lag = day_ranked[-1]
        cards.append(
            {
                "title": "Equities & Sectors",
                "primary": str(lead.get("sector_key") or "—"),
                "primary_delta": fmt_signed((lead.get("metrics") or {}).get(metric), "fraction"),
                "support": "Laggard {0} {1}".format(lag.get("sector_key") or "—", fmt_signed((lag.get("metrics") or {}).get(metric), "fraction")),
                "as_of": lead.get("as_of"),
                "route": "sectors",
                "link": "Open Equities & Sectors",
            }
        )
    curve = [row for row in rates.get("curve") or [] if row.get("tenor") == "10Y" and row.get("yield_pct") is not None]
    if curve:
        ten = curve[0]
        slope = (rates.get("slopes") or {}).get("2s10s") or (rates.get("slopes") or {}).get("2Y10Y") or {}
        slope_txt = fmt_signed(slope.get("value"), "bps") if isinstance(slope, dict) and slope.get("value") is not None else "—"
        cards.append(
            {
                "title": "Rates & Curve",
                "primary": fmt(ten.get("yield_pct"), "pct"),
                "primary_delta": fmt_signed(ten.get("chg_prev_bps"), "bps") if ten.get("chg_prev_bps") is not None else None,
                "support": "2s10s {0}".format(slope_txt),
                "as_of": ten.get("observation_date"),
                "route": "rates",
                "link": "Open Rates & Curve",
            }
        )
    buckets = [row for row in (credit.get("buckets") or []) if row.get("bucket") in {"ig_broad", "hy_broad"}]
    if buckets:
        ig = next((row for row in buckets if row["bucket"] == "ig_broad"), buckets[0])
        hy = next((row for row in buckets if row["bucket"] == "hy_broad"), None)
        support = "HY {0}".format(fmt(hy.get("oas_bps"), "bps").replace("+", "") if hy and hy.get("oas_bps") is not None else "—")
        if hy and hy.get("change_1d_bps") is not None:
            support += " ({0})".format(fmt_signed(hy.get("change_1d_bps"), "bps"))
        cards.append(
            {
                "title": "Credit",
                "primary": "IG {0}".format(fmt(ig.get("oas_bps"), "bps").replace("+", "") if ig.get("oas_bps") is not None else "—"),
                "primary_delta": fmt_signed(ig.get("change_1d_bps"), "bps") if ig.get("change_1d_bps") is not None else None,
                "support": support,
                "as_of": ig.get("as_of"),
                "route": "credit",
                "link": "Open Credit",
            }
        )
    cats = macro.get("categories") or {}
    for cat in PRIMARY_MACRO:
        blocks = cats.get(cat) or []
        if not blocks:
            continue
        block = blocks[0]
        transforms = block.get("transforms") or {}
        headline = transforms.get("yoy_pct") or transforms.get("qoq_saar_pct") or transforms.get("mom_change") or transforms.get("chg_4w") or transforms.get("wow_change")
        cards.append(
            {
                "title": "Macro & Liquidity",
                "primary": str(block.get("label") or block.get("series_id") or cat),
                "primary_delta": _transform_text(headline) if headline else None,
                "support": "Observation {0}".format(block.get("latest", {}).get("observation_date") or "—"),
                "as_of": block.get("latest", {}).get("observation_date"),
                "route": "macro",
                "link": "Open Macro & Liquidity",
            }
        )
        break
    commodity_blocks = cats.get("commodities") or []
    if commodity_blocks:
        block = commodity_blocks[0]
        transforms = block.get("transforms") or {}
        headline = transforms.get("chg_prev") or transforms.get("mom_pct") or transforms.get("wow_change")
        cards.append(
            {
                "title": "Commodities & Energy",
                "primary": str(block.get("label") or block.get("series_id")),
                "primary_delta": _transform_text(headline) if headline else fmt(block.get("latest", {}).get("value"), None),
                "support": "Cadence-aware release; not a live futures quote",
                "as_of": block.get("latest", {}).get("observation_date"),
                "route": "commodities",
                "link": "Open Commodities & Energy",
            }
        )
    if options and (options.get("symbols") or options.get("vix")):
        symbols = options.get("symbols") or []
        if symbols and symbols[0].get("iv_30d") is not None:
            row = symbols[0]
            cards.append(
                {
                    "title": "Options & Volatility",
                    "primary": "{0} ATM IV {1}".format(row.get("underlying_symbol") or "", _fmt_or_dash(row.get("iv_30d"), "pct")),
                    "primary_delta": row.get("delay_label"),
                    "support": "Session {0}".format(row.get("session_date") or "—"),
                    "as_of": row.get("session_date") or row.get("observation_date"),
                    "route": "options",
                    "link": "Open Options & Volatility",
                }
            )
        elif options.get("vix"):
            vix = options["vix"]
            cards.append(
                {
                    "title": "Options & Volatility",
                    "primary": "VX M1 {0}".format(_fmt_or_dash((vix.get("m1") or {}).get("price"), None)),
                    "primary_delta": str(vix.get("front_shape") or ""),
                    "support": "Futures curve EOD {0}".format(vix.get("observation_date") or "—"),
                    "as_of": vix.get("observation_date"),
                    "route": "options",
                    "link": "Open Options & Volatility",
                }
            )
    breadth = next((row for row in ((order_flow.get("breadth") or {}).get("rows") or []) if (row.get("product_category") or "").lower() == "all securities"), None)
    if breadth and len(cards) < 6:
        cards.append(
            {
                "title": "Bond Trading Activity",
                "primary": fmt(breadth.get("total_volume"), None),
                "primary_delta": fmt_signed(breadth.get("volume_change"), None) if breadth.get("volume_change") is not None else None,
                "support": "Trades {0}".format(fmt(breadth.get("total_trades"), None)),
                "as_of": breadth.get("observation_date"),
                "route": "order_flow",
                "link": "Open Bond Trading Activity",
            }
        )
    cards = cards[:6]
    if not cards:
        st.info("No category cards have usable stored observations yet.")
        return
    cols = st.columns(min(3, len(cards)))
    for index, card in enumerate(cards):
        with cols[index % len(cols)]:
            st.markdown("**{0}**".format(card["title"]))
            st.metric("Primary", card["primary"], card.get("primary_delta"), label_visibility="collapsed")
            if card.get("support"):
                st.caption(card["support"])
            if card.get("as_of"):
                st.caption("Observation {0}".format(card["as_of"]))
            open_registered_page(card["route"], card["link"])


def _render_overview_visuals(*, rates: dict[str, Any], credit: dict[str, Any], sectors: dict[str, Any], horizon: str) -> None:
    left, right = st.columns(2)
    rs_rows = (sectors.get("datasets") or {}).get("ETF_RS_VS_SPY") or []
    metric = {"1D": "ret_1d", "1W": "ret_1w", "1M": "ret_1m"}.get(horizon, "ret_1d")
    with left:
        st.subheader("Sector leadership")
        usable = [row for row in rs_rows if (row.get("metrics") or {}).get(metric) is not None]
        if usable:
            frame = pd.DataFrame(
                [
                    {
                        "Sector": row.get("sector_key") or row.get("instrument_id"),
                        "Return": (row.get("metrics") or {}).get(metric),
                    }
                    for row in usable
                ]
            ).sort_values("Return", ascending=True)
            try:
                import plotly.express as px

                fig = px.bar(frame, x="Return", y="Sector", orientation="h", title="{0} absolute return".format(horizon))
                fig.update_layout(height=360, margin=dict(l=10, r=10, t=40, b=10), xaxis_tickformat=".1%")
                fig.update_xaxes(fixedrange=True)
                fig.update_yaxes(fixedrange=True)
                st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": False})
            except ImportError:  # pragma: no cover
                st.bar_chart(frame.set_index("Sector")["Return"])
            st.caption("Absolute ETF proxy returns for the selected horizon. Relative strength lives on Equities & Sectors.")
            open_registered_page("sectors", "Open Equities & Sectors")
        else:
            st.info("No sector returns stored for {0}.".format(horizon))
    with right:
        curve = [row for row in (rates.get("curve") or []) if row.get("yield_pct") is not None]
        buckets = [row for row in (credit.get("buckets") or []) if row.get("bucket") in {"ig_broad", "hy_broad"} and row.get("oas_bps") is not None]
        mixed = bool(rates.get("curve_dates_mixed"))
        if curve and not mixed:
            st.subheader("Treasury curve")
            try:
                import plotly.express as px

                frame = pd.DataFrame([{"Tenor": row.get("tenor"), "Yield %": row.get("yield_pct"), "Obs": row.get("observation_date")} for row in curve])
                fig = px.line(frame, x="Tenor", y="Yield %", markers=True, title="Latest coherent curve ({0})".format(rates.get("complete_curve_date") or "same-date"))
                fig.update_layout(height=360, margin=dict(l=10, r=10, t=40, b=10))
                fig.update_xaxes(fixedrange=True)
                fig.update_yaxes(fixedrange=True)
                st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": False})
            except ImportError:  # pragma: no cover
                st.line_chart(pd.DataFrame(curve).set_index("tenor")["yield_pct"])
            st.caption("Same-date complete curve on {0}. Mixed-date legs are not drawn as one print.".format(rates.get("complete_curve_date") or "—"))
            open_registered_page("rates", "Open Rates & Curve")
        elif curve and mixed:
            st.subheader("Treasury curve")
            st.warning(
                "Tenors span observation dates {0}. A connected curve is withheld; open Rates for the tenor table.".format(
                    ", ".join(rates.get("curve_observation_dates") or []) or "—"
                )
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Tenor": row.get("tenor"), "Yield %": row.get("yield_pct"), "Observation": row.get("observation_date"), "Source": row.get("source_id")}
                        for row in curve
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            open_registered_page("rates", "Open Rates & Curve")
        elif buckets:
            st.subheader("Credit spreads")
            try:
                import plotly.express as px

                frame = pd.DataFrame([{"Bucket": row.get("label"), "OAS bps": row.get("oas_bps")} for row in buckets])
                fig = px.bar(frame, x="Bucket", y="OAS bps", title="Broad IG / HY OAS")
                fig.update_layout(height=360, margin=dict(l=10, r=10, t=40, b=10))
                fig.update_xaxes(fixedrange=True)
                fig.update_yaxes(fixedrange=True)
                st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": False})
            except ImportError:  # pragma: no cover
                st.bar_chart(pd.DataFrame(buckets).set_index("label")["oas_bps"])
            open_registered_page("credit", "Open Credit")
        else:
            st.subheader("Rates / Credit")
            st.info("No Treasury curve or broad credit spreads are stored for a second overview visual.")


def render_market_pulse() -> None:
    health_result = load_optional("source_health", default=[])
    health = health_result.get("data") or []
    rates = load_or_stop("rates_context")
    credit = load_or_stop("credit_context")
    sectors = load_or_stop("sectors_context")
    macro = _optional_data(load_optional("macro_context", default={})) or {}
    order_flow = _optional_data(load_optional("order_flow_overview", default={})) or {}
    options_result = load_optional("options_volatility_context", default={})
    options_vol = options_result.get("data") if options_result.get("available") else None

    displayed = _overview_displayed_dates(rates=rates, credit=credit, sectors=sectors, order_flow=order_flow)
    as_of, freshness = compact_as_of(displayed, freshness=_worst_freshness(health) if health else None)
    page_header(
        "Market Overview",
        "High-level market intelligence from validated stored observations. Detail lives on each category page.",
        as_of=as_of,
        freshness=freshness,
        warning=_material_warning(health, displayed_dates=displayed),
    )
    collectors = _optional_data(load_optional("ibkr_collector_status", default=[])) or []
    quotes = _optional_data(load_optional("ibkr_quotes_latest", default=[])) or []
    quote_state = derive_quote_status(collectors=collectors, quotes=quotes)
    st.caption(overview_caption(quote_state))

    horizon = st.radio("Market-move horizon", ("1D", "1W", "1M"), index=0, horizontal=True, key="overview_horizon")
    st.caption("Horizon applies to daily equity session moves only. Macro releases and weekly positioning keep their own cadence.")

    takeaways = build_what_matters(
        rates=rates,
        credit=credit,
        sectors=sectors,
        macro=macro,
        order_flow=order_flow,
        options=options_vol if isinstance(options_vol, dict) else None,
        horizon=horizon,
        limit=5,
    )
    st.subheader("What matters")
    if takeaways:
        for signal in takeaways:
            cols = st.columns([6, 1.2])
            cols[0].markdown("- {0}".format(signal.text))
            with cols[1]:
                open_registered_page(signal.drilldown_route, "Open {0}".format(signal.category))
    else:
        st.info("No material stored changes passed the display rules for this horizon.")

    st.subheader("Category snapshot")
    _render_overview_cards(
        rates=rates,
        credit=credit,
        sectors=sectors,
        macro=macro,
        order_flow=order_flow,
        options=options_vol if isinstance(options_vol, dict) else None,
        horizon=horizon,
    )
    _render_overview_visuals(rates=rates, credit=credit, sectors=sectors, horizon=horizon)


# ---- Macro ---------------------------------------------------------------------------

def render_macro_overview() -> None:
    macro = load_or_stop("macro_context")
    cats = macro.get("categories") or {}
    dates = [block.get("latest", {}).get("observation_date") for blocks in cats.values() for block in blocks]
    page_header(
        "Macro & Liquidity",
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

def _render_current_curve_banner(rates: dict[str, Any], *, current_date: date | None, mixed: bool) -> None:
    source = source_display(rates.get("source_ids"))
    if rates.get("fallback") and "FRED" not in source:
        source = "{0}, FRED".format(source) if source != "—" else "FRED"
    complete = "Yes" if current_date is not None and not mixed else "No"
    st.markdown("**Current Treasury Curve**")
    st.markdown("## {0}".format(format_curve_date(current_date)))
    st.caption("Source: {0}".format(source))
    st.caption("Complete curve: {0}".format(complete))


def _custom_comparison_date(current_date: date | None) -> date | None:
    if current_date is None:
        st.info("A custom comparison needs a complete current Treasury curve.")
        return None
    bounds = load_or_stop("treasury_complete_curve_bounds", current_date.isoformat())
    earliest = parse_curve_date((bounds or {}).get("earliest_complete_date"))
    latest = parse_curve_date((bounds or {}).get("latest_complete_date")) or current_date
    latest = min(latest, current_date)
    if earliest is None or earliest > latest:
        st.info(NO_CURVE_MESSAGE)
        return None
    default = min(latest, max(earliest, current_date - timedelta(days=7)))
    picked = st.date_input(
        "Comparison date",
        value=default,
        min_value=earliest,
        max_value=latest,
        key="rates_custom_date",
        help="Type a date or use the calendar. Weekends and holidays use the prior complete Treasury curve.",
    )
    return parse_curve_date(picked)


def _load_comparison_curve(mode: str, current_date: date | None, custom_date: date | None) -> dict[str, Any] | None:
    if current_date is None:
        return None
    target = comparison_target(mode, current_date, custom_date)
    if target is None:
        return None
    return load_or_stop("complete_treasury_curve_on_or_before", target.isoformat(), current_date.isoformat())


def _render_comparison_note(comparison: dict[str, Any] | None, *, current_date: date | None) -> None:
    if not comparison:
        return
    if not comparison.get("found"):
        if comparison.get("reason") == REASON_AFTER_CURRENT:
            st.info(AFTER_CURRENT_MESSAGE)
        else:
            st.info(NO_CURVE_MESSAGE)
        return
    st.caption("{0} — Current · {1} — Comparison".format(format_curve_date(current_date), format_curve_date(comparison.get("effective_date"))))
    if comparison.get("fallback"):
        st.caption("Requested date: {0}".format(format_curve_date(comparison.get("requested_date"))))
        st.caption("Using nearest prior complete curve: {0}".format(format_curve_date(comparison.get("effective_date"))))


def _comparison_frame(rows: list[dict[str, Any]]) -> pd.DataFrame | None:
    present = [row for row in rows if row.get("yield_pct") is not None and row.get("observation_date")]
    if not present:
        return None
    dates = {str(row.get("observation_date"))[:10] for row in present}
    if len(dates) != 1:
        return None
    frame = pd.DataFrame(present)
    frame["tenor_order"] = frame["tenor"].map({tenor: i for i, tenor in enumerate(CURVE_TENORS)})
    return frame.sort_values("tenor_order")


def render_rates_curve() -> None:
    rates = load_or_stop("rates_context")
    curve = rates.get("curve") or []
    present = [row for row in curve if row.get("yield_pct") is not None]
    page_header(
        "Rates & Curve",
        "Treasury curve in percent; changes in basis points. A connected curve is drawn only for a same-date complete print.",
        as_of=compact_as_of([row.get("observation_date") for row in present])[0],
        warning="Tenors have different observation dates: {0}. Connected curve withheld.".format(", ".join(rates.get("curve_observation_dates") or [])) if rates.get("curve_dates_mixed") else None,
    )
    current_date = parse_curve_date(rates.get("complete_curve_date"))
    mixed = bool(rates.get("curve_dates_mixed"))
    _render_current_curve_banner(rates, current_date=current_date, mixed=mixed)
    if not present:
        st.info("No Treasury curve observations stored.")
        return
    frame = pd.DataFrame(present)
    frame["tenor_order"] = frame["tenor"].map({tenor: i for i, tenor in enumerate(CURVE_TENORS)})
    frame = frame.sort_values("tenor_order")
    compare = st.radio("Compare with", list(COMPARE_OPTIONS), horizontal=True, key="rates_compare")
    custom_date = _custom_comparison_date(current_date) if compare == COMPARE_CUSTOM else None
    comparison = _load_comparison_curve(compare, current_date, custom_date) if compare != COMPARE_NONE and not mixed else None
    _render_comparison_note(comparison, current_date=current_date)
    try:
        import plotly.graph_objects as go

        fig = go.Figure()
        mode = "markers" if mixed else "lines+markers"
        current_name = "{0} — Current".format(format_curve_date(current_date)) if current_date and not mixed else ("Latest per tenor (dates differ)" if mixed else "Current")
        fig.add_trace(go.Scatter(x=frame["tenor"], y=frame["yield_pct"], mode=mode, name=current_name))
        if comparison and comparison.get("found") and not mixed:
            comp_frame = _comparison_frame(comparison.get("curve") or [])
            if comp_frame is not None and not comp_frame.empty:
                comp_name = "{0} — Comparison".format(format_curve_date(comparison.get("effective_date")))
                fig.add_trace(go.Scatter(x=comp_frame["tenor"], y=comp_frame["yield_pct"], mode="lines+markers", name=comp_name, line=dict(dash="dash")))
        fig.update_layout(height=360, margin=dict(l=10, r=10, t=30, b=10), yaxis_title="percent", legend=dict(orientation="h"))
        st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": False})
    except ImportError:  # pragma: no cover
        st.line_chart(frame.set_index("tenor")["yield_pct"])
    if mixed:
        st.caption("Points are latest observations per tenor and are not a single coherent curve print.")
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
    coverage = credit_sector_coverage(credit)
    page_header(
        "Credit",
        "ICE BofA option-adjusted spreads by broad market and rating. Sector/subsector OAS only where stored coverage supports it.",
        as_of=compact_as_of([row.get("as_of") for row in buckets])[0],
    )
    if not buckets:
        st.info("No credit index snapshots stored.")
        return

    view = st.radio("Credit view", ("Broad market", "Ratings", "Sectors & subsectors"), horizontal=True, key="credit_view")
    broad = [row for row in buckets if row["bucket"] in {"ig_broad", "hy_broad"}]
    rating = [row for row in buckets if row["bucket"] not in {"ig_broad", "hy_broad"}]

    if view == "Broad market":
        st.subheader("Key takeaways")
        cols = st.columns(max(1, len(broad)))
        for i, row in enumerate(broad):
            change = row.get("change_1d_bps")
            delta = None
            if change is not None:
                widen = "widening" if change > 0 else ("tightening" if change < 0 else "unchanged")
                delta = "{0} ({1})".format(fmt_signed(change, "bps"), widen)
            cols[i].metric(row["label"], "{0:.0f} bps".format(row["oas_bps"]) if row.get("oas_bps") is not None else "—", delta)
        st.caption(credit.get("attribution") or "")
        if broad:
            try:
                import plotly.express as px

                frame = pd.DataFrame([{"Bucket": row["label"], "OAS bps": row.get("oas_bps"), "1D": row.get("change_1d_bps")} for row in broad])
                fig = px.bar(frame, x="Bucket", y="OAS bps", title="Broad IG / HY OAS")
                fig.update_layout(height=320, margin=dict(l=10, r=10, t=40, b=10))
                fig.update_xaxes(fixedrange=True)
                fig.update_yaxes(fixedrange=True)
                st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": False})
            except ImportError:  # pragma: no cover
                pass
        ids = [row["series_id"] for row in broad] or [row["series_id"] for row in buckets]
        chosen = st.selectbox("History", ids, format_func=lambda series_id: CATALOG_BY_ID[series_id].label if series_id in CATALOG_BY_ID else series_id, key="credit_hist_broad")
        history = load_or_stop("metric_history", "{0}.oas_bps".format(chosen))
        history_chart(history, x="as_of", y="value", title="{0} OAS (bps)".format(CATALOG_BY_ID[chosen].label if chosen in CATALOG_BY_ID else chosen), units="bps")

    elif view == "Ratings":
        st.subheader("Rating buckets")
        if rating:
            table = pd.DataFrame(
                [
                    {
                        "Bucket": row["label"],
                        "As of": row["as_of"],
                        "OAS (bps)": row["oas_bps"],
                        "1D": row["change_1d_bps"],
                        "1W": row["change_1w_bps"],
                        "1M": row["change_1m_bps"],
                    }
                    for row in rating
                ]
            )
            st.dataframe(
                table.style.format(
                    {
                        "OAS (bps)": lambda v: "—" if v is None else "{0:.0f}".format(v),
                        "1D": lambda v: "—" if v is None else "{0:+.0f}".format(v),
                        "1W": lambda v: "—" if v is None else "{0:+.0f}".format(v),
                        "1M": lambda v: "—" if v is None else "{0:+.0f}".format(v),
                    },
                    na_rep="—",
                ),
                use_container_width=True,
                hide_index=True,
            )
            try:
                import plotly.express as px

                fig = px.bar(table.dropna(subset=["OAS (bps)"]), x="Bucket", y="OAS (bps)", title="Rating-bucket OAS")
                fig.update_layout(height=320, margin=dict(l=10, r=10, t=40, b=10))
                fig.update_xaxes(fixedrange=True)
                fig.update_yaxes(fixedrange=True)
                st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": False})
            except ImportError:  # pragma: no cover
                pass
        else:
            st.info("No rating-bucket OAS rows are stored.")
        ids = [row["series_id"] for row in rating] or [row["series_id"] for row in buckets]
        chosen = st.selectbox("History", ids, format_func=lambda series_id: CATALOG_BY_ID[series_id].label if series_id in CATALOG_BY_ID else series_id, key="credit_hist_rating")
        history = load_or_stop("metric_history", "{0}.oas_bps".format(chosen))
        history_chart(history, x="as_of", y="value", title="{0} OAS (bps)".format(CATALOG_BY_ID[chosen].label if chosen in CATALOG_BY_ID else chosen), units="bps")

    else:
        st.subheader("Sectors & subsectors")
        st.info(coverage.get("note") or "Sector OAS is unavailable.")
        st.caption("Status: {0}".format(coverage.get("status")))
        st.dataframe(
            pd.DataFrame(
                [
                    {"Available dimension": "Broad market / rating buckets", "Series count": len(coverage.get("available_series") or [])},
                    {"Available dimension": "Sector OAS", "Series count": 0},
                    {"Available dimension": "Subsector OAS", "Series count": 0},
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
        with st.expander("Missing inputs"):
            for item in coverage.get("missing_inputs") or []:
                st.markdown("- {0}".format(item))
            st.caption("FINRA activity aggregates are not a substitute sector-spread chart.")
            open_registered_page("order_flow", "Open Bond Trading Activity")

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
    from market_intelligence.equity_live import (
        attach_live_1d_to_sector_rows,
        attach_live_1d_to_subgroup_rows,
        preferred_canonical_sector_rows,
        subgroup_rows_for_parent,
    )

    sectors = load_or_stop("sectors_context")
    live = load_or_stop("equity_live_context")
    chart = load_or_stop("spy_rsp_chart_context")
    datasets = sectors.get("datasets") or {}
    raw_rs = datasets.get("ETF_RS_VS_SPY") or []
    rs_rows = attach_live_1d_to_sector_rows(preferred_canonical_sector_rows(raw_rs), live)
    page_header(
        "Equities & Sectors",
        "Sector comparison from stored snapshots. Live 1D uses current last vs prior completed session; longer windows stay finalized EOD.",
        fred=False,
        as_of=compact_as_of([row.get("as_of") for row in rs_rows])[0],
        live_quotes_label=live.get("quotes_as_of_label") or "Live quotes unavailable",
    )
    if not datasets:
        st.info("No sector snapshots stored.")
        return
    pit = load_or_stop("pit_sector_context")
    if not pit.get("available"):
        st.caption("Point-in-time constituent internals are not ingested. Current-universe breadth below is a snapshot, not PIT.")

    if chart.get("available") and chart.get("series"):
        st.subheader("SPY vs RSP")
        frame = pd.DataFrame(chart["series"])
        frame["date"] = pd.to_datetime(frame["date"])
        try:
            import plotly.graph_objects as go

            fig = go.Figure()
            fig.add_trace(go.Scatter(x=frame["date"], y=frame["SPY"], name="SPY", mode="lines"))
            fig.add_trace(go.Scatter(x=frame["date"], y=frame["RSP"], name="RSP", mode="lines"))
            fig.update_layout(
                height=280,
                margin=dict(l=10, r=10, t=30, b=10),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
                yaxis_title="Indexed (start = 100)",
                xaxis_title=None,
            )
            st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": False})
        except Exception:  # noqa: BLE001 - plotly optional fallback
            st.line_chart(frame.set_index("date")[["SPY", "RSP"]])
        st.caption(
            "Cap-weight (SPY) vs equal-weight (RSP) S&P 500 proxies, normalized to 100 at the first common observation. "
            "Data through {0}. Stored EQUITY_EOD closes only.".format(chart.get("as_of") or "—")
        )
    else:
        st.caption("SPY vs RSP chart unavailable until both series have stored EQUITY_EOD history (RSP is in the dashboard EOD universe).")

    if rs_rows:
        metric = st.radio(
            "Metric",
            ["Relative Strength", "Absolute Return"],
            horizontal=True,
            key="sector_metric",
            index=0,
        )
        as_ofs = sorted({str(row["as_of"]) for row in rs_rows if row.get("as_of")})
        basis = rs_rows[0].get("return_basis") if rs_rows else "—"
        st.caption(
            "Benchmark SPY · EOD as of {0} · {1} · ETF proxy, not a constituent aggregate. "
            "Live 1D is session-current; 1W+ are finalized historical windows.".format(", ".join(as_ofs), basis)
        )
        if metric.startswith("Relative"):
            frame = pd.DataFrame(
                [
                    {
                        "Sector": row.get("canonical_sector") or row["sector_key"],
                        "ETF": row["instrument_id"],
                        "Live 1D RS": (row["metrics"] or {}).get("live_rs_chg_1d"),
                        "1W RS": (row["metrics"] or {}).get("rs_chg_1w"),
                        "1M RS": (row["metrics"] or {}).get("rs_chg_1m"),
                        "3M RS": (row["metrics"] or {}).get("rs_chg_3m"),
                        "6M RS": (row["metrics"] or {}).get("rs_chg_6m"),
                        "12M RS": (row["metrics"] or {}).get("rs_chg_12m"),
                    }
                    for row in rs_rows
                ]
            )
            value_cols = [name for name in ("Live 1D RS", "1W RS", "1M RS", "3M RS", "6M RS", "12M RS") if name in frame.columns]
            st.caption(
                "Live 1D RS = (1 + live asset return) / (1 + live SPY return) − 1. "
                "Longer RS windows use finalized adjusted closes on aligned sessions."
            )
        else:
            frame = pd.DataFrame(
                [
                    {
                        "Sector": row.get("canonical_sector") or row["sector_key"],
                        "ETF": row["instrument_id"],
                        "Live 1D Return": (row["metrics"] or {}).get("live_ret_1d"),
                        "1W Return": (row["metrics"] or {}).get("ret_1w"),
                        "1M Return": (row["metrics"] or {}).get("ret_1m"),
                        "3M Return": (row["metrics"] or {}).get("ret_3m"),
                        "6M Return": (row["metrics"] or {}).get("ret_6m"),
                        "12M Return": (row["metrics"] or {}).get("ret_12m"),
                    }
                    for row in rs_rows
                ]
            )
            value_cols = [
                name
                for name in ("Live 1D Return", "1W Return", "1M Return", "3M Return", "6M Return", "12M Return")
                if name in frame.columns
            ]
            st.caption("Live 1D Return = current last / prior completed-session close − 1. Longer windows stay finalized EOD.")
        order = {name: i for i, name in enumerate(CANONICAL_SECTORS)}
        frame["_o"] = frame["Sector"].map(lambda name: order.get(name, 99))
        frame = frame.sort_values(["_o", "Sector"]).drop(columns="_o")
        st.dataframe(styled_heatmap(frame, value_cols), use_container_width=True, hide_index=True)
        heatmap_legend(scale_note="each column uses its own near-zero band")

        names = [row.get("canonical_sector") or row["sector_key"] for row in rs_rows]
        chosen = st.selectbox("Sector drilldown", names, key="sector_drilldown")
        selected = next((row for row in rs_rows if (row.get("canonical_sector") or row["sector_key"]) == chosen), rs_rows[0])
        metrics = selected.get("metrics") or {}
        cols = st.columns(6)
        cols[0].metric("Live 1D return", fmt_signed(metrics.get("live_ret_1d"), "fraction") if metrics.get("live_ret_1d") is not None else "—")
        cols[1].metric("Live 1D RS vs SPY", fmt_signed(metrics.get("live_rs_chg_1d"), "fraction") if metrics.get("live_rs_chg_1d") is not None else "—")
        cols[2].metric("1M RS vs SPY", fmt_signed(metrics.get("rs_chg_1m"), "fraction") if metrics.get("rs_chg_1m") is not None else "—")
        cols[3].metric("1M ETF return", fmt_signed(metrics.get("ret_1m"), "fraction") if metrics.get("ret_1m") is not None else "—")
        cols[4].metric("vs 50DMA", fmt_signed(metrics.get("pct_vs_50dma"), "fraction") if metrics.get("pct_vs_50dma") is not None else "—")
        cols[5].metric("vs 200DMA", fmt_signed(metrics.get("pct_vs_200dma"), "fraction") if metrics.get("pct_vs_200dma") is not None else "—")
        stale = (selected.get("coverage") or {}).get("price_status")
        if stale == "STALE":
            st.warning("{0} last EOD price predates the bundle as-of; windowed metrics stay blank rather than being relabelled current.".format(selected.get("instrument_id")))

        with st.expander("ETF trend and risk (finalized EOD)"):
            trend = pd.DataFrame(
                [
                    {
                        "Sector": row.get("canonical_sector") or row["sector_key"],
                        "ETF": row["instrument_id"],
                        "1M return": (row["metrics"] or {}).get("ret_1m"),
                        "3M return": (row["metrics"] or {}).get("ret_3m"),
                        "12M return": (row["metrics"] or {}).get("ret_12m"),
                        "vs 50DMA": (row["metrics"] or {}).get("pct_vs_50dma"),
                        "vs 200DMA": (row["metrics"] or {}).get("pct_vs_200dma"),
                    }
                    for row in rs_rows
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
                pd.DataFrame(
                    [
                        {
                            "Sector": row["sector_key"],
                            "As of": row["as_of"],
                            "Universe": (row.get("coverage") or {}).get("universe_size"),
                            "% > 50DMA": fmt((row["metrics"] or {}).get("pct_above_50dma"), "fraction").replace("+", ""),
                            "% > 200DMA": fmt((row["metrics"] or {}).get("pct_above_200dma"), "fraction").replace("+", ""),
                            "EW std": fmt((row["metrics"] or {}).get("equal_weight_std"), None, digits=3),
                            "CW std": fmt((row["metrics"] or {}).get("cap_weight_std"), None, digits=3),
                            "Top5 weight": fmt((row["metrics"] or {}).get("top5_weight"), "fraction").replace("+", ""),
                        }
                        for row in disp
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

    st.subheader("Industry & Subgroup Leadership")
    st.warning(
        "Current-context baskets and industry rows are NOT point-in-time and are not appropriate for trusted historical research."
    )
    industries = load_or_stop("industries_context")
    ind_datasets = industries.get("datasets") or {}
    if not ind_datasets:
        st.info("No industry or subgroup snapshots stored. A meaningful subgroup is shown as unavailable rather than guessed.")
        return

    parent_options = sorted(
        {
            parent
            for dataset_name, by_parent in ind_datasets.items()
            if dataset_name in {"INDUSTRY_RS_VS_SECTOR_ETF", "THEME_RS", "SUBGROUP_UNAVAILABLE"}
            for parent in (by_parent or {})
        }
        | set(CANONICAL_SECTORS)
    )
    # Prefer canonical sector labels first in the selector.
    parent_options = [p for p in CANONICAL_SECTORS if p in parent_options] + [
        p for p in parent_options if p not in CANONICAL_SECTORS
    ]
    parent = st.selectbox("Sector / Theme", parent_options, key="industry_parent")
    sub_metric = st.radio(
        "Metric",
        ["Relative Strength", "Absolute Returns"],
        horizontal=True,
        key="industry_metric",
        index=0,
    )
    items, unavailable = subgroup_rows_for_parent(industries, parent)
    items = attach_live_1d_to_subgroup_rows(items, live, parent_sector=parent)
    if not items and unavailable:
        st.info("No curated subgroup is defined for this sector. Coverage is not guessed.")
        frame = pd.DataFrame(
            [
                {
                    "Sector": row.get("parent_sector_key") or parent,
                    "Group": row["industry_key"],
                    "As of": row["as_of"],
                    "Status": (row.get("coverage") or {}).get("status") or "UNAVAILABLE",
                }
                for row in unavailable
            ]
        )
        st.dataframe(frame, use_container_width=True, hide_index=True)
        return
    if not items:
        st.info("No industry or subgroup rows for this selection.")
        return

    if sub_metric.startswith("Relative"):
        frame = pd.DataFrame(
            [
                {
                    "Group": row["industry_key"],
                    "Members / ETF": ", ".join((row.get("coverage") or {}).get("membership") or [])
                    or row.get("instrument_id")
                    or "—",
                    "Live 1D RS": (row["metrics"] or {}).get("live_rs_chg_1d"),
                    "1W RS": (row["metrics"] or {}).get("rs_chg_1w"),
                    "1M RS": (row["metrics"] or {}).get("rs_chg_1m"),
                    "3M RS": (row["metrics"] or {}).get("rs_chg_3m"),
                    "6M RS": (row["metrics"] or {}).get("rs_chg_6m"),
                    "12M RS": (row["metrics"] or {}).get("rs_chg_12m"),
                }
                for row in items
            ]
        )
        value_cols = [c for c in ("Live 1D RS", "1W RS", "1M RS", "3M RS", "6M RS", "12M RS") if c in frame.columns]
        st.caption("Subgroup Live 1D RS is versus the parent sector ETF live return (for example Financials → XLF).")
    else:
        frame = pd.DataFrame(
            [
                {
                    "Group": row["industry_key"],
                    "Members / ETF": ", ".join((row.get("coverage") or {}).get("membership") or [])
                    or row.get("instrument_id")
                    or "—",
                    "Live 1D Return": (row["metrics"] or {}).get("live_ret_1d"),
                    "1W Return": (row["metrics"] or {}).get("ret_1w"),
                    "1M Return": (row["metrics"] or {}).get("ret_1m"),
                    "3M Return": (row["metrics"] or {}).get("ret_3m"),
                    "6M Return": (row["metrics"] or {}).get("ret_6m"),
                    "12M Return": (row["metrics"] or {}).get("ret_12m"),
                }
                for row in items
            ]
        )
        value_cols = [
            c for c in ("Live 1D Return", "1W Return", "1M Return", "3M Return", "6M Return", "12M Return") if c in frame.columns
        ]
    # Drop columns that are entirely missing so we only show supported horizons.
    keep = ["Group", "Members / ETF"] + [
        c for c in value_cols if frame[c].notna().any()
    ]
    frame = frame[keep]
    value_cols = [c for c in value_cols if c in frame.columns]
    st.dataframe(styled_heatmap(frame, value_cols), use_container_width=True, hide_index=True)
    st.caption(
        "Custom baskets are daily-rebalanced equal-dollar indexes (current-context membership, not PIT). "
        "Missing required members leave Live 1D blank rather than changing composition. "
        "SMH/XSD and other ETF comparisons are not exclusive subindustries."
    )
    chosen_g = st.selectbox("Group details", [row["industry_key"] for row in items], key="industry_member")
    detail = next((row for row in items if row["industry_key"] == chosen_g), items[0])
    cov = detail.get("coverage") or {}
    st.caption(
        "Members: {0} · used: {1} · missing: {2} · live used: {3} · live missing: {4} · method: {5}".format(
            ", ".join(cov.get("membership") or []) or "—",
            ", ".join(cov.get("members_used") or []) or "—",
            ", ".join(cov.get("members_missing") or []) or "—",
            ", ".join(cov.get("live_members_used") or []) or "—",
            ", ".join(cov.get("live_members_missing") or []) or "—",
            cov.get("weighting") or detail.get("return_basis") or "—",
        )
    )
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

# Manual EMMA website review is the current workflow. Keep the registered backend
# available for a future subscription, but omit it from this operational surface.
_DATA_HEALTH_HIDDEN_SOURCES = frozenset({"MSRB_EMMA"})

def render_data_health() -> None:
    health = [row for row in load_or_stop("source_health") if row.get("source_id") not in _DATA_HEALTH_HIDDEN_SOURCES]
    runs = [row for row in load_or_stop("recent_runs", 200) if row.get("source_id") not in _DATA_HEALTH_HIDDEN_SOURCES]
    ctx = load_or_stop("data_health_context")
    page_header("Data Health", "Available sources, needs attention, and optional/not-active products. Healthy sources are summarized compactly.", fred=False)
    stale = [row for row in health if str(row.get("freshness_status") or "").upper() in {"STALE", "STALE_INGESTION"} and not row.get("retired_optional")]
    failed = [row for row in health if str(row.get("transport_status") or "").upper() in {"FAILED", "METADATA_REJECTED", "PARTIAL"} and not row.get("retired_optional")]
    gated = [row for row in health if row.get("retired_optional") or row.get("optional_disabled")]
    if not health:
        st.info("No sources registered yet.")
    if stale or failed:
        st.subheader("Needs attention")
        st.caption("STALE_INGESTION means our pipeline is behind the provider. HEALTHY_PUBLICATION_LAG / CURRENT_TO_SOURCE means the provider has not published a newer print. Counts exclude deliberately disabled products.")
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
                        "Feature affected": exception_note(row),
                    }
                    for row in problem
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    healthy = [row for row in health if row not in stale and row not in failed and row not in gated]
    if healthy:
        st.subheader("Available")
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
    if gated:
        with st.expander("Optional / not active ({0})".format(len(gated)), expanded=False):
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
                            "Latest observation": row.get("latest_observation_date") or "—",
                            "Why": exception_note(row),
                        }
                        for row in gated
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
                    "sources": len({row.get("source_id") for row in health}),
                    "stale": len(stale),
                    "failed_transport": len(failed),
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
        "Bond Trading Activity",
        "Corporate bond TRACE aggregates — reported volume, trade count, breadth, and customer measures. Not a live institutional order book.",
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
    order_flow = _optional_data(load_optional("order_flow_overview", default={})) or {}
    health = _optional_data(load_optional("source_health", default=[])) or []
    page_header(
        "Bond Research",
        "Individual securities, tax-aware comparisons, and analytical ladders. Market-wide rates, credit, and TRACE activity live on their category pages.",
        fred=False,
    )
    st.caption("Streamlit does not place orders or fetch live brokers. Manual municipal inputs are scenarios, not live quotes.")
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
    ctx_cols = st.columns(3)
    with ctx_cols[0]:
        open_registered_page("rates", "Rates & Curve context")
    with ctx_cols[1]:
        open_registered_page("credit", "Credit context")
    with ctx_cols[2]:
        open_registered_page("order_flow", "Bond Trading Activity")
    fi_ids = {
        "TREASURY",
        "FRED",
        "FINRA_QUERY",
        "FINRA_TRACE",
        "IBKR_CORPORATE_BONDS",
        "IBKR_MUNICIPAL_BONDS",
    }
    fi_health = [row for row in health if str(row.get("source_id") or "") in fi_ids]
    if fi_health:
        with st.expander("Bond-research source states"):
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
    tab_corporates, tab_munis, tab_rv, tab_ladder = st.tabs(["Corporates / TRACE", "Municipals", "Relative value", "Ladder builder"])
    curve = [row for row in (rates.get("curve") or []) if row.get("yield_pct") is not None]
    buckets = credit.get("buckets") or []
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
        open_registered_page("order_flow", "Open Bond Trading Activity")
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
        "Commodities & Energy",
        "Stored energy and metal levels from PostgreSQL. Copper (PCOPPUSDM) is monthly. Not a futures curve and not a live quote.",
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
    "render_options_volatility",
    "render_order_flow",
    "render_pit_sector_internals",
    "render_rates_curve",
    "render_sector_rotation_v2",
]
