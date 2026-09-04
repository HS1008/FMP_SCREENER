"""Stage 2 aggregation. Never mixes Stage 1 rows into Stage 2 gates."""

from __future__ import annotations

from typing import Any

import pandas as pd

from qc_research.aggregation import is_stage1, smoke_mask


PASS = "PASS"
WATCH = "WATCH"
FAIL = "FAIL"
IN_PROGRESS = "IN_PROGRESS"
COMPLETE = "COMPLETE"
INCOMPLETE = "INCOMPLETE"
ECONOMIC_GATE_APPLIED = "APPLIED"
ECONOMIC_GATE_NOT_DEFINED = "NOT_DEFINED"

STAGE2_SUITE_PREFIXES = ("S2",)
HOLDOUT_TYPES = {
    "ML_FINAL_HOLDOUT",
    "POST_HOLDOUT_ML_TRAIN",
    "POST_HOLDOUT_ML_OOS",
}


def is_stage2_name(name: Any) -> bool:
    return bool(name) and str(name).startswith("S2__")


def is_stage2(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=bool)
    mask = pd.Series(False, index=df.index)
    if "research_suite_version" in df.columns:
        version = df["research_suite_version"].fillna("").astype(str)
        mask = mask | version.str.upper().isin({"S2", "S2.0"}) | version.str.startswith("S2")
    if "name" in df.columns:
        mask = mask | df["name"].fillna("").astype(str).str.startswith("S2__")
    if "research_kind" in df.columns:
        mask = mask | df["research_kind"].fillna("").astype(str).str.lower().eq("stage2_ml")
    return mask & ~is_stage1(df)


def stage2_backtests(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    stage = df.loc[is_stage2(df)].copy()
    return stage.loc[~smoke_mask(stage)].copy()


def stage2_research_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Stage 2 rows that may enter PASS/WATCH/FAIL. Holdout excluded."""
    stage = stage2_backtests(df)
    if stage is None or stage.empty:
        return stage
    holdout = pd.Series(False, index=stage.index)
    if "research_is_holdout" in stage.columns:
        holdout = holdout | stage["research_is_holdout"].fillna(False).astype(bool)
    if "research_test_type" in stage.columns:
        holdout = holdout | stage["research_test_type"].fillna("").astype(str).isin(HOLDOUT_TYPES)
    if "research_phase" in stage.columns:
        holdout = holdout | stage["research_phase"].fillna("").astype(str).eq("HOLDOUT")
    return stage.loc[~holdout].copy()


def stage2_holdout_rows(df: pd.DataFrame) -> pd.DataFrame:
    stage = stage2_backtests(df)
    if stage is None or stage.empty:
        return stage
    research = stage2_research_rows(stage)
    return stage.loc[~stage.index.isin(research.index)].copy()


def assess_stage2_progress(
    *,
    expected: int,
    completed: int,
    failed: int = 0,
    skipped: int = 0,
    missing_required_artifacts: bool = False,
    running: bool = False,
) -> str:
    accounted = completed + failed + skipped
    if missing_required_artifacts:
        return INCOMPLETE
    if accounted < expected:
        return IN_PROGRESS if running or completed > 0 else INCOMPLETE
    if failed or skipped:
        return INCOMPLETE
    return COMPLETE


def assess_stage2(
    research_rows: pd.DataFrame | None = None,
    *,
    expected: int = 31,
    completed: int | None = None,
    failed: int = 0,
    skipped: int = 0,
    missing_required_artifacts: bool = False,
    running: bool = False,
    thresholds: dict[str, Any] | None = None,
    oos_metrics: dict[str, Any] | None = None,
    holdout_rows: pd.DataFrame | None = None,
) -> dict[str, Any]:
    del holdout_rows  # never used for the gate
    if completed is None and research_rows is not None and not research_rows.empty:
        status = research_rows.get("status")
        if status is not None:
            completed = int(status.astype(str).str.lower().eq("completed").sum())
        else:
            completed = 0
    completed = int(completed or 0)
    progress = assess_stage2_progress(
        expected=expected,
        completed=completed,
        failed=failed,
        skipped=skipped,
        missing_required_artifacts=missing_required_artifacts,
        running=running,
    )
    result = {
        "progress": progress,
        "status": progress,
        "label_uses_holdout": False,
        "economic_gate": None,
        "economic_status": None,
        "thresholds": {},
        "oos_metrics": {},
        "reasons": [],
    }
    if progress != COMPLETE:
        return result
    gates = {
        key: value
        for key, value in dict(thresholds or {}).items()
        if value is not None
    }
    metrics = {
        key: value
        for key, value in dict(oos_metrics or {}).items()
        if "holdout" not in str(key).lower()
    }
    result["thresholds"] = dict(gates)
    result["oos_metrics"] = dict(metrics)
    if not gates:
        result["economic_gate"] = ECONOMIC_GATE_NOT_DEFINED
        result["reasons"] = ["thresholds_not_defined"]
        return result
    result["economic_gate"] = ECONOMIC_GATE_APPLIED
    min_ic = gates.get("min_median_oos_rank_ic")
    median_ic = metrics.get("median_rank_ic")
    if min_ic is not None and median_ic is not None and float(median_ic) < float(min_ic):
        result["status"] = FAIL
        result["economic_status"] = FAIL
        result["reasons"] = ["median_oos_rank_ic"]
        return result
    min_pos = gates.get("min_positive_ic_fraction")
    pos = metrics.get("positive_ic_fraction")
    if min_pos is not None and pos is not None and float(pos) < float(min_pos):
        result["status"] = WATCH
        result["economic_status"] = WATCH
        result["reasons"] = ["positive_ic_fraction"]
        return result
    result["status"] = PASS
    result["economic_status"] = PASS
    return result
