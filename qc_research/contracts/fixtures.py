"""Sanitized consumer fixtures mirroring quant-strategies research/artifact_fixtures.py."""

from __future__ import annotations

from typing import Any

from qc_research.contracts.hashing import payload_for_hash, sha256_payload
from qc_research.contracts.kinds import PRICE_TECH_V1_FEATURE_ORDER

FEATURE_HASH = "64b6a92c52a207b18bb6df72a0872cd08a2f3084f33622f72e11b34d6b292e93"
TARGET_HASH = "494299984781b88598fc90e153adfd32420715a83dc75699bfb5736dbb451ff3"
CONFIG_FP = "7684df2e9dff44fa"
RUN_ID = "STAGE2_CrossSectionalFactorML_FIXTURE01"
GIT = "ef270841621933f5039680cb070559f43bd1e3c8"


def _attach(payload: dict[str, Any]) -> dict[str, Any]:
    payload["artifact_sha256"] = sha256_payload(payload_for_hash(payload))
    return payload


def stage1_run_summary() -> dict[str, Any]:
    return _attach(
        {
            "schema_version": "stage1_run_summary_v1",
            "research_run_id": "STAGE1_SPYTrend_FIXTURE01",
            "strategy_id": "SPYTrend",
            "research_lineage_id": "SPYTrend",
            "source": "orchestrator",
            "run_status": "COMPLETE",
            "expected_experiment_count": 81,
            "completed_count": 81,
            "failed_count": 0,
            "skipped_count": 0,
            "skipped_experiments": [],
            "git_commit": "f04dbfb1a936c753a42a1389d9181f7c22f551a3",
            "source_git_sha": "f04dbfb1a936c753a42a1389d9181f7c22f551a3",
            "config_fingerprint": "ae38eb0e1ff2e078",
            "holdout_accessed": False,
            "authoritative_milestone": "stage1-spytrend-complete",
            "provenance": "SANITIZED_CONTRACT_FIXTURE",
            "economic_gate": "NOT_DEFINED",
        }
    )


def stage2_run_manifest() -> dict[str, Any]:
    return _attach(
        {
            "schema_version": "stage2_ml_v1",
            "research_run_id": RUN_ID,
            "strategy_id": "CrossSectionalFactorML",
            "research_lineage_id": "CrossSectionalFactorML",
            "git_commit": GIT,
            "git_branch": "research-integration",
            "dirty": False,
            "resolved_config": {"feature_set_id": "PRICE_TECH_V1"},
            "config_fingerprint": CONFIG_FP,
            "feature_set_id": "PRICE_TECH_V1",
            "feature_set_hash": FEATURE_HASH,
            "feature_order": list(PRICE_TECH_V1_FEATURE_ORDER),
            "target_id": "SECTOR_REL_RANK_21D_V1",
            "target_hash": TARGET_HASH,
            "outer_windows": ["2015"],
            "inner_cv_config": {"folds": 3, "purge": True, "embargo": True},
            "candidate_model_spec": {"family": "Ridge", "alpha_grid": "frozen_v1"},
            "portfolio_spec": {"construction": "frozen_v1"},
            "cost_spec": {"assumptions": "frozen_v1"},
            "expected_qc_experiments": 31,
            "expected_internal_trials": 55,
            "expected_cv_fits": 165,
            "holdout_spec": {"accessed": False, "maximum_data_date": "2024-12-31"},
            "holdout_accessed": False,
            "authoritative_milestone": "csfml-v1-nonholdout-complete",
            "provenance": "SANITIZED_CONTRACT_FIXTURE",
        }
    )


def stage2_run_summary() -> dict[str, Any]:
    return _attach(
        {
            "schema_version": "stage2_ml_v1",
            "research_run_id": RUN_ID,
            "strategy_id": "CrossSectionalFactorML",
            "run_status": "COMPLETE",
            "expected_qc_experiments": 31,
            "completed_qc_experiments": 31,
            "failed_qc_experiments": 0,
            "skipped_qc_experiments": 0,
            "expected_internal_trials": 55,
            "completed_internal_trials": 55,
            "expected_cv_fits": 165,
            "completed_cv_fits": 165,
            "ml_oos_count": 10,
            "baseline_oos_count": 10,
            "final_prep_status": "complete",
            "warnings": [],
            "git_commit": GIT,
            "config_fingerprint": CONFIG_FP,
            "feature_set_id": "PRICE_TECH_V1",
            "feature_set_hash": FEATURE_HASH,
            "target_id": "SECTOR_REL_RANK_21D_V1",
            "target_hash": TARGET_HASH,
            "holdout_accessed": False,
            "economic_gate": "NOT_DEFINED",
            "provenance": "SANITIZED_CONTRACT_FIXTURE",
        }
    )


def stage2_training_summary() -> dict[str, Any]:
    return _attach(
        {
            "schema_version": "stage2_ml_v1",
            "research_run_id": RUN_ID,
            "experiment_id": "ML_TRAIN_2015",
            "window_id": "2015",
            "train_start": "2010-01-04",
            "train_end": "2014-12-31",
            "n_months": 60,
            "n_samples": 1000,
            "n_symbols": 50,
            "n_sectors": 11,
            "data_quality": {"cohorts_accepted": 10, "cohorts_rejected": 0},
            "inner_folds": [{"fold": 1}],
            "candidate_trials": [{"trial_id": 1, "alpha": 1.0, "score": None}],
            "selected_trial": {"trial_id": 1, "alpha": 1.0},
            "selected_hyperparameters": {"alpha": 1.0},
            "plateau_members": [],
            "robustness_label": "STABLE_PLATEAU",
            "model_id": "MODEL_FIXTURE",
            "object_store_key": "stage2/CrossSectionalFactorML/{0}/2015/model.pkl".format(RUN_ID),
            "model_sha256": "0" * 64,
            "feature_diagnostics": {"feature_order": list(PRICE_TECH_V1_FEATURE_ORDER)},
            "warnings": [],
            "holdout_accessed": False,
            "provenance": "SANITIZED_CONTRACT_FIXTURE",
        }
    )


def stage2_model_metadata() -> dict[str, Any]:
    return _attach(
        {
            "schema_version": "stage2_ml_v1",
            "model_id": "MODEL_FIXTURE",
            "run_id": RUN_ID,
            "outer_window_id": "2015",
            "strategy_id": "CrossSectionalFactorML",
            "feature_set_id": "PRICE_TECH_V1",
            "feature_set_hash": FEATURE_HASH,
            "target_id": "SECTOR_REL_RANK_21D_V1",
            "target_hash": TARGET_HASH,
            "model_family": "Ridge",
            "hyperparameters": {"alpha": 1.0},
            "train_start": "2010-01-04",
            "train_end": "2014-12-31",
            "git_commit": GIT,
            "config_fingerprint": CONFIG_FP,
            "object_store_key": "stage2/CrossSectionalFactorML/{0}/2015/model.pkl".format(RUN_ID),
            "model_sha256": "0" * 64,
            "created_at": "2026-01-01T00:00:00+00:00",
            "binary_published": False,
            "holdout_accessed": False,
            "provenance": "SANITIZED_CONTRACT_FIXTURE",
        }
    )


def stage2_oos_diagnostics() -> dict[str, Any]:
    return _attach(
        {
            "schema_version": "stage2_ml_v1",
            "research_run_id": RUN_ID,
            "experiment_id": "ML_OOS_2015",
            "backtest_id": "bt-fixture-ml",
            "window_id": "2015",
            "monthly_signal_diagnostics": [],
            "rank_ic": None,
            "turnover": None,
            "gross_return": None,
            "net_return": None,
            "stress_returns": None,
            "warnings": [],
            "holdout_accessed": False,
            "metric_source": "nullable_official",
            "provenance": "SANITIZED_CONTRACT_FIXTURE",
        }
    )


def stage2_baseline_oos_diagnostics() -> dict[str, Any]:
    payload = stage2_oos_diagnostics()
    payload["experiment_id"] = "BASELINE_OOS_2015"
    payload["backtest_id"] = "bt-fixture-baseline"
    return _attach(payload_for_hash(payload))


def stage2_oos_aggregate() -> dict[str, Any]:
    return _attach(
        {
            "schema_version": "stage2_ml_v1",
            "research_run_id": RUN_ID,
            "strategy_id": "CrossSectionalFactorML",
            "windows": ["2015"],
            "ml": {"rank_ic": None},
            "baseline": {"rank_ic": None},
            "comparison": {},
            "stability": {},
            "feature_stability": {"feature_order": list(PRICE_TECH_V1_FEATURE_ORDER)},
            "cost": {},
            "metric_source": "nullable_official",
            "create_accounting": {"official": 31, "holdout": 0},
            "holdout_excluded": True,
            "holdout_accessed": False,
            "economic_gate": "NOT_DEFINED",
            "provenance": "SANITIZED_CONTRACT_FIXTURE",
        }
    )


def stage2_nonholdout_assessment() -> dict[str, Any]:
    return _attach(
        {
            "schema_version": "stage2_ml_v1",
            "research_run_id": RUN_ID,
            "progress": "COMPLETE",
            "status": "COMPLETE",
            "economic_gate": "NOT_DEFINED",
            "label_uses_holdout": False,
            "holdout_excluded": True,
            "research_experiment_count": 31,
            "supported_threshold_keys": [],
            "promotion_gate": "HUMAN_REVIEW_REQUIRED",
            "holdout_accessed": False,
            "provenance": "SANITIZED_CONTRACT_FIXTURE",
        }
    )


def platform_strategy_spec() -> dict[str, Any]:
    return _attach(
        {
            "schema_version": "platform_artifact_v1",
            "research_run_id": "PLATFORM_TLT_FIXTURE01",
            "strategy_id": "TLTDurationMomentum",
            "research_lineage_id": "LINEAGE_TLT_DURATION_MOMENTUM_V0",
            "kind": "strategy_spec",
            "economic_gate": "NOT_DEFINED",
            "promotion_gate": "HUMAN_REVIEW_REQUIRED",
            "holdout_accessed": False,
            "holdout_locked": True,
            "provenance": "SANITIZED_CONTRACT_FIXTURE",
        }
    )


CONSUMER_FIXTURES = {
    "run_manifest": stage2_run_manifest,
    "run_summary": stage2_run_summary,
    "training_summary": stage2_training_summary,
    "model_metadata": stage2_model_metadata,
    "oos_diagnostics": stage2_oos_diagnostics,
    "baseline_oos_diagnostics": stage2_baseline_oos_diagnostics,
    "oos_aggregate": stage2_oos_aggregate,
    "nonholdout_assessment": stage2_nonholdout_assessment,
    "stage1_run_summary": stage1_run_summary,
    "strategy_spec": platform_strategy_spec,
}
