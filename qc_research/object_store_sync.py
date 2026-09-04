"""Backend Object Store synchronization for Stage 2 artifacts.

Does not depend on Streamlit. Does not launch backtests. Idempotent.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable, Iterable

from sqlalchemy import text


logger = logging.getLogger(__name__)

SCHEMA_VERSION = "stage2_ml_v1"
PLATFORM_SCHEMA_VERSIONS = {SCHEMA_VERSION, "platform_v1", "platform_artifact_v1"}
REQUIRED_RUN_ARTIFACTS = ("run_manifest", "run_summary")

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


class ArtifactSyncError(ValueError):
    """An Object Store artifact failed validation."""


def canonical_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_payload(value: Any) -> str:
    return hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()


def parse_json_payload(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def validate_artifact(kind: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ArtifactSyncError("{0} is not a JSON object".format(kind))
    version = payload.get("schema_version")
    if version in {"platform_v1", "platform_artifact_v1"}:
        return payload
    if kind in PLATFORM_KINDS:
        if version not in PLATFORM_SCHEMA_VERSIONS:
            raise ArtifactSyncError(
                "{0} schema_version {1!r} not in {2}".format(kind, version, sorted(PLATFORM_SCHEMA_VERSIONS))
            )
        return payload
    if kind != "model_metadata" and version != SCHEMA_VERSION:
        raise ArtifactSyncError(
            "{0} schema_version {1!r} != {2}".format(kind, version, SCHEMA_VERSION)
        )
    missing = [field for field in KIND_REQUIRED_FIELDS.get(kind, ()) if field not in payload]
    if missing:
        raise ArtifactSyncError("{0} missing fields: {1}".format(kind, missing))
    return payload


def payload_for_hash(payload: dict[str, Any]) -> dict[str, Any]:
    """Hash the artifact body. artifact_sha256 is a digest, not an input."""
    return {key: value for key, value in payload.items() if key != "artifact_sha256"}


def verify_hash(payload: dict[str, Any], expected: str | None) -> str:
    body = payload_for_hash(payload) if isinstance(payload, dict) else payload
    actual = sha256_payload(body)
    if expected and actual != str(expected).strip():
        raise ArtifactSyncError(
            "SHA-256 mismatch: expected {0}, computed {1}".format(expected, actual)
        )
    return actual


def object_store_key(strategy_id: str, run_id: str, filename: str, window_id: str | None = None) -> str:
    if window_id:
        return "stage2/{0}/{1}/{2}/{3}".format(strategy_id, run_id, window_id, filename)
    return "stage2/{0}/{1}/{2}".format(strategy_id, run_id, filename)


def expected_keys_for_run(strategy_id: str, run_id: str, window_ids: Iterable[str]) -> dict[str, str]:
    keys = {
        "run_manifest": object_store_key(strategy_id, run_id, "run_manifest.json"),
        "run_summary": object_store_key(strategy_id, run_id, "run_summary.json"),
    }
    for window_id in window_ids:
        keys["training_summary:{0}".format(window_id)] = object_store_key(
            strategy_id, run_id, "training_summary.json", window_id
        )
        keys["oos_diagnostics:{0}".format(window_id)] = object_store_key(
            strategy_id, run_id, "oos_diagnostics.json", window_id
        )
        keys["model_metadata:{0}".format(window_id)] = object_store_key(
            strategy_id, run_id, "model_metadata.json", window_id
        )
    return keys


def should_redownload(existing_sha: str | None, remote_sha: str | None) -> bool:
    if not existing_sha:
        return True
    if remote_sha and existing_sha == remote_sha:
        return False
    if remote_sha and existing_sha != remote_sha:
        return True
    return False


def extract_object_payload(response: dict[str, Any] | None) -> dict[str, Any] | None:
    if not response:
        return None
    for key in ("object", "value", "data", "objectData", "payload"):
        parsed = parse_json_payload(response.get(key))
        if parsed is not None:
            return parsed
    if response.get("schema_version") or response.get("research_run_id"):
        return {key: value for key, value in response.items() if key != "success"}
    return None


def extract_remote_hash(response: dict[str, Any] | None) -> str | None:
    if not response:
        return None
    for key in ("md5", "sha256", "hash", "checksum"):
        if response.get(key):
            return str(response[key])
    props = response.get("properties") or response.get("object")
    if isinstance(props, dict):
        for key in ("sha256", "md5", "hash"):
            if props.get(key):
                return str(props[key])
    return None


def identify_stage2_runs(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row.get("name") or "")
        suite = str(row.get("research_suite_version") or "")
        kind = str(row.get("research_kind") or "")
        run_id = str(row.get("research_run_id") or "")
        if not run_id:
            continue
        if not (name.startswith("S2__") or suite.startswith("S2") or kind == "stage2_ml"):
            continue
        current = found.setdefault(
            run_id,
            {
                "research_run_id": run_id,
                "strategy_id": row.get("strategy_id"),
                "windows": set(),
            },
        )
        window = row.get("research_window_id")
        if window:
            current["windows"].add(str(window))
    return list(found.values())


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
    conn.execute(
        text(UPSERT_ARTIFACT_SQL),
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
    for trial in trials:
        conn.execute(
            text(UPSERT_TRIAL_SQL),
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
        text(UPSERT_MODEL_SQL),
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
    count = 0
    for row in rows:
        conn.execute(
            text(UPSERT_FEATURE_SQL),
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
    for point in points:
        conn.execute(
            text(UPSERT_SIGNAL_SQL),
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
    conn.execute(
        text(
            """
            UPDATE research_runs
            SET run_status = 'INCOMPLETE',
                last_seen_at = NOW()
            WHERE research_run_id = :run_id
            """
        ),
        {"run_id": run_id},
    )
    logger.warning("Stage 2 run %s marked INCOMPLETE: %s", run_id, warning)


def update_run_metadata(conn, payload: dict[str, Any]) -> None:
    """Insert or update a Stage 2 research_runs row from published JSON.

    Does not mark holdout_accessed. Sealed holdout dates are metadata only.
    """
    run_id = payload.get("research_run_id") or payload.get("run_id")
    if not run_id:
        return
    holdout = payload.get("holdout_spec") or payload.get("holdout") or {}
    conn.execute(
        text(
            """
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
                orchestrator_summary_json
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
                CAST(:summary AS JSONB)
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
                run_status = COALESCE(EXCLUDED.run_status, research_runs.run_status),
                holdout_start = COALESCE(EXCLUDED.holdout_start, research_runs.holdout_start),
                holdout_end = COALESCE(EXCLUDED.holdout_end, research_runs.holdout_end),
                research_lineage_id = COALESCE(EXCLUDED.research_lineage_id, research_runs.research_lineage_id),
                orchestrator_summary_json = COALESCE(EXCLUDED.orchestrator_summary_json, research_runs.orchestrator_summary_json),
                git_commit = COALESCE(EXCLUDED.git_commit, research_runs.git_commit),
                dirty = COALESCE(EXCLUDED.dirty, research_runs.dirty)
            """
        ),
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
        },
    )


class ObjectStoreClient:
    """Thin adapter over a qc_post(endpoint, payload) callable."""

    def __init__(self, qc_post: Callable[[str, dict[str, Any]], dict[str, Any]]):
        self.qc_post = qc_post
        self._organization_id: str | None = None

    def organization_id(self) -> str:
        if self._organization_id:
            return self._organization_id
        account = self.qc_post("/account/read", {})
        oid = account.get("organizationId") or account.get("organization_id")
        if not oid:
            raise ArtifactSyncError("QuantConnect /account/read missing organizationId")
        self._organization_id = str(oid)
        return self._organization_id

    def object_properties(self, key: str) -> dict[str, Any]:
        return self.qc_post(
            "/object/properties",
            {"organizationId": self.organization_id(), "key": key},
        )

    def object_get(self, key: str) -> dict[str, Any]:
        return self.qc_post(
            "/object/get",
            {"organizationId": self.organization_id(), "key": key},
        )


def existing_artifact_hash(conn, key: str) -> str | None:
    row = conn.execute(
        text("SELECT sha256 FROM research_artifacts WHERE artifact_key = :key"),
        {"key": key},
    ).mappings().first()
    if not row:
        return None
    return row.get("sha256")


def ingest_artifact(
    conn,
    *,
    key: str,
    kind: str,
    payload: dict[str, Any],
    expected_hash: str | None = None,
    transport: str | None = None,
    logical_path: str | None = None,
) -> str:
    validate_artifact(kind, payload)
    provenance = str(payload.get("provenance") or (payload.get("payload") or {}).get("provenance") or "")
    if provenance == "SYNTHETIC_TEST_ONLY":
        raise ArtifactSyncError("SYNTHETIC_TEST_ONLY artifacts cannot be ingested as research evidence")
    sha = verify_hash(payload, expected_hash)
    upsert_artifact(
        conn,
        key=key,
        run_id=str(payload.get("research_run_id") or payload.get("run_id") or ""),
        kind=kind,
        payload=payload,
        sha=sha,
        transport=transport,
        logical_path=logical_path,
    )
    if kind == "training_summary":
        upsert_trials_from_training_summary(conn, payload)
        upsert_features_from_training_summary(conn, payload)
    elif kind == "model_metadata":
        upsert_model_from_metadata(conn, payload)
    elif kind == "oos_diagnostics":
        upsert_signals_from_oos(conn, payload)
    if kind in {"run_manifest", "run_summary"}:
        update_run_metadata(conn, payload)
    platform_schema = str(payload.get("schema_version") or "") in {"platform_v1", "platform_artifact_v1"}
    if kind in PLATFORM_KINDS or platform_schema:
        from qc_research.platform_ingest import ingest_platform_payload

        ingest_platform_payload(conn, kind=kind, payload=payload)
    return sha


def audit_stage2_model_objects(
    engine,
    *,
    strategy_id: str,
    qc_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    store: ObjectStoreClient | None = None,
) -> dict[str, Any]:
    """Properties-only existence audit. Never downloads Object Store content."""
    summary = {"runs": 0, "exists": 0, "missing": 0, "errors": []}
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT model_id, object_store_key, research_run_id, metadata_json
                FROM ml_models
                WHERE research_run_id LIKE :prefix
                """
            ),
            {"prefix": "STAGE2_{0}_%".format(strategy_id)},
        ).mappings().all()
    if not rows:
        return summary
    client = store or ObjectStoreClient(qc_post)
    with engine.begin() as conn:
        for row in rows:
            key = row.get("object_store_key")
            if not key:
                continue
            try:
                props = client.object_properties(str(key))
                exists = bool(props) and props.get("success") is not False
                if exists:
                    summary["exists"] += 1
                else:
                    summary["missing"] += 1
                meta = row.get("metadata_json")
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except ValueError:
                        meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                meta = dict(meta)
                meta["model_object_exists"] = bool(exists)
                conn.execute(
                    text(
                        """
                        UPDATE ml_models
                        SET metadata_json = CAST(:metadata_json AS JSONB)
                        WHERE model_id = :model_id
                        """
                    ),
                    {
                        "model_id": row.get("model_id"),
                        "metadata_json": canonical_dumps(meta),
                    },
                )
            except Exception as exc:
                summary["errors"].append("{0}: {1}".format(key, exc))
                summary["missing"] += 1
        summary["runs"] += 1
    return summary


def sync_stage2_object_store(
    engine,
    *,
    strategy_id: str,
    qc_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    store: ObjectStoreClient | None = None,
) -> dict[str, Any]:
    """Deprecated as an ingest path. Properties-only audit; never calls object_get."""
    return audit_stage2_model_objects(
        engine,
        strategy_id=strategy_id,
        qc_post=qc_post,
        store=store,
    )
