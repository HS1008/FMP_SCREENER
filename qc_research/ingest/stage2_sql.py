"""Stage 2 upsert SQL and research_runs metadata writes.

Does not launch QuantConnect. Does not mark holdout_accessed.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

from qc_research.contracts.hashing import canonical_dumps
from qc_research.contracts.kinds import SCHEMA_VERSION
from qc_research.lifecycle import normalize_research_lifecycle


def conflict_sql(statement: str, *, sealed: bool) -> str:
    """Sealed official rows insert once; later re-ingest cannot rewrite them."""
    if not sealed:
        return statement
    head, sep, rest = statement.partition("ON CONFLICT")
    if not sep:
        return statement
    target = rest.split(" DO ", 1)[0]
    return "{0}ON CONFLICT{1} DO NOTHING".format(head, target)


def _run_is_sealed(run_id: str | None) -> bool:
    from qc_research.contracts.sealed_results import sealed_results_run_ids

    key = str(run_id or "")
    return bool(key) and key in sealed_results_run_ids()


def _payload_is_sealed(payload: dict[str, Any] | None) -> bool:
    record = dict(payload or {})
    if _run_is_sealed(record.get("research_run_id") or record.get("run_id")):
        return True
    qc_id = str(record.get("backtest_id") or "")
    if qc_id:
        from qc_research.contracts.sealed_results import official_sealed_qc_backtest_ids

        if qc_id in official_sealed_qc_backtest_ids():
            return True
    model_id = str(record.get("model_id") or "")
    if not model_id:
        return False
    from qc_research.contracts.sealed_results import official_sealed_model_ids

    return model_id in official_sealed_model_ids()


UPSERT_ARTIFACT_SQL = """
INSERT INTO research_artifacts (
    artifact_key, research_run_id, research_experiment_id, artifact_type,
    sha256, payload_json, created_at, synced_at, transport, logical_path
) VALUES (
    :artifact_key, :research_run_id, :research_experiment_id, :artifact_type,
    :sha256, CAST(:payload_json AS JSONB), NOW(), NOW(), :transport, :logical_path
)
ON CONFLICT (artifact_key) DO UPDATE SET
    sha256 = EXCLUDED.sha256,
    payload_json = EXCLUDED.payload_json,
    synced_at = NOW(),
    transport = EXCLUDED.transport,
    logical_path = EXCLUDED.logical_path
"""

UPSERT_TRIAL_SQL = """
INSERT INTO ml_trials (
    research_run_id, outer_window_id, trial_id, model_family,
    hyperparameters_json, median_rank_ic, mean_rank_ic, icir,
    positive_ic_fraction, worst_fold_ic, fold_metrics_json,
    selected, robustness_label, status
) VALUES (
    :research_run_id, :outer_window_id, :trial_id, :model_family,
    CAST(:hyperparameters_json AS JSONB), :median_rank_ic, :mean_rank_ic, :icir,
    :positive_ic_fraction, :worst_fold_ic, CAST(:fold_metrics_json AS JSONB),
    :selected, :robustness_label, :status
)
ON CONFLICT (research_run_id, outer_window_id, trial_id) DO UPDATE SET
    model_family = EXCLUDED.model_family,
    hyperparameters_json = EXCLUDED.hyperparameters_json,
    median_rank_ic = EXCLUDED.median_rank_ic,
    mean_rank_ic = EXCLUDED.mean_rank_ic,
    icir = EXCLUDED.icir,
    positive_ic_fraction = EXCLUDED.positive_ic_fraction,
    worst_fold_ic = EXCLUDED.worst_fold_ic,
    fold_metrics_json = EXCLUDED.fold_metrics_json,
    selected = EXCLUDED.selected,
    robustness_label = EXCLUDED.robustness_label,
    status = EXCLUDED.status
"""

UPSERT_MODEL_SQL = """
INSERT INTO ml_models (
    model_id, research_run_id, outer_window_id, model_family,
    hyperparameters_json, feature_set_id, feature_set_hash, target_id,
    target_hash, train_start, train_end, object_store_key, model_sha256,
    metadata_json
) VALUES (
    :model_id, :research_run_id, :outer_window_id, :model_family,
    CAST(:hyperparameters_json AS JSONB), :feature_set_id, :feature_set_hash,
    :target_id, :target_hash, :train_start, :train_end, :object_store_key,
    :model_sha256, CAST(:metadata_json AS JSONB)
)
ON CONFLICT (model_id) DO UPDATE SET
    hyperparameters_json = EXCLUDED.hyperparameters_json,
    model_sha256 = EXCLUDED.model_sha256,
    metadata_json = EXCLUDED.metadata_json,
    object_store_key = EXCLUDED.object_store_key
"""

UPSERT_FEATURE_SQL = """
INSERT INTO ml_feature_diagnostics (
    research_run_id, outer_window_id, feature_name, ridge_coefficient,
    coefficient_rank, mean_univariate_rank_ic, median_univariate_rank_ic,
    positive_ic_fraction, missing_fraction, metadata_json
) VALUES (
    :research_run_id, :outer_window_id, :feature_name, :ridge_coefficient,
    :coefficient_rank, :mean_univariate_rank_ic, :median_univariate_rank_ic,
    :positive_ic_fraction, :missing_fraction, CAST(:metadata_json AS JSONB)
)
ON CONFLICT (research_run_id, outer_window_id, feature_name) DO UPDATE SET
    ridge_coefficient = EXCLUDED.ridge_coefficient,
    coefficient_rank = EXCLUDED.coefficient_rank,
    mean_univariate_rank_ic = EXCLUDED.mean_univariate_rank_ic,
    median_univariate_rank_ic = EXCLUDED.median_univariate_rank_ic,
    positive_ic_fraction = EXCLUDED.positive_ic_fraction,
    missing_fraction = EXCLUDED.missing_fraction,
    metadata_json = EXCLUDED.metadata_json
"""

UPSERT_SIGNAL_SQL = """
INSERT INTO ml_signal_points (
    backtest_id, research_run_id, timestamp, scope, rank_ic, n_names,
    turnover, gross_return, net_return, stress_10bps_return, stress_20bps_return
) VALUES (
    :backtest_id, :research_run_id, :timestamp, :scope, :rank_ic, :n_names,
    :turnover, :gross_return, :net_return, :stress_10bps_return, :stress_20bps_return
)
ON CONFLICT (backtest_id, timestamp, scope) DO UPDATE SET
    rank_ic = EXCLUDED.rank_ic,
    n_names = EXCLUDED.n_names,
    turnover = EXCLUDED.turnover,
    gross_return = EXCLUDED.gross_return,
    net_return = EXCLUDED.net_return,
    stress_10bps_return = EXCLUDED.stress_10bps_return,
    stress_20bps_return = EXCLUDED.stress_20bps_return
"""


def _json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return canonical_dumps(value)


def upsert_artifact(
    conn,
    *,
    key: str,
    run_id: str,
    kind: str,
    payload: dict[str, Any],
    sha: str,
    transport: str | None = None,
    logical_path: str | None = None,
) -> None:
    path_run = None
    if key or logical_path:
        from qc_research.contracts.sealed_results import sealed_run_id_in_path

        path_run = sealed_run_id_in_path(key) or sealed_run_id_in_path(logical_path)
    conn.execute(
        text(conflict_sql(UPSERT_ARTIFACT_SQL, sealed=_run_is_sealed(run_id) or _run_is_sealed(path_run))),
        {
            "artifact_key": key,
            "research_run_id": run_id,
            "research_experiment_id": payload.get("experiment_id"),
            "artifact_type": kind,
            "sha256": sha,
            "payload_json": canonical_dumps(payload),
            "transport": transport,
            "logical_path": logical_path or key,
        },
    )


def upsert_trials_from_training_summary(conn, payload: dict[str, Any]) -> int:
    trials = payload.get("candidate_trials") or []
    count = 0
    sql = conflict_sql(
        UPSERT_TRIAL_SQL,
        sealed=_run_is_sealed(payload.get("research_run_id") or payload.get("run_id")),
    )
    for trial in trials:
        conn.execute(
            text(sql),
            {
                "research_run_id": payload.get("research_run_id"),
                "outer_window_id": payload.get("window_id"),
                "trial_id": trial.get("trial_id"),
                "model_family": trial.get("model_family"),
                "hyperparameters_json": _json(trial.get("hyperparameters")),
                "median_rank_ic": trial.get("median_rank_ic"),
                "mean_rank_ic": trial.get("mean_rank_ic"),
                "icir": trial.get("icir"),
                "positive_ic_fraction": trial.get("positive_ic_fraction"),
                "worst_fold_ic": trial.get("worst_fold_ic"),
                "fold_metrics_json": _json(trial.get("fold_metrics")),
                "selected": bool(trial.get("selected")),
                "robustness_label": trial.get("robustness_label"),
                "status": trial.get("status"),
            },
        )
        count += 1
    return count


def upsert_model_from_metadata(conn, payload: dict[str, Any]) -> None:
    conn.execute(
        text(
            conflict_sql(
                UPSERT_MODEL_SQL,
                sealed=_payload_is_sealed(payload),
            )
        ),
        {
            "model_id": payload.get("model_id"),
            "research_run_id": payload.get("run_id") or payload.get("research_run_id"),
            "outer_window_id": payload.get("outer_window_id"),
            "model_family": payload.get("model_family"),
            "hyperparameters_json": _json(payload.get("hyperparameters")),
            "feature_set_id": payload.get("feature_set_id"),
            "feature_set_hash": payload.get("feature_set_hash"),
            "target_id": payload.get("target_id"),
            "target_hash": payload.get("target_hash"),
            "train_start": payload.get("train_start"),
            "train_end": payload.get("train_end"),
            "object_store_key": payload.get("object_store_key"),
            "model_sha256": payload.get("model_sha256"),
            "metadata_json": canonical_dumps(payload),
        },
    )


def upsert_features_from_training_summary(conn, payload: dict[str, Any]) -> int:
    rows = payload.get("feature_diagnostics") or []
    if isinstance(rows, dict):
        rows = rows.get("features") or rows.get("rows") or []
    count = 0
    sql = conflict_sql(
        UPSERT_FEATURE_SQL,
        sealed=_run_is_sealed(payload.get("research_run_id") or payload.get("run_id")),
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        conn.execute(
            text(sql),
            {
                "research_run_id": payload.get("research_run_id"),
                "outer_window_id": payload.get("window_id"),
                "feature_name": row.get("feature_name"),
                "ridge_coefficient": row.get("ridge_coefficient"),
                "coefficient_rank": row.get("coefficient_rank"),
                "mean_univariate_rank_ic": row.get("mean_univariate_rank_ic"),
                "median_univariate_rank_ic": row.get("median_univariate_rank_ic"),
                "positive_ic_fraction": row.get("positive_ic_fraction"),
                "missing_fraction": row.get("missing_fraction"),
                "metadata_json": _json(row.get("metadata") or {}),
            },
        )
        count += 1
    return count


def upsert_signals_from_oos(conn, payload: dict[str, Any]) -> int:
    points = payload.get("monthly_signal_diagnostics") or []
    count = 0
    sql = conflict_sql(UPSERT_SIGNAL_SQL, sealed=_payload_is_sealed(payload))
    for point in points:
        conn.execute(
            text(sql),
            {
                "backtest_id": payload.get("backtest_id") or "",
                "research_run_id": payload.get("research_run_id"),
                "timestamp": point.get("timestamp") or point.get("date"),
                "scope": point.get("scope") or "month",
                "rank_ic": point.get("rank_ic"),
                "n_names": point.get("n_names"),
                "turnover": point.get("turnover"),
                "gross_return": point.get("gross_return"),
                "net_return": point.get("net_return"),
                "stress_10bps_return": point.get("stress_10bps_return"),
                "stress_20bps_return": point.get("stress_20bps_return"),
            },
        )
        count += 1
    return count


def mark_run_incomplete(conn, run_id: str, warning: str) -> None:
    from qc_research.contracts.sealed_results import sealed_results_run_ids

    if str(run_id or "") in sealed_results_run_ids():
        return
    conn.execute(
        text(
            """
            UPDATE research_runs
            SET run_status = 'INCOMPLETE',
                last_seen_at = NOW()
            WHERE research_run_id = :run_id
              AND COALESCE(run_status, '') <> 'COMPLETE'
            """
        ),
        {"run_id": run_id},
    )
    logger.warning("Stage 2 run %s marked INCOMPLETE: %s", run_id, warning)


def update_run_metadata(conn, payload: dict[str, Any]) -> None:
    """Insert or update a Stage 2 research_runs row from published JSON.

    Persists lifecycle fields via normalize_research_lifecycle.
    Does not mark holdout_accessed. Sealed holdout dates are metadata only.
    Does not change economic numbers; economic_gate defaults to NOT_DEFINED.
    """
    run_id = payload.get("research_run_id") or payload.get("run_id")
    if not run_id:
        return
    from qc_research.contracts.sealed_results import (
        refuse_sealed_committed_mismatch,
        research_run_exists,
        sealed_results_run_ids,
    )

    refuse_sealed_committed_mismatch(payload)
    if str(run_id) in sealed_results_run_ids() and research_run_exists(conn, str(run_id)):
        return
    holdout = payload.get("holdout_spec") or payload.get("holdout") or {}
    lifecycle = normalize_research_lifecycle(payload)
    statement = """
            INSERT INTO research_runs (
                research_run_id,
                strategy_id,
                suite_version,
                git_commit,
                dirty,
                first_seen_at,
                last_seen_at,
                holdout_accessed,
                holdout_access_count,
                research_kind,
                artifact_schema_version,
                feature_set_id,
                feature_set_hash,
                target_id,
                target_hash,
                planned_internal_trials,
                completed_internal_trials,
                planned_cv_fits,
                completed_cv_fits,
                expected_experiment_count,
                completed_count,
                failed_count,
                skipped_count,
                run_status,
                holdout_start,
                holdout_end,
                research_lineage_id,
                orchestrator_summary_json,
                promotion_gate,
                holdout_status,
                economic_gate,
                delivery_status
            ) VALUES (
                :research_run_id,
                :strategy_id,
                'S2',
                :git_commit,
                :dirty,
                NOW(),
                NOW(),
                FALSE,
                0,
                'stage2_ml',
                :schema_version,
                :feature_set_id,
                :feature_set_hash,
                :target_id,
                :target_hash,
                :planned_internal_trials,
                :completed_internal_trials,
                :planned_cv_fits,
                :completed_cv_fits,
                :expected_experiment_count,
                :completed_count,
                :failed_count,
                :skipped_count,
                :run_status,
                CAST(:holdout_start AS DATE),
                CAST(:holdout_end AS DATE),
                :research_lineage_id,
                CAST(:summary AS JSONB),
                :promotion_gate,
                :holdout_status,
                :economic_gate,
                :delivery_status
            )
            ON CONFLICT (research_run_id) DO UPDATE SET
                last_seen_at = NOW(),
                research_kind = 'stage2_ml',
                artifact_schema_version = COALESCE(EXCLUDED.artifact_schema_version, research_runs.artifact_schema_version),
                feature_set_id = COALESCE(EXCLUDED.feature_set_id, research_runs.feature_set_id),
                feature_set_hash = COALESCE(EXCLUDED.feature_set_hash, research_runs.feature_set_hash),
                target_id = COALESCE(EXCLUDED.target_id, research_runs.target_id),
                target_hash = COALESCE(EXCLUDED.target_hash, research_runs.target_hash),
                planned_internal_trials = COALESCE(EXCLUDED.planned_internal_trials, research_runs.planned_internal_trials),
                completed_internal_trials = COALESCE(EXCLUDED.completed_internal_trials, research_runs.completed_internal_trials),
                planned_cv_fits = COALESCE(EXCLUDED.planned_cv_fits, research_runs.planned_cv_fits),
                completed_cv_fits = COALESCE(EXCLUDED.completed_cv_fits, research_runs.completed_cv_fits),
                expected_experiment_count = COALESCE(EXCLUDED.expected_experiment_count, research_runs.expected_experiment_count),
                completed_count = COALESCE(EXCLUDED.completed_count, research_runs.completed_count),
                failed_count = COALESCE(EXCLUDED.failed_count, research_runs.failed_count),
                skipped_count = COALESCE(EXCLUDED.skipped_count, research_runs.skipped_count),
                run_status = CASE
                    WHEN research_runs.run_status = 'COMPLETE' THEN research_runs.run_status
                    ELSE COALESCE(EXCLUDED.run_status, research_runs.run_status)
                END,
                holdout_start = COALESCE(EXCLUDED.holdout_start, research_runs.holdout_start),
                holdout_end = COALESCE(EXCLUDED.holdout_end, research_runs.holdout_end),
                research_lineage_id = COALESCE(EXCLUDED.research_lineage_id, research_runs.research_lineage_id),
                orchestrator_summary_json = COALESCE(EXCLUDED.orchestrator_summary_json, research_runs.orchestrator_summary_json),
                git_commit = COALESCE(EXCLUDED.git_commit, research_runs.git_commit),
                dirty = COALESCE(EXCLUDED.dirty, research_runs.dirty),
                promotion_gate = COALESCE(EXCLUDED.promotion_gate, research_runs.promotion_gate),
                holdout_status = CASE
                    WHEN UPPER(COALESCE(research_runs.holdout_status, '')) = 'ACCESSED'
                        THEN research_runs.holdout_status
                    WHEN UPPER(COALESCE(EXCLUDED.holdout_status, '')) = 'ACCESSED'
                        THEN EXCLUDED.holdout_status
                    ELSE COALESCE(EXCLUDED.holdout_status, research_runs.holdout_status)
                END,
                holdout_accessed = COALESCE(research_runs.holdout_accessed, FALSE)
                    OR COALESCE(EXCLUDED.holdout_accessed, FALSE),
                economic_gate = COALESCE(EXCLUDED.economic_gate, research_runs.economic_gate),
                delivery_status = COALESCE(EXCLUDED.delivery_status, research_runs.delivery_status)
            """
    conn.execute(
        text(conflict_sql(statement, sealed=str(run_id) in sealed_results_run_ids())),
        {
            "research_run_id": str(run_id),
            "strategy_id": payload.get("strategy_id") or "",
            "git_commit": payload.get("git_commit"),
            "dirty": bool(payload.get("dirty")),
            "schema_version": payload.get("schema_version") or SCHEMA_VERSION,
            "feature_set_id": payload.get("feature_set_id"),
            "feature_set_hash": payload.get("feature_set_hash"),
            "target_id": payload.get("target_id"),
            "target_hash": payload.get("target_hash"),
            "planned_internal_trials": payload.get("expected_internal_trials"),
            "completed_internal_trials": payload.get("completed_internal_trials"),
            "planned_cv_fits": payload.get("expected_cv_fits"),
            "completed_cv_fits": payload.get("completed_cv_fits"),
            "expected_experiment_count": payload.get("expected_qc_experiments"),
            "completed_count": payload.get("completed_qc_experiments"),
            "failed_count": payload.get("failed_qc_experiments"),
            "skipped_count": payload.get("skipped_qc_experiments"),
            "run_status": payload.get("run_status"),
            "holdout_start": holdout.get("start"),
            "holdout_end": holdout.get("end") if holdout.get("end") not in {None, "TODAY"} else None,
            "research_lineage_id": payload.get("research_lineage_id") or payload.get("strategy_id"),
            "summary": canonical_dumps(payload),
            "promotion_gate": lifecycle.get("promotion_gate"),
            "holdout_status": lifecycle.get("holdout_status"),
            "economic_gate": lifecycle.get("economic_gate") or "NOT_DEFINED",
            "delivery_status": lifecycle.get("delivery_status"),
        },
    )
