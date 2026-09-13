"""Consumer artifact kinds and required identity fields."""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "stage2_ml_v1"
PLATFORM_SCHEMA_VERSIONS = {SCHEMA_VERSION, "platform_v1", "platform_artifact_v1"}

KIND_REQUIRED_FIELDS = {
    "run_manifest": ("schema_version", "research_run_id", "strategy_id"),
    "run_summary": ("schema_version", "research_run_id", "run_status"),
    "training_summary": ("schema_version", "research_run_id", "window_id", "candidate_trials"),
    "oos_diagnostics": ("schema_version", "research_run_id", "window_id"),
    "model_metadata": ("model_id", "run_id", "model_sha256"),
    "oos_aggregate": ("schema_version", "research_run_id", "windows", "ml", "baseline", "holdout_excluded"),
    "nonholdout_assessment": ("schema_version", "research_run_id", "progress", "status", "economic_gate"),
}

PLATFORM_KINDS = {
    "strategy_spec",
    "experiment_manifest",
    "assessment",
    "risk_diagnostics",
    "parameter_sensitivity",
    "walk_forward",
    "trials",
    "feature_diagnostics",
    "selection_diagnostics",
    "strategy_intent",
    "search_space",
    "pair_diagnostics",
    "fixed_income_risk",
    "fixed_income_diagnostics",
    "curve_diagnostics",
    "futures_roll_diagnostics",
    "roll_diagnostics",
}

PRICE_TECH_V1_FEATURE_ORDER = (
    "MOM_12_1",
    "MOM_6_1",
    "RET_3M",
    "REV_1M",
    "MOM_ACCEL",
    "VOL_252",
    "MAXDD_252",
    "MOMVOL",
    "TREND_50_200",
    "DIST_200",
    "ABOVE_200",
)


class ArtifactContractError(ValueError):
    """Consumer-side contract failure."""


def reject_synthetic_official(payload: dict[str, Any]) -> None:
    provenance = str(payload.get("provenance") or (payload.get("payload") or {}).get("provenance") or "")
    if provenance == "SYNTHETIC_TEST_ONLY":
        raise ArtifactContractError("SYNTHETIC_TEST_ONLY artifacts cannot be ingested as research evidence")


def _flag_true(value: Any) -> bool:
    return value is True or value in {1, "1", "true", "True", "yes", "YES", "on", "ON"}


def reject_holdout_access(payload: dict[str, Any]) -> None:
    if _flag_true(payload.get("holdout_accessed")):
        raise ArtifactContractError("Official non-holdout ingest refuses holdout_accessed=true")
    spec = payload.get("holdout_spec")
    if isinstance(spec, dict) and _flag_true(spec.get("accessed")):
        raise ArtifactContractError("Official non-holdout ingest refuses holdout_spec.accessed=true")
    if str(payload.get("holdout_status") or "").strip().upper() == "ACCESSED":
        raise ArtifactContractError("Official non-holdout ingest refuses holdout_status=ACCESSED")
    if str(payload.get("economic_gate") or "NOT_DEFINED") not in {"NOT_DEFINED", None, ""}:
        if payload.get("economic_gate") in {"PASS", "WATCH", "FAIL"}:
            raise ArtifactContractError("economic_gate PASS/WATCH/FAIL is not a producer-defined threshold")
