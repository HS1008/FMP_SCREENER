"""Idea registry: versioning, hash-bound approval, dry-run-only queue, boundary validation, linking."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from market_intelligence import ideas
from market_intelligence.nulls import canonical_sha256

# A COMPLETE spec: every APPROVAL_REQUIRED_FIELDS decision was made by a human. No thresholds.
SPEC = {
    "title": "Sector RS breadth precedes 1M sector leadership",
    "research_type": "SECTOR_ROTATION_DIAGNOSTIC",
    "hypothesis": "Sectors whose 1W RS change turns positive while 3M RS is negative lead over the next month.",
    "economic_rationale": "Leadership mean-reversion after a long RS drawdown as flows rebalance toward laggards.",
    "universe": {"sectors": "canonical_sectors_v1", "benchmark": "SPY"},
    "signal": {"rs_chg_1w": ">0", "rs_chg_3m": "<0", "short_sessions": 5, "long_sessions": 63, "forward_sessions": 21},
    "signal_timing": {"decision": "close of session t using data through t", "first_effect": "session t+1 open"},
    "expected_behavior": "Positive forward RS spread for signal sectors vs others; rank IC > 0 on average.",
    "invalidation": "Spread <= 0 or unstable sign across sub-periods; no economic threshold is defined here.",
    "horizon": "21 trading days",
    "costs": {"model": "none (descriptive diagnostic, no trading)", "note": "any later strategy uses platform base/stress costs"},
    "validation_protocol": {"method": "descriptive diagnostic with overlapping windows disclosed", "sub_periods": "yearly"},
    "data_requirements": {"pit_required": False, "note": "ETF closes are point-in-time by construction", "history_start": "2015-01-02", "history_end": "2024-12-31"},
    "holdout_policy": {"holdout_start": "2025-01-01", "access": "NONE"},
    "data_used": [{"source_id": "FMP_LEGACY", "dataset": "ETF_RS_VS_SPY"}, {"source_id": "FRED", "series": ["DGS10", "DGS2"]}],
    "params": {"start": "2015-01-02", "end": "2024-12-31"},
}

# A registrable DRAFT that is not yet complete (no rationale/timing/costs/validation/...).
DRAFT_SPEC = {k: SPEC[k] for k in ("title", "research_type", "hypothesis", "universe", "signal", "horizon", "data_used", "params")}


def _register(conn, spec=SPEC, **kw):
    return ideas.register_idea(conn, spec, actor=kw.pop("actor", "alice"), **kw)


def test_validate_spec_hash_is_deterministic_and_boundary_enforced():
    a = ideas.validate_spec(SPEC)
    b = ideas.validate_spec(json.loads(json.dumps(SPEC)))
    assert a.ok and a.spec_hash == b.spec_hash and a.execution_support == ideas.SUPPORT_DRY_RUN_CONTRACT
    assert a.completeness == ideas.COMPLETENESS_COMPLETE and a.missing_fields == []
    assert a.economic_gate == ideas.ECONOMIC_GATE_NOT_DEFINED and any("never invents thresholds" in w for w in a.warnings)
    assert a.normalized["schema_version"] == "idea_spec_v2"
    assert a.normalized["holdout_policy"]["access"] == "NONE" and a.normalized["holdout_policy"]["holdout_start"] == "2025-01-01"
    assert {r["path"] for r in a.date_ranges} == {"params.start/end", "data_requirements.history_start/history_end"}
    bad = ideas.validate_spec(dict(SPEC, params={"start": "2015-01-02", "end": "2025-06-30"}))
    assert not bad.ok and any("protected window" in e for e in bad.errors) and any("effective holdout boundary" in e for e in bad.errors)
    bad2 = ideas.validate_spec(dict(SPEC, holdout_policy={"access": "READ"}))
    assert not bad2.ok and any("holdout_policy.access" in e for e in bad2.errors)
    missing = ideas.validate_spec({"title": "x"})
    assert not missing.ok and any("research_type" in e for e in missing.errors)
    unsupported = ideas.validate_spec(dict(SPEC, research_type="CREDIT_SPREAD_SIGNAL"))
    assert unsupported.ok and unsupported.execution_support == ideas.SUPPORT_UNSUPPORTED and unsupported.warnings
    unknown = ideas.validate_spec(dict(SPEC, research_type="MAGIC"))
    assert not unknown.ok


def test_draft_spec_registers_but_is_incomplete_until_every_decision_is_made():
    v = ideas.validate_spec(DRAFT_SPEC)
    assert v.ok and v.completeness == ideas.COMPLETENESS_INCOMPLETE
    assert v.missing_fields == ["economic_rationale", "signal_timing", "expected_behavior", "invalidation", "costs", "validation_protocol", "data_requirements", "holdout_policy"]
    # Adding a threshold does not make the spec complete; leaving it out never invents one.
    with_thresholds = ideas.validate_spec(dict(SPEC, acceptance_thresholds={"min_rank_ic": 0.02, "decided_by": "pm"}))
    assert with_thresholds.ok and with_thresholds.economic_gate == ideas.ECONOMIC_GATE_HUMAN_SUPPLIED
    bad = ideas.validate_spec(dict(SPEC, acceptance_thresholds="0.02"))
    assert not bad.ok and any("acceptance_thresholds" in e for e in bad.errors)
    bad_pit = ideas.validate_spec(dict(SPEC, data_requirements={"note": "?"}))
    assert not bad_pit.ok and any("pit_required" in e for e in bad_pit.errors)


def test_stricter_idea_holdout_is_kept_not_loosened_to_platform_cutoff():
    strict = dict(SPEC, holdout_policy={"holdout_start": "2023-01-01", "access": "NONE"}, params={"start": "2015-01-02", "end": "2022-12-30"}, data_requirements=dict(SPEC["data_requirements"], history_end="2022-12-30"))
    v = ideas.validate_spec(strict)
    assert v.ok and v.effective_holdout_start == date(2023, 1, 1)
    assert v.normalized["holdout_policy"]["holdout_start"] == "2023-01-01"
    # Data inside the platform window but on/after the idea's own boundary is refused.
    leak = ideas.validate_spec(dict(strict, params={"start": "2015-01-02", "end": "2023-06-30"}))
    assert not leak.ok and any("2023-01-01" in e for e in leak.errors)
    later = ideas.validate_spec(dict(SPEC, holdout_policy={"holdout_start": "2026-01-01", "access": "NONE"}))
    assert not later.ok and any("later than the platform boundary" in e for e in later.errors)


def test_structured_date_ranges_are_validated_not_token_matched():
    inverted = ideas.validate_spec(dict(SPEC, params={"start": "2024-12-31", "end": "2015-01-02"}))
    assert not inverted.ok and any("precedes start" in e for e in inverted.errors)
    half = ideas.validate_spec(dict(SPEC, params={"start": "2015-01-02"}))
    assert not half.ok and any("both start and end are required" in e for e in half.errors)
    garbage = ideas.validate_spec(dict(SPEC, params={"start": "2015-13-45", "end": "2024-12-31"}))
    assert not garbage.ok and any("not an ISO calendar date" in e or "invalid calendar date" in e for e in garbage.errors)
    nested = ideas.validate_spec(dict(SPEC, validation_protocol={"method": "wfo", "folds": [{"train_start": "2015-01-02", "train_end": "2018-12-31", "oos_start": "2019-01-02", "oos_end": "2025-01-02"}]}))
    assert not nested.ok and any("oos_start/oos_end" in e and "2025-01-02" in e for e in nested.errors)
    # Token warnings remain advisory only.
    tokens = ideas.validate_spec(dict(SPEC, signal=dict(SPEC["signal"], note="ignore holdout")))
    assert tokens.ok and any("holdout/2025+ tokens" in w for w in tokens.warnings)


def test_execution_support_reflects_implemented_adapters_only():
    assert ideas.RESEARCH_TYPES["SECTOR_ROTATION_DIAGNOSTIC"]["support"] == ideas.SUPPORT_DRY_RUN_CONTRACT
    assert ideas.RESEARCH_TYPES["SECTOR_INTERNALS_PIT"]["support"] == ideas.SUPPORT_DRY_RUN_CONTRACT
    # Existing engines without an idea->spec adapter are MANUAL_SPEC_REQUIRED, never "supported".
    for rtype in ("SINGLE_ASSET_MOMENTUM", "CROSS_SECTIONAL_FACTOR_ML"):
        assert ideas.RESEARCH_TYPES[rtype]["support"] == ideas.SUPPORT_MANUAL_SPEC_REQUIRED
        assert "no idea->StrategySpecV1 adapter" in ideas.RESEARCH_TYPES[rtype]["adapter"]
    for rtype in ("EVENT_STUDY", "CONDITIONAL_RETURN", "CREDIT_SPREAD_SIGNAL", "OTHER"):
        assert ideas.RESEARCH_TYPES[rtype]["support"] == ideas.SUPPORT_UNSUPPORTED
    assert not hasattr(ideas, "SUPPORT_EXISTING_INFRA")


def test_full_dry_run_lifecycle_with_hash_bound_approval(mi_db):
    with mi_db.begin() as conn:
        reg = _register(conn)
        idea_id, h1 = reg["idea_id"], reg["spec_hash"]
        assert reg["state"] == ideas.DRAFT and reg["version"] == 1
        # Cannot approve a DRAFT, cannot queue without approval.
        with pytest.raises(ideas.IdeaError, match="SPEC_FROZEN"):
            ideas.approve_research(conn, idea_id, approved_by="bob", version=1, spec_hash=h1)
        ideas.freeze_spec(conn, idea_id, actor="alice")
        with pytest.raises(ideas.IdeaError, match="spec_hash does not match"):
            ideas.approve_research(conn, idea_id, approved_by="bob", version=1, spec_hash="f" * 64)
        with pytest.raises(ideas.IdeaError, match="version"):
            ideas.approve_research(conn, idea_id, approved_by="bob", version=2, spec_hash=h1)
        approved = ideas.approve_research(conn, idea_id, approved_by="bob", version=1, spec_hash=h1)
        assert approved["state"] == ideas.RESEARCH_APPROVED
        # Real execution is refused; dry-run records a contract.
        with pytest.raises(ideas.ExecutionBlocked):
            ideas.queue_research(conn, idea_id, actor="bob", dry_run=False)
        queued = ideas.queue_research(conn, idea_id, actor="bob")
        assert queued["execution_status"] == "DRY_RUN_PLANNED" and queued["state"] == ideas.RESEARCH_QUEUED
        contract = queued["contract"]
        assert contract["schema_version"] == "idea_research_contract_v2" and contract["execution_mode"] == "DRY_RUN"
        assert contract["constraints"]["no_data_on_or_after"] == "2025-01-01" and contract["constraints"]["no_backtest_launch"] is True
        assert contract["approval"]["approved_by"] == "bob" and contract["spec_hash"] == h1
        assert contract["spec_completeness"] == ideas.COMPLETENESS_COMPLETE and contract["missing_fields"] == []
        assert contract["effective_holdout_start"] == "2025-01-01"
        assert contract["economic_gate"] == {"status": ideas.ECONOMIC_GATE_NOT_DEFINED, "acceptance_thresholds": None}
        assert {r["path"] for r in contract["date_ranges"]} == {"params.start/end", "data_requirements.history_start/history_end"}
        assert canonical_sha256(contract["spec"]) == h1
        body = {k: v for k, v in contract.items() if k != "artifact_sha256"}
        assert canonical_sha256(body) == contract["artifact_sha256"]
        # A second approval of the same frozen version is refused (state + active-approval guard).
        with pytest.raises(ideas.IdeaError):
            ideas.approve_research(conn, idea_id, approved_by="carol", version=1, spec_hash=h1)
        # Linking requires a canonical research run; unknown ids are rejected.
        with pytest.raises(ideas.IdeaError, match="not in research_runs"):
            ideas.link_result(conn, idea_id, research_run_id="nope", actor="bob")
        conn.execute(text("INSERT INTO research_runs (research_run_id, strategy_id, run_status, first_seen_at, last_seen_at) VALUES ('IDEA_RUN_1', 'IdeaStrategy', 'COMPLETE', NOW(), NOW())"))
        linked = ideas.link_result(conn, idea_id, research_run_id="IDEA_RUN_1", actor="bob")
        assert linked["state"] == ideas.HUMAN_REVIEW and linked["run_status"] == "COMPLETE"
        shown = ideas.show_idea(conn, idea_id)
    states = [t["to_state"] for t in shown["transitions"]]
    assert states == [ideas.DRAFT, ideas.SPEC_FROZEN, ideas.RESEARCH_APPROVED, ideas.RESEARCH_QUEUED, ideas.RESEARCH_COMPLETE, ideas.HUMAN_REVIEW]
    assert [t["execution_status"] for t in shown["tests"]] == ["DRY_RUN_PLANNED", "LINKED:COMPLETE"]
    assert shown["tests"][1]["research_run_id"] == "IDEA_RUN_1"
    # No duplicated research results: the registry stores only the link.
    assert "sharpe" not in json.dumps(shown).lower()


def test_spec_change_after_approval_creates_version_and_revokes_approval(mi_db):
    with mi_db.begin() as conn:
        reg = _register(conn)
        idea_id, h1 = reg["idea_id"], reg["spec_hash"]
        ideas.freeze_spec(conn, idea_id, actor="alice")
        ideas.approve_research(conn, idea_id, approved_by="bob", version=1, spec_hash=h1)
        unchanged = ideas.revise_idea(conn, idea_id, json.loads(json.dumps(SPEC)), actor="alice")
        assert unchanged.get("unchanged") is True and unchanged["version"] == 1
        revised = ideas.revise_idea(conn, idea_id, dict(SPEC, horizon="42 trading days"), actor="alice")
        assert revised["version"] == 2 and revised["spec_hash"] != h1 and revised["approvals_revoked"] == 1 and revised["state"] == ideas.DRAFT
        assert ideas.active_approval(conn, idea_id, 1, h1) is None
        with pytest.raises(ideas.IdeaError, match="no active approval"):
            ideas.build_contract(conn, idea_id)
        # Old approval cannot be reused for the new version even with the old hash.
        ideas.freeze_spec(conn, idea_id, actor="alice")
        with pytest.raises(ideas.IdeaError, match="spec_hash does not match"):
            ideas.approve_research(conn, idea_id, approved_by="bob", version=2, spec_hash=h1)
        shown = ideas.show_idea(conn, idea_id)
    assert [v["version"] for v in shown["versions"]] == [1, 2]
    assert shown["approvals"][0]["revoked_at"] is not None and "superseded by version 2" in shown["approvals"][0]["revoke_reason"]


def test_incomplete_spec_can_be_frozen_but_never_approved_or_contracted(mi_db):
    with mi_db.begin() as conn:
        reg = _register(conn, DRAFT_SPEC)
        assert reg["spec_completeness"] == ideas.COMPLETENESS_INCOMPLETE and "costs" in reg["missing_fields"]
        frozen = ideas.freeze_spec(conn, reg["idea_id"], actor="alice")
        assert frozen["state"] == ideas.SPEC_FROZEN and frozen["spec_completeness"] == ideas.COMPLETENESS_INCOMPLETE
        with pytest.raises(ideas.IdeaError, match="INCOMPLETE; decide and freeze these fields") as exc:
            ideas.approve_research(conn, reg["idea_id"], approved_by="bob", version=1, spec_hash=reg["spec_hash"])
        assert "economic_rationale" in str(exc.value) and "holdout_policy" in str(exc.value)
        assert conn.execute(text("SELECT COUNT(*) FROM mi_research_idea_approvals WHERE idea_id=:i"), {"i": reg["idea_id"]}).scalar() == 0
        view = conn.execute(text("SELECT spec_completeness, missing_fields, economic_gate, effective_holdout_start FROM mi_v_research_ideas WHERE idea_id=:i"), {"i": reg["idea_id"]}).mappings().one()
        assert view["spec_completeness"] == "INCOMPLETE" and "signal_timing" in view["missing_fields"] and view["economic_gate"] == "NOT_DEFINED"
        assert str(view["effective_holdout_start"]) == "2025-01-01"
        # Completing the decisions creates a new version; only that version can be approved.
        revised = ideas.revise_idea(conn, reg["idea_id"], SPEC, actor="alice")
        assert revised["version"] == 2 and revised["spec_completeness"] == ideas.COMPLETENESS_COMPLETE
        ideas.freeze_spec(conn, reg["idea_id"], actor="alice")
        approved = ideas.approve_research(conn, reg["idea_id"], approved_by="bob", version=2, spec_hash=revised["spec_hash"])
        assert approved["state"] == ideas.RESEARCH_APPROVED


def test_pre_013_versions_default_to_incomplete_and_cannot_be_approved(mi_db):
    """Rows written before completeness existed must not become approvable by default."""
    with mi_db.begin() as conn:
        reg = _register(conn)
        conn.execute(text("UPDATE mi_research_idea_versions SET spec_completeness = DEFAULT, missing_fields_json = DEFAULT WHERE idea_id=:i"), {"i": reg["idea_id"]})
        ideas.freeze_spec(conn, reg["idea_id"], actor="alice")
        with pytest.raises(ideas.IdeaError, match="INCOMPLETE"):
            ideas.approve_research(conn, reg["idea_id"], approved_by="bob", version=1, spec_hash=reg["spec_hash"])


def test_manual_spec_required_types_queue_honestly(mi_db):
    spec = dict(SPEC, research_type="SINGLE_ASSET_MOMENTUM", title="TLT 12-1 momentum variant", universe={"symbols": ["TLT"]})
    with mi_db.begin() as conn:
        reg = _register(conn, spec)
        assert reg["execution_support"] == ideas.SUPPORT_MANUAL_SPEC_REQUIRED
        ideas.freeze_spec(conn, reg["idea_id"], actor="alice")
        ideas.approve_research(conn, reg["idea_id"], approved_by="bob", version=1, spec_hash=reg["spec_hash"])
        queued = ideas.queue_research(conn, reg["idea_id"], actor="bob")
        assert queued["execution_status"] == "MANUAL_SPEC_REQUIRED"
        assert "no idea->StrategySpecV1 adapter" in ideas.show_idea(conn, reg["idea_id"])["tests"][0]["notes"]


def test_identical_spec_cannot_be_registered_twice(mi_db):
    with mi_db.begin() as conn:
        _register(conn)
        with pytest.raises(ideas.IdeaError, match="already registered"):
            _register(conn)


def test_stricter_holdout_flows_into_contract_constraints(mi_db):
    strict = dict(SPEC, holdout_policy={"holdout_start": "2023-01-01", "access": "NONE"}, params={"start": "2015-01-02", "end": "2022-12-30"}, data_requirements=dict(SPEC["data_requirements"], history_end="2022-12-30"))
    with mi_db.begin() as conn:
        reg = _register(conn, strict)
        assert reg["effective_holdout_start"] == "2023-01-01"
        ideas.freeze_spec(conn, reg["idea_id"], actor="alice")
        ideas.approve_research(conn, reg["idea_id"], approved_by="bob", version=1, spec_hash=reg["spec_hash"])
        contract = ideas.build_contract(conn, reg["idea_id"])
    assert contract["effective_holdout_start"] == "2023-01-01"
    assert contract["constraints"]["no_data_on_or_after"] == "2023-01-01" and contract["constraints"]["platform_holdout_start"] == "2025-01-01"
    assert contract["spec"]["holdout_policy"]["holdout_start"] == "2023-01-01"


def test_ai_actor_cannot_approve_or_queue_but_can_draft(mi_db):
    with mi_db.begin() as conn:
        reg = _register(conn, actor="ai-assistant", actor_kind="AI")
        ideas.freeze_spec(conn, reg["idea_id"], actor="ai-assistant", actor_kind="AI")
        # approve_research is human-only by construction (actor_kind fixed), so try a raw transition.
        idea = ideas._idea_row(conn, reg["idea_id"])
        with pytest.raises(ideas.IdeaError, match="AI actors cannot"):
            ideas._transition(conn, idea, ideas.RESEARCH_APPROVED, actor="ai", actor_kind="AI", reason="x", version=1, bound_spec_hash=reg["spec_hash"])


def test_unsupported_type_registers_honestly_and_queues_as_unsupported(mi_db):
    with mi_db.begin() as conn:
        reg = _register(conn, dict(SPEC, research_type="CREDIT_SPREAD_SIGNAL", title="HY OAS widening predicts XLF underperformance"))
        assert reg["execution_support"] == ideas.SUPPORT_UNSUPPORTED
        ideas.freeze_spec(conn, reg["idea_id"], actor="alice")
        ideas.approve_research(conn, reg["idea_id"], approved_by="bob", version=1, spec_hash=reg["spec_hash"])
        queued = ideas.queue_research(conn, reg["idea_id"], actor="bob")
        assert queued["execution_status"] == "UNSUPPORTED"
        assert "no execution adapter" in ideas.show_idea(conn, reg["idea_id"])["tests"][0]["notes"]
        row = conn.execute(text("SELECT execution_support FROM mi_v_research_ideas WHERE idea_id=:i"), {"i": reg["idea_id"]}).scalar()
    assert row == ideas.SUPPORT_UNSUPPORTED


def test_snapshot_reference_must_precede_conception_and_be_real(mi_db):
    with mi_db.begin() as conn:
        with pytest.raises(ideas.IdeaError, match="does not exist"):
            _register(conn, source_snapshot_id="missing_snapshot")
        conn.execute(
            text(
                """
                INSERT INTO mi_morning_context_snapshots (snapshot_id, schema_version, as_of_date, generated_at, cutoff_at, snapshot_json, snapshot_sha256, completeness, generation_params, input_refs, sections_status, created_by)
                VALUES ('snap_1', 'morning_context_v1', '2024-12-31', '2025-01-02T11:30:00+00:00', '2025-01-02T11:30:00+00:00', '{}'::jsonb, :h, 'PARTIAL', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, 'test')
                """
            ),
            {"h": "a" * 64},
        )
        with pytest.raises(ideas.IdeaError, match="inputs must precede"):
            _register(conn, source_snapshot_id="snap_1", conception_at=datetime(2025, 1, 2, 9, tzinfo=timezone.utc))
        reg = _register(conn, source_snapshot_id="snap_1", conception_at=datetime(2025, 1, 2, 12, tzinfo=timezone.utc))
        version = conn.execute(text("SELECT source_snapshot_id, source_snapshot_hash FROM mi_research_idea_versions WHERE idea_id=:i"), {"i": reg["idea_id"]}).one()
    assert version.source_snapshot_id == "snap_1" and version.source_snapshot_hash == "a" * 64


def test_cli_validate_register_and_refuse_execute(mi_db, tmp_path, capsys):
    spec_path = tmp_path / "idea.json"
    spec_path.write_text(json.dumps(SPEC), encoding="utf-8")
    assert ideas.main(["validate", "--spec", str(spec_path)]) == 0
    assert ideas.main(["register", "--spec", str(spec_path), "--actor", "alice"], engine=mi_db) == 0
    idea_id = _last_json(capsys)["idea_id"]
    assert ideas.main(["freeze", "--idea", idea_id, "--actor", "alice"], engine=mi_db) == 0
    h = _last_json(capsys)["spec_hash"]
    assert ideas.main(["approve", "--idea", idea_id, "--approved-by", "bob", "--version", "1", "--spec-hash", h], engine=mi_db) == 0
    capsys.readouterr()
    assert ideas.main(["queue", "--idea", idea_id, "--actor", "bob", "--execute"], engine=mi_db) == 2
    assert _last_json(capsys)["status"] == "REJECTED"
    out = tmp_path / "contract.json"
    assert ideas.main(["queue", "--idea", idea_id, "--actor", "bob", "--contract-out", str(out)], engine=mi_db) == 0
    contract = json.loads(out.read_text())
    assert contract["execution_mode"] == "DRY_RUN" and contract["idea_id"] == idea_id
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(dict(SPEC, params={"end": "2025-03-01"})), encoding="utf-8")
    assert ideas.main(["validate", "--spec", str(bad)]) == 2


def _last_json(capsys):
    out = capsys.readouterr().out.strip()
    start = out.rfind("\n{")
    return json.loads(out if start < 0 and out.startswith("{") else out[start + 1 :])
