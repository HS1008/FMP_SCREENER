"""Producer and consumer SHA-256 must match when both repos are present."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from qc_research.contracts.fixtures import CONSUMER_FIXTURES
from qc_research.contracts.hashing import canonical_dumps, payload_for_hash, sha256_payload
from qc_research.contracts.producer_fields import required_by_kind


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


def test_fetch_remote_artifact_refuses_empty_ref():
    from qc_research.fetch_remote_artifact import fetch_remote_artifact, github_raw_url

    with pytest.raises(ValueError, match="SOURCE_REF"):
        github_raw_url("hs1008/quant-strategies", "", "research/platform_smokes/x.json")
    with pytest.raises(ValueError, match="SOURCE_REF"):
        fetch_remote_artifact(
            repo="hs1008/quant-strategies",
            path="research/platform_smokes/x.json",
            dest=Path("/tmp/unused.json"),
            ref="",
        )


def test_fetch_remote_artifact_refuses_branch_names_and_short_shas():
    from qc_research.fetch_remote_artifact import github_contents_url, github_raw_url, require_source_ref

    sha = "ef270841621933f5039680cb070559f43bd1e3c8"
    assert require_source_ref(sha) == sha
    assert require_source_ref(sha.upper()) == sha
    assert sha in github_raw_url("hs1008/quant-strategies", sha, "research/platform_smokes/x.json")
    assert "ref={0}".format(sha) in github_contents_url("hs1008/quant-strategies", sha, "research/platform_smokes")
    for floating in ("main", "research-integration", "abc", "csfml-v1-nonholdout-complete", "EF270841"):
        with pytest.raises(ValueError, match="SHA"):
            github_raw_url("hs1008/quant-strategies", floating, "research/platform_smokes/x.json")
        with pytest.raises(ValueError, match="SHA"):
            github_contents_url("hs1008/quant-strategies", floating, "research/platform_smokes")


def test_committed_contract_digests_match_files():
    from qc_research.contracts.digests import verify_contract_digests

    checked = verify_contract_digests()
    assert "producer_required_fields.json" in checked
    assert "csfml_v1_label_integrity.json" in checked
    assert "sealed_results.json" in checked


def test_pinned_producer_required_fields_are_satisfied_by_official_fixtures():
    pinned = required_by_kind()
    assert pinned
    for kind, fields in pinned.items():
        payload = CONSUMER_FIXTURES[kind]()
        missing = [field for field in fields if field not in payload]
        assert missing == [], "{0} missing pinned producer fields: {1}".format(kind, missing)


def test_kind_required_fields_are_subset_of_pinned_producer_fields():
    from qc_research.contracts.kinds import KIND_REQUIRED_FIELDS
    from qc_research.contracts.producer_fields import required_by_kind

    pinned = required_by_kind()
    for kind, fields in KIND_REQUIRED_FIELDS.items():
        assert kind in pinned, kind
        assert set(fields) <= set(pinned[kind]), kind


def test_producer_ref_is_a_full_git_sha():
    from qc_research.contracts.producer_fields import load_producer_required

    payload = load_producer_required()
    ref = str(payload.get("producer_ref") or "")
    assert len(ref) == 40
    assert all(ch in "0123456789abcdef" for ch in ref)


@pytest.mark.skipif(QS_ROOT is None, reason="quant-strategies sibling repo not present")
def test_official_fixtures_include_producer_required_fields():
    sys.path.insert(0, str(QS_ROOT))
    from research.stage2.artifact_contract import REQUIRED_BY_KIND

    from qc_research.contracts.kinds import KIND_REQUIRED_FIELDS
    from qc_research.contracts.producer_fields import required_by_kind as pinned_required

    mapping = {
        "run_manifest": "run_manifest",
        "run_summary": "run_summary",
        "training_summary": "training_summary",
        "model_metadata": "model_metadata",
        "oos_diagnostics": "oos_diagnostics",
        "oos_aggregate": "oos_aggregate",
        "nonholdout_assessment": "nonholdout_assessment",
    }
    producer_snapshot = json.loads(
        (QS_ROOT / "research" / "contracts" / "producer_required_fields.json").read_text(
            encoding="utf-8"
        )
    )
    for kind, producer_kind in mapping.items():
        consumer_required = set(KIND_REQUIRED_FIELDS[kind])
        producer_required = set(REQUIRED_BY_KIND[producer_kind])
        assert set(pinned_required()[kind]) == producer_required, kind
        assert set(producer_snapshot["required_by_kind"][producer_kind]) == producer_required, kind
        assert consumer_required <= producer_required, kind
        payload = CONSUMER_FIXTURES[kind]()
        missing = [field for field in producer_required if field not in payload]
        assert missing == [], "{0} missing producer fields: {1}".format(kind, missing)


def test_csfml_v1_label_integrity_pin_does_not_change_economics():
    from qc_research.contracts.label_integrity import (
        csfml_status_distinction,
        csfml_v1_historical_impact_for_run,
        csfml_v1_integrity_caption,
        load_csfml_v1_label_integrity,
    )

    pin = load_csfml_v1_label_integrity()
    assert pin["historical_v1_impact"] == "CANNOT_RULE_OUT"
    assert pin["impact_status"] == "CANNOT_RULE_OUT"
    assert pin["rerun_decision"] == "UNDETERMINED_HUMAN_GATE"
    assert pin["rerun_authorized"] is False
    assert pin["economic_gate"] == "NOT_DEFINED"
    assert pin["artifact_provenance"] == "VERIFIED"
    assert pin["corrected_engineering"] == "IMPLEMENTED_TESTED"
    assert pin["historical_rerun"] == "NOT_AUTHORIZED_HUMAN_DECISION_PENDING"
    assert pin["promotion"] == "HUMAN_REVIEW_REQUIRED"
    assert pin["holdout"] == "LOCKED"
    assert pin["holdout_accessed"] is False
    caption = csfml_v1_integrity_caption("CrossSectionalFactorML")
    assert caption and "CANNOT_RULE_OUT" in caption
    assert "NOT_DEFINED" in caption
    assert csfml_v1_integrity_caption("SPYTrend") is None
    assert csfml_v1_integrity_caption("TLTDurationMomentum") is None
    assert csfml_v1_integrity_caption("CrossSectionalFactorML", pin["full_suite_run_id"]) == caption
    assert csfml_v1_integrity_caption("CrossSectionalFactorML", "STAGE2_CrossSectionalFactorML_FIXTURE01") is None
    assert (
        csfml_v1_historical_impact_for_run("CrossSectionalFactorML", pin["full_suite_run_id"])
        == "CANNOT_RULE_OUT"
    )
    assert (
        csfml_v1_historical_impact_for_run(
            "CrossSectionalFactorML", "STAGE2_CrossSectionalFactorML_FIXTURE01"
        )
        is None
    )
    assert csfml_v1_historical_impact_for_run("CrossSectionalFactorML", None) is None
    assert csfml_v1_historical_impact_for_run("SPYTrend", pin["full_suite_run_id"]) is None
    assert "PASS" not in caption
    distinction = csfml_status_distinction("CrossSectionalFactorML", pin["full_suite_run_id"])
    assert distinction is not None
    assert distinction["historical_integrity"] == "CANNOT_RULE_OUT"
    assert distinction["economic_approval"] == "NOT_DEFINED"
    assert distinction["artifact_provenance"] == "VERIFIED"
    assert distinction["corrected_engineering"] == "IMPLEMENTED_TESTED"
    assert distinction["historical_rerun"] == "NOT_AUTHORIZED_HUMAN_DECISION_PENDING"
    assert distinction["promotion"] == "HUMAN_REVIEW_REQUIRED"
    assert distinction["holdout"] == "LOCKED"
    assert "does not quantify or clear official V1" in distinction["engineering_completion"]
    assert csfml_status_distinction("SPYTrend") is None
    ui = (
        Path(__file__).resolve().parents[1] / "qc_research" / "ml_monitor_ui.py"
    ).read_text(encoding="utf-8")
    assert "csfml_v1_integrity_caption" in ui
    assert "csfml_status_distinction" in ui
    assert "Artifact provenance:" in ui
    assert "Historical label integrity:" in ui
    assert "Corrected engineering:" in ui
    assert "Historical rerun:" in ui
    assert "economic_gate=NOT_DEFINED is not a failure" in ui


@pytest.mark.skipif(QS_ROOT is None, reason="quant-strategies sibling repo not present")
def test_csfml_v1_label_integrity_pin_matches_producer_forensic():
    from qc_research.contracts.label_integrity import load_csfml_v1_label_integrity

    pin = load_csfml_v1_label_integrity()
    producer_pin = json.loads(
        (QS_ROOT / "research" / "contracts" / "csfml_v1_label_integrity.json").read_text(
            encoding="utf-8"
        )
    )
    forensic = json.loads(
        (QS_ROOT / "research" / "stage2" / "forensic_csfml_v1_official.json").read_text(
            encoding="utf-8"
        )
    )
    state = json.loads((QS_ROOT / "research" / "integration_state.json").read_text(encoding="utf-8"))
    assert pin == producer_pin
    assert forensic["historical_v1_impact"] == pin["historical_v1_impact"]
    assert forensic["rerun_authorized"] is pin["rerun_authorized"]
    assert forensic["safe_when_t21_present"] is pin["safe_when_t21_present"]
    assert state["authoritative_csfml_v1_sha"] == pin["authoritative_csfml_v1_sha"]
    assert state["authoritative_csfml_v1_qc_sha"] == pin["authoritative_csfml_v1_qc_sha"]
    assert state["cross_sectional_factor_ml"]["economic_gate"] == pin["economic_gate"]
    assert state["cross_sectional_factor_ml"]["holdout_accessed"] is pin["holdout_accessed"]
    assert state["cross_sectional_factor_ml"]["label_integrity"]["historical_impact"] == pin[
        "historical_v1_impact"
    ]
    producer_digest = json.loads(
        (QS_ROOT / "research" / "contracts" / "contract_digests.json").read_text(encoding="utf-8")
    )
    consumer_digest = json.loads(
        (Path(__file__).resolve().parents[1] / "qc_research" / "contracts" / "contract_digests.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        producer_digest["files"]["csfml_v1_label_integrity.json"]
        == consumer_digest["files"]["csfml_v1_label_integrity.json"]
    )


def test_official_csfml_v1_published_tree_cannot_prove_zero_delisting_impact():
    from qc_research.contracts.label_integrity import scan_official_csfml_v1_published_tree

    bound = scan_official_csfml_v1_published_tree()
    assert bound["historical_v1_impact"] == "CANNOT_RULE_OUT"
    assert bound["rerun_authorized"] is False
    assert bound["historical_fields_present"] == []
    assert bound["json_files"] > 0


@pytest.mark.skipif(QS_ROOT is None, reason="quant-strategies sibling repo not present")
def test_producer_sealed_run_ids_are_covered_by_consumer():
    sys.path.insert(0, str(QS_ROOT))
    from research.launch_seal import sealed_results_run_ids as producer_ids

    from qc_research.contracts.sealed_results import sealed_results_run_ids

    qs_ids = producer_ids()
    fmp_ids = set(sealed_results_run_ids())
    assert qs_ids <= fmp_ids
