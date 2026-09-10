"""Research-delivery visibility: remote 404 + good local fallback are separate facts; repeated
ingestion is idempotent; invalid hashes are rejected; previous valid data is preserved.

No research is rerun; the committed TLT artifact is read, never modified.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from qc_research import delivery_visibility as dv
from qc_research import pull_public_platform_artifacts as pull

ROOT = Path(__file__).resolve().parent.parent
TLT = ROOT / "qc_research" / "platform_artifacts" / "tlt_duration_momentum.json"
WORKFLOW = (ROOT / ".github" / "workflows" / "ingest_platform_research.yml").read_text(encoding="utf-8")
LIVE_SCRIPT = (ROOT / "scripts" / "ingest_platform_live.sh").read_text(encoding="utf-8")


def _blocked_pull_report(tmp_path: Path) -> Path:
    def _raise(*_a, **_k):
        raise urllib.error.HTTPError("https://api.github.com/x", 404, "Not Found", hdrs=None, fp=None)

    original = pull.list_remote_json_paths
    pull.list_remote_json_paths = _raise
    try:
        report = pull.pull_complete_artifacts(
            tmp_path / "incoming",
            repo="hs1008/quant-strategies",
            path="research/platform_smokes",
            ref="ef270841621933f5039680cb070559f43bd1e3c8",
        )
    finally:
        pull.list_remote_json_paths = original
    out = tmp_path / "pull.json"
    out.write_text(json.dumps(report), encoding="utf-8")
    return out


def test_pull_refuses_branch_names_without_listing(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("listing must not run for a floating ref")

    monkeypatch.setattr(pull, "list_remote_json_paths", _boom)
    report = pull.pull_complete_artifacts(
        tmp_path / "incoming",
        repo="hs1008/quant-strategies",
        path="research/platform_smokes",
        ref="main",
    )
    assert report["blocked"] is True and report["pulled"] == 0
    assert "SHA" in report["reason"]
    empty = pull.pull_complete_artifacts(
        tmp_path / "incoming",
        repo="hs1008/quant-strategies",
        path="research/platform_smokes",
        ref="",
    )
    assert empty["blocked"] is True and "SOURCE_REF" in empty["reason"]


def test_pull_reports_404_as_blocked_without_token_leak(tmp_path, monkeypatch):
    monkeypatch.setenv("QS_READ_TOKEN", "ghp_secret_value")
    report = json.loads(_blocked_pull_report(tmp_path).read_text())
    assert report["blocked"] is True and report["pulled"] == 0 and report["delivery_status"] == "BLOCKED"
    assert "404" in report["reason"] and "research/platform_smokes" in report["reason"]
    assert "ef270841621933f5039680cb070559f43bd1e3c8" in report["reason"]
    assert "ghp_secret_value" not in json.dumps(report)
    assert not list((tmp_path / "incoming").glob("*.json"))


def test_remote_404_plus_local_fallback_are_separate_facts(tmp_path, monkeypatch):
    pull_report = json.loads(_blocked_pull_report(tmp_path).read_text())
    now = datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc)
    report = dv.build_report(
        event="schedule",
        target=TLT.parent,
        repo_root=ROOT,
        remote=pull_report,
        remote_config={"repo": "hs1008/quant-strategies", "ref": "", "ref_source": "unset -> provider default branch", "path": "research/platform_smokes"},
        now=now,
    )
    assert report["remote"]["attempted"] is True and report["remote"]["status"] == "BLOCKED"
    assert report["ingest"]["source"] == "LOCAL_FALLBACK"
    assert report["upstream_delivery_status"] == "BLOCKED" and report["downstream_data_status"] == "LAST_KNOWN_GOOD"
    assert report["claims"] == {"new_remote_artifact_delivered": False, "local_fallback_ingested": True}
    tlt = next(a for a in report["ingest"]["artifacts"] if a["path"].endswith("tlt_duration_momentum.json"))
    assert tlt["sha256"] == dv._sha256_file(TLT) and tlt["strategy_id"] == "TLTDurationMomentum"
    assert tlt["artifact_claims_delivery_status"] == "DELIVERED"  # the artifact's own field, kept distinct from transport truth
    assert tlt["git_commit"] and tlt["git_committed_at"] and tlt["age_days_since_commit"] is not None
    markdown = dv.render_markdown(report)
    assert "| Remote fetch | BLOCKED" in markdown and "**LOCAL_FALLBACK**" in markdown and "New remote artifact delivered | **False**" in markdown


def test_remote_ok_into_incoming_is_fresh_remote(tmp_path):
    incoming = tmp_path / "qc_research" / "platform_artifacts" / "incoming"
    incoming.mkdir(parents=True)
    (incoming / "x.json").write_text(json.dumps({"strategy_id": "X"}), encoding="utf-8")
    remote = {"blocked": False, "pulled": 1, "skipped": 0, "reason": "Pulled complete public artifacts"}
    report = dv.build_report(event="schedule", target=incoming, repo_root=tmp_path, remote=remote, remote_config={"repo": "r", "ref": "abc123", "path": "p"})
    assert report["remote"]["status"] == "OK" and report["ingest"]["source"] == "REMOTE"
    assert report["upstream_delivery_status"] == "FRESH_REMOTE" and report["downstream_data_status"] == "FRESH_REMOTE"
    assert report["claims"]["new_remote_artifact_delivered"] is True
    assert report["remote"]["ref_source"] == "explicit"


def test_explicit_local_ingest_has_no_remote_claim():
    report = dv.build_report(event="workflow_dispatch", target=TLT, repo_root=ROOT, remote=None, remote_config={"repo": "r", "ref": "", "path": ""}, explicit_local=True)
    assert report["remote"]["status"] == "NOT_ATTEMPTED" and report["ingest"]["source"] == "LOCAL_EXPLICIT"
    assert report["upstream_delivery_status"] == "NOT_ATTEMPTED" and report["downstream_data_status"] == "LAST_KNOWN_GOOD"
    assert report["claims"]["new_remote_artifact_delivered"] is False


def test_build_cli_is_stdlib_only_and_writes_summary(tmp_path):
    pull_report = _blocked_pull_report(tmp_path)
    out = tmp_path / "delivery" / "report.json"
    summary = tmp_path / "summary.md"
    code = "import sys; sys.modules['sqlalchemy'] = None; sys.modules['pandas'] = None; from qc_research.delivery_visibility import main; raise SystemExit(main(sys.argv[1:]))"
    proc = subprocess.run(
        [sys.executable, "-c", code, "build", "--event", "schedule", "--target", str(TLT.parent), "--pull-report", str(pull_report), "--repo", "hs1008/quant-strategies", "--ref", "", "--ref-source", "unset -> provider default branch", "--path", "research/platform_smokes", "--out", str(out), "--summary", str(summary)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(out.read_text())
    assert report["upstream_delivery_status"] == "BLOCKED" and report["downstream_data_status"] == "LAST_KNOWN_GOOD"
    assert "Research delivery (schedule)" in summary.read_text()


def test_workflow_and_live_script_wire_explicit_ref_and_report():
    assert "vars.QS_ARTIFACT_SOURCE_REF" in WORKFLOW and "vars.QS_ARTIFACT_SOURCE_PATH" in WORKFLOW
    assert "qc_research.delivery_visibility build" in WORKFLOW
    assert "--report \"$DELIVERY_DIR/pull.json\"" in WORKFLOW
    assert "LAST_KNOWN_GOOD, not a new delivery" in WORKFLOW
    assert "will not float to the provider default branch" in WORKFLOW
    assert "[0-9a-fA-F]{40}" in WORKFLOW
    assert "full 40-character git SHA" in WORKFLOW
    assert "qc_research.contracts.digests" in LIVE_SCRIPT
    assert "qc_research.delivery_visibility record" in LIVE_SCRIPT
    assert "--require-postgres" in LIVE_SCRIPT
    record_block = LIVE_SCRIPT.split("delivery_visibility record", 1)[1]
    assert "|| echo" not in record_block.split("else", 1)[0]
    assert "WARN:" not in LIVE_SCRIPT
    # The fallback is preserved: committed artifacts still ingest when remote is blocked.
    assert 'TARGET="$LOCAL_ROOT"' in WORKFLOW and 'TARGET="$LOCAL_CANDIDATE"' in WORKFLOW
    # Triggers unchanged: no push trigger was introduced.
    assert "push:" not in WORKFLOW
    query = LIVE_SCRIPT.split("verify_tlt_monitor --live", 1)[0]
    assert "/etc/fmp/fmp-dashboard.env" in query
    assert "unset DATABASE_URL" in query
    assert "FMP_IDENTITY_ENV_ONLY=1" in query
    assert query.rfind("source /etc/fmp/fmp-dashboard.env") > query.rfind("source \"$DROPLET_ENV\"")
    assert "source /root/FMP_SCREENER/.env" not in LIVE_SCRIPT.split("verify_tlt_monitor --live", 1)[1]
    assert "--allow-missing" not in LIVE_SCRIPT
    assert "/var/lib/fmp/deploy/tlt_v0_live.json" in LIVE_SCRIPT
    assert "DROPLET_ENV=/etc/fmp/fmp-writer.env" in LIVE_SCRIPT
    assert LIVE_SCRIPT.index("DROPLET_ENV=/etc/fmp/fmp-writer.env") < LIVE_SCRIPT.index(
        "DROPLET_ENV=/root/FMP_SCREENER/.env"
    )
    assert LIVE_SCRIPT.index('source "$DROPLET_ENV"') < LIVE_SCRIPT.index(
        "unset FMP_STREAMLIT_READONLY STREAMLIT_ALLOW_PROVIDER_FETCH DASHBOARD_ALLOW_WRITER_FALLBACK"
    )
    assert LIVE_SCRIPT.index(
        "unset FMP_STREAMLIT_READONLY STREAMLIT_ALLOW_PROVIDER_FETCH DASHBOARD_ALLOW_WRITER_FALLBACK"
    ) < LIVE_SCRIPT.index("python -m qc_research.ingest_platform_artifacts")
    assert "CODE_ROOT=" in LIVE_SCRIPT
    assert 'IMMUTABLE_ROOT="/opt/fmp/current"' in LIVE_SCRIPT
    assert 'GIT_CHECKOUT="/root/FMP_SCREENER"' in LIVE_SCRIPT
    assert LIVE_SCRIPT.index('IMMUTABLE_ROOT="/opt/fmp/current"') < LIVE_SCRIPT.index(
        'GIT_CHECKOUT="/root/FMP_SCREENER"'
    )
    assert LIVE_SCRIPT.index('if [ -d "$IMMUTABLE_ROOT" ]') < LIVE_SCRIPT.index(
        'elif [ -d "$GIT_CHECKOUT" ]'
    )
    assert 'CODE_ROOT="$IMMUTABLE_ROOT"' in LIVE_SCRIPT
    assert 'CODE_ROOT="$GIT_CHECKOUT"' in LIVE_SCRIPT
    assert 'CODE_ROOT="$ROOT"' in LIVE_SCRIPT
    assert 'source "$CODE_ROOT/venv/bin/activate"' in LIVE_SCRIPT
    assert 'export PYTHONPATH="$CODE_ROOT"' in LIVE_SCRIPT
    assert "python -m jobs.apply_migrations" not in LIVE_SCRIPT
    assert "deployed ingest module missing" in LIVE_SCRIPT
    assert 'if [ "$CANONICAL_ONLY" = "1" ]' in LIVE_SCRIPT
    assert '[ -d "$TARGET" ] && [ "$CANONICAL_ONLY" = "1" ]' not in LIVE_SCRIPT
    assert "/opt/fmp/current/scripts/ingest_platform_live.sh" in WORKFLOW
    assert "/root/FMP_SCREENER/scripts/ingest_platform_live.sh" in WORKFLOW
    assert WORKFLOW.index("/opt/fmp/current/scripts/ingest_platform_live.sh") < WORKFLOW.index(
        "/root/FMP_SCREENER/scripts/ingest_platform_live.sh"
    )
    assert "ingest_platform_live.sh missing on droplet" in WORKFLOW
    assert "bash /tmp/fmp-platform-ingest/scripts/ingest_platform_live.sh" not in WORKFLOW
    assert "live PostgreSQL ingest is allowed only from refs/heads/main" in WORKFLOW
    assert "github.ref == 'refs/heads/main'" in WORKFLOW


# ---- PostgreSQL-backed: idempotent ingestion, invalid hash, preservation, recorded facts -----------------

@pytest.fixture
def research_db(pg_engine):
    with pg_engine.begin() as conn:
        for table in ("research_artifacts", "research_experiments", "research_runs"):
            conn.execute(text("DELETE FROM {0} WHERE TRUE".format(table)))
    return pg_engine


def _ingest(engine, paths):
    from qc_research.platform_ingest import ingest_platform_files

    with engine.begin() as conn:
        return ingest_platform_files(conn, paths)


def _tlt_state(engine):
    with engine.connect() as conn:
        run = conn.execute(text("SELECT COUNT(*), MAX(run_status), MAX(economic_gate), MAX(promotion_gate), MAX(holdout_status) FROM research_runs WHERE strategy_id='TLTDurationMomentum'")).one()
        artifacts = conn.execute(text("SELECT COUNT(*), MIN(sha256), MAX(sha256) FROM research_artifacts")).one()
    return tuple(run), tuple(artifacts)


def test_repeated_fallback_ingestion_is_idempotent_and_preserves_frozen_tlt(research_db):
    first = _ingest(research_db, [TLT])
    assert first["ingested"] > 0 and not first["errors"]
    state_after_first = _tlt_state(research_db)
    second = _ingest(research_db, [TLT])
    assert second["ingested"] == first["ingested"] and not second["errors"]
    assert _tlt_state(research_db) == state_after_first
    run_count, status, econ, promo, holdout = state_after_first[0]
    assert run_count == 1 and status == "COMPLETE" and econ == "NOT_DEFINED"
    assert promo in {"HUMAN_REVIEW_REQUIRED", "LOCKED"} and holdout == "LOCKED"


def test_invalid_hash_rejected_and_previous_valid_data_preserved(research_db, tmp_path):
    _ingest(research_db, [TLT])
    before = _tlt_state(research_db)
    good = json.loads(TLT.read_text(encoding="utf-8"))
    from qc_research.platform_ingest import _hashed_envelope

    envelope = _hashed_envelope("run_summary", "TAMPERED_RUN", dict(good, strategy_id="TLTDurationMomentum"), strategy_id="TLTDurationMomentum")
    envelope["artifact_sha256"] = "0" * 64
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(envelope), encoding="utf-8")
    from qc_research.object_store_sync import ArtifactSyncError

    # A tampered/invalid hash aborts the ingest transaction (visible failure), never a silent skip.
    with pytest.raises(ArtifactSyncError, match="SHA-256 mismatch"):
        _ingest(research_db, [tampered])
    assert _tlt_state(research_db) == before
    with research_db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM research_runs WHERE research_run_id='TAMPERED_RUN'")).scalar() == 0


def test_delivery_facts_recorded_in_market_intelligence_tables(mi_db, tmp_path):
    pull_report = json.loads(_blocked_pull_report(tmp_path).read_text())
    report = dv.build_report(event="schedule", target=TLT.parent, repo_root=ROOT, remote=pull_report, remote_config={"repo": "hs1008/quant-strategies", "ref": "", "path": "research/platform_smokes"})
    result = dv.record_to_postgres(mi_db, report)
    assert result["transport_status"] == "FAILED"
    with mi_db.connect() as conn:
        health = conn.execute(text("SELECT source_id, enabled, access_status, transport_status, freshness_status, latest_observation_date, last_error_redacted FROM mi_v_source_health WHERE source_id=:s"), {"s": dv.SOURCE_ID}).mappings().one()
        run = conn.execute(text("SELECT status, details_json FROM mi_ingestion_runs WHERE source_id=:s"), {"s": dv.SOURCE_ID}).mappings().one()
    assert health["access_status"] == "SOURCE_REF_NOT_CONFIGURED" and health["transport_status"] == "FAILED"
    assert health["freshness_status"] == "UNKNOWN"  # ON_DEMAND cadence: no freshness claim from a fallback
    assert health["latest_observation_date"] is not None  # newest local artifact commit date, as provenance
    assert "downstream LAST_KNOWN_GOOD" in health["last_error_redacted"]
    assert run["status"] == "FAILED" and run["details_json"]["claims"]["new_remote_artifact_delivered"] is False
    assert run["details_json"]["upstream_delivery_status"] == "BLOCKED"
    # A later successful remote pull records OK without erasing the earlier failure row.
    ok = dv.build_report(event="schedule", target=tmp_path / "qc_research" / "platform_artifacts" / "incoming", repo_root=ROOT, remote={"blocked": False, "pulled": 1, "reason": "Pulled"}, remote_config={"repo": "r", "ref": "deadbeef", "path": "p"})
    dv.record_to_postgres(mi_db, ok)
    with mi_db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM mi_ingestion_runs WHERE source_id=:s"), {"s": dv.SOURCE_ID}).scalar() == 2
        assert conn.execute(text("SELECT transport_status FROM mi_data_freshness WHERE source_id=:s"), {"s": dv.SOURCE_ID}).scalar() == "OK"


def test_record_require_postgres_fails_closed_without_writer(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    missing = tmp_path / "missing.json"
    assert dv.main(["record", "--report", str(missing), "--require-postgres"]) == 2
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"event": "schedule"}), encoding="utf-8")
    assert dv.main(["record", "--report", str(report), "--require-postgres"]) == 2
    assert dv.main(["record", "--report", str(missing)]) == 0


def test_delivery_record_refuses_streamlit_readonly_even_with_database_url(monkeypatch, tmp_path):
    monkeypatch.setenv("FMP_STREAMLIT_READONLY", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://writer:secret@127.0.0.1:5432/fmp")

    def _boom(*_args, **_kwargs):
        raise AssertionError("Streamlit identity must not create a writer engine")

    monkeypatch.setattr("sqlalchemy.create_engine", _boom)
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"event": "schedule"}), encoding="utf-8")
    assert dv.main(["record", "--report", str(report), "--require-postgres"]) == 4
    assert dv.main(["record", "--report", str(report)]) == 4
