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

STAGE2_THRESHOLD_KEYS = (
    "min_median_oos_rank_ic",
    "min_positive_ic_fraction",
    "min_windows_ml_ic_gt_baseline_fraction",
    "min_windows_ml_net_gt_baseline_fraction",
    "min_ml_minus_baseline_risk_adjusted",
    "cost_stress_robustness",
    "min_parameter_or_feature_stability",
)
STAGE2_RESERVED_THRESHOLD_KEYS = (
    "cost_stress_robustness",
    "min_parameter_or_feature_stability",
)

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


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
    except Exception:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def nonholdout_research_experiment_count(
    run_summary: dict[str, Any] | None = None,
    research_rows: pd.DataFrame | None = None,
) -> int:
    if research_rows is not None and not research_rows.empty:
        return int(len(research_rows))
    summary = dict(run_summary or {})
    completed = int(summary.get("completed_qc_experiments") or 0)
    holdout_completed = int(summary.get("holdout_completed") or summary.get("holdout_qc_experiments") or 0)
    count = completed - holdout_completed
    if count > 0:
        return count
    return int(summary.get("expected_qc_experiments") or 0)


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
    run_summary: dict[str, Any] | None = None,
    research_experiment_count: int | None = None,
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
    if research_experiment_count is not None:
        count = int(research_experiment_count)
    else:
        count = nonholdout_research_experiment_count(run_summary, research_rows)
        if research_rows is None and not run_summary:
            count = completed if completed else 0
    result = {
        "progress": progress,
        "status": progress,
        "research_experiment_count": count,
        "label_uses_holdout": False,
        "economic_gate": None,
        "economic_status": None,
        "thresholds": {},
        "oos_metrics": {},
        "reasons": [],
        "supported_threshold_keys": list(STAGE2_THRESHOLD_KEYS),
        "unevaluated_threshold_keys": [],
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
    failures = []
    watches = []
    evaluated = False
    min_ic = gates.get("min_median_oos_rank_ic")
    median_ic = metrics.get("median_rank_ic")
    if min_ic is not None and median_ic is not None:
        evaluated = True
        if float(median_ic) < float(min_ic):
            failures.append("median_oos_rank_ic")
    min_pos = gates.get("min_positive_ic_fraction")
    pos = metrics.get("positive_ic_fraction")
    if min_pos is not None and pos is not None:
        evaluated = True
        if float(pos) < float(min_pos):
            watches.append("positive_ic_fraction")
    min_ic_frac = gates.get("min_windows_ml_ic_gt_baseline_fraction")
    ic_frac = _as_float(metrics.get("windows_ml_ic_gt_baseline_fraction"))
    if min_ic_frac is not None and ic_frac is not None:
        evaluated = True
        if ic_frac < float(min_ic_frac):
            watches.append("windows_ml_ic_gt_baseline_fraction")
    min_net_frac = gates.get("min_windows_ml_net_gt_baseline_fraction")
    net_frac = _as_float(metrics.get("windows_ml_net_gt_baseline_fraction"))
    if min_net_frac is not None and net_frac is not None:
        evaluated = True
        if net_frac < float(min_net_frac):
            watches.append("windows_ml_net_gt_baseline_fraction")
    min_risk = gates.get("min_ml_minus_baseline_risk_adjusted")
    risk_delta = _as_float(
        metrics.get("ml_minus_baseline_risk_adjusted")
        if metrics.get("ml_minus_baseline_risk_adjusted") is not None
        else metrics.get("sharpe_ratio_diff")
    )
    if min_risk is not None and risk_delta is not None:
        evaluated = True
        if risk_delta < float(min_risk):
            failures.append("ml_minus_baseline_risk_adjusted")
    unevaluated = [key for key in STAGE2_RESERVED_THRESHOLD_KEYS if key in gates]
    result["unevaluated_threshold_keys"] = unevaluated
    if failures:
        result["status"] = FAIL
        result["economic_status"] = FAIL
        result["reasons"] = failures
        return result
    if watches:
        result["status"] = WATCH
        result["economic_status"] = WATCH
        result["reasons"] = watches
        return result
    if evaluated:
        result["status"] = PASS
        result["economic_status"] = PASS
        return result
    result["status"] = COMPLETE
    result["economic_status"] = None
    result["reasons"] = ["thresholds_reserved_unevaluated"] if unevaluated else ["thresholds_not_evaluated"]
    return result
