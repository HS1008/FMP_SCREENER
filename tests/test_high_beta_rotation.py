"""HighBetaRotation ingest and read-only monitor view."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from qc_research.contracts.hashing import canonical_dumps, payload_for_hash, sha256_payload
from qc_research.high_beta_rotation_ingest import (
    BLOCKED_RUN_ID,
    IngestError,
    PriorState,
    blocked_transport_bundle,
    ingest_hbr_bundle,
)
from qc_research.high_beta_rotation_monitor import (
    build_hbr_monitor_view,
    format_hbr_metric,
)
from qc_research.read_models.monitor_queries import (
    HBR_STRATEGY_ROWS_SQL,
    PLATFORM_STRATEGY_ROWS_SQL,
)
from qc_research.research_library import _stage_label


ROOT = Path(__file__).resolve().parents[1]
MONITOR = (ROOT / "qc_research" / "high_beta_rotation_monitor.py").read_text(encoding="utf-8")
PAGE = (ROOT / "pages" / "strategy_monitor.py").read_text(encoding="utf-8")


class FakeConn:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params))


def test_monitor_module_is_read_only():
    assert "requests" not in MONITOR
    assert "INSERT" not in MONITOR
    assert "/backtests/create" not in MONITOR
    assert "/backtests/create" not in PAGE
    assert "return False" not in PAGE.split("def strategy_has_platform_research", 1)[1]


def test_discovery_sql_keeps_platform_research_separate():
    assert "research_kind = 'platform_research'" in PLATFORM_STRATEGY_ROWS_SQL
    assert "high_beta_rotation_rule_v1" not in PLATFORM_STRATEGY_ROWS_SQL
    assert "research_kind = 'high_beta_rotation_rule_v1'" in HBR_STRATEGY_ROWS_SQL
    assert _stage_label("high_beta_rotation_rule_v1", "HIGH_BETA_ROTATION") == "High-beta rotation"


def test_blocked_bundle_ingests_once_and_skips_the_same_hash():
    bundle = blocked_transport_bundle("spec-hash")
    first_conn = FakeConn()
    first = ingest_hbr_bundle(first_conn, bundle)
    assert first["research_run_id"] == BLOCKED_RUN_ID
    assert first["written_artifacts"]
    assert first["run_status"] == "BLOCKED_TRANSPORT"
    joined = " ".join(sql for sql, _ in first_conn.calls)
    assert "INSERT INTO research_runs" in joined
    assert "INSERT INTO research_artifacts" in joined
    assert "INSERT INTO strategies" in joined
    assert "ml_trials" not in joined
    kinds = [
        params.get("research_kind")
        for _, params in first_conn.calls
        if params and params.get("research_kind")
    ]
    assert kinds == ["high_beta_rotation_rule_v1"]
    prior = PriorState(
        run_kinds={BLOCKED_RUN_ID: "high_beta_rotation_rule_v1"},
        run_status={BLOCKED_RUN_ID: "BLOCKED_TRANSPORT"},
        artifact_sha={key: key.rsplit("|", 1)[-1] for key in first["written_artifacts"]},
    )
    second_conn = FakeConn()
    second = ingest_hbr_bundle(second_conn, bundle, prior=prior)
    assert second["written_artifacts"] == []
    assert second["skipped_artifacts"]
    second_sql = " ".join(sql for sql, _ in second_conn.calls)
    assert "INSERT INTO research_artifacts" not in second_sql


def test_ingest_refuses_tamper_holdout_and_synthetic_complete_without_writes():
    bundle = blocked_transport_bundle("spec-hash")
    tampered = deepcopy(bundle)
    tampered["artifacts"]["run_summary"]["variants"]["HBR_MAIN"]["cagr"] = 0.25
    conn = FakeConn()
    with pytest.raises(IngestError):
        ingest_hbr_bundle(conn, tampered)
    assert conn.calls == []

    dated = deepcopy(bundle)
    dated["performance_end"] = "2025-01-02"
    conn = FakeConn()
    with pytest.raises(IngestError):
        ingest_hbr_bundle(conn, dated)
    assert conn.calls == []

    accessed = deepcopy(bundle)
    accessed["holdout_accessed"] = True
    conn = FakeConn()
    with pytest.raises(IngestError):
        ingest_hbr_bundle(conn, accessed)
    assert conn.calls == []

    synthetic = deepcopy(bundle)
    synthetic["run_status"] = "COMPLETE"
    synthetic["provenance"] = "SYNTHETIC_TEST_ONLY"
    synthetic["transport_blocked"] = False
    conn = FakeConn()
    with pytest.raises(IngestError):
        ingest_hbr_bundle(conn, synthetic)
    assert conn.calls == []

    incomplete_complete = deepcopy(bundle)
    incomplete_complete["run_status"] = "COMPLETE"
    incomplete_complete["provenance"] = "REAL_QC"
    incomplete_complete["transport_blocked"] = False
    conn = FakeConn()
    with pytest.raises(IngestError):
        ingest_hbr_bundle(conn, incomplete_complete)
    assert conn.calls == []


def test_complete_hash_change_and_stage2_run_are_refused():
    bundle = blocked_transport_bundle("spec-hash")
    stored_key = "HBR_HighBetaRotationV1_BLOCKED_TRANSPORT|run_summary|old"
    prior = PriorState(
        run_kinds={BLOCKED_RUN_ID: "high_beta_rotation_rule_v1"},
        run_status={BLOCKED_RUN_ID: "COMPLETE"},
        artifact_sha={stored_key: "old"},
    )
    conn = FakeConn()
    with pytest.raises(IngestError):
        ingest_hbr_bundle(conn, bundle, prior=prior)
    assert conn.calls == []

    protected = PriorState(
        run_kinds={BLOCKED_RUN_ID: "stage2_ml"},
        run_status={BLOCKED_RUN_ID: "COMPLETE"},
    )
    conn = FakeConn()
    with pytest.raises(IngestError):
        ingest_hbr_bundle(conn, bundle, prior=protected)
    assert conn.calls == []

    stolen = deepcopy(bundle)
    stolen["strategy_id"] = "SPYTrend"
    conn = FakeConn()
    with pytest.raises(IngestError):
        ingest_hbr_bundle(conn, stolen)
    assert conn.calls == []


def test_monitor_keeps_null_distinct_from_zero():
    bundle = blocked_transport_bundle("spec-hash")
    view = build_hbr_monitor_view(
        {
            "strategy_id": "HighBetaRotationV1",
            "research_run_id": bundle["research_run_id"],
            "run_status": "BLOCKED_TRANSPORT",
            "economic_gate": "NOT_DEFINED",
        },
        [
            {"artifact_type": kind, "payload": payload}
            for kind, payload in bundle["artifacts"].items()
        ],
    )
    assert view["historical_label"].startswith("Historical through 2024-12-31")
    assert view["economic_rating"] == "UNRATED"
    assert view["thresholds"] == "THRESHOLDS_NOT_PREDEFINED"
    assert view["launches_backtests"] is False
    assert view["writes_state"] is False
    assert view["metrics"]["cagr"]["value"] is None
    assert view["metrics"]["cagr"]["status"] == "undefined"
    assert format_hbr_metric(view["metrics"]["cagr"]) == "undefined"
    assert view["cost_stress"]["0"]["value"] is None
    assert "2025-01-02" not in json.dumps(view)

    observed = deepcopy(bundle["artifacts"]["run_summary"])
    observed["variants"]["HBR_MAIN"]["cash_shortfall"] = 0
    observed.pop("artifact_sha256", None)
    observed["artifact_sha256"] = sha256_payload(payload_for_hash(observed))
    zero_view = build_hbr_monitor_view(
        {"run_status": "BLOCKED_TRANSPORT"},
        [{"artifact_type": "run_summary", "payload": observed}],
    )
    assert zero_view["metrics"]["cash_shortfall"]["value"] == 0
    assert zero_view["metrics"]["cash_shortfall"]["status"] == "observed"
    assert format_hbr_metric(zero_view["metrics"]["cash_shortfall"]) == "0"
    body = payload_for_hash(bundle["artifacts"]["run_summary"])
    assert canonical_dumps(body) == json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
