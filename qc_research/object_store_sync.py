"""Backend Object Store synchronization for Stage 2 artifacts.

Does not depend on Streamlit. Does not launch backtests. Idempotent.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Iterable

from sqlalchemy import text


logger = logging.getLogger(__name__)

from qc_research.contracts.kinds import (
    KIND_REQUIRED_FIELDS,
    PLATFORM_KINDS,
    PLATFORM_SCHEMA_VERSIONS,
    SCHEMA_VERSION,
    reject_holdout_access,
    reject_synthetic_official,
)
from qc_research.contracts.label_integrity import refuse_impersonated_official_csfml_v1

REQUIRED_RUN_ARTIFACTS = ("run_manifest", "run_summary")


class ArtifactSyncError(ValueError):
    """An Object Store artifact failed validation."""


from qc_research.contracts.hashing import canonical_dumps, payload_for_hash, sha256_payload
from qc_research.ingest.stage2_sql import (
    UPSERT_ARTIFACT_SQL,
    UPSERT_FEATURE_SQL,
    UPSERT_MODEL_SQL,
    UPSERT_SIGNAL_SQL,
    UPSERT_TRIAL_SQL,
    mark_run_incomplete,
    update_run_metadata,
    upsert_artifact,
    upsert_features_from_training_summary,
    upsert_model_from_metadata,
    upsert_signals_from_oos,
    upsert_trials_from_training_summary,
)


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
    try:
        reject_synthetic_official(payload)
        reject_holdout_access(payload)
        refuse_impersonated_official_csfml_v1(payload)
    except ValueError as exc:
        raise ArtifactSyncError(str(exc)) from exc
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
    platform_schema = str(payload.get("schema_version") or "") in {"platform_v1", "platform_artifact_v1"}
    is_platform = kind in PLATFORM_KINDS or platform_schema
    if kind in {"run_manifest", "run_summary"} and not is_platform:
        update_run_metadata(conn, payload)
    if is_platform:
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
