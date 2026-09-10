"""Official Stage 1 query-back checks identity, not economics."""

from __future__ import annotations

import json
from pathlib import Path

from qc_research.contracts.sealed_results import MINIMUM_STAGE1_PINS
from qc_research.verify_stage1 import (
    OFFICIAL_RUN_ID,
    evaluate_stage1_row,
    verify_exit_code,
    write_live_report,
)


ROOT = Path(__file__).resolve().parents[1]
PIN = dict(MINIMUM_STAGE1_PINS[OFFICIAL_RUN_ID])


def test_missing_official_run_is_recorded_not_a_deploy_failure():
    report = evaluate_stage1_row(None)
    assert report["present"] is False
    assert report["identity_ok"] is False
    assert report["blockers"] == ["official_run_missing"]
    assert report["research_run_id"] == OFFICIAL_RUN_ID
    assert report["expected_experiment_count"] == 81
    assert verify_exit_code(report, require_present=False) == 0
    assert verify_exit_code(report, require_present=True) == 3


def test_live_report_is_written_without_secrets(tmp_path):
    out = tmp_path / "stage1_live.json"
    write_live_report(
        evaluate_stage1_row(None),
        str(out),
        code_root="/opt/fmp/current",
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["present"] is False
    assert payload["identity_ok"] is False
    assert payload["blockers"] == ["official_run_missing"]
    assert payload["code_root"] == "/opt/fmp/current"
    assert payload["expected_experiment_count"] == 81
    assert "postgresql://" not in out.read_text(encoding="utf-8")


def test_pinned_stage1_row_is_accepted():
    report = evaluate_stage1_row(
        {
            "research_run_id": OFFICIAL_RUN_ID,
            "strategy_id": PIN["strategy_id"],
            "git_commit": PIN["git_commit"],
            "run_status": "COMPLETE",
            "expected_experiment_count": 81,
            "completed_count": 81,
            "failed_count": 0,
            "skipped_count": 0,
            "holdout_accessed": False,
            "holdout_access_count": 0,
        }
    )
    assert report["identity_ok"] is True
    assert report["git_commit_pinned"] is True
    assert verify_exit_code(report, require_present=True) == 0


def test_wrong_sha_count_or_holdout_fails_closed():
    base = {
        "research_run_id": OFFICIAL_RUN_ID,
        "strategy_id": PIN["strategy_id"],
        "git_commit": PIN["git_commit"],
        "run_status": "COMPLETE",
        "expected_experiment_count": 81,
        "completed_count": 81,
        "failed_count": 0,
        "skipped_count": 0,
        "holdout_accessed": False,
        "holdout_access_count": 0,
    }
    wrong_sha = evaluate_stage1_row({**base, "git_commit": "0" * 40})
    assert wrong_sha["identity_ok"] is False
    assert "git_commit" in wrong_sha["blockers"]
    assert verify_exit_code(wrong_sha, require_present=False) == 2
    count = evaluate_stage1_row({**base, "completed_count": 1})
    assert "completed_count" in count["blockers"]
    holdout = evaluate_stage1_row({**base, "holdout_accessed": True})
    assert "holdout_accessed" in holdout["blockers"]


def test_live_stage1_strips_writer_env_before_engine(monkeypatch):
    from db.dashboard_engine import DashboardIdentityError
    from qc_research.verify_stage1 import main

    called: list[str] = []

    def load():
        called.append("load")
        return ["DATABASE_URL"]

    def engine():
        called.append("engine")
        raise DashboardIdentityError("stop")

    monkeypatch.setenv("DASHBOARD_READONLY_URL", "postgresql://dashboard_readonly:x@127.0.0.1/fmp")
    monkeypatch.delenv("DASHBOARD_ALLOW_WRITER_FALLBACK", raising=False)
    monkeypatch.setattr("db.dashboard_engine.load_streamlit_env", load)
    monkeypatch.setattr("db.dashboard_engine.dashboard_engine", engine)
    try:
        main(["--live"])
    except DashboardIdentityError:
        pass
    else:
        raise AssertionError("expected DashboardIdentityError")
    assert called == ["load", "engine"]


def test_verify_module_is_readonly_and_wired_without_require_present():
    text = (ROOT / "qc_research" / "verify_stage1.py").read_text(encoding="utf-8")
    assert "dashboard_engine" in text
    assert "load_streamlit_env()" in text
    assert "strip_writer_database_env()" not in text
    assert "postgres_engine" not in text
    assert "begin()" not in text
    assert "DATABASE_URL" not in text
    assert "backtests/create" not in text
    deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "qc_research.verify_stage1" in deploy
    assert "--live" in deploy
    assert "--require-present" not in deploy
    stage1 = deploy.split("qc_research.verify_stage1 --live", 1)[0]
    assert "/etc/fmp/fmp-dashboard.env" in stage1[-400:]
    assert "unset DATABASE_URL" in stage1[-400:]
    assert "source /root/FMP_SCREENER/.env" not in stage1[-400:]
    assert ". /root/FMP_SCREENER/.env" not in stage1[-400:]
    assert "/var/lib/fmp/deploy/stage1_live.json" in deploy
    assert deploy.index("qc_research.verify_tlt_monitor --live") < deploy.index(
        "qc_research.verify_stage1 --live"
    )
    assert deploy.index("qc_research.verify_stage1 --live") < deploy.index(
        "jobs.record_deploy_identity_db"
    )
    assert "--stage1 /var/lib/fmp/deploy/stage1_live.json" in deploy
    assert "jobs.record_research_live_identity_db" not in deploy
    stage1_verify = (ROOT / ".github" / "workflows" / "stage1_verify.yml").read_text(
        encoding="utf-8"
    )
    assert "qc_research.verify_stage1 --live" in stage1_verify
    assert "/var/lib/fmp/deploy/stage1_live.json" in stage1_verify
    assert "/var/lib/fmp/deploy/stage1_verify.json" in stage1_verify
    assert "jobs.record_research_live_identity_db" in stage1_verify
    assert "--require-present" not in stage1_verify
