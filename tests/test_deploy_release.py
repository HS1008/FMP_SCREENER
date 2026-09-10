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
    assert "scripts/verify_dashboard_identity.sh" in deploy
    assert "scripts/verify_dashboard_identity.sh" in script
    assert "dashboard_readonly_verify_rc" not in deploy
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
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (release_root / sha / "README").is_file()
    assert current.resolve() == (release_root / sha).resolve()


def test_data_health_keeps_ops_off_main_pages():
    ui = (ROOT / "market_intelligence" / "pages_ui.py").read_text(encoding="utf-8")
    assert "Platform ops summary" in ui
    dashboard = (ROOT / "dashboard.py").read_text(encoding="utf-8")
    assert "Platform ops summary" not in dashboard
