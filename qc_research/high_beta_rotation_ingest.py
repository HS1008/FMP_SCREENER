"""Idempotent HighBetaRotation research ingest.

Writes research_runs, research_artifacts, and a research-only strategies row.
Does not write ml_trials, launch QuantConnect, or overwrite Stage 1, Stage 2,
or platform research rows.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from sqlalchemy import text

from qc_research.contracts.hashing import (
    ArtifactHashError,
    payload_for_hash,
    verify_artifact_sha256,
)
from qc_research.contracts.kinds import (
    ArtifactContractError,
    reject_holdout_access,
    reject_synthetic_official,
)

RESEARCH_KIND = "high_beta_rotation_rule_v1"
STRATEGY_ID = "HighBetaRotationV1"
RESEARCH_LINEAGE_ID = "HighBetaRotationV1"
RESEARCH_PROJECT = "HighBetaRotationV1Research"
SCHEMA_VERSION = "high_beta_rotation_v1"
PERFORMANCE_START = "2010-01-01"
PERFORMANCE_END = "2024-12-31"
HOLDOUT_START = "2025-01-01"
BLOCKED_RUN_ID = "HBR_HighBetaRotationV1_BLOCKED_TRANSPORT"

COMPLETE_STATUSES = {"COMPLETE", "RESEARCH_COMPLETE", "NON_HOLDOUT_COMPLETE"}
PROTECTED_KINDS = {
    "stage1",
    "stage1_research",
    "stage2",
    "stage2_ml",
    "platform_research",
}
PROTECTED_STRATEGY_IDS = {"SPYTrend", "CrossSectionalFactorML", "TLTDurationMomentum"}
REQUIRED_COMPLETE_KINDS = (
    "strategy_spec",
    "run_manifest",
    "run_summary",
    "selection_ledger",
    "event_ledger",
    "fill_ledger",
    "risk_diagnostics",
    "rotation_diagnostics",
    "signal_health",
    "benchmark_diagnostics",
    "annual_results",
    "cost_stress",
    "assessment",
)
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_HOLDOUT_KEYS = {"holdout_start", "holdout_end"}


class IngestError(ValueError):
    """HighBetaRotation bundle cannot be written."""


@dataclass
class PriorState:
    """Rows already stored. Tests pass this directly. Production loaders can too."""

    run_kinds: dict[str, str] = field(default_factory=dict)
    run_status: dict[str, str] = field(default_factory=dict)
    artifact_sha: dict[str, str] = field(default_factory=dict)


def _finite(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise IngestError("non-finite value at {0}".format(path or "<root>"))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _finite(item, "{0}[{1}]".format(path, index))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            child = "{0}.{1}".format(path, key) if path else str(key)
            _finite(item, child)
        return
    raise IngestError("unsupported type {0} at {1}".format(type(value).__name__, path or "<root>"))


_METADATA_DATE_KEYS = {
    "generated_at",
    "created_at",
    "synced_at",
    "updated_at",
    "first_seen_at",
    "last_seen_at",
    "ingested_at",
    "commit_timestamp",
    "artifact_created_at",
}
_OBSERVATION_DATE_KEYS = {
    "performance_start",
    "performance_end",
    "session",
    "sessions",
    "start",
    "end",
    "label_end",
    "signal_session",
    "trade_session",
    "history_start",
    "history_end",
    "backtest_start",
    "backtest_end",
    "as_of",
    "ex_date",
    "delist_session",
    "window_start",
    "window_end",
}


def _observation_key(key: str) -> bool:
    if not key or key in _METADATA_DATE_KEYS or key in _HOLDOUT_KEYS:
        return False
    if key in _OBSERVATION_DATE_KEYS:
        return True
    return key.endswith("_session") or key.endswith("_date")


def _reject_dates(value: Any, path: str, key: str = "") -> None:
    """Reject sealed market dates. Leave ingestion and commit timestamps alone."""
    if key in _METADATA_DATE_KEYS:
        return
    if key in _HOLDOUT_KEYS and value == HOLDOUT_START:
        return
    observe = _observation_key(key)
    if isinstance(value, str):
        if not observe:
            return
        for found in _ISO_DATE.findall(value):
            if found >= HOLDOUT_START:
                raise IngestError(
                    "market or research date {0} at {1} is inside the sealed window".format(
                        found, path or key or "<root>"
                    )
                )
        return
    if isinstance(value, list):
        child_key = "session" if key == "sessions" else (key if observe else "")
        for index, item in enumerate(value):
            _reject_dates(item, "{0}[{1}]".format(path, index), child_key)
        return
    if isinstance(value, dict):
        for child_key, item in value.items():
            child = "{0}.{1}".format(path, child_key) if path else str(child_key)
            _reject_dates(item, child, str(child_key))


def _artifact_key(run_id: str, artifact_type: str, digest: str) -> str:
    return "{0}|{1}|{2}".format(run_id, artifact_type, digest)


def _seal_body(artifact_type: str, body: dict[str, Any]) -> dict[str, Any]:
    payload = dict(body)
    payload["schema_version"] = SCHEMA_VERSION
    payload["artifact_type"] = artifact_type
    payload.pop("artifact_sha256", None)
    digest = verify_artifact_sha256(payload, None)
    payload["artifact_sha256"] = digest
    return payload


def blocked_transport_bundle(spec_hash: str) -> dict[str, Any]:
    """Incomplete run with null metrics. This is not official 2010-2024 performance."""
    run_id = BLOCKED_RUN_ID
    summary = _seal_body(
        "run_summary",
        {
            "research_run_id": run_id,
            "strategy_id": STRATEGY_ID,
            "research_lineage_id": RESEARCH_LINEAGE_ID,
            "research_kind": RESEARCH_KIND,
            "run_status": "BLOCKED_TRANSPORT",
            "provenance": "UNAVAILABLE",
            "performance_start": PERFORMANCE_START,
            "performance_end": PERFORMANCE_END,
            "holdout_start": HOLDOUT_START,
            "holdout_accessed": False,
            "economic_gate": "NOT_DEFINED",
            "economic_rating": "UNRATED",
            "variants": {
                "HBR_MAIN": {
                    "cagr": None,
                    "cagr_reason": "official evidence was not produced because ledger transport is blocked",
                    "max_drawdown": None,
                    "sortino": None,
                    "calmar": None,
                    "ex_ante_beta": None,
                    "turnover": None,
                    "cash_shortfall": None,
                    "constraint_shortfall": None,
                }
            },
            "beta_control_comparison": None,
            "cost_stress_bps": {"0": None, "10": None, "20": None},
            "yearly_returns": None,
            "rolling_beta": None,
            "exposures": None,
            "rotation_efficacy": None,
            "signal_health": None,
        },
    )
    assessment = _seal_body(
        "assessment",
        {
            "research_run_id": run_id,
            "strategy_id": STRATEGY_ID,
            "economic_rating": "UNRATED",
            "thresholds": "THRESHOLDS_NOT_PREDEFINED",
            "research_validity": "NOT_CERTIFIED",
            "evidence_completeness": "INCOMPLETE",
            "qc_execution": "BLOCKED_TRANSPORT",
        },
    )
    return {
        "research_run_id": run_id,
        "strategy_id": STRATEGY_ID,
        "research_lineage_id": RESEARCH_LINEAGE_ID,
        "research_kind": RESEARCH_KIND,
        "research_mode": "HIGH_BETA_ROTATION",
        "asset_class": "US_EQUITY",
        "strategy_family_id": "HIGH_BETA_ROTATION",
        "strategy_spec_hash": spec_hash,
        "run_status": "BLOCKED_TRANSPORT",
        "promotion_gate": "HUMAN_REVIEW_REQUIRED",
        "holdout_status": "LOCKED",
        "economic_gate": "NOT_DEFINED",
        "delivery_status": "NOT_DEPLOYED",
        "holdout_accessed": False,
        "provenance": "UNAVAILABLE",
        "performance_start": PERFORMANCE_START,
        "performance_end": PERFORMANCE_END,
        "holdout_start": HOLDOUT_START,
        "transport_blocked": True,
        "expected_experiment_count": 4,
        "completed_count": 0,
        "artifacts": {
            "run_summary": summary,
            "assessment": assessment,
        },
    }


def _stored_type_sha(prior: PriorState, run_id: str, artifact_type: str) -> dict[str, str]:
    prefix = "{0}|{1}|".format(run_id, artifact_type)
    return {
        key: digest
        for key, digest in prior.artifact_sha.items()
        if key.startswith(prefix)
    }


def validate_bundle(bundle: Mapping[str, Any], prior: PriorState | None = None) -> None:
    """Reject a bundle before any database write."""
    record = dict(bundle)
    state = prior or PriorState()
    _finite(record, "")
    _reject_dates(record, "")
    try:
        reject_holdout_access(record)
        reject_synthetic_official(record)
    except ArtifactContractError as exc:
        raise IngestError(str(exc)) from exc
    strategy_id = str(record.get("strategy_id") or "")
    if strategy_id in PROTECTED_STRATEGY_IDS:
        raise IngestError("refusing to write a Stage 1, Stage 2, or platform strategy id")
    if strategy_id != STRATEGY_ID:
        raise IngestError("strategy_id must be HighBetaRotationV1")
    if str(record.get("research_kind") or "") != RESEARCH_KIND:
        raise IngestError("research_kind must be high_beta_rotation_rule_v1")
    if str(record.get("research_lineage_id") or "") != RESEARCH_LINEAGE_ID:
        raise IngestError("research_lineage_id must be HighBetaRotationV1")
    run_id = str(record.get("research_run_id") or "")
    if not run_id:
        raise IngestError("research_run_id is required")
    prior_kind = state.run_kinds.get(run_id)
    if prior_kind and prior_kind != RESEARCH_KIND:
        raise IngestError("run {0} already belongs to {1}".format(run_id, prior_kind))
    if prior_kind in PROTECTED_KINDS:
        raise IngestError("refusing to overwrite protected research kind {0}".format(prior_kind))
    status = str(record.get("run_status") or "")
    provenance = str(record.get("provenance") or "")
    artifacts = record.get("artifacts") or {}
    if not isinstance(artifacts, dict) or not artifacts:
        raise IngestError("at least one artifact is required")
    if status in COMPLETE_STATUSES:
        if record.get("transport_blocked") is True:
            raise IngestError("blocked transport cannot be marked COMPLETE")
        if provenance != "REAL_QC":
            raise IngestError("COMPLETE requires REAL_QC provenance")
        missing = [kind for kind in REQUIRED_COMPLETE_KINDS if kind not in artifacts]
        if missing:
            raise IngestError("COMPLETE is missing {0}".format(", ".join(missing)))
    for artifact_type, payload in artifacts.items():
        if not isinstance(payload, dict):
            raise IngestError("artifact {0} must be an object".format(artifact_type))
        try:
            reject_holdout_access(payload)
            reject_synthetic_official(payload)
        except ArtifactContractError as exc:
            raise IngestError(str(exc)) from exc
        _finite(payload, artifact_type)
        _reject_dates(payload, artifact_type)
        claimed = str(payload.get("artifact_sha256") or "")
        if not claimed:
            raise IngestError("artifact {0} is missing artifact_sha256".format(artifact_type))
        try:
            digest = verify_artifact_sha256(payload_for_hash(payload), claimed)
        except ArtifactHashError as exc:
            raise IngestError(str(exc)) from exc
        stored = _stored_type_sha(state, run_id, str(artifact_type))
        key = _artifact_key(run_id, str(artifact_type), digest)
        if stored and key not in stored:
            prior_status = state.run_status.get(run_id, "")
            if prior_status in COMPLETE_STATUSES or status in COMPLETE_STATUSES:
                raise IngestError(
                    "refusing to replace {0} on a COMPLETE run".format(artifact_type)
                )


UPSERT_RUN_SQL = """
INSERT INTO research_runs (
    research_run_id,
    strategy_id,
    research_kind,
    research_mode,
    asset_class,
    strategy_family_id,
    strategy_spec_hash,
    research_lineage_id,
    run_status,
    promotion_gate,
    holdout_status,
    economic_gate,
    delivery_status,
    holdout_accessed,
    holdout_access_count,
    holdout_start,
    expected_experiment_count,
    completed_count,
    failed_count
) VALUES (
    :research_run_id,
    :strategy_id,
    :research_kind,
    :research_mode,
    :asset_class,
    :strategy_family_id,
    :strategy_spec_hash,
    :research_lineage_id,
    :run_status,
    :promotion_gate,
    :holdout_status,
    :economic_gate,
    :delivery_status,
    FALSE,
    0,
    CAST(:holdout_start AS DATE),
    :expected_experiment_count,
    :completed_count,
    0
)
ON CONFLICT (research_run_id) DO UPDATE SET
    last_seen_at = NOW(),
    run_status = CASE
        WHEN research_runs.run_status IN ('COMPLETE', 'RESEARCH_COMPLETE', 'NON_HOLDOUT_COMPLETE')
            THEN research_runs.run_status
        ELSE EXCLUDED.run_status
    END,
    research_kind = CASE
        WHEN research_runs.research_kind IS NULL OR research_runs.research_kind = ''
            THEN EXCLUDED.research_kind
        WHEN research_runs.research_kind = EXCLUDED.research_kind
            THEN research_runs.research_kind
        ELSE research_runs.research_kind
    END
"""

INSERT_ARTIFACT_SQL = """
INSERT INTO research_artifacts (
    artifact_key, research_run_id, research_experiment_id, artifact_type,
    sha256, payload_json, created_at, synced_at, transport, logical_path
) VALUES (
    :artifact_key, :research_run_id, NULL, :artifact_type,
    :sha256, CAST(:payload_json AS JSONB), NOW(), NOW(), :transport, :logical_path
)
ON CONFLICT (artifact_key) DO NOTHING
"""

REGISTER_STRATEGY_SQL = """
INSERT INTO strategies (
    strategy_id, name, environment, status,
    qc_research_project_name
) VALUES (
    :strategy_id, :name, 'research', :status,
    :qc_research_project_name
)
ON CONFLICT (strategy_id) DO UPDATE SET
    name = EXCLUDED.name,
    environment = CASE
        WHEN strategies.environment IN ('paper', 'live', 'trading') THEN strategies.environment
        ELSE EXCLUDED.environment
    END,
    status = EXCLUDED.status,
    qc_research_project_name = COALESCE(
        EXCLUDED.qc_research_project_name, strategies.qc_research_project_name
    )
"""


SELECT_RUN_LOCK_SQL = """
SELECT research_kind, run_status
FROM research_runs
WHERE research_run_id = :research_run_id
FOR UPDATE
"""

SELECT_ARTIFACT_LOCK_SQL = """
SELECT artifact_key, artifact_type, sha256
FROM research_artifacts
WHERE research_run_id = :research_run_id
FOR UPDATE
"""


def _mapping_rows(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    mappings = getattr(result, "mappings", None)
    if mappings is None:
        return []
    return [dict(row) for row in mappings()]


def load_prior_locked(conn: Any, run_id: str) -> PriorState:
    """Read and lock the existing run before deciding whether a write is new."""
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:research_run_id)::bigint)"),
        {"research_run_id": run_id},
    )
    run_rows = _mapping_rows(conn.execute(text(SELECT_RUN_LOCK_SQL), {"research_run_id": run_id}))
    artifact_rows = _mapping_rows(
        conn.execute(text(SELECT_ARTIFACT_LOCK_SQL), {"research_run_id": run_id})
    )
    state = PriorState()
    if run_rows:
        state.run_kinds[run_id] = str(run_rows[0].get("research_kind") or "")
        state.run_status[run_id] = str(run_rows[0].get("run_status") or "")
    for row in artifact_rows:
        key = str(row.get("artifact_key") or "")
        if key:
            state.artifact_sha[key] = str(row.get("sha256") or "")
    return state


def ingest_hbr_bundle(conn: Any, bundle: Mapping[str, Any], *, prior: PriorState | None = None) -> dict[str, Any]:
    """Validate, then write. Production locks the run when prior is omitted."""
    if prior is not None:
        return _write_bundle(conn, bundle, prior)
    validate_bundle(bundle, PriorState())
    begin = getattr(conn, "begin", None)
    if begin is None:
        raise IngestError("production ingest requires a transactional connection that can lock existing rows")
    with conn.begin():
        state = load_prior_locked(conn, str(bundle.get("research_run_id") or ""))
        return _write_bundle(conn, bundle, state)


def _write_bundle(conn: Any, bundle: Mapping[str, Any], state: PriorState) -> dict[str, Any]:
    validate_bundle(bundle, state)
    record = dict(bundle)
    run_id = str(record["research_run_id"])
    conn.execute(
        text(UPSERT_RUN_SQL),
        {
            "research_run_id": run_id,
            "strategy_id": STRATEGY_ID,
            "research_kind": RESEARCH_KIND,
            "research_mode": record.get("research_mode") or "HIGH_BETA_ROTATION",
            "asset_class": record.get("asset_class") or "US_EQUITY",
            "strategy_family_id": record.get("strategy_family_id") or "HIGH_BETA_ROTATION",
            "strategy_spec_hash": record.get("strategy_spec_hash"),
            "research_lineage_id": RESEARCH_LINEAGE_ID,
            "run_status": record.get("run_status"),
            "promotion_gate": record.get("promotion_gate") or "HUMAN_REVIEW_REQUIRED",
            "holdout_status": record.get("holdout_status") or "LOCKED",
            "economic_gate": "NOT_DEFINED",
            "delivery_status": record.get("delivery_status") or "NOT_DEPLOYED",
            "holdout_start": HOLDOUT_START,
            "expected_experiment_count": record.get("expected_experiment_count") or 4,
            "completed_count": record.get("completed_count") or 0,
        },
    )
    written = []
    skipped = []
    for artifact_type, payload in record["artifacts"].items():
        digest = verify_artifact_sha256(payload_for_hash(payload), payload.get("artifact_sha256"))
        key = _artifact_key(run_id, str(artifact_type), digest)
        if key in state.artifact_sha:
            skipped.append(key)
            continue
        conn.execute(
            text(INSERT_ARTIFACT_SQL),
            {
                "artifact_key": key,
                "research_run_id": run_id,
                "artifact_type": artifact_type,
                "sha256": digest,
                "payload_json": json.dumps(payload, sort_keys=True),
                "transport": "canonical_json",
                "logical_path": "high_beta_rotation/{0}/{1}.json".format(run_id, artifact_type),
            },
        )
        written.append(key)
    conn.execute(
        text(REGISTER_STRATEGY_SQL),
        {
            "strategy_id": STRATEGY_ID,
            "name": "High-beta rotation",
            "status": record.get("run_status") or "INCOMPLETE",
            "qc_research_project_name": RESEARCH_PROJECT,
        },
    )
    return {
        "research_run_id": run_id,
        "written_artifacts": written,
        "skipped_artifacts": skipped,
        "run_status": record.get("run_status"),
    }
