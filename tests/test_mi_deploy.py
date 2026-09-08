"""Deployment templates: installer dry-run is side-effect free, rendered units are complete, no secrets."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_market_intelligence_timers.sh"
TEMPLATES = ROOT / "deploy" / "market_intelligence"


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, check=False)


@pytest.fixture
def fake_host(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "FMP_SCREENER"
    (root / "venv" / "bin").mkdir(parents=True)
    python = root / "venv" / "bin" / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    env_file = tmp_path / "market_intelligence.env"
    env_file.write_text("FRED_API_KEY=placeholder\n")
    os.chmod(env_file, 0o600)
    return {"root": root, "systemd": tmp_path / "systemd", "env": env_file}


def test_dry_run_is_default_and_writes_nothing(fake_host):
    result = _run("--root", str(fake_host["root"]), "--systemd-dir", str(fake_host["systemd"]), "--env-file", str(fake_host["env"]), "--with-api", cwd=ROOT)
    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert not fake_host["systemd"].exists()
    assert "__ROOT__" not in result.stdout.replace("placeholders: ", "")
    assert "fmp-ai-context-api.service" in result.stdout
    assert "systemctl enable --now fmp-mi-refresh.timer" in result.stdout


def test_apply_renders_units_idempotently_and_never_touches_other_units(fake_host):
    fake_host["systemd"].mkdir()
    other = fake_host["systemd"] / "fmp-dashboard.service"
    other.write_text("[Unit]\nDescription=existing dashboard\n")
    args = ["--root", str(fake_host["root"]), "--systemd-dir", str(fake_host["systemd"]), "--env-file", str(fake_host["env"]), "--with-api", "--apply", "--no-systemctl"]
    first = _run(*args, cwd=ROOT)
    assert first.returncode == 0, first.stderr
    written = sorted(p.name for p in fake_host["systemd"].iterdir())
    assert written == ["fmp-ai-context-api.service", "fmp-dashboard.service", "fmp-mi-refresh.service", "fmp-mi-refresh.timer"]
    assert other.read_text() == "[Unit]\nDescription=existing dashboard\n"
    service = (fake_host["systemd"] / "fmp-mi-refresh.service").read_text()
    assert "__" not in re.sub(r"^#.*$", "", service, flags=re.M)
    assert "ExecStart={0}/venv/bin/python -m jobs.market_intelligence_refresh --all-configured --json".format(fake_host["root"]) in service
    assert "EnvironmentFile=-{0}".format(fake_host["env"]) in service
    api = (fake_host["systemd"] / "fmp-ai-context-api.service").read_text()
    assert "--host ${AI_CONTEXT_API_HOST}" in api and "AI_CONTEXT_API_HOST=127.0.0.1" in api
    second = _run(*args, cwd=ROOT)
    assert second.stdout.count("(unchanged)") == 3


def test_timer_schedule_is_weekday_new_york_and_persistent():
    timer = (TEMPLATES / "fmp-mi-refresh.timer").read_text()
    assert timer.count("OnCalendar=Mon..Fri") == 2
    assert "America/New_York" in timer and "Persistent=true" in timer


def test_env_example_has_only_placeholders_and_documents_every_consumed_variable():
    example = (TEMPLATES / "market_intelligence.env.example").read_text()
    assigned = re.findall(r"^#?\s*([A-Z_]+)=(.*)$", example, flags=re.M)
    assert assigned
    for name, value in assigned:
        cleaned = value.split("#")[0].strip().strip('"')
        assert cleaned in {"", "0", "127.0.0.1", "8765", "5432", "fmp", "fmp_writer", "FMP Research ops@example.com", "/root/FMP_SCREENER/outputs/precomputed"} or "CHANGE_ME" in cleaned, (name, value)
    names = {n for n, _ in assigned}
    for required in ("FRED_API_KEY", "DATABASE_READONLY_URL", "AI_CONTEXT_API_TOKEN", "SEC_USER_AGENT", "MI_EDGAR_ENABLED", "MI_TRACE_ENABLED", "MARKET_INTELLIGENCE_DATABASE_URL"):
        assert required in names


def test_units_reference_real_entrypoints():
    refresh = (TEMPLATES / "fmp-mi-refresh.service").read_text()
    assert (ROOT / "jobs" / "market_intelligence_refresh.py").exists() and "jobs.market_intelligence_refresh" in refresh
    api = (TEMPLATES / "fmp-ai-context-api.service").read_text()
    assert (ROOT / "ai_context_api.py").exists() and "ai_context_api:app" in api
    assert "SuccessExitStatus=0 2 75" in refresh
    assert "deploy/market_intelligence" not in (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
