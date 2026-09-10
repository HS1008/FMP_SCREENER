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
    api_env = tmp_path / "ai_context_api.env"
    api_env.write_text("AI_CONTEXT_API_TOKEN=placeholder\nDATABASE_READONLY_URL=postgresql://mi_readonly:CHANGE_ME@127.0.0.1:5432/fmp\n")
    os.chmod(api_env, 0o600)
    return {"root": root, "systemd": tmp_path / "systemd", "env": env_file, "api_env": api_env}


def test_dry_run_is_default_and_writes_nothing(fake_host):
    result = _run("--root", str(fake_host["root"]), "--systemd-dir", str(fake_host["systemd"]), "--env-file", str(fake_host["env"]), "--api-env-file", str(fake_host["api_env"]), "--with-api", cwd=ROOT)
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
    args = ["--root", str(fake_host["root"]), "--systemd-dir", str(fake_host["systemd"]), "--env-file", str(fake_host["env"]), "--api-env-file", str(fake_host["api_env"]), "--with-api", "--apply", "--no-systemctl"]
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
    assert "EnvironmentFile=-{0}".format(fake_host["api_env"]) in api
    assert "FRED_API_KEY" not in api
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
    for required in ("FRED_API_KEY", "DATABASE_READONLY_URL", "AI_CONTEXT_API_TOKEN", "SEC_USER_AGENT", "MI_EDGAR_ENABLED", "MI_TRACE_ENABLED", "MI_FINRA_ENABLED", "FINRA_CLIENT_ID", "MARKET_INTELLIGENCE_DATABASE_URL"):
        assert required in names


def test_units_reference_real_entrypoints():
    refresh = (TEMPLATES / "fmp-mi-refresh.service").read_text()
    assert (ROOT / "jobs" / "market_intelligence_refresh.py").exists() and "jobs.market_intelligence_refresh" in refresh
    api = (TEMPLATES / "fmp-ai-context-api.service").read_text()
    assert (ROOT / "ai_context_api.py").exists() and "ai_context_api:app" in api
    assert "SuccessExitStatus=0 2 75" in refresh
    deploy_yml = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    host = (ROOT / "scripts" / "deploy_host.sh").read_text()
    assert "deploy/market_intelligence" not in deploy_yml
    assert "fmp-ibkr-ingest.service" in host
    assert "fmp_backups/checkout_preserve" in host
    assert "git checkout --" in host
    assert "git reset --hard" not in host
    assert "git clean -" not in host
    assert "git reset --hard" not in deploy_yml
    assert "git clean -" not in deploy_yml


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
    out = _run("--apply", "--with-api", "--no-systemctl", "--root", str(fake_host["root"]), "--user", "svc", "--env-file", str(fake_host["env"]), "--api-env-file", str(fake_host["api_env"]), "--systemd-dir", str(fake_host["systemd"]), cwd=ROOT)
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
        if unit is api:
            assert "EnvironmentFile=-{0}".format(fake_host["api_env"]) in unit
            assert "EnvironmentFile=-{0}".format(fake_host["env"]) not in unit
        elif unit is refresh:
            assert "EnvironmentFile=-{0}".format(fake_host["env"]) in unit
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


def test_finra_validation_workflow_is_manual_or_feature_branch_and_receives_finra_secrets_explicitly():
    raw = (ROOT / ".github" / "workflows" / "finra_validation.yml").read_text()
    text = _yaml_without_comments(ROOT / ".github" / "workflows" / "finra_validation.yml")
    assert "pull_request:" not in text and "pull_request_target" not in text
    assert "schedule:" not in text and "workflow_run:" not in text
    assert "workflow_dispatch:" in text
    assert "cursor/order-flow-finra-ibkr-674b" in raw
    assert "secrets.FINRA_CLIENT_ID" in raw and "secrets.FINRA_CLIENT_SECRET" in raw
    assert "image: postgres:16" in text and "127.0.0.1:5432" in text
    assert "jobs.validate_finra_live" in text
    assert "digitalocean" not in text.lower()
    assert "traqs" not in text.lower()
    pr = _yaml_without_comments(ROOT / ".github" / "workflows" / "pr_validation.yml")
    assert "secrets.FINRA_CLIENT_ID" not in pr and "secrets.FINRA_CLIENT_SECRET" not in pr


def test_validate_finra_live_refuses_production_urls_and_missing_config(monkeypatch, capsys):
    from jobs.validate_finra_live import EXIT_CONFIG, EXIT_REFUSED, run

    monkeypatch.delenv("FINRA_CLIENT_ID", raising=False)
    monkeypatch.delenv("FINRA_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("FINRA_API_CLIENT_ID", raising=False)
    monkeypatch.delenv("FINRA_API_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("FMP_TEST_DATABASE_URL", raising=False)
    assert run([]) == EXIT_CONFIG
    monkeypatch.setenv("FINRA_CLIENT_ID", "test-finra-id-not-real")
    monkeypatch.setenv("FINRA_CLIENT_SECRET", "test-finra-secret-not-real")
    monkeypatch.setenv("FMP_TEST_DATABASE_URL", "postgresql://user:pw@db.ondigitalocean.com:25060/fmp")
    assert run([]) == EXIT_REFUSED
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "test-finra-id-not-real" not in combined
    assert "test-finra-secret-not-real" not in combined


def test_ai_context_env_example_has_no_provider_secrets():
    example = (TEMPLATES / "ai_context_api.env.example").read_text()
    assert "FRED_API_KEY" not in example
    assert "FINRA_CLIENT" not in example
    assert "IBKR_INGEST" not in example
    assert "MARKET_INTELLIGENCE_DATABASE_URL" not in example
    assert "AI_CONTEXT_API_TOKEN" in example
    assert "DATABASE_READONLY_URL" in example


def test_mi_host_workflows_are_not_pull_request_and_do_not_print_secrets():
    for name in ("mi_host_preflight.yml", "mi_production_activate.yml", "mi_research_workspace_verify.yml"):
        raw = (ROOT / ".github" / "workflows" / name).read_text()
        text = _yaml_without_comments(ROOT / ".github" / "workflows" / name)
        assert "pull_request:" not in text and "pull_request_target" not in text
        assert "printf '%s" not in raw or "FRED_API_KEY" in raw  # key written to a 0600 file, never echoed
        assert "echo \"$FRED" not in raw and "echo $FRED" not in raw
    verify = (ROOT / ".github" / "workflows" / "mi_research_workspace_verify.yml").read_text()
    assert "refresh_journal_sanitized" in verify
    assert "refresh_env_writer_url" in verify
    assert "activate_market_intelligence_host.sh" in verify
    assert "market_intelligence.env" in verify
    assert "stale_source=" in verify
    assert "failed_source=" in verify
    assert "ibkr_quote_code=" in verify
    assert "Deploy FMP Dashboard" in verify
    assert "cursor/mi-research-verify-674b" not in verify
    assert "latest_observation_date" not in verify.split("python - <<'PY'", 1)[-1].split("PY", 1)[0]


def test_mi_research_workspace_identity_report_does_not_source_writer_checkout_env():
    verify = (ROOT / ".github" / "workflows" / "mi_research_workspace_verify.yml").read_text()
    prefix = verify.split("Re-AppTest and report sanitized source/identity status", 1)[1]
    prefix = prefix.split("python - <<'PY'", 1)[0]
    assert "/etc/fmp/market_intelligence.env" in prefix
    assert "unset DATABASE_URL" in prefix
    assert "MARKET_INTELLIGENCE_DATABASE_URL" in prefix
    assert "source /root/FMP_SCREENER/.env" not in prefix
    assert ". /root/FMP_SCREENER/.env" not in prefix
    preflight = (ROOT / ".github" / "workflows" / "mi_host_preflight.yml").read_text()
    assert "source /root/FMP_SCREENER/.env" in preflight


def test_activate_verify_phase_does_not_source_writer_checkout_env():
    text = (ROOT / "scripts" / "activate_market_intelligence_host.sh").read_text()
    verify = text.split("phase_verify()", 1)[1].split("sanitize_unit_journal", 1)[0]
    assert "load_writer_env" not in verify
    assert 'source "$ENV_FILE"' in verify
    assert "unset DATABASE_URL" in verify
    assert "unset MARKET_INTELLIGENCE_DATABASE_URL" in verify or "MARKET_INTELLIGENCE_DATABASE_URL" in verify
    assert "FMP_IDENTITY_ENV_ONLY=1" in verify
    assert "verify_mi_dashboard" in verify
    assert "verify_dashboard_identity.sh" in verify
    ingest = text.split("phase_ingest_fred()", 1)[1].split("phase_ingest_finra()", 1)[0]
    assert "load_writer_env" in ingest
    mi_verify = (ROOT / "jobs" / "verify_mi_dashboard.py").read_text()
    assert "load_streamlit_env" in mi_verify


def test_activate_ingest_unsets_streamlit_identity_after_writer_env():
    text = (ROOT / "scripts" / "activate_market_intelligence_host.sh").read_text()
    block = text.split("load_writer_env() {", 1)[1].split("writer_db_meta()", 1)[0]
    unset = (
        "unset FMP_STREAMLIT_READONLY STREAMLIT_ALLOW_PROVIDER_FETCH "
        "DASHBOARD_ALLOW_WRITER_FALLBACK"
    )
    assert unset in block
    assert block.index('source "$DASHBOARD_ENV"') < block.index(unset)
    assert block.index('source "$ENV_FILE"') < block.index(unset)
    ingest = text.split("phase_ingest_fred()", 1)[1].split("phase_ingest_finra()", 1)[0]
    assert "load_writer_env" in ingest
    assert ingest.index("load_writer_env") < ingest.index("jobs.market_intelligence_refresh")


def test_mi_writer_engine_refuses_streamlit_only_when_writer_url_present(monkeypatch):
    from market_intelligence.writer_db import WriterConfigurationError, writer_engine
    from qc_research.platform_ingest import StreamlitIngestRefused

    monkeypatch.setenv("FMP_STREAMLIT_READONLY", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://writer:secret@127.0.0.1:5432/fmp")

    def _boom(*_args, **_kwargs):
        raise AssertionError("Streamlit identity must not create an MI writer engine")

    monkeypatch.setattr("sqlalchemy.create_engine", _boom)
    with pytest.raises(StreamlitIngestRefused, match="Streamlit read-only"):
        writer_engine()
    with pytest.raises(StreamlitIngestRefused, match="Streamlit read-only"):
        writer_engine("postgresql://writer:secret@127.0.0.1:5432/fmp")

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MARKET_INTELLIGENCE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_NAME", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    with pytest.raises(WriterConfigurationError, match="No writer database configured"):
        writer_engine()


def test_activate_host_script_uses_admin_or_peer_for_role_sql():
    text = (ROOT / "scripts" / "activate_market_intelligence_host.sh").read_text()
    assert "MI_ADMIN_DATABASE_URL" in text
    assert "ADMIN_DATABASE_URL" in text
    assert "sudo -n -u postgres" in text
    assert "writer_host_kind" in text
    assert "postgres_peer" in text
    assert "--phase probe" in text
    assert "run_with_heartbeat" in text
    assert "ingest-analytics" in text
    assert "ingest-finra" in text
    assert "ai_context_api.env" in text
    assert "materialize_ai_context_env.py" in text
    assert "materialize_mi_writer_url.py" in text
    assert "writer_url_source" in text
    assert "scripts/provision_dashboard_readonly.sh" in text
    assert "dashboard_readonly_pw_file=" in text
    assert "wait_for_local_api" in text
    assert "127.0.0.1:8765/health" in text
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
    assert "ServerAliveInterval 15" in raw
    assert "ingest-finra" in raw
    assert "FINRA_CLIENT_ID" in raw
    assert "cursor/order-flow-activate-674b" in raw
    assert "[schedule-only]" in raw
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


def test_materialize_mi_writer_url_from_db_star_and_never_prints_secret(tmp_path, monkeypatch, capsys):
    from scripts.materialize_mi_writer_url import main, resolve_writer_url

    monkeypatch.delenv("MARKET_INTELLIGENCE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_NAME", "quant_monitor")
    monkeypatch.setenv("DB_USER", "fmp_writer")
    monkeypatch.setenv("DB_PASSWORD", "not-a-real-writer-password")
    out = tmp_path / "writer.url"
    assert main(["--output", str(out)]) == 0
    captured = capsys.readouterr()
    assert "writer_url_source=db_star" in captured.out
    assert "not-a-real-writer-password" not in captured.out
    text = out.read_text()
    assert text.startswith("postgresql://")
    assert "quant_monitor" in text
    assert "not-a-real-writer-password" in text
    url, source = resolve_writer_url({
        "MARKET_INTELLIGENCE_DATABASE_URL": "postgresql://writer:CHANGE_ME@127.0.0.1:5432/fmp",
        "DATABASE_URL": "postgresql://writer:from-database-url@127.0.0.1:5432/fmp",
    })
    assert source == "database_url" and "from-database-url" in url
    url, source = resolve_writer_url({"MARKET_INTELLIGENCE_DATABASE_URL": "postgresql://writer:keep@127.0.0.1:5432/fmp"})
    assert source == "dedicated" and url.endswith("/fmp")
    assert resolve_writer_url({}) == ("", "missing")


def test_update_protected_env_scrubs_streamlit_writer_keys_without_printing(tmp_path):
    script = ROOT / "scripts" / "update_protected_env.py"
    env_file = tmp_path / "fmp-dashboard.env"
    env_file.write_text(
        "FMP_API_KEY=keep-me\n"
        "DATABASE_URL=postgresql://writer:secret@127.0.0.1/fmp\n"
        "DB_USER=writer\n"
        "STREAMLIT_ALLOW_PROVIDER_FETCH=1\n"
        "DASHBOARD_READONLY_URL=postgresql://dashboard_readonly:x@127.0.0.1/fmp\n",
        encoding="utf-8",
    )
    os.chmod(env_file, 0o600)
    out = subprocess.run(
        [sys.executable, str(script), "--env-file", str(env_file), "--scrub-streamlit-writer"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    text = env_file.read_text(encoding="utf-8")
    assert "FMP_API_KEY=keep-me" in text
    assert "DASHBOARD_READONLY_URL=postgresql://dashboard_readonly:x@127.0.0.1/fmp" in text
    assert "DATABASE_URL=" not in text
    assert "DB_USER=" not in text
    assert "STREAMLIT_ALLOW_PROVIDER_FETCH=" not in text
    assert "FMP_STREAMLIT_READONLY=1" in text
    assert "secret" not in out.stdout
    assert "writer_keys_removed=" in out.stdout
    assert "DATABASE_URL" in out.stdout
    assert "STREAMLIT_ALLOW_PROVIDER_FETCH" in out.stdout


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
