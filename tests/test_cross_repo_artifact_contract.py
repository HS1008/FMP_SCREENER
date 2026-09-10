"""Producer and consumer SHA-256 must match when both repos are present."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from qc_research.contracts.fixtures import CONSUMER_FIXTURES
from qc_research.contracts.hashing import canonical_dumps, payload_for_hash, sha256_payload


def _quant_strategies_root() -> Path | None:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "quant-strategies",
        Path("/agent/repos/quant-strategies"),
        (here.parents[1].parent / "quant-strategies"),
    ]
    for path in candidates:
        if (path / "research" / "artifact_fixtures.py").is_file():
            return path
    return None


QS_ROOT = _quant_strategies_root()


@pytest.mark.skipif(QS_ROOT is None, reason="quant-strategies sibling repo not present")
def test_hashing_implementations_agree():
    sys.path.insert(0, str(QS_ROOT))
    from research.stage2.artifact_contract import canonical_dumps as producer_dumps
    from research.stage2.artifact_contract import sha256_payload as producer_sha

    payload = {
        "artifact_sha256": "ignore-me",
        "note": "café",
        "schema_version": "stage2_ml_v1",
        "holdout_accessed": False,
        "economic_gate": "NOT_DEFINED",
    }
    body = payload_for_hash(payload)
    assert canonical_dumps(body) == producer_dumps(body)
    assert sha256_payload(body) == producer_sha(body)


@pytest.mark.skipif(QS_ROOT is None, reason="quant-strategies sibling repo not present")
def test_sanitized_fixtures_share_sha256():
    sys.path.insert(0, str(QS_ROOT))
    from research.artifact_fixtures import FIXTURE_BUILDERS

    mapping = {
        "stage1_run_summary": "stage1_run_summary",
        "run_manifest": "stage2_run_manifest",
        "run_summary": "stage2_run_summary",
        "training_summary": "stage2_training_summary",
        "model_metadata": "stage2_model_metadata",
        "oos_diagnostics": "stage2_oos_diagnostics",
        "baseline_oos_diagnostics": "stage2_baseline_oos_diagnostics",
        "oos_aggregate": "stage2_oos_aggregate",
        "nonholdout_assessment": "stage2_nonholdout_assessment",
        "strategy_spec": "platform_strategy_spec",
    }
    for consumer_name, producer_name in mapping.items():
        consumer = CONSUMER_FIXTURES[consumer_name]()
        producer = FIXTURE_BUILDERS[producer_name][1]()
        assert consumer["artifact_sha256"] == producer["artifact_sha256"], consumer_name
        assert consumer["holdout_accessed"] is False
        assert producer["holdout_accessed"] is False
        assert consumer.get("economic_gate", "NOT_DEFINED") in {None, "NOT_DEFINED"} or consumer_name == "stage1_run_summary"


@pytest.mark.skipif(QS_ROOT is None, reason="quant-strategies sibling repo not present")
def test_official_fixtures_include_producer_required_fields():
    sys.path.insert(0, str(QS_ROOT))
    from research.stage2.artifact_contract import REQUIRED_BY_KIND

    from qc_research.contracts.kinds import KIND_REQUIRED_FIELDS

    mapping = {
        "run_manifest": "run_manifest",
        "run_summary": "run_summary",
        "training_summary": "training_summary",
        "model_metadata": "model_metadata",
        "oos_diagnostics": "oos_diagnostics",
        "oos_aggregate": "oos_aggregate",
        "nonholdout_assessment": "nonholdout_assessment",
    }
    for kind, producer_kind in mapping.items():
        consumer_required = set(KIND_REQUIRED_FIELDS[kind])
        producer_required = set(REQUIRED_BY_KIND[producer_kind])
        assert consumer_required <= producer_required, kind
        payload = CONSUMER_FIXTURES[kind]()
        missing = [field for field in producer_required if field not in payload]
        assert missing == [], "{0} missing producer fields: {1}".format(kind, missing)
