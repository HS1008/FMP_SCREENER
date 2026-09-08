"""Deployment templates: installer dry-run is side-effect free, rendered units are complete, no secrets."""

from __future__ import annotations

import os
import re
import subprocess
import sys
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
    deploy_yml = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    assert "deploy/market_intelligence" not in deploy_yml
    assert "fmp-ibkr-ingest.service" in deploy_yml


# ---- operational verification: DST, unit semantics, secrets in process lines, CI triggers -------------------

def _systemd_analyze() -> str | None:
    import shutil

    return shutil.which("systemd-analyze")


@pytest.mark.parametrize(
    "base_time, spec, expected_utc",
    [
        # Standard time (EST, UTC-5): 09:15 New York = 14:15 UTC.
        ("2026-03-06 00:00:00 UTC", "Mon..Fri 09:15 America/New_York", "2026-03-06 14:15:00 UTC"),
        # After the 2026-03-08 spring-forward (EDT, UTC-4): 09:15 New York = 13:15 UTC.
        ("2026-03-09 00:00:00 UTC", "Mon..Fri 09:15 America/New_York", "2026-03-09 13:15:00 UTC"),
        # Before/after the 2026-11-01 fall-back for the evening run.
        ("2026-10-30 00:00:00 UTC", "Mon..Fri 18:30 America/New_York", "2026-10-30 22:30:00 UTC"),
        ("2026-11-02 00:00:00 UTC", "Mon..Fri 18:30 America/New_York", "2026-11-02 23:30:00 UTC"),
        # Weekend skipped: Saturday base rolls to Monday.
        ("2026-09-12 00:00:00 UTC", "Mon..Fri 09:15 America/New_York", "2026-09-14 13:15:00 UTC"),
    ],
)
def test_timer_calendar_follows_new_york_dst_and_skips_weekends(base_time, spec, expected_utc):
    tool = _systemd_analyze()
    if tool is None:
        pytest.skip("systemd-analyze not available; OnCalendar DST semantics unverified on this host")
    timer = (TEMPLATES / "fmp-mi-refresh.timer").read_text()
    assert "OnCalendar={0}".format(spec) in timer
    out = subprocess.run([tool, "calendar", "--base-time={0}".format(base_time), "--iterations=1", spec], capture_output=True, text=True, check=True, env={**os.environ, "TZ": "UTC"}).stdout
    match = re.search(r"Next elapse:\s+\w+ (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC)", out)
    assert match, out
    assert match.group(1) == expected_utc


def test_rendered_units_pass_systemd_verify_and_encode_lock_restart_and_port_semantics(fake_host):
    out = _run("--apply", "--with-api", "--no-systemctl", "--root", str(fake_host["root"]), "--user", "svc", "--env-file", str(fake_host["env"]), "--systemd-dir", str(fake_host["systemd"]), cwd=ROOT)
    assert out.returncode == 0, out.stderr
    refresh = (fake_host["systemd"] / "fmp-mi-refresh.service").read_text()
    api = (fake_host["systemd"] / "fmp-ai-context-api.service").read_text()
    timer = (fake_host["systemd"] / "fmp-mi-refresh.timer").read_text()
    # Overlap: a oneshot unit cannot be started twice concurrently by systemd, and the job itself
    # takes the shared PostgreSQL advisory lock (exit 75 = contention, treated as success for the unit).
    assert "Type=oneshot" in refresh and "SuccessExitStatus=0 2 75" in refresh and "--all-configured" in refresh
    assert "TimeoutStartSec=" in refresh and "Restart=" not in refresh  # scheduled job: the timer re-runs it, no restart loop
    # API: localhost only on the documented port, restarts on failure, read-only filesystem, no capabilities.
    assert "Environment=AI_CONTEXT_API_HOST=127.0.0.1" in api and "Environment=AI_CONTEXT_API_PORT=8765" in api
    assert "Restart=on-failure" in api and "ProtectSystem=strict" in api and "CapabilityBoundingSet=" in api
    # Secrets come from the protected EnvironmentFile; no credential appears on any command line.
    for unit in (refresh, api, timer):
        assert "EnvironmentFile=-{0}".format(fake_host["env"]) in unit or unit is timer
        for line in unit.splitlines():
            if line.startswith("ExecStart="):
                assert not re.search(r"(password|token|api_key|postgres(ql)?://)", line, flags=re.I), line
    assert "Unit=fmp-mi-refresh.service" in timer and "Persistent=true" in timer
    tool = _systemd_analyze()
    if tool is None:
        pytest.skip("systemd-analyze not available; unit file verification skipped on this host")
    verify = subprocess.run([tool, "verify", *(str(p) for p in sorted(fake_host["systemd"].glob("fmp-*")))], capture_output=True, text=True, check=False)
    problems = [l for l in (verify.stdout + verify.stderr).splitlines() if l.strip() and "fmp-" in l and "Failed to" in l]
    assert verify.returncode == 0 and not problems, verify.stdout + verify.stderr


def test_env_example_never_suggests_a_password_on_the_command_line():
    example = (TEMPLATES / "market_intelligence.env.example").read_text()
    assert "ro_password=\"$(cat " in example  # protected-file form
    for line in example.splitlines():
        assert not re.search(r"-v\s+\w*password\w*='", line), line  # literal password in argv


def _yaml_without_comments(path: Path) -> str:
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))


def test_pr_validation_workflow_is_secretless_and_uses_only_a_disposable_database():
    workflow = _yaml_without_comments(ROOT / ".github" / "workflows" / "pr_validation.yml")
    assert "pull_request_target" not in workflow
    assert re.search(r"^on:\n  pull_request:\n  workflow_dispatch:", workflow, flags=re.M)
    assert "secrets." not in workflow and "DO_SSH_KEY" not in workflow and "ssh " not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "image: postgres:16" in workflow and "MI_REQUIRE_DB_TESTS: \"1\"" in workflow
    assert "127.0.0.1:5432" in workflow and "digitalocean" not in workflow.lower()
    assert "apply_migrations" in workflow and "second apply must be a no-op" in workflow
    # The deployment workflow is untouched by validation and still the only path that mutates production.
    deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    assert "pull_request" not in deploy and "branches:\n      - main" in deploy


def test_no_workflow_runs_untrusted_pr_code_with_credentials():
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        text = _yaml_without_comments(path)
        assert "pull_request_target" not in text, path.name
        if "pull_request:" in text:
            assert "secrets." not in text, "{0}: pull_request workflows must not consume secrets".format(path.name)


def test_fred_validation_workflow_is_manual_only_and_receives_the_fred_secret_explicitly():
    raw = (ROOT / ".github" / "workflows" / "fred_validation.yml").read_text()
    text = _yaml_without_comments(ROOT / ".github" / "workflows" / "fred_validation.yml")
    assert "pull_request:" not in text and "pull_request_target" not in text
    assert "schedule:" not in text and "workflow_run:" not in text
    assert "workflow_dispatch:" in text
    if "push:" in text:
        assert "cursor/market-intelligence-v1-674b" in raw
        assert "fred_validation.yml" in raw
    assert "secrets.FRED_API_KEY" in raw
    assert "image: postgres:16" in text and "127.0.0.1:5432" in text
    assert "jobs.validate_fred_live" in text
    assert "digitalocean" not in text.lower()
    assert "QC_API_TOKEN" in text and 'QC_API_TOKEN: ""' in text
    pr = _yaml_without_comments(ROOT / ".github" / "workflows" / "pr_validation.yml")
    assert "secrets.FRED_API_KEY" not in pr and 'FRED_API_KEY: ""' in pr


def test_mi_host_workflows_are_not_pull_request_and_do_not_print_secrets():
    for name in ("mi_host_preflight.yml", "mi_production_activate.yml"):
        raw = (ROOT / ".github" / "workflows" / name).read_text()
        text = _yaml_without_comments(ROOT / ".github" / "workflows" / name)
        assert "pull_request:" not in text and "pull_request_target" not in text
        assert "printf '%s" not in raw or "FRED_API_KEY" in raw  # key written to a 0600 file, never echoed
        assert "echo \"$FRED" not in raw and "echo $FRED" not in raw


def test_activate_host_script_uses_admin_or_peer_for_role_sql():
    text = (ROOT / "scripts" / "activate_market_intelligence_host.sh").read_text()
    assert "MI_ADMIN_DATABASE_URL" in text
    assert "ADMIN_DATABASE_URL" in text
    assert "sudo -n -u postgres" in text
    assert "writer_host_kind" in text
    assert "postgres_peer" in text
    assert "--phase probe" in text
    assert '-f -' in text
    for line in text.splitlines():
        if "market_intelligence_readonly.sql" in line:
            assert "DATABASE_URL" not in line
            assert "DB_USER" not in line


def test_activate_workflow_installs_fixed_script_and_keeps_existing_secrets():
    raw = (ROOT / ".github" / "workflows" / "mi_production_activate.yml").read_text()
    text = _yaml_without_comments(ROOT / ".github" / "workflows" / "mi_production_activate.yml")
    assert "actions/checkout@v4" in raw
    assert "activate_market_intelligence_host.sh" in raw
    assert "--phase probe" in raw
    assert "existing secret files are not overwritten" in raw
    assert "eb20bb84209c1a1aa1896063d91803b6eb2bd591" not in raw
    assert "pull_request:" not in text


def test_validate_fred_live_refuses_production_urls_and_missing_config(monkeypatch, capsys):
    from jobs.validate_fred_live import EXIT_CONFIG, EXIT_REFUSED, run

    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("FMP_TEST_DATABASE_URL", raising=False)
    assert run([]) == EXIT_CONFIG
    monkeypatch.setenv("FRED_API_KEY", "test-fred-key-not-real")
    monkeypatch.setenv("FMP_TEST_DATABASE_URL", "postgresql://user:pw@db.ondigitalocean.com:25060/fmp")
    assert run([]) == EXIT_REFUSED
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "test-fred-key-not-real" not in combined


def test_update_protected_env_preserves_other_keys_and_does_not_print_the_value(tmp_path):
    script = ROOT / "scripts" / "update_protected_env.py"
    env_file = tmp_path / "app.env"
    env_file.write_text("OTHER=keep\nDATABASE_READONLY_URL=CHANGE_ME\n")
    os.chmod(env_file, 0o600)
    value = tmp_path / "secret"
    value.write_text("generated-readonly-url-not-real\n")
    os.chmod(value, 0o600)
    out = subprocess.run([sys.executable, str(script), "--env-file", str(env_file), "--key", "DATABASE_READONLY_URL", "--value-file", str(value)], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    text = env_file.read_text()
    assert "OTHER=keep" in text
    assert "DATABASE_READONLY_URL=generated-readonly-url-not-real" in text
    assert "generated-readonly-url-not-real" not in out.stdout


def test_digitalocean_secret_script_is_dry_run_by_default_and_never_activates(tmp_path):
    script = ROOT / "scripts" / "provision_digitalocean_mi_secrets.sh"
    env_file = tmp_path / "market_intelligence.env"
    env_file.write_text("FRED_API_KEY=CHANGE_ME\nDATABASE_URL=postgresql://writer:keep@127.0.0.1/fmp\n")
    os.chmod(env_file, 0o600)
    key = tmp_path / "fred_api_key"
    key.write_text("not-a-real-fred-key\n")
    os.chmod(key, 0o600)
    dry = subprocess.run(["bash", str(script), "--env-file", str(env_file), "--fred-key-file", str(key), "--root", str(ROOT)], capture_output=True, text=True, check=False)
    assert dry.returncode == 0, dry.stderr
    assert "DRY RUN" in dry.stdout and "will NOT" in dry.stdout
    assert env_file.read_text() == "FRED_API_KEY=CHANGE_ME\nDATABASE_URL=postgresql://writer:keep@127.0.0.1/fmp\n"
    assert "not-a-real-fred-key" not in dry.stdout
    applied = subprocess.run(["bash", str(script), "--apply", "--env-file", str(env_file), "--fred-key-file", str(key), "--root", str(ROOT)], capture_output=True, text=True, check=False)
    assert applied.returncode == 0, applied.stderr
    text = env_file.read_text()
    assert "FRED_API_KEY=not-a-real-fred-key" in text
    assert "DATABASE_URL=postgresql://writer:keep@127.0.0.1/fmp" in text
    assert "systemctl" not in applied.stdout and "not-a-real-fred-key" not in applied.stdout
