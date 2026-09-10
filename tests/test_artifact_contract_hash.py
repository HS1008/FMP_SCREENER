"""Producer/consumer SHA-256 rule: exclude artifact_sha256."""

from __future__ import annotations

from qc_research.contracts.hashing import payload_for_hash, sha256_payload, verify_artifact_sha256
from qc_research.object_store_sync import verify_hash


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
