"""
Technology valuation confirmation (dashboard only).

Cap-weighted PE / PEG snapshot vs simple historical averages from FMP annual ratios
and income-statement growth. Not for ranking or factor scoring.
"""

from __future__ import annotations

import json
import math
import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import config
import data_loader
import tech_universe

TECH = "Technology"

VALUATION_MIN_MARKET_CAP: int = 10_000_000_000
VALUATION_MIN_AVG_VOLUME: int = 1_000_000
VALUATION_MIN_PRICE: float = 10.0
# Annual PE rows to request (newest first from FMP); we average up to 5 for "historical".
HISTORICAL_PE_MAX_YEARS: int = 5

PANEL_CACHE_DIR: Path = config.OUTPUT_DIR / "cache" / "valuation_tech_confirmation"
PANEL_SCHEMA: str = "tech_val_panel_v2"
_SYMBOL_FETCH_SLEEP_S: float = 0.04


def _eps_growth_to_pct_points(growth: float | None) -> float | None:
    """
    FMP `growthEPS` is usually a decimal (e.g. 0.12 = 12%). PEG uses growth as percentage points
    in the denominator (e.g. 12), not 0.12.
    """
    if growth is None:
        return None
    try:
        g = float(growth)
    except (TypeError, ValueError):
        return None
    if math.isnan(g) or math.isinf(g):
        return None
    if abs(g) < 1.0:
        return g * 100.0
    return g


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


def _cap_weight_series(market_caps: pd.Series) -> pd.Series:
    x = pd.to_numeric(market_caps, errors="coerce").clip(lower=0.0)
    tot = float(x.sum())
    if tot <= 0 or math.isnan(tot):
        return pd.Series(0.0, index=x.index)
    return x / tot


def _fetch_symbol_valuation_panel(
    session: Any,
    api_key: str,
    symbol: str,
    *,
    force_refresh: bool,
) -> dict[str, Any]:
    """Cached per-symbol inputs for valuation universe row."""
    sym = str(symbol).upper().strip()
    if not force_refresh:
        cached = _read_cached_panel(sym)
        if cached is not None:
            return {k: v for k, v in cached.items() if k != "_schema"}

    out: dict[str, Any] = {
        "current_pe": None,
        "historical_avg_pe": None,
        "historical_pe_years_used": 0,
        "historical_pe_label": None,
        "eps_growth": None,
        "historical_avg_eps_growth": None,
        "historical_eps_growth_years_used": 0,
        "eps_ttm": None,
        "ebitda_latest": None,
        "free_cash_flow_latest": None,
    }

    rt = _fmp_list(session, api_key, "ratios-ttm", symbol=sym)
    if rt:
        r0 = rt[0]
        out["current_pe"] = data_loader._safe_float(r0.get("priceToEarningsRatioTTM"))  # noqa: SLF001
        out["eps_ttm"] = data_loader._safe_float(r0.get("netIncomePerShareTTM"))  # noqa: SLF001

    annual = _fmp_list(
        session,
        api_key,
        "ratios",
        symbol=sym,
        period="annual",
        limit=str(max(HISTORICAL_PE_MAX_YEARS + 1, 6)),
    )
    pe_vals: list[float] = []
    for row in annual[:HISTORICAL_PE_MAX_YEARS]:
        pe = data_loader._safe_float(row.get("priceToEarningsRatio"))  # noqa: SLF001
        if pe is not None and pe > 0:
            pe_vals.append(float(pe))
    if pe_vals:
        out["historical_avg_pe"] = float(sum(pe_vals) / len(pe_vals))
        out["historical_pe_years_used"] = int(len(pe_vals))
        n = len(pe_vals)
        if n >= HISTORICAL_PE_MAX_YEARS:
            out["historical_pe_label"] = f"{HISTORICAL_PE_MAX_YEARS}-year average annual PE"
        else:
            out["historical_pe_label"] = f"{n}-year average annual PE (longest available, up to {HISTORICAL_PE_MAX_YEARS}y)"

    gr = _fmp_list(session, api_key, "income-statement-growth", symbol=sym, limit="8")
    if gr:
        g0 = gr[0]
        out["eps_growth"] = data_loader._safe_float(g0.get("growthEPS"))  # noqa: SLF001
        hist_g: list[float] = []
        for row in gr[1 : 1 + HISTORICAL_PE_MAX_YEARS]:
            g = data_loader._safe_float(row.get("growthEPS"))  # noqa: SLF001
            if g is not None and g == g:
                hist_g.append(float(g))
        if hist_g:
            out["historical_avg_eps_growth"] = float(sum(hist_g) / len(hist_g))
            out["historical_eps_growth_years_used"] = int(len(hist_g))

    inc = _fmp_list(session, api_key, "income-statement", symbol=sym, limit="1")
    if inc:
        out["ebitda_latest"] = data_loader._safe_float(inc[0].get("ebitda"))  # noqa: SLF001

    cf = _fmp_list(session, api_key, "cash-flow-statement", symbol=sym, limit="1")
    if cf:
        out["free_cash_flow_latest"] = data_loader._safe_float(cf[0].get("freeCashFlow"))  # noqa: SLF001

    _write_cached_panel(sym, out)
    time.sleep(_SYMBOL_FETCH_SLEEP_S)
    return out


def build_valuation_universe(
    session: Any,
    api_key: str,
    *,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    US Technology names passing valuation liquidity screens, with PE/PEG fields.

    Filters: market cap > 10B, avg volume > 1M, price > 10, US, active, major exchanges,
    positive EPS (TTM), positive EBITDA (latest period), positive FCF when the field exists,
    current PE > 0, EPS growth > 0, excludes ETFs/funds and warrant-like symbols.
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

    df = df[df["marketCap"] > float(VALUATION_MIN_MARKET_CAP)]
    df = df[avg_vol > float(VALUATION_MIN_AVG_VOLUME)]
    df = df[df["price"] > float(VALUATION_MIN_PRICE)]

    if "symbol" in df.columns:
        df = df[~df["symbol"].astype(str).map(data_loader._looks_like_warrant_or_right)]
    if "industry" in df.columns:
        df = df[~df["industry"].map(data_loader._looks_like_warrant_industry)]
    if "symbol" in df.columns:
        df = df[~df["symbol"].astype(str).map(data_loader._looks_like_preferred_or_unit)]

    df = df.dropna(subset=["symbol"]).drop_duplicates(subset=["symbol"], keep="last")
    if df.empty:
        return pd.DataFrame()

    rows_out: list[dict[str, Any]] = []
    iterator = data_loader._progress(df.to_dict("records"), desc="Tech valuation panels")  # noqa: SLF001
    for rec in iterator:
        sym = str(rec.get("symbol", "")).upper().strip()
        if not sym:
            continue
        panel = _fetch_symbol_valuation_panel(session, api_key, sym, force_refresh=force_refresh)

        eps_ttm = panel.get("eps_ttm")
        if eps_ttm is None or float(eps_ttm) <= 0:
            continue

        ebitda = panel.get("ebitda_latest")
        if ebitda is None or float(ebitda) <= 0:
            continue

        fcf = panel.get("free_cash_flow_latest")
        if fcf is not None and fcf == fcf and float(fcf) <= 0:
            continue

        cur_pe = panel.get("current_pe")
        if cur_pe is None or float(cur_pe) <= 0:
            continue

        eps_g = panel.get("eps_growth")
        if eps_g is None or float(eps_g) <= 0:
            continue

        eps_growth_pct = _eps_growth_to_pct_points(float(eps_g))
        if eps_growth_pct is None or eps_growth_pct <= 0:
            continue

        hist_pe = panel.get("historical_avg_pe")
        pe_pd: float | None = None
        if hist_pe is not None and float(hist_pe) > 0 and cur_pe is not None and float(cur_pe) > 0:
            pe_pd = float(cur_pe) / float(hist_pe) - 1.0

        cur_peg: float | None = None
        if float(cur_pe) > 0 and eps_growth_pct > 0:
            cur_peg = float(cur_pe) / float(eps_growth_pct)
            if not math.isfinite(cur_peg):
                cur_peg = None

        hist_peg: float | None = None
        h_eps_g = panel.get("historical_avg_eps_growth")
        h_eps_g_pct = _eps_growth_to_pct_points(float(h_eps_g)) if h_eps_g is not None else None
        if (
            hist_pe is not None
            and float(hist_pe) > 0
            and h_eps_g_pct is not None
            and h_eps_g_pct > 0
        ):
            hist_peg = float(hist_pe) / float(h_eps_g_pct)
            if not math.isfinite(hist_peg):
                hist_peg = None

        peg_bucket = "na"
        if cur_peg is not None and cur_peg == cur_peg:
            if cur_peg < 1.0:
                peg_bucket = "under_1"
            elif cur_peg <= 2.0:
                peg_bucket = "1_to_2"
            else:
                peg_bucket = "over_2"

        rows_out.append(
            {
                "symbol": sym,
                "companyName": str(rec.get("companyName", "") or ""),
                "industry": str(rec.get("industry", "") or "") if rec.get("industry") is not None else "",
                "marketCap": float(pd.to_numeric(rec.get("marketCap"), errors="coerce") or 0.0),
                "current_pe": float(cur_pe) if cur_pe is not None else float("nan"),
                "historical_avg_pe": float(hist_pe) if hist_pe is not None else float("nan"),
                "pe_premium_discount": pe_pd,
                "eps_growth": float(eps_g) if eps_g is not None else float("nan"),
                "current_peg": cur_peg,
                "historical_peg": hist_peg,
                "peg_bucket": peg_bucket,
            }
        )

    data_loader._done_progress("Tech valuation panels")  # noqa: SLF001
    return pd.DataFrame(rows_out).reset_index(drop=True)


def calculate_cap_weighted_valuations(universe: pd.DataFrame) -> dict[str, Any]:
    """Market-cap-weighted valuation snapshot and PEG bucket participation rates."""
    empty = {
        "universe_size": 0,
        "cap_weighted_current_pe": None,
        "cap_weighted_historical_pe": None,
        "cap_weighted_pe_premium_discount": None,
        "cap_weighted_current_peg": None,
        "cap_weighted_historical_peg": None,
        "pct_peg_under_1": None,
        "pct_peg_1_to_2": None,
        "pct_peg_over_2": None,
        "historical_pe_label": (
            f"Average of up to {HISTORICAL_PE_MAX_YEARS} fiscal-year trailing PE ratios per company "
            "(longest available history when fewer years exist)."
        ),
    }
    if universe.empty or "marketCap" not in universe.columns:
        return empty

    w = _cap_weight_series(universe["marketCap"])
    w = w.reindex(universe.index)
    if float(w.sum()) <= 0:
        return {**empty, "universe_size": int(len(universe))}

    def _cw(col: str) -> float | None:
        if col not in universe.columns:
            return None
        s = pd.to_numeric(universe[col], errors="coerce")
        m = s.notna() & w.notna() & (w > 0)
        if not m.any():
            return None
        wn = w[m] / float(w[m].sum())
        return float((wn * s[m]).sum())

    peg = pd.to_numeric(universe.get("current_peg"), errors="coerce").replace([np.inf, -np.inf], np.nan)
    peg_valid = peg.notna()
    n_peg = int(peg_valid.sum())

    def _peg_share(pred) -> float | None:
        if n_peg <= 0:
            return None
        sub = pred(peg[peg_valid])
        return float(sub.sum() / n_peg)

    lbl = (
        f"Average of up to {HISTORICAL_PE_MAX_YEARS} fiscal-year trailing PE ratios per company "
        "(longest available history when fewer years exist)."
    )

    cw_cur = _cw("current_pe")
    cw_hist = _cw("historical_avg_pe")
    cw_pe_pd: float | None = None
    if cw_cur is not None and cw_hist is not None and cw_cur > 0 and cw_hist > 0:
        cw_pe_pd = float(cw_cur) / float(cw_hist) - 1.0
        if not math.isfinite(cw_pe_pd):
            cw_pe_pd = None

    return {
        "universe_size": int(len(universe)),
        "cap_weighted_current_pe": cw_cur,
        "cap_weighted_historical_pe": cw_hist,
        "cap_weighted_pe_premium_discount": cw_pe_pd,
        "cap_weighted_current_peg": _cw("current_peg"),
        "cap_weighted_historical_peg": _cw("historical_peg"),
        "pct_peg_under_1": _peg_share(lambda s: s < 1.0),
        "pct_peg_1_to_2": _peg_share(lambda s: (s >= 1.0) & (s <= 2.0)),
        "pct_peg_over_2": _peg_share(lambda s: s > 2.0),
        "historical_pe_label": lbl,
    }


def build_industry_valuation_breakdown(universe: pd.DataFrame) -> pd.DataFrame:
    """Per-industry cap-weighted PE/PEG and PEG bucket mix."""
    cols = [
        "industry",
        "company_count",
        "cap_weighted_current_pe",
        "cap_weighted_historical_pe",
        "cap_weighted_current_peg",
        "pct_peg_under_1",
        "pct_peg_1_to_2",
        "pct_peg_over_2",
    ]
    if universe.empty or "industry" not in universe.columns:
        return pd.DataFrame(columns=cols)

    rows: list[dict[str, Any]] = []
    for ind, g in universe.groupby(universe["industry"].fillna("").astype(str), sort=True):
        if ind == "":
            continue
        summ = calculate_cap_weighted_valuations(g)
        rows.append(
            {
                "industry": ind,
                "company_count": int(len(g)),
                "cap_weighted_current_pe": summ.get("cap_weighted_current_pe"),
                "cap_weighted_historical_pe": summ.get("cap_weighted_historical_pe"),
                "cap_weighted_current_peg": summ.get("cap_weighted_current_peg"),
                "pct_peg_under_1": summ.get("pct_peg_under_1"),
                "pct_peg_1_to_2": summ.get("pct_peg_1_to_2"),
                "pct_peg_over_2": summ.get("pct_peg_over_2"),
            }
        )
    out = pd.DataFrame(rows)
    return out.sort_values("industry").reset_index(drop=True) if not out.empty else pd.DataFrame(columns=cols)


def build_valuation_dashboard_bundle(
    session: Any,
    api_key: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Streamlit-oriented package: universe, cap-weighted summary, industry table."""
    try:
        uni = build_valuation_universe(session, api_key, force_refresh=force_refresh)
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "as_of": date.today(),
            "universe": pd.DataFrame(),
            "summary": {},
            "industry_breakdown": pd.DataFrame(),
        }

    if uni.empty:
        return {
            "ok": False,
            "error": "Empty valuation universe after filters and data pulls.",
            "as_of": date.today(),
            "universe": uni,
            "summary": {},
            "industry_breakdown": pd.DataFrame(),
        }

    summ = calculate_cap_weighted_valuations(uni)
    ind = build_industry_valuation_breakdown(uni)
    return {
        "ok": True,
        "error": None,
        "as_of": date.today(),
        "universe": uni,
        "summary": summ,
        "industry_breakdown": ind,
    }
