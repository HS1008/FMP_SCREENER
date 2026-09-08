"""Idea registry: versioning, hash-bound approval, dry-run-only queue, boundary validation, linking."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from market_intelligence import ideas
from market_intelligence.nulls import canonical_sha256

SPEC = {
    "title": "Sector RS breadth precedes 1M sector leadership",
    "research_type": "SECTOR_ROTATION_DIAGNOSTIC",
    "hypothesis": "Sectors whose 1W RS change turns positive while 3M RS is negative lead over the next month.",
    "universe": {"sectors": "canonical_sectors_v1", "benchmark": "SPY"},
    "signal": {"rs_chg_1w": ">0", "rs_chg_3m": "<0"},
    "horizon": "21 trading days",
    "data_used": [{"source_id": "FMP_LEGACY", "dataset": "ETF_RS_VS_SPY"}, {"source_id": "FRED", "series": ["DGS10", "DGS2"]}],
    "params": {"start": "2015-01-02", "end": "2024-12-31"},
}


def _register(conn, spec=SPEC, **kw):
    return ideas.register_idea(conn, spec, actor=kw.pop("actor", "alice"), **kw)


def test_validate_spec_hash_is_deterministic_and_boundary_enforced():
    a = ideas.validate_spec(SPEC)
    b = ideas.validate_spec(json.loads(json.dumps(SPEC)))
    assert a.ok and a.spec_hash == b.spec_hash and a.execution_support == ideas.SUPPORT_DRY_RUN_CONTRACT
    assert a.normalized["holdout_policy"]["access"] == "NONE" and a.normalized["holdout_policy"]["holdout_start"] == "2025-01-01"
    bad = ideas.validate_spec(dict(SPEC, params={"start": "2015-01-02", "end": "2025-06-30"}))
    assert not bad.ok and any("protected window" in e for e in bad.errors)
    bad2 = ideas.validate_spec(dict(SPEC, holdout_policy={"access": "READ"}))
    assert not bad2.ok and any("holdout_policy.access" in e for e in bad2.errors)
    missing = ideas.validate_spec({"title": "x"})
    assert not missing.ok and any("research_type" in e for e in missing.errors)
    unsupported = ideas.validate_spec(dict(SPEC, research_type="CREDIT_SPREAD_SIGNAL"))
    assert unsupported.ok and unsupported.execution_support == ideas.SUPPORT_UNSUPPORTED and unsupported.warnings
    unknown = ideas.validate_spec(dict(SPEC, research_type="MAGIC"))
    assert not unknown.ok


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
        assert contract["schema_version"] == ideas.CONTRACT_SCHEMA_VERSION and contract["execution_mode"] == "DRY_RUN"
        assert contract["constraints"]["no_data_on_or_after"] == "2025-01-01" and contract["constraints"]["no_backtest_launch"] is True
        assert contract["approval"]["approved_by"] == "bob" and contract["spec_hash"] == h1
        body = {k: v for k, v in contract.items() if k != "artifact_sha256"}
        assert canonical_sha256(body) == contract["artifact_sha256"]
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
