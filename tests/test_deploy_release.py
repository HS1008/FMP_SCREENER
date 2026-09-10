"""Immutable release script exists beside the live git-pull deploy."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_script_is_additive_and_supports_rollback():
    script = (ROOT / "scripts" / "deploy_release.sh").read_text(encoding="utf-8")
    assert "/opt/fmp/releases" in script
    assert "--rollback" in script
    assert "git reset --hard" not in script
    deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "git pull --ff-only origin main" in deploy
    assert (ROOT / "docs" / "IMMUTABLE_DEPLOY.md").is_file()


def test_data_health_keeps_ops_off_main_pages():
    ui = (ROOT / "market_intelligence" / "pages_ui.py").read_text(encoding="utf-8")
    assert "Platform ops summary" in ui
    dashboard = (ROOT / "dashboard.py").read_text(encoding="utf-8")
    assert "Platform ops summary" not in dashboard
