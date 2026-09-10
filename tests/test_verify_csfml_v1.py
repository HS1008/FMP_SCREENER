"""Official CSFML V1 query-back checks identity, not economics."""

from __future__ import annotations

from pathlib import Path

from qc_research.contracts.label_integrity import load_csfml_v1_label_integrity
from qc_research.verify_csfml_v1 import evaluate_csfml_v1_row, verify_exit_code


ROOT = Path(__file__).resolve().parents[1]
PIN = load_csfml_v1_label_integrity()


def test_missing_official_run_is_recorded_not_a_deploy_failure():
    report = evaluate_csfml_v1_row(None, PIN)
    assert report["present"] is False
    assert report["identity_ok"] is False
    assert report["blockers"] == ["official_run_missing"]
    assert report["historical_v1_impact"] == "CANNOT_RULE_OUT"
    assert report["rerun_authorized"] is False
    assert verify_exit_code(report, require_present=False) == 0
    assert verify_exit_code(report, require_present=True) == 3


def test_pinned_qc_sha_and_integration_sha_are_accepted():
    for commit in (
        PIN["authoritative_csfml_v1_qc_sha"],
        PIN["authoritative_csfml_v1_sha"],
    ):
        report = evaluate_csfml_v1_row(
            {
                "research_run_id": PIN["full_suite_run_id"],
                "strategy_id": PIN["strategy_id"],
                "git_commit": commit,
                "holdout_accessed": False,
                "holdout_access_count": 0,
                "economic_gate": "NOT_DEFINED",
            },
            PIN,
        )
        assert report["identity_ok"] is True
        assert report["git_commit_pinned"] is True
        assert verify_exit_code(report, require_present=True) == 0


def test_wrong_sha_holdout_or_gate_fails_closed():
    base = {
        "research_run_id": PIN["full_suite_run_id"],
        "strategy_id": PIN["strategy_id"],
        "git_commit": PIN["authoritative_csfml_v1_qc_sha"],
        "holdout_accessed": False,
        "holdout_access_count": 0,
        "economic_gate": "NOT_DEFINED",
    }
    wrong_sha = evaluate_csfml_v1_row({**base, "git_commit": "0" * 40}, PIN)
    assert wrong_sha["identity_ok"] is False
    assert "git_commit_not_pinned" in wrong_sha["blockers"]
    assert verify_exit_code(wrong_sha, require_present=False) == 2
    holdout = evaluate_csfml_v1_row({**base, "holdout_accessed": True}, PIN)
    assert "holdout_accessed" in holdout["blockers"]
    gate = evaluate_csfml_v1_row({**base, "economic_gate": "PASS"}, PIN)
    assert "economic_gate_changed" in gate["blockers"]


def test_verify_module_is_readonly_and_wired_without_require_present():
    text = (ROOT / "qc_research" / "verify_csfml_v1.py").read_text(encoding="utf-8")
    assert "dashboard_engine" in text
    assert "postgres_engine" not in text
    assert "begin()" not in text
    assert "DATABASE_URL" not in text
    assert "backtests/create" not in text
    deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "qc_research.verify_csfml_v1" in deploy
    assert "--live" in deploy
    assert "--require-present" not in deploy
    verify = (ROOT / ".github" / "workflows" / "platform_research_verify.yml").read_text(
        encoding="utf-8"
    )
    assert "qc_research.verify_csfml_v1 --live" in verify
    assert "--require-present" not in verify
    assert verify.index("verify_tlt_monitor --live") < verify.index("verify_csfml_v1 --live")
    assert "source /root/FMP_SCREENER/.env" not in verify
    assert ". /root/FMP_SCREENER/.env" not in verify
    assert "/etc/fmp/fmp-dashboard.env" in verify
