"""
Technology fundamentals confirmation layer (dashboard only).

Cap-weighted aggregates and participation metrics for a filtered US Technology
universe. Not used for ranking or factor scoring.

Data: FMP `profile-bulk` (universe) plus per-symbol cached panels
(`ratios-ttm`, `income-statement-growth`, `analyst-estimates`, optional quarterly
`ratios` for prior operating margin).
"""

from __future__ import annotations

import json
import math
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import config
import data_loader
import tech_universe

TECH = "Technology"

# --- Universe thresholds (Technology fundamentals, not dispersion universe) ---
FUNDAMENTALS_MIN_MARKET_CAP: int = 5_000_000_000
FUNDAMENTALS_MIN_AVG_VOLUME: int = 1_000_000
FUNDAMENTALS_MIN_PRICE: float = 10.0

PANEL_CACHE_DIR: Path = config.OUTPUT_DIR / "cache" / "fundamentals_tech_confirmation"
PANEL_SCHEMA: str = "tech_fund_panel_v1"

# Gentle pacing between per-symbol FMP calls (bulk profile is separate).
_SYMBOL_FETCH_SLEEP_S: float = 0.04


def _panel_cache_path(symbol: str) -> Path:
    return PANEL_CACHE_DIR / f"{symbol.upper().strip()}.json"


def _read_cached_panel(symbol: str) -> dict[str, Any] | None:
    path = _panel_cache_path(symbol)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if raw.get("_schema") != PANEL_SCHEMA:
        return None
    return raw


def _write_cached_panel(symbol: str, payload: dict[str, Any]) -> None:
    PANEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _panel_cache_path(symbol)
    out = {"_schema": PANEL_SCHEMA, **payload}
    path.write_text(json.dumps(out, indent=0), encoding="utf-8")


def _fmp_list(session: Any, api_key: str, path: str, **params: str) -> list:
    data = data_loader._fmp_get(session, api_key, path, **params)  # noqa: SLF001
    return data if isinstance(data, list) else []


def _fetch_symbol_fundamental_panel(
    session: Any,
    api_key: str,
    symbol: str,
    *,
    force_refresh: bool,
) -> dict[str, Any]:
    """
    Pull one symbol's growth, margins, EPS sign, analyst EPS estimates, prior-quarter margin.
    Cached under ``PANEL_CACHE_DIR``.
    """
    sym = str(symbol).upper().strip()
    if not force_refresh:
        cached = _read_cached_panel(sym)
        if cached is not None:
            return {k: v for k, v in cached.items() if k != "_schema"}

    out: dict[str, Any] = {
        "revenueGrowth": None,
        "epsGrowth": None,
        "operatingMargin": None,
        "netMargin": None,
        "grossMargin": None,
        "eps_ttm": None,
        "epsEstimateCurrentYear": None,
        "epsEstimateNextYear": None,
        "analystRating": None,
        "operatingMargin_prior": None,
    }

    # --- TTM ratios + implied EPS (TTM) ---
    rt = _fmp_list(session, api_key, "ratios-ttm", symbol=sym)
    if rt:
        r0 = rt[0]
        out["operatingMargin"] = data_loader._safe_float(r0.get("operatingProfitMarginTTM"))  # noqa: SLF001
        out["netMargin"] = data_loader._safe_float(r0.get("netProfitMarginTTM"))  # noqa: SLF001
        out["grossMargin"] = data_loader._safe_float(r0.get("grossProfitMarginTTM"))  # noqa: SLF001
        out["eps_ttm"] = data_loader._safe_float(r0.get("netIncomePerShareTTM"))  # noqa: SLF001

    # --- YoY growth (latest reported period row) ---
    gr = _fmp_list(session, api_key, "income-statement-growth", symbol=sym, limit="4")
    if gr:
        g0 = gr[0]
        out["revenueGrowth"] = data_loader._safe_float(g0.get("growthRevenue"))  # noqa: SLF001
        out["epsGrowth"] = data_loader._safe_float(g0.get("growthEPS"))  # noqa: SLF001

    # --- Analyst consensus EPS: two nearest forward annual estimate rows ---
    est = _fmp_list(session, api_key, "analyst-estimates", symbol=sym, period="annual", limit="12")
    if est:
        rows: list[tuple[pd.Timestamp, float]] = []
        for row in est:
            d = row.get("date")
            eps_a = data_loader._safe_float(row.get("epsAvg"))  # noqa: SLF001
            if d is None or eps_a is None:
                continue
            ts = pd.to_datetime(d, errors="coerce")
            if pd.isna(ts):
                continue
            rows.append((pd.Timestamp(ts), float(eps_a)))
        rows.sort(key=lambda x: x[0])
        if len(rows) >= 2:
            out["epsEstimateCurrentYear"] = rows[0][1]
            out["epsEstimateNextYear"] = rows[1][1]
        elif len(rows) == 1:
            out["epsEstimateCurrentYear"] = rows[0][1]

    # --- Prior quarter operating margin (for breadth: expansion) ---
    rq = _fmp_list(session, api_key, "ratios", symbol=sym, period="quarter", limit="2")
    if len(rq) >= 2:
        cur_m = data_loader._safe_float(rq[0].get("operatingProfitMargin"))  # noqa: SLF001
        pri_m = data_loader._safe_float(rq[1].get("operatingProfitMargin"))  # noqa: SLF001
        if out["operatingMargin"] is None and cur_m is not None:
            out["operatingMargin"] = cur_m
        out["operatingMargin_prior"] = pri_m

    _write_cached_panel(sym, out)
    time.sleep(_SYMBOL_FETCH_SLEEP_S)
    return out


def build_fundamental_universe(
    session: Any,
    api_key: str,
    *,
    force_refresh_profiles: bool = False,
    force_refresh_panels: bool = False,
) -> pd.DataFrame:
    """
    US Technology names passing liquidity / quality screens, enriched with growth,
    margins, and analyst EPS estimates. Excludes ETFs, funds, warrants, preferreds.

    Columns:
        symbol, companyName, industry, marketCap, revenueGrowth, epsGrowth,
        operatingMargin, netMargin, grossMargin, analystRating,
        epsEstimateCurrentYear, epsEstimateNextYear,
        plus optional operatingMargin_prior when quarterly ratios succeed.
    """
    prof = tech_universe.fetch_profile_bulk_all(session, api_key)
    if prof.empty or "symbol" not in prof.columns:
        return pd.DataFrame()

    df = prof.copy()
    df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()
    df = tech_universe._normalize_exchange_column(df)  # noqa: SLF001

    if "sector" in df.columns:
        df = df[df["sector"].astype(str).str.strip() == TECH]
    else:
        return pd.DataFrame()

    if "country" in df.columns:
        df = df[df["country"].astype(str).str.upper() == "US"]

    if "isActivelyTrading" in df.columns:
        df = df[tech_universe._truthy_mask(df["isActivelyTrading"])]  # noqa: SLF001

    if "isEtf" in df.columns:
        df = df[~tech_universe._truthy_mask(df["isEtf"])]  # noqa: SLF001
    if "isFund" in df.columns:
        df = df[~tech_universe._truthy_mask(df["isFund"])]  # noqa: SLF001

    if "exchangeShortName" in df.columns:
        df = df[df["exchangeShortName"].isin(config.US_MAJOR_EXCHANGES)]

    df["marketCap"] = pd.to_numeric(df.get("marketCap"), errors="coerce")
    vol_parts: list[pd.Series] = []
    if "averageVolume" in df.columns:
        vol_parts.append(pd.to_numeric(df["averageVolume"], errors="coerce"))
    if "volume" in df.columns:
        vol_parts.append(pd.to_numeric(df["volume"], errors="coerce"))
    if vol_parts:
        avg_vol = vol_parts[0]
        for s in vol_parts[1:]:
            avg_vol = avg_vol.combine_first(s)
    else:
        avg_vol = pd.Series(np.nan, index=df.index)
    df["price"] = pd.to_numeric(df.get("price"), errors="coerce")

    df = df[df["marketCap"] > float(FUNDAMENTALS_MIN_MARKET_CAP)]
    df = df[avg_vol > float(FUNDAMENTALS_MIN_AVG_VOLUME)]
    df = df[df["price"] > float(FUNDAMENTALS_MIN_PRICE)]

    if "symbol" in df.columns:
        df = df[~df["symbol"].astype(str).map(data_loader._looks_like_warrant_or_right)]
    if "industry" in df.columns:
        df = df[~df["industry"].map(data_loader._looks_like_warrant_industry)]
    if "symbol" in df.columns:
        df = df[~df["symbol"].astype(str).map(data_loader._looks_like_preferred_or_unit)]

    df = df.dropna(subset=["symbol"])
    df = df.drop_duplicates(subset=["symbol"], keep="last")

    if df.empty:
        return pd.DataFrame()

    # Analyst rating from profile when present (string / score varies by FMP export).
    rating_col = None
    for c in ("analystConsensusRating", "rating", "ratingScore"):
        if c in df.columns:
            rating_col = c
            break

    rows_out: list[dict[str, Any]] = []
    iterator = data_loader._progress(df.to_dict("records"), desc="Tech fundamentals panels")  # noqa: SLF001
    for rec in iterator:
        sym = str(rec.get("symbol", "")).upper().strip()
        if not sym:
            continue
        panel = _fetch_symbol_fundamental_panel(session, api_key, sym, force_refresh=force_refresh_panels)
        op_m = panel.get("operatingMargin")
        eps_ttm = panel.get("eps_ttm")
        if eps_ttm is None or float(eps_ttm) <= 0:
            continue
        if op_m is None or float(op_m) <= 0:
            continue

        row: dict[str, Any] = {
            "symbol": sym,
            "companyName": str(rec.get("companyName", "") or ""),
            "industry": str(rec.get("industry", "") or "") if rec.get("industry") is not None else "",
            "marketCap": float(pd.to_numeric(rec.get("marketCap"), errors="coerce") or 0.0),
            "revenueGrowth": panel.get("revenueGrowth"),
            "epsGrowth": panel.get("epsGrowth"),
            "operatingMargin": panel.get("operatingMargin"),
            "netMargin": panel.get("netMargin"),
            "grossMargin": panel.get("grossMargin"),
            "analystRating": rec.get(rating_col) if rating_col else panel.get("analystRating"),
            "epsEstimateCurrentYear": panel.get("epsEstimateCurrentYear"),
            "epsEstimateNextYear": panel.get("epsEstimateNextYear"),
            "operatingMargin_prior": panel.get("operatingMargin_prior"),
        }
        rows_out.append(row)

    data_loader._done_progress("Tech fundamentals panels")  # noqa: SLF001
    return pd.DataFrame(rows_out).reset_index(drop=True)


def _cap_weight_series(market_caps: pd.Series) -> pd.Series:
    x = pd.to_numeric(market_caps, errors="coerce").clip(lower=0.0)
    tot = float(x.sum())
    if tot <= 0 or math.isnan(tot):
        return pd.Series(0.0, index=x.index)
    return x / tot


def calculate_cap_weighted_fundamentals(df: pd.DataFrame) -> dict[str, float | None]:
    """Cap-weighted averages of growth and margin columns (decimals, not %)."""
    keys = [
        ("revenueGrowth", "cap_weighted_revenue_growth"),
        ("epsGrowth", "cap_weighted_eps_growth"),
        ("operatingMargin", "cap_weighted_operating_margin"),
        ("netMargin", "cap_weighted_net_margin"),
        ("grossMargin", "cap_weighted_gross_margin"),
    ]
    out: dict[str, float | None] = {v: None for _, v in keys}
    if df.empty or "marketCap" not in df.columns:
        return out
    w = _cap_weight_series(df["marketCap"])
    if float(w.sum()) <= 0:
        return out
    w = w.reindex(df.index)
    for col, out_key in keys:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        m = s.notna() & w.notna() & (w > 0)
        if not m.any():
            out[out_key] = None
            continue
        w_sub = w[m] / float(w[m].sum())
        out[out_key] = float((w_sub * s[m]).sum())
    return out


def calculate_revision_metrics(df: pd.DataFrame) -> dict[str, Any]:
    """Forward EPS estimate step-up and optional revision share (not wired yet)."""
    out: dict[str, Any] = {"avg_eps_estimate_growth": None, "pct_positive_revisions": None}
    if df.empty:
        return out
    cy = pd.to_numeric(df.get("epsEstimateCurrentYear"), errors="coerce")
    ny = pd.to_numeric(df.get("epsEstimateNextYear"), errors="coerce")
    both = cy.notna() & ny.notna() & (cy.abs() > 1e-9)
    if not both.any():
        return out
    step = (ny[both] - cy[both]) / cy[both].abs()
    step = step.replace([np.inf, -np.inf], np.nan).dropna()
    if len(step):
        out["avg_eps_estimate_growth"] = float(step.mean())
    return out


def calculate_fundamental_breadth(df: pd.DataFrame) -> dict[str, Any]:
    """Participation: positive growth rates and operating-margin expansion vs prior quarter."""
    out: dict[str, Any] = {
        "pct_positive_revenue_growth": None,
        "pct_positive_eps_growth": None,
        "pct_operating_margin_expansion": None,
    }
    if df.empty:
        return out
    rg = pd.to_numeric(df.get("revenueGrowth"), errors="coerce")
    eg = pd.to_numeric(df.get("epsGrowth"), errors="coerce")
    if rg.notna().any():
        out["pct_positive_revenue_growth"] = float((rg.dropna() > 0).mean())
    if eg.notna().any():
        out["pct_positive_eps_growth"] = float((eg.dropna() > 0).mean())
    if "operatingMargin_prior" in df.columns:
        cur = pd.to_numeric(df.get("operatingMargin"), errors="coerce")
        prev = pd.to_numeric(df.get("operatingMargin_prior"), errors="coerce")
        m = cur.notna() & prev.notna()
        if m.any():
            out["pct_operating_margin_expansion"] = float((cur[m] > prev[m]).mean())
    return out


def build_fundamental_summary(df: pd.DataFrame) -> dict[str, Any]:
    """Single snapshot dict for KPIs and chart seeding."""
    cw = calculate_cap_weighted_fundamentals(df)
    rev = calculate_revision_metrics(df)
    br = calculate_fundamental_breadth(df)
    return {
        "universe_size": int(len(df)),
        **cw,
        "avg_eps_estimate_growth": rev.get("avg_eps_estimate_growth"),
        "pct_positive_revisions": rev.get("pct_positive_revisions"),
        "pct_positive_revenue_growth": br.get("pct_positive_revenue_growth"),
        "pct_positive_eps_growth": br.get("pct_positive_eps_growth"),
        "pct_operating_margin_expansion": br.get("pct_operating_margin_expansion"),
    }


def build_fundamental_quarterly_time_series() -> pd.DataFrame:
    """
    Placeholder for historical quarterly cap-weighted fundamentals.

    When implemented, return rows with a ``date`` column (quarter end) and the
    same metrics as ``build_fundamental_health_chart_df`` expects.
    """
    return pd.DataFrame(
        columns=[
            "date",
            "cap_weighted_revenue_growth",
            "cap_weighted_eps_growth",
            "avg_eps_estimate_growth",
            "pct_positive_revenue_growth",
            "pct_positive_eps_growth",
        ]
    )


def build_fundamental_health_chart_df(summary: dict[str, Any]) -> pd.DataFrame:
    """
    Rows for ``st.line_chart``: percentages (0–100 scale) for snapshot or history.

    Column names match dashboard display expectations.
    """
    d0 = summary.get("_chart_date")
    if isinstance(d0, pd.Timestamp):
        idx = d0.normalize()
    elif isinstance(d0, datetime):
        idx = pd.Timestamp(d0).normalize()
    elif isinstance(d0, date):
        idx = pd.Timestamp(d0)
    else:
        idx = pd.Timestamp(date.today())

    def _to_chart_pct_growth(x: object) -> float:
        """Growth / estimate step-up stored as decimals -> chart %."""
        v = pd.to_numeric(x, errors="coerce")
        if v is None or pd.isna(v):
            return float("nan")
        return float(v) * 100.0

    def _to_chart_pct_participation(x: object) -> float:
        """Participation rates in [0, 1] -> chart %."""
        v = pd.to_numeric(x, errors="coerce")
        if v is None or pd.isna(v):
            return float("nan")
        f = float(v)
        if 0.0 <= f <= 1.0:
            return f * 100.0
        return f

    crg = summary.get("cap_weighted_revenue_growth")
    ceg = summary.get("cap_weighted_eps_growth")
    aeg = summary.get("avg_eps_estimate_growth")
    prg = summary.get("pct_positive_revenue_growth")
    peg = summary.get("pct_positive_eps_growth")

    row = {
        "Cap-weighted Revenue Growth %": _to_chart_pct_growth(crg),
        "Cap-weighted EPS Growth %": _to_chart_pct_growth(ceg),
        "Avg EPS Estimate Growth %": _to_chart_pct_growth(aeg),
        "% Positive Revenue Growth": _to_chart_pct_participation(prg),
        "% Positive EPS Growth": _to_chart_pct_participation(peg),
    }
    out = pd.DataFrame([row], index=[idx])
    return out.sort_index()


def build_industry_fundamental_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    """Per-industry participation and cap-weighted growth (decimals)."""
    if df.empty or "industry" not in df.columns:
        return pd.DataFrame(
            columns=[
                "industry",
                "company_count",
                "cap_weighted_revenue_growth",
                "cap_weighted_eps_growth",
                "avg_operating_margin",
                "pct_positive_eps_growth",
            ]
        )
    rows: list[dict[str, Any]] = []
    for ind, g in df.groupby(df["industry"].fillna("").astype(str), sort=True):
        if ind == "":
            continue
        cw_r = calculate_cap_weighted_fundamentals(g).get("cap_weighted_revenue_growth")
        cw_e = calculate_cap_weighted_fundamentals(g).get("cap_weighted_eps_growth")
        om = pd.to_numeric(g.get("operatingMargin"), errors="coerce")
        eg = pd.to_numeric(g.get("epsGrowth"), errors="coerce")
        rows.append(
            {
                "industry": ind,
                "company_count": int(len(g)),
                "cap_weighted_revenue_growth": cw_r,
                "cap_weighted_eps_growth": cw_e,
                "avg_operating_margin": float(om.mean()) if om.notna().any() else None,
                "pct_positive_eps_growth": float((eg.dropna() > 0).mean()) if eg.notna().any() else None,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("industry").reset_index(drop=True)


def _weighted_marginal_contribution(
    df: pd.DataFrame,
    *,
    value_col: str,
    cap_weighted_mean: float | None,
) -> pd.Series:
    """Per-symbol cap-weight times (value - cap-weighted mean)."""
    if df.empty or value_col not in df.columns:
        return pd.Series(dtype=float)
    mc = pd.to_numeric(df["marketCap"], errors="coerce").clip(lower=0.0)
    w = _cap_weight_series(mc)
    w.index = df.index
    v = pd.to_numeric(df[value_col], errors="coerce")
    if cap_weighted_mean is None or cap_weighted_mean != cap_weighted_mean:
        return w * v
    return w * (v - float(cap_weighted_mean))


def build_top_fundamental_contributors(
    df: pd.DataFrame,
    *,
    cap_weighted_revenue_growth: float | None,
    n: int = 25,
) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(
            columns=[
                "symbol",
                "companyName",
                "marketCap",
                "revenueGrowth",
                "epsGrowth",
                "operatingMargin",
                "weighted_contribution",
            ]
        )
    wc = _weighted_marginal_contribution(
        df, value_col="revenueGrowth", cap_weighted_mean=cap_weighted_revenue_growth
    )
    t = df.assign(weighted_contribution=wc).sort_values("weighted_contribution", ascending=False)
    cols = [
        "symbol",
        "companyName",
        "marketCap",
        "revenueGrowth",
        "epsGrowth",
        "operatingMargin",
        "weighted_contribution",
    ]
    for c in cols:
        if c not in t.columns:
            t[c] = np.nan
    return t[cols].head(int(n)).reset_index(drop=True)


def build_weakest_fundamental_contributors(
    df: pd.DataFrame,
    *,
    cap_weighted_revenue_growth: float | None,
    n: int = 25,
) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(
            columns=[
                "symbol",
                "companyName",
                "marketCap",
                "revenueGrowth",
                "epsGrowth",
                "operatingMargin",
                "weighted_contribution",
            ]
        )
    wc = _weighted_marginal_contribution(
        df, value_col="revenueGrowth", cap_weighted_mean=cap_weighted_revenue_growth
    )
    t = df.assign(weighted_contribution=wc).sort_values("weighted_contribution", ascending=True)
    cols = [
        "symbol",
        "companyName",
        "marketCap",
        "revenueGrowth",
        "epsGrowth",
        "operatingMargin",
        "weighted_contribution",
    ]
    for c in cols:
        if c not in t.columns:
            t[c] = np.nan
    return t[cols].head(int(n)).reset_index(drop=True)


def run_fundamentals_dashboard_bundle(
    session: Any,
    api_key: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    Package universe, summary, tables, and chart-ready time series for Streamlit.

    ``fundamental_ts`` is empty until quarterly history is implemented; the dashboard
    falls back to a one-row snapshot from ``build_fundamental_health_chart_df``.
    """
    try:
        uni = build_fundamental_universe(
            session,
            api_key,
            force_refresh_profiles=force_refresh,
            force_refresh_panels=force_refresh,
        )
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "universe": pd.DataFrame(),
            "summary": {},
            "fundamental_ts": pd.DataFrame(),
            "industry_breakdown": pd.DataFrame(),
            "top_contributors": pd.DataFrame(),
            "weakest_contributors": pd.DataFrame(),
            "as_of": date.today(),
        }

    if uni.empty:
        return {
            "ok": False,
            "error": "Empty fundamentals universe after filters and data pulls.",
            "universe": uni,
            "summary": {},
            "fundamental_ts": pd.DataFrame(),
            "industry_breakdown": pd.DataFrame(),
            "top_contributors": pd.DataFrame(),
            "weakest_contributors": pd.DataFrame(),
            "as_of": date.today(),
        }

    summ = build_fundamental_summary(uni)
    summ["_chart_date"] = date.today()
    crg = summ.get("cap_weighted_revenue_growth")
    industry = build_industry_fundamental_breakdown(uni)
    top_c = build_top_fundamental_contributors(uni, cap_weighted_revenue_growth=crg, n=40)
    bot_c = build_weakest_fundamental_contributors(uni, cap_weighted_revenue_growth=crg, n=40)

    fq_ts = build_fundamental_quarterly_time_series()
    chart_ts = fq_ts if not fq_ts.empty else pd.DataFrame()
    if chart_ts.empty:
        snap = build_fundamental_health_chart_df(summ)
        chart_ts = snap

    return {
        "ok": True,
        "error": None,
        "universe": uni,
        "summary": summ,
        "fundamental_ts": chart_ts,
        "industry_breakdown": industry,
        "top_contributors": top_c,
        "weakest_contributors": bot_c,
        "as_of": date.today(),
    }
