"""
Multi-factor Z-scores **within each sector** (peers vs peers).

Pipeline:
1) Impute missing fundamentals / price metrics with the **sector median** (then global median).
2) Z-score each column inside the sector: Z = (x - mean) / std (population std, ddof=0).
3) For "inverse" metrics (cheaper / safer = better), use **-Z** so higher is always better.
4) Build sub-scores (quality, value, momentum, risk) then **final_score**.

Beginner note: `final_score` is a *blend of Z-scores*, not itself a Z-score.
"""

from __future__ import annotations

import pandas as pd

import metrics

SECTOR_COL = "sector"

# Every column we impute before Z-scoring
ALL_MODEL_COLS: tuple[str, ...] = (
    "roic",
    "operating_margin",
    "revenue_growth",
    "fcf_yield",
    "ev_to_ebitda",
    "ret_12m_skip1m_pct",
    "ret_3m_pct",
    "momentum_accel",
    "mom_vol_adj",
    "relative_strength_sector",
    "ann_volatility",
    "max_drawdown_pct",
    "trend_strength_50_200",
    "distance_200dma",
    "above_200dma",
)

# Raw metrics where *smaller* values should increase the score (after -Z)
INVERSE_RAW_COLS: frozenset[str] = frozenset(
    {"ev_to_ebitda", "ann_volatility", "max_drawdown_pct", "distance_200dma"}
)

# --- Within-factor weights (on adjusted Z) ---
QUALITY_WEIGHTS: dict[str, float] = {
    "roic": 0.50,
    "operating_margin": 0.30,
    "revenue_growth": 0.20,
}

VALUE_WEIGHTS: dict[str, float] = {
    "fcf_yield": 0.70,
    "ev_to_ebitda": 0.30,
}

MOMENTUM_WEIGHTS: dict[str, float] = {
    "ret_12m_skip1m_pct": 0.35,
    "mom_vol_adj": 0.25,
    "relative_strength_sector": 0.20,
    "momentum_accel": 0.10,
    "ret_3m_pct": 0.10,
}

RISK_WEIGHTS: dict[str, float] = {
    "ann_volatility": 0.60,
    "max_drawdown_pct": 0.40,
}

# --- Across-factor weights ---
FACTOR_WEIGHTS: dict[str, float] = {
    "momentum_z": 0.35,
    "quality_z": 0.30,
    "value_z": 0.20,
    "risk_z": 0.15,
}

# Cap extreme tails before Z-scoring so one outlier does not dominate a sector.
WINSORIZE_COLS: tuple[str, ...] = (
    "roic",
    "operating_margin",
    "revenue_growth",
    "fcf_yield",
    "ev_to_ebitda",
    "ret_12m_skip1m_pct",
    "ret_3m_pct",
    "momentum_accel",
    "mom_vol_adj",
    "relative_strength_sector",
    "ann_volatility",
    "max_drawdown_pct",
    "trend_strength_50_200",
    "distance_200dma",
)


def impute_sector_medians(
    df: pd.DataFrame,
    cols: tuple[str, ...],
    *,
    sector_col: str = SECTOR_COL,
) -> pd.DataFrame:
    """Fill NaNs with sector median, then overall median (stable Z-score inputs)."""
    out = df.copy()
    for col in cols:
        out[col] = pd.to_numeric(out[col], errors="coerce")
        med_s = out.groupby(sector_col)[col].transform("median")
        out[col] = out[col].fillna(med_s)
        out[col] = out[col].fillna(out[col].median())
    return out


def winsorize_within_sector(
    df: pd.DataFrame,
    cols: tuple[str, ...],
    sector_col: str = SECTOR_COL,
    lower: float = 0.05,
    upper: float = 0.95,
) -> pd.DataFrame:
    """
    Sector-aware winsorization:
    clip each raw metric to sector-specific quantile bands before Z-scores.
    """
    out = df.copy()
    for col in cols:
        if col not in out.columns:
            continue
        out[col] = pd.to_numeric(out[col], errors="coerce")

        def _clip_sector(s: pd.Series) -> pd.Series:
            if s.dropna().empty:
                return s
            lo = s.quantile(lower)
            hi = s.quantile(upper)
            if pd.isna(lo) or pd.isna(hi) or lo == hi:
                return s
            return s.clip(lower=lo, upper=hi)

        out[col] = out.groupby(sector_col)[col].transform(_clip_sector)
    return out


def _z_within_sector(df: pd.DataFrame, col: str, *, sector_col: str) -> pd.Series:
    return df.groupby(sector_col, group_keys=False)[col].transform(metrics.zscore_within_series)


def _adjusted_z(df: pd.DataFrame, col: str, *, sector_col: str) -> pd.Series:
    z = _z_within_sector(df, col, sector_col=sector_col)
    if col in INVERSE_RAW_COLS:
        return -z
    return z


def _weighted_subscore(df: pd.DataFrame, weights: dict[str, float], *, sector_col: str) -> pd.Series:
    parts: list[pd.Series] = []
    for col, w in weights.items():
        parts.append(_adjusted_z(df, col, sector_col=sector_col) * w)
    return sum(parts)


def attach_multifactor_scores(df: pd.DataFrame, *, sector_col: str = SECTOR_COL) -> pd.DataFrame:
    """
    Returns a copy with:
    - quality_z, value_z, momentum_z, risk_z
    - final_score
    """
    out = winsorize_within_sector(df, WINSORIZE_COLS, sector_col=sector_col)
    # Keep the binary trend flag numeric before imputation/scoring math.
    out["above_200dma"] = pd.to_numeric(out["above_200dma"], errors="coerce")
    out["above_200dma"] = out["above_200dma"].map(lambda x: 1.0 if pd.notna(x) and x >= 0.5 else 0.0)
    out = impute_sector_medians(out, ALL_MODEL_COLS, sector_col=sector_col)
    out["quality_z"] = _weighted_subscore(out, QUALITY_WEIGHTS, sector_col=sector_col)
    out["value_z"] = _weighted_subscore(out, VALUE_WEIGHTS, sector_col=sector_col)
    out["momentum_z"] = _weighted_subscore(out, MOMENTUM_WEIGHTS, sector_col=sector_col)
    # Small trend boost: reward clean trend structure without overfitting the momentum score.
    trend_strength_z = _z_within_sector(out, "trend_strength_50_200", sector_col=sector_col)
    out["momentum_z"] = (
        out["momentum_z"]
        + 0.10 * (out["above_200dma"] - 0.5)
        + 0.10 * trend_strength_z
    )
    out["risk_z"] = _weighted_subscore(out, RISK_WEIGHTS, sector_col=sector_col)
    wq, wv, wm, wr = (
        FACTOR_WEIGHTS["quality_z"],
        FACTOR_WEIGHTS["value_z"],
        FACTOR_WEIGHTS["momentum_z"],
        FACTOR_WEIGHTS["risk_z"],
    )
    out["final_score"] = wq * out["quality_z"] + wv * out["value_z"] + wm * out["momentum_z"] + wr * out["risk_z"]
    return out
