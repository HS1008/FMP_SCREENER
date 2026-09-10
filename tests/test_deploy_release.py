"""Immutable release script exists beside the live git-pull deploy."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_script_is_additive_and_supports_rollback():
    script = (ROOT / "scripts" / "deploy_release.sh").read_text(encoding="utf-8")
    assert "/opt/fmp/releases" in script
    assert "--rollback" in script
    assert "--skip-restart" in script
    assert "git reset --hard" not in script
    deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "git pull --ff-only origin main" in deploy
    assert "qc_research.contracts.digests" in deploy
    assert "scripts/provision_dashboard_readonly.sh --require" in deploy
    assert deploy.index("scripts/provision_dashboard_readonly.sh --require") < deploy.index(
        "scripts/verify_dashboard_identity.sh"
    )
    assert "scripts/verify_dashboard_identity.sh" in deploy
    assert "/opt/fmp/current/scripts/verify_dashboard_identity.sh" in deploy
    assert "immutable current is missing scripts/verify_dashboard_identity.sh" in deploy
    assert "--skip-identity" in script
    assert "qc_research.contracts.digests" in script
    assert "scripts/provision_dashboard_readonly.sh --require" in script
    assert "scripts/verify_dashboard_identity.sh" in script
    identity_block = script[script.index('if [ "$SKIP_IDENTITY" != 1 ]'):]
    assert "qc_research.contracts.digests" in identity_block
    assert "provision_dashboard_readonly.sh --require" in identity_block
    preflight_block = script[script.index('if [ "$SKIP_PREFLIGHT" != 1 ]'):script.index('if [ "$SKIP_IDENTITY" != 1 ]')]
    assert "provision_dashboard_readonly.sh" not in preflight_block
    assert "verify_dashboard_identity.sh" not in preflight_block
    assert "--verify-rc" in deploy
    assert "dashboard identity verify failed" in deploy
    assert deploy.index("jobs.report_deploy_identity") < deploy.index("systemctl restart fmp-dashboard")
    assert deploy.index("--verify-rc") < deploy.index("systemctl restart fmp-dashboard")
    assert "/opt/fmp/releases" in deploy
    assert "--skip-restart" in deploy
    assert "--skip-identity" not in deploy
    assert "FMP_IMMUTABLE_RELEASE_STRICT" in deploy
    assert "jobs.report_deploy_identity" in deploy
    assert "jobs.audit_host_dashboard" in deploy
    assert "--require-readonly" in deploy
    assert "/var/lib/fmp/deploy/host_audit.json" in deploy
    assert deploy.index("jobs.audit_host_dashboard") < deploy.index("systemctl restart fmp-dashboard")
    assert deploy.index("/etc/fmp/fmp-dashboard.env") < deploy.index("jobs.audit_host_dashboard")
    assert "jobs.cutover_dashboard_systemd" in deploy
    assert "qc_research.verify_csfml_v1 --live" in deploy
    assert "--require-present" not in deploy
    assert "--apply" not in deploy
    assert "/var/lib/fmp/deploy/cutover_readiness.json" in deploy
    assert deploy.index("jobs.cutover_dashboard_systemd") < deploy.index("systemctl restart fmp-dashboard")
    assert "systemctl restart fmp-dashboard" in deploy
    assert (ROOT / "docs" / "IMMUTABLE_DEPLOY.md").is_file()
    docs = (ROOT / "docs" / "IMMUTABLE_DEPLOY.md").read_text(encoding="utf-8")
    assert "/var/lib/fmp/streamlit" in docs
    assert "DASHBOARD_READONLY_URL" in docs
    example = (ROOT / "deploy" / "fmp-dashboard.service.example").read_text(encoding="utf-8")
    assert "DASHBOARD_READONLY_URL" in example
    env = (ROOT / "deploy" / "fmp-dashboard.env.example").read_text(encoding="utf-8")
    assert "DASHBOARD_READONLY_URL=" in env
    assert "mi_readonly" not in env.split("DASHBOARD_READONLY_URL=", 1)[1].splitlines()[0]


def test_release_script_symlink_layout_without_host_restart(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "README").write_text("release\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    release_root = tmp_path / "releases"
    current = tmp_path / "current"
    previous = tmp_path / "previous"
    env = os.environ.copy()
    env["FMP_CURRENT_LINK"] = str(current)
    env["FMP_PREVIOUS_LINK"] = str(previous)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "deploy_release.sh"),
            "--sha",
            sha,
            "--repo",
            str(repo),
            "--release-root",
            str(release_root),
            "--skip-restart",
            "--skip-preflight",
            "--skip-identity",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (release_root / sha / "README").is_file()
    assert current.resolve() == (release_root / sha).resolve()


def test_report_deploy_identity_writes_no_secrets(tmp_path, monkeypatch):
    from jobs.report_deploy_identity import build_record, write_record

    monkeypatch.setenv("DASHBOARD_READONLY_URL", "postgresql://dashboard_readonly:secret@127.0.0.1/fmp")
    monkeypatch.delenv("DASHBOARD_ALLOW_WRITER_FALLBACK", raising=False)
    monkeypatch.delenv("STREAMLIT_ALLOW_PROVIDER_FETCH", raising=False)
    for key in (
        "DATABASE_URL",
        "MARKET_INTELLIGENCE_DATABASE_URL",
        "DB_PASSWORD",
        "DB_HOST",
        "DB_USER",
        "DB_NAME",
        "DB_PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FMP_DASHBOARD_READONLY_PW", str(tmp_path / "missing.pw"))
    monkeypatch.setenv("FMP_CURRENT_LINK", str(tmp_path / "missing-current"))
    monkeypatch.setenv("FMP_SYSTEMD_EXEC_START", "/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py")
    path = tmp_path / "current.json"
    record = build_record(
        sha="abc123",
        checkout="/root/FMP_SCREENER",
        mode="git_pull",
        immutable_rc=0,
        verify_rc=3,
    )
    write_record(record, path)
    text = path.read_text(encoding="utf-8")
    assert "secret" not in text
    assert "postgresql://" not in text
    assert record["dashboard_readonly_url_set"] is True
    assert record["dashboard_readonly_password_file_present"] is False
    assert record["writer_fallback"] is False
    assert record["writer_env_keys_present"] == []
    assert record["provider_fetch"] is False
    assert record["systemd_still_git_pull"] is True
    assert record["systemd_cutover_proven"] is False
    assert record["readonly_verify_rc"] == 3
    assert record["readonly_proven"] is False
    assert record["csfml_v1_label_integrity"] == "CANNOT_RULE_OUT"
    assert record["csfml_v1_rerun_authorized"] is False


def test_data_health_keeps_ops_off_main_pages():
    ui = (ROOT / "market_intelligence" / "pages_ui.py").read_text(encoding="utf-8")
    assert "Platform ops summary" in ui
    dashboard = (ROOT / "dashboard.py").read_text(encoding="utf-8")
    assert "Platform ops summary" not in dashboard
