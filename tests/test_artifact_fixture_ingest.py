"""Consumer ingest contract for sanitized fixtures."""

from __future__ import annotations

import pytest

from qc_research.contracts.fixtures import CONSUMER_FIXTURES, stage1_run_summary, stage2_run_manifest
from qc_research.contracts.hashing import payload_for_hash, sha256_payload, verify_artifact_sha256
from qc_research.contracts.kinds import PRICE_TECH_V1_FEATURE_ORDER, reject_synthetic_official
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


def test_stage1_fixture_keeps_81_and_no_holdout():
    summary = stage1_run_summary()
    assert summary["expected_experiment_count"] == 81
    assert summary["holdout_accessed"] is False
    assert summary["skipped_experiments"] == []
