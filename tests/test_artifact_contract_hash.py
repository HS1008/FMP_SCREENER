"""Producer/consumer SHA-256 rule: exclude artifact_sha256."""

from __future__ import annotations

import pytest

from qc_research.contracts.hashing import ArtifactHashError, payload_for_hash, sha256_payload, verify_artifact_sha256
from qc_research.object_store_sync import ArtifactSyncError, verify_hash


def test_artifact_sha_excludes_digest_and_matches_object_store_sync():
    body = {
        "schema_version": "stage2_ml_v1",
        "research_run_id": "STAGE2_TEST",
        "strategy_id": "CrossSectionalFactorML",
        "holdout_accessed": False,
        "economic_gate": "NOT_DEFINED",
    }
    digest = sha256_payload(payload_for_hash(body))
    wrapped = dict(body)
    wrapped["artifact_sha256"] = digest
    assert verify_artifact_sha256(wrapped, digest) == digest
    assert verify_hash(wrapped, digest) == digest
    assert sha256_payload(payload_for_hash(wrapped)) == digest


def test_synthetic_marker_is_not_hashed_away():
    payload = {"schema_version": "stage2_ml_v1", "provenance": "SYNTHETIC_TEST_ONLY"}
    assert "SYNTHETIC_TEST_ONLY" in str(payload_for_hash(payload))


def test_omitted_expected_hash_still_checks_payload_digest():
    body = {
        "schema_version": "stage2_ml_v1",
        "research_run_id": "STAGE2_TEST",
        "holdout_accessed": False,
    }
    digest = sha256_payload(payload_for_hash(body))
    wrapped = dict(body)
    wrapped["artifact_sha256"] = digest
    assert verify_artifact_sha256(wrapped, None) == digest
    assert verify_hash(wrapped, None) == digest
    wrapped["artifact_sha256"] = "0" * 64
    with pytest.raises(ArtifactHashError, match="SHA-256 mismatch"):
        verify_artifact_sha256(wrapped, None)
    with pytest.raises(ArtifactSyncError, match="SHA-256 mismatch"):
        verify_hash(wrapped, None)
    assert verify_hash(body, None) == digest
