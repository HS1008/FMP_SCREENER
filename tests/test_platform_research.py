"""Platform research: fail-closed gates, migration, monitor labels, ingest."""

from __future__ import annotations

import json

import pytest

from jobs.apply_migrations import MIGRATIONS_DIR, pending_migration_files
from qc_research.economic_gate import apply_economic_gates
from qc_research.ml_aggregation import COMPLETE, FAIL, PASS, assess_stage2
from qc_research.ml_monitor_ui import UNAVAILABLE, format_monitor_value, infer_research_labels
from qc_research.object_store_sync import PLATFORM_KINDS, ingest_artifact, validate_artifact
from qc_research.stage2_results_sync import KIND_BY_FILENAME


def test_fail_closed_unknown_and_reserved_gates():
    unknown = apply_economic_gates({"not_a_real_gate": 1}, {"median_rank_ic": 0.9})
    assert unknown["status"] != PASS
    assert unknown["economic_status"] == FAIL
    reserved = assess_stage2(
        expected=31,
        completed=31,
        thresholds={"cost_stress_robustness": True},
        oos_metrics={"median_rank_ic": 0.2},
    )
    assert reserved["progress"] == COMPLETE
    assert reserved["status"] == COMPLETE
    assert reserved["economic_status"] == FAIL
    assert reserved["status"] != PASS


def test_migration_005_is_additive_and_pending():
    sql = (MIGRATIONS_DIR / "005_platform_research.sql").read_text(encoding="utf-8")
    assert "DROP TABLE" not in sql
    assert "RENAME" not in sql
    assert "CREATE TABLE IF NOT EXISTS strategy_specs" in sql
    assert "CREATE TABLE IF NOT EXISTS research_trials" in sql
    assert "CREATE TABLE IF NOT EXISTS research_pair_diagnostics" in sql
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    pending = pending_migration_files(files, {path.name for path in files if path.name != "005_platform_research.sql"})
    assert [path.name for path in pending] == ["005_platform_research.sql"]
    skipped = pending_migration_files(files, {path.name for path in files})
    assert skipped == []


def test_monitor_labels_and_unavailable_metrics():
    labels = infer_research_labels(strategy_id="CrossSectionalFactorML", run_summary={})
    assert labels["research_mode_label"] == "ML Discovery"
    assert labels["asset_class_label"] == "Equity"
    labels = infer_research_labels(strategy_id="SPYTrend")
    assert labels["research_mode_label"] == "Manual"
    labels = infer_research_labels(strategy_id="ManualSmaQQQ", run_summary={"research_mode": "MANUAL", "asset_class": "ETF", "research_state": "HUMAN_REVIEW_REQUIRED"})
    assert labels["research_mode_label"] == "Manual"
    assert labels["research_state"] == "HUMAN_REVIEW_REQUIRED"
    from qc_research.ml_monitor_ui import build_platform_monitor_view

    view = build_platform_monitor_view(
        strategy_id="ManualSmaQQQ",
        selected_run="PLATFORM_QQQ",
        run_summary={"schema_version": "platform_artifact_v1", "provenance": "UNAVAILABLE", "payload": {"research_mode": "MANUAL", "asset_class": "ETF"}},
        oos={"payload": {"sharpe_ratio": None, "provenance": "UNAVAILABLE", "windows": []}},
    )
    assert view["sharpe"] == UNAVAILABLE
    assert view["trial_count"] == UNAVAILABLE
    assert format_monitor_value(None) == UNAVAILABLE
    assert format_monitor_value(0, available=False) == UNAVAILABLE
    assert format_monitor_value(0.0, provenance="UNAVAILABLE") == UNAVAILABLE
    reconstructed = format_monitor_value(-0.2, reconstructed=True)
    assert reconstructed["source_label"].startswith("monthly-sampled")
    assert "QuantConnect Max Drawdown" in reconstructed["source_label"]


def test_synthetic_artifacts_are_rejected_from_ingest():
    from qc_research.object_store_sync import ArtifactSyncError, ingest_artifact

    class FakeConn:
        def execute(self, statement, params=None):
            raise AssertionError("synthetic ingest must not touch the database")

    payload = {
        "schema_version": "platform_artifact_v1",
        "kind": "trials",
        "provenance": "SYNTHETIC_TEST_ONLY",
        "research_run_id": "FAKE",
        "payload": {"candidates": [], "provenance": "SYNTHETIC_TEST_ONLY"},
    }
    with pytest.raises(ArtifactSyncError, match="SYNTHETIC_TEST_ONLY"):
        ingest_artifact(FakeConn(), key="x", kind="trials", payload=payload)
    class FakeConn:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params=None):
            self.calls.append(params)

    from qc_research.object_store_sync import payload_for_hash, sha256_payload

    payload = {
        "schema_version": "platform_artifact_v1",
        "kind": "pair_diagnostics",
        "research_run_id": "PLATFORM_KOPEP",
        "payload": {
            "pair": ["KO", "PEP"],
            "hedge_ratio_method": "rolling_ols",
            "hedge_ratio": 0.9,
            "correlation": 0.8,
            "half_life": 12.0,
            "selection_used_oos": False,
        },
    }
    payload["artifact_sha256"] = sha256_payload(payload_for_hash(payload))
    validate_artifact("pair_diagnostics", payload)
    conn = FakeConn()
    ingest_artifact(conn, key="p1", kind="pair_diagnostics", payload=payload)
    first = len(conn.calls)
    ingest_artifact(conn, key="p1", kind="pair_diagnostics", payload=payload)
    assert len(conn.calls) == first * 2
    assert KIND_BY_FILENAME["pair_diagnostics.json"] == "pair_diagnostics"
    assert "pair_diagnostics" in PLATFORM_KINDS
