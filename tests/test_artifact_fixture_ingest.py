"""Consumer ingest contract for sanitized fixtures."""

from __future__ import annotations

import pytest

from qc_research.contracts.fixtures import CONSUMER_FIXTURES, stage1_run_summary, stage2_run_manifest
from qc_research.contracts.hashing import payload_for_hash, sha256_payload, verify_artifact_sha256
from qc_research.contracts.kinds import PRICE_TECH_V1_FEATURE_ORDER, reject_holdout_access, reject_synthetic_official
from qc_research.object_store_sync import ArtifactSyncError, validate_artifact, verify_hash


def test_stage2_fixtures_validate_hash_and_identity():
    for kind, builder in CONSUMER_FIXTURES.items():
        payload = builder()
        assert payload["holdout_accessed"] is False
        assert payload.get("economic_gate", "NOT_DEFINED") in {"NOT_DEFINED", None} or kind == "stage1_run_summary"
        assert payload["provenance"] == "SANITIZED_CONTRACT_FIXTURE"
        assert payload["artifact_sha256"] == sha256_payload(payload_for_hash(payload))
        verify_artifact_sha256(payload, payload["artifact_sha256"])
        if kind in {"stage1_run_summary"}:
            continue
        mapped = "oos_diagnostics" if kind == "baseline_oos_diagnostics" else kind
        if mapped == "strategy_spec":
            validate_artifact(mapped, payload)
            continue
        validate_artifact(mapped, payload)
        verify_hash(payload, payload["artifact_sha256"])


def test_price_tech_order_and_hashes():
    manifest = stage2_run_manifest()
    assert manifest["feature_order"] == list(PRICE_TECH_V1_FEATURE_ORDER)
    assert manifest["feature_set_id"] == "PRICE_TECH_V1"
    assert manifest["feature_set_hash"].startswith("64b6a92c")
    assert manifest["target_id"] == "SECTOR_REL_RANK_21D_V1"


def test_synthetic_official_is_rejected():
    payload = {"provenance": "SYNTHETIC_TEST_ONLY", "schema_version": "stage2_ml_v1"}
    with pytest.raises((ArtifactSyncError, ValueError), match="SYNTHETIC_TEST_ONLY"):
        reject_synthetic_official(payload)


def test_holdout_access_is_rejected_at_ingest():
    payload = {
        "schema_version": "stage2_ml_v1",
        "research_run_id": "STAGE2_X",
        "run_status": "COMPLETE",
        "holdout_accessed": True,
    }
    with pytest.raises((ArtifactSyncError, ValueError), match="holdout_accessed"):
        reject_holdout_access(payload)
    from qc_research.object_store_sync import ingest_artifact

    class _Conn:
        def execute(self, *args, **kwargs):
            raise AssertionError("holdout payload must not reach SQL")

    with pytest.raises(ArtifactSyncError, match="holdout"):
        ingest_artifact(_Conn(), key="bad", kind="run_summary", payload=payload)
    with pytest.raises((ArtifactSyncError, ValueError), match="holdout_status"):
        reject_holdout_access(
            {
                "schema_version": "stage2_ml_v1",
                "research_run_id": "STAGE2_X",
                "run_status": "COMPLETE",
                "holdout_accessed": False,
                "holdout_status": "ACCESSED",
            }
        )


def test_fixtures_ingest_against_disposable_postgres(pg_engine):
    from sqlalchemy import text

    from qc_research.object_store_sync import ingest_artifact

    kind_map = {
        "baseline_oos_diagnostics": "oos_diagnostics",
        "stage1_run_summary": None,
        "strategy_spec": "strategy_spec",
    }
    with pg_engine.begin() as conn:
        for name, builder in CONSUMER_FIXTURES.items():
            kind = kind_map.get(name, name)
            if kind is None:
                continue
            payload = builder()
            ingest_artifact(
                conn,
                key="fixture/{0}".format(name),
                kind=kind,
                payload=payload,
                expected_hash=payload["artifact_sha256"],
            )
        rows = conn.execute(text("SELECT artifact_key, sha256, artifact_type FROM research_artifacts")).mappings().all()
        assert rows
        assert all(row["sha256"] for row in rows)
        models = conn.execute(text("SELECT metadata_json FROM ml_models")).mappings().all()
        for row in models:
            meta = row["metadata_json"] or {}
            if isinstance(meta, str):
                import json

                meta = json.loads(meta)
            assert meta.get("binary_published") is not True
        lifecycle = conn.execute(
            text(
                """
                SELECT promotion_gate, delivery_status, economic_gate, holdout_status, holdout_accessed
                FROM research_runs
                WHERE research_kind = 'stage2_ml'
                """
            )
        ).mappings().first()
        assert lifecycle is not None
        assert lifecycle["economic_gate"] == "NOT_DEFINED"
        assert lifecycle["promotion_gate"] == "HUMAN_REVIEW_REQUIRED"
        assert lifecycle["holdout_status"] == "LOCKED"
        assert lifecycle["holdout_accessed"] is False


def test_stage1_fixture_keeps_81_and_no_holdout():
    summary = stage1_run_summary()
    assert summary["expected_experiment_count"] == 81
    assert summary["holdout_accessed"] is False
    assert summary["skipped_experiments"] == []
