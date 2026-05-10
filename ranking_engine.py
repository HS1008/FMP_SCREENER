"""
Multi-factor sector ranking: Quality, Value, Momentum, Risk (all Z-scored within sector).

Outputs top 10 names per sector plus display returns. See `factors.py` for weights.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import requests

import config
import data_loader
import factors
import metrics


def _safe_sheet_name(name: str) -> str:
    bad = str.maketrans({c: None for c in r'[]:*?/\\'})
    n2 = str(name).translate(bad).strip()[:31]
    return n2 or "Sector"


def _print_full_universe_momentum_diagnostics(scored: pd.DataFrame) -> None:
    """
    Quick calibration view on the *full scored universe* (before top-10 filtering).
    This helps interpret tails and sector balance for new momentum/trend signals.
    """
    cols = [
        "ret_12m_skip1m_pct",
        "relative_strength_sector",
        "momentum_accel",
        "mom_vol_adj",
        "trend_strength_50_200",
        "distance_200dma",
        "above_200dma",
    ]
    pretty = {
        "ret_12m_skip1m_pct": "12M Skip 1M return",
        "relative_strength_sector": "Relative Strength vs Sector",
        "momentum_accel": "Momentum Acceleration",
        "mom_vol_adj": "Vol Adj Momentum",
        "trend_strength_50_200": "Trend Strength 50/200",
        "distance_200dma": "Distance from 200DMA",
        "above_200dma": "Above 200DMA",
    }
    print("\n=== Full-universe momentum diagnostics (pre top-10 filter) ===", flush=True)
    for col in cols:
        if col not in scored.columns:
            continue
        s = pd.to_numeric(scored[col], errors="coerce")
        if s.dropna().empty:
            print(f"- {pretty[col]}: all NaN", flush=True)
            continue
        q = s.quantile([0.05, 0.50, 0.95])
        print(
            f"- {pretty[col]}: n={int(s.notna().sum())} min={s.min():.4f} "
            f"p5={q.loc[0.05]:.4f} median={q.loc[0.50]:.4f} p95={q.loc[0.95]:.4f} max={s.max():.4f}",
            flush=True,
        )
    if "above_200dma" in scored.columns and "sector" in scored.columns:
        print("\nAbove 200DMA rate by sector (full universe):", flush=True)
        rate = (
            scored.assign(_a200=pd.to_numeric(scored["above_200dma"], errors="coerce"))
            .groupby("sector")["_a200"]
            .mean()
            .sort_values(ascending=False)
            .round(3)
        )
        print(rate.to_string(), flush=True)


def build_multifactor_panel(
    session: requests.Session,
    api_key: str,
    universe: pd.DataFrame,
    *,
    force_refresh: bool,
) -> pd.DataFrame:
    """
    One row per universe ticker: fundamentals + return/risk metrics from cached prices.

    Beginner note: we prefetch prices first so the per-ticker loop mostly hits disk, not the API.
    """
    syms = universe["symbol"].astype(str).str.upper().tolist()
    date_from, date_to = data_loader.default_price_window()
    print("[ranking] Prefetching price histories (uses cache when available)...", flush=True)
    data_loader.prefetch_stock_prices(
        session,
        api_key,
        syms,
        date_from,
        date_to,
        force_refresh=force_refresh,
    )

    rows: list[dict] = []
    iterator = data_loader._progress(universe.to_dict("records"), desc="Multifactor panel")  # noqa: SLF001
    for base in iterator:
        sym = str(base.get("symbol", "")).upper()
        if not sym:
            continue
        row = dict(base)
        try:
            fund = data_loader.get_fundamentals(session, api_key, sym, force_refresh=force_refresh)
            for k, v in fund.items():
                row[k] = v
        except Exception as e:
            print(f"  warning: fundamentals {sym}: {e}", file=sys.stderr)

        if row.get("market_cap") is None and base.get("marketCap") is not None:
            row["market_cap"] = float(pd.to_numeric(base["marketCap"], errors="coerce"))

        try:
            px = data_loader.get_price_history(
                session, api_key, sym, date_from, date_to, force_refresh=force_refresh
            )
            pcol = data_loader.pick_price_column(px)
            pm = metrics.price_factor_metrics(
                px,
                pcol,
                n_1w=config.TRADING_DAYS_1W,
                n_1m=config.TRADING_DAYS_1M,
                n_3m=config.TRADING_DAYS_3M,
                n_1y=config.TRADING_DAYS_1Y,
            )
            row.update(pm)
        except Exception as e:
            print(f"  warning: price metrics {sym}: {e}", file=sys.stderr)
            row.update(
                {
                    "ret_1w_pct": float("nan"),
                    "ret_1m_pct": float("nan"),
                    "ret_3m_pct": float("nan"),
                    "ret_1y_pct": float("nan"),
                    "ret_12m_skip1m_pct": float("nan"),
                    "momentum_accel": float("nan"),
                    "mom_vol_adj": float("nan"),
                    "above_200dma": float("nan"),
                    "trend_strength_50_200": float("nan"),
                    "distance_200dma": float("nan"),
                    "ret_ytd_pct": float("nan"),
                    "ann_volatility": float("nan"),
                    "max_drawdown_pct": float("nan"),
                }
            )
        rows.append(row)

    data_loader._done_progress("Multifactor panel")  # noqa: SLF001
    return pd.DataFrame(rows)


def print_multifactor_console(scored: pd.DataFrame) -> None:
    """Top/bottom names per sector + sector averages (full universe)."""
    pd.set_option("display.max_rows", 20)
    pd.set_option("display.width", 120)

    print("\n=== Sectors by average final_score (full universe) ===\n", flush=True)
    avg = scored.groupby("sector", sort=True)["final_score"].mean().sort_values(ascending=False)
    print(avg.round(4).to_string(), flush=True)

    disp_cols = ["symbol", "final_score", "quality_z", "value_z", "momentum_z", "risk_z"]
    for sec, g in scored.groupby("sector", sort=True):
        g2 = g.sort_values("final_score", ascending=False).reset_index(drop=True)
        n = len(g2)
        top_n = min(3, n)
        bot_n = min(3, n)
        cols = ["companyName", *disp_cols] if "companyName" in g2.columns else disp_cols
        print(f"\n--- {sec}: top {top_n} by final_score ---", flush=True)
        print(g2.head(top_n)[cols].round(3).to_string(index=False), flush=True)
        print(f"--- {sec}: bottom {bot_n} by final_score ---", flush=True)
        print(g2.tail(bot_n)[cols].round(3).to_string(index=False), flush=True)


def save_final_sector_model(df: pd.DataFrame, path: Path) -> None:
    """Excel: combined sheet plus one tab per sector."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.round(4).to_excel(writer, sheet_name="All sectors", index=False)
        for sec, grp in df.groupby("sector", sort=True):
            grp.round(4).to_excel(writer, sheet_name=_safe_sheet_name(sec), index=False)


def run_full_scored_universe(
    session: requests.Session,
    api_key: str,
    *,
    top_n: int | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Full pipeline up to full scored universe:
    universe -> panel -> relative strength -> within-sector factor scores.

    Returns one row per stock (before top-10-per-sector filtering).
    """
    cap_n = int(top_n) if top_n is not None else int(config.MULTIFACTOR_UNIVERSE_TOP_N)
    print(f"[ranking] Building universe (top {cap_n} by market cap)...", flush=True)
    uni = data_loader.get_stock_universe(session, api_key, top_n=cap_n)

    panel = build_multifactor_panel(session, api_key, uni, force_refresh=force_refresh)
    if panel.empty:
        return pd.DataFrame()

    # Sector-relative strength: 12M skip-1M minus sector median momentum.
    # Median is used (not mean) to reduce outlier distortion inside small sectors.
    panel["ret_12m_skip1m_pct"] = pd.to_numeric(panel.get("ret_12m_skip1m_pct"), errors="coerce")
    panel["relative_strength_sector"] = (
        panel["ret_12m_skip1m_pct"]
        - panel.groupby("sector")["ret_12m_skip1m_pct"].transform("median")
    )
    panel["relative_strength_sector"] = pd.to_numeric(panel["relative_strength_sector"], errors="coerce")

    scored = factors.attach_multifactor_scores(panel, sector_col="sector")
    if bool(getattr(config, "PRINT_FULL_UNIVERSE_DIAGNOSTICS", False)):
        _print_full_universe_momentum_diagnostics(scored)
    print_multifactor_console(scored)
    return scored


def top10_by_sector_from_scored(scored: pd.DataFrame) -> pd.DataFrame:
    """Convert full scored universe into the exported top-10-per-sector output table."""
    if scored.empty:
        return pd.DataFrame()
    picks: list[pd.DataFrame] = []
    for sec, grp in scored.groupby("sector", sort=True):
        g2 = grp.sort_values("final_score", ascending=False).head(10).copy()
        g2["rank"] = list(range(1, len(g2) + 1))
        picks.append(g2)
    top = pd.concat(picks, ignore_index=True)

    out = pd.DataFrame(
        {
            "sector": top["sector"],
            "rank": top["rank"],
            "symbol": top["symbol"],
            "companyName": top.get("companyName", top["symbol"]),
            "exchange": top.get("exchangeShortName", top.get("exchange", "")),
            "marketCap": pd.to_numeric(top.get("marketCap"), errors="coerce"),
            "final_score": pd.to_numeric(top["final_score"], errors="coerce").round(4),
            "quality_z": pd.to_numeric(top["quality_z"], errors="coerce").round(4),
            "value_z": pd.to_numeric(top["value_z"], errors="coerce").round(4),
            "momentum_z": pd.to_numeric(top["momentum_z"], errors="coerce").round(4),
            "risk_z": pd.to_numeric(top["risk_z"], errors="coerce").round(4),
            "1W return": pd.to_numeric(top["ret_1w_pct"], errors="coerce"),
            "1M return": pd.to_numeric(top["ret_1m_pct"], errors="coerce"),
            "YTD return": pd.to_numeric(top["ret_ytd_pct"], errors="coerce"),
            "1Y return": pd.to_numeric(top["ret_1y_pct"], errors="coerce"),
            "12M Skip 1M return": pd.to_numeric(top["ret_12m_skip1m_pct"], errors="coerce"),
            "Relative Strength vs Sector": pd.to_numeric(top["relative_strength_sector"], errors="coerce"),
            "Momentum Acceleration": pd.to_numeric(top["momentum_accel"], errors="coerce"),
            "Vol Adj Momentum": pd.to_numeric(top["mom_vol_adj"], errors="coerce"),
            "Above 200DMA": pd.to_numeric(top["above_200dma"], errors="coerce"),
            "Trend Strength 50/200": pd.to_numeric(top["trend_strength_50_200"], errors="coerce"),
            "Distance from 200DMA": pd.to_numeric(top["distance_200dma"], errors="coerce"),
        }
    )
    return out.sort_values(["sector", "rank"], ascending=[True, True]).reset_index(drop=True)


def run_multifactor_ranking(
    session: requests.Session,
    api_key: str,
    *,
    top_n: int | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Backward-compatible entrypoint for ranking output only.

    Returns the **top-10-per-sector output** table.
    """
    scored = run_full_scored_universe(
        session,
        api_key,
        top_n=top_n,
        force_refresh=force_refresh,
    )
    return top10_by_sector_from_scored(scored)


# --- Backward-compatible aliases (older single-factor notebook / scripts) ---


def run_ranking(
    session: requests.Session,
    api_key: str,
    *,
    top_n: int | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Deprecated name: use `run_multifactor_ranking`."""
    return run_multifactor_ranking(session, api_key, top_n=top_n, force_refresh=force_refresh)


def print_sector_tables(df: pd.DataFrame) -> None:
    """Small sample print of the exported top-10 table."""
    cols = [
        "rank",
        "symbol",
        "companyName",
        "final_score",
        "quality_z",
        "value_z",
        "momentum_z",
        "risk_z",
        "1W return",
        "1M return",
        "YTD return",
        "1Y return",
    ]
    for sec, grp in df.groupby("sector", sort=True):
        print(f"\n=== {sec} (top {len(grp)}) ===", flush=True)
        print(grp[cols].round(2).to_string(index=False), flush=True)


def save_excel(df: pd.DataFrame, path: Path) -> None:
    """Generic Excel writer; multifactor default path is `config.FINAL_SECTOR_MODEL_OUTPUT`."""
    save_final_sector_model(df, path)
