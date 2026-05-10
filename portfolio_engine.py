"""
Portfolio construction engine built on top of scored multi-factor outputs.

Beginner notes:
- We start from the scored universe, then apply risk filters.
- We tilt sectors (overweight/neutral/underweight) using sector-level scores.
- Inside sectors, we size positions mostly by inverse volatility.
"""

from __future__ import annotations

import pandas as pd

import config


def apply_risk_filters(scored: pd.DataFrame) -> pd.DataFrame:
    """
    Filter high-risk names before portfolio construction.

    Rules:
    - max_drawdown_pct <= configured cap
    - ann_volatility <= sector percentile threshold
    - optional trend rule: above_200dma == 1
    """
    if scored.empty:
        return scored.copy()

    out = scored.copy()
    out["ann_volatility"] = pd.to_numeric(out.get("ann_volatility"), errors="coerce")
    out["max_drawdown_pct"] = pd.to_numeric(out.get("max_drawdown_pct"), errors="coerce")
    out["above_200dma"] = pd.to_numeric(out.get("above_200dma"), errors="coerce")

    max_dd = float(config.RISK_FILTER_MAX_DRAWDOWN_PCT)
    vol_pct = float(config.RISK_FILTER_MAX_SECTOR_VOL_PERCENTILE)
    vol_thresh = out.groupby("sector")["ann_volatility"].transform(lambda s: s.quantile(vol_pct))

    pass_dd = out["max_drawdown_pct"].isna() | (out["max_drawdown_pct"] <= max_dd)
    pass_vol = out["ann_volatility"].isna() | vol_thresh.isna() | (out["ann_volatility"] <= vol_thresh)
    pass_trend = pd.Series(True, index=out.index)
    if bool(config.RISK_FILTER_REQUIRE_ABOVE_200DMA):
        pass_trend = out["above_200dma"] >= 0.5

    out["risk_filter_pass"] = pass_dd & pass_vol & pass_trend
    kept = out.loc[out["risk_filter_pass"]].copy()

    removed_n = int((~out["risk_filter_pass"]).sum())
    if removed_n > 0:
        print(f"[portfolio] Risk filters removed {removed_n} names.", flush=True)
    return kept


def calculate_sector_scores(filtered: pd.DataFrame) -> pd.DataFrame:
    """Build sector-level tilt table from filtered names."""
    if filtered.empty:
        return pd.DataFrame(
            columns=[
                "sector",
                "sector_final_score",
                "sector_momentum_z",
                "sector_quality_z",
                "name_count",
                "sector_rank",
                "sector_tilt",
                "sector_multiplier",
            ]
        )

    sec = (
        filtered.groupby("sector", as_index=False)
        .agg(
            sector_final_score=("final_score", "mean"),
            sector_momentum_z=("momentum_z", "mean"),
            sector_quality_z=("quality_z", "mean"),
            name_count=("symbol", "count"),
        )
        .sort_values("sector_final_score", ascending=False)
        .reset_index(drop=True)
    )
    sec["sector_rank"] = range(1, len(sec) + 1)

    top_n = int(config.TOP_SECTOR_COUNT)
    bot_n = int(config.BOTTOM_SECTOR_COUNT)
    top_set = set(sec.head(top_n)["sector"])
    bot_set = set(sec.tail(bot_n)["sector"])

    def _tilt(s: str) -> str:
        if s in top_set:
            return "overweight"
        if s in bot_set:
            return "underweight"
        return "neutral"

    sec["sector_tilt"] = sec["sector"].map(_tilt)
    sec["sector_multiplier"] = sec["sector_tilt"].map(
        {
            "overweight": float(config.SECTOR_OVERWEIGHT_MULTIPLIER),
            "neutral": float(config.SECTOR_NEUTRAL_MULTIPLIER),
            "underweight": float(config.SECTOR_UNDERWEIGHT_MULTIPLIER),
        }
    )
    return sec


def select_portfolio_candidates(filtered: pd.DataFrame, top_n_per_sector: int = 10) -> pd.DataFrame:
    """Select top-N by final score within each sector."""
    if filtered.empty:
        return filtered.copy()
    picks: list[pd.DataFrame] = []
    for sec, grp in filtered.groupby("sector", sort=True):
        g2 = grp.sort_values("final_score", ascending=False).head(int(top_n_per_sector)).copy()
        g2["rank"] = range(1, len(g2) + 1)
        picks.append(g2)
    if not picks:
        return pd.DataFrame(columns=list(filtered.columns) + ["rank"])
    return pd.concat(picks, ignore_index=True)


def _allocate_with_caps(df: pd.DataFrame, max_w: float, min_w: float) -> pd.DataFrame:
    """Cap big positions, drop tiny positions, and re-normalize to sum to 1."""
    out = df.copy()
    if out.empty:
        out["final_weight"] = []
        return out

    w = pd.to_numeric(out["raw_weight"], errors="coerce").fillna(0.0)
    if w.sum() <= 0:
        w = pd.Series(1.0 / len(out), index=out.index)
    else:
        w = w / w.sum()

    # Iterative cap redistribution to uncapped names.
    for _ in range(10):
        over = w > max_w
        if not over.any():
            break
        excess = float((w[over] - max_w).sum())
        w[over] = max_w
        under = w < max_w
        room = float((max_w - w[under]).sum())
        if room <= 0:
            break
        w.loc[under] = w.loc[under] + excess * ((max_w - w.loc[under]) / room)

    # Remove tiny positions, then normalize again.
    keep = w >= min_w
    if keep.any():
        out = out.loc[keep].copy()
        w = w.loc[keep]
    if w.sum() <= 0:
        w = pd.Series(1.0 / len(out), index=out.index) if len(out) else pd.Series(dtype=float)
    else:
        w = w / w.sum()

    out["final_weight"] = w.to_numpy(dtype=float)
    return out


def calculate_volatility_weighted_positions(
    candidates: pd.DataFrame, sector_scores: pd.DataFrame
) -> pd.DataFrame:
    """Create final position weights from volatility and sector tilt."""
    if candidates.empty:
        return candidates.copy()

    out = candidates.copy()
    out = out.merge(
        sector_scores[["sector", "sector_tilt", "sector_multiplier"]],
        on="sector",
        how="left",
    )
    out["sector_multiplier"] = pd.to_numeric(out["sector_multiplier"], errors="coerce").fillna(
        float(config.SECTOR_NEUTRAL_MULTIPLIER)
    )
    out["ann_volatility"] = pd.to_numeric(out.get("ann_volatility"), errors="coerce")

    # Fill missing/invalid volatility with sector median, then global median.
    sec_med = out.groupby("sector")["ann_volatility"].transform("median")
    out["ann_volatility"] = out["ann_volatility"].where(out["ann_volatility"] > 0)
    out["ann_volatility"] = out["ann_volatility"].fillna(sec_med)
    global_med = pd.to_numeric(out["ann_volatility"], errors="coerce").median()
    if pd.isna(global_med) or global_med <= 0:
        # Fallback: equal weights scaled by sector multipliers when volatility data is unusable.
        out["inverse_vol"] = 1.0
    else:
        out["ann_volatility"] = out["ann_volatility"].fillna(global_med)
        out["inverse_vol"] = 1.0 / out["ann_volatility"]

    out["raw_weight"] = out["inverse_vol"] * out["sector_multiplier"]
    out = _allocate_with_caps(
        out, max_w=float(config.MAX_STOCK_WEIGHT), min_w=float(config.MIN_STOCK_WEIGHT)
    )
    if out.empty:
        return out
    out["final_weight"] = pd.to_numeric(out["final_weight"], errors="coerce")
    out["final_weight"] = out["final_weight"] / out["final_weight"].sum()
    out["final_weight_pct"] = out["final_weight"] * 100.0
    return out


def build_portfolio(scored: pd.DataFrame, top_n_per_sector: int = 10) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    End-to-end portfolio build from scored universe.

    Returns:
    - portfolio table (weighted holdings)
    - sector scores table (sector tilt diagnostics)
    """
    if scored.empty:
        return pd.DataFrame(), pd.DataFrame()

    filtered = apply_risk_filters(scored)
    if filtered.empty:
        print("[portfolio] Warning: all names filtered out; falling back to unfiltered universe.", flush=True)
        filtered = scored.copy()
        filtered["risk_filter_pass"] = True

    sector_scores = calculate_sector_scores(filtered)
    candidates = select_portfolio_candidates(filtered, top_n_per_sector=top_n_per_sector)
    portfolio = calculate_volatility_weighted_positions(candidates, sector_scores)

    if portfolio.empty:
        return portfolio, sector_scores

    # Keep output columns consistent and beginner-friendly.
    portfolio = portfolio.copy()
    rename_map = {
        "ret_1w_pct": "1W return",
        "ret_1m_pct": "1M return",
        "ret_ytd_pct": "YTD return",
        "ret_1y_pct": "1Y return",
    }
    portfolio = portfolio.rename(columns=rename_map)
    required_cols = [
        "sector",
        "rank",
        "symbol",
        "companyName",
        "exchangeShortName",
        "marketCap",
        "final_score",
        "quality_z",
        "value_z",
        "momentum_z",
        "risk_z",
        "ann_volatility",
        "max_drawdown_pct",
        "above_200dma",
        "trend_strength_50_200",
        "distance_200dma",
        "sector_tilt",
        "sector_multiplier",
        "raw_weight",
        "final_weight",
        "final_weight_pct",
        "1W return",
        "1M return",
        "YTD return",
        "1Y return",
    ]
    if "exchangeShortName" in portfolio.columns:
        portfolio = portfolio.rename(columns={"exchangeShortName": "exchange"})
    else:
        portfolio["exchange"] = portfolio.get("exchange", "")
    required_cols[4] = "exchange"
    for c in required_cols:
        if c not in portfolio.columns:
            portfolio[c] = float("nan")
    portfolio = portfolio[required_cols].sort_values("final_weight", ascending=False).reset_index(drop=True)
    return portfolio, sector_scores

