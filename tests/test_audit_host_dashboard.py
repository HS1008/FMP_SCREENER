"""Host Streamlit identity audit records facts and never writes secrets."""

from __future__ import annotations

import json
from pathlib import Path

from jobs.audit_host_dashboard import (
    audit_exit_code,
    collect_facts,
    identity_env_from_file,
    write_facts,
)


def test_identity_env_from_file_strips_writer_keys_and_overlays_url(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fmp:secret@127.0.0.1/fmp")
    monkeypatch.setenv("DASHBOARD_READONLY_URL", "")
    env_file = tmp_path / "fmp-dashboard.env"
    env_file.write_text(
        "DASHBOARD_READONLY_URL=postgresql://dashboard_readonly:secret@127.0.0.1/fmp\n",
        encoding="utf-8",
    )
    environ = identity_env_from_file(env_file)
    assert environ["DASHBOARD_READONLY_URL"].startswith("postgresql://dashboard_readonly:")
    assert "DATABASE_URL" not in environ or not environ.get("DATABASE_URL")


def test_collect_facts_marks_unproven_root_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_READONLY_URL", raising=False)
    monkeypatch.delenv("DASHBOARD_ALLOW_WRITER_FALLBACK", raising=False)
    monkeypatch.delenv("STREAMLIT_ALLOW_PROVIDER_FETCH", raising=False)
    facts = collect_facts(
        env={},
        password_file=tmp_path / "missing.pw",
        current_link=tmp_path / "missing-current",
        systemd_exec="/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py",
        verify_rc=3,
    )
    assert facts["readonly_proven"] is False
    assert facts["systemd_cutover_proven"] is False
    assert facts["systemd_exec_contains_root_checkout"] is True
    assert facts["csfml_v1_label_integrity"] == "CANNOT_RULE_OUT"
    assert facts["csfml_v1_rerun_authorized"] is False
    assert audit_exit_code(facts, require_readonly=True) == 3
    assert audit_exit_code(facts, require_readonly=False) == 0


def test_collect_facts_proves_readonly_without_cutover(tmp_path):
    pw = tmp_path / "dashboard_readonly.pw"
    pw.write_text("x\n", encoding="utf-8")
    facts = collect_facts(
        env={"DASHBOARD_READONLY_URL": "postgresql://dashboard_readonly:secret@127.0.0.1/fmp"},
        password_file=pw,
        current_link=tmp_path / "missing-current",
        systemd_exec="/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py",
        verify_rc=0,
    )
    assert facts["readonly_proven"] is True
    assert facts["dashboard_readonly_url_set"] is True
    assert facts["systemd_cutover_proven"] is False
    assert audit_exit_code(facts, require_readonly=True) == 0
    path = tmp_path / "host_audit.json"
    write_facts(facts, path)
    text = path.read_text(encoding="utf-8")
    assert "secret" not in text
    assert "postgresql://" not in text
    payload = json.loads(text)
    assert payload["readonly_proven"] is True


def test_audit_refuses_writer_keys_in_process_env(tmp_path):
    pw = tmp_path / "dashboard_readonly.pw"
    pw.write_text("x\n", encoding="utf-8")
    facts = collect_facts(
        env={
            "DASHBOARD_READONLY_URL": "postgresql://dashboard_readonly:secret@127.0.0.1/fmp",
            "DATABASE_URL": "postgresql://fmp:secret@127.0.0.1/fmp",
        },
        password_file=pw,
        systemd_exec="/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py",
        verify_rc=0,
    )
    assert facts["readonly_proven"] is False
    assert facts["writer_env_keys_present"] == ["DATABASE_URL"]
    assert audit_exit_code(facts, require_readonly=True) == 4
    path = tmp_path / "host_audit.json"
    write_facts(facts, path)
    text = path.read_text(encoding="utf-8")
    assert "secret" not in text
    assert "postgresql://" not in text
    assert "DATABASE_URL" in text


def test_audit_refuses_writer_fallback_and_provider_fetch():
    writer = collect_facts(
        env={"DASHBOARD_ALLOW_WRITER_FALLBACK": "1"},
        systemd_exec="",
        verify_rc=0,
    )
    assert audit_exit_code(writer, require_readonly=False) == 4
    provider = collect_facts(
        env={"STREAMLIT_ALLOW_PROVIDER_FETCH": "true"},
        systemd_exec="",
        verify_rc=0,
    )
    assert audit_exit_code(provider, require_readonly=False) == 5


def test_systemd_cutover_requires_opt_fmp_current_only():
    facts = collect_facts(
        env={"DASHBOARD_READONLY_URL": "postgresql://dashboard_readonly:x@127.0.0.1/fmp"},
        systemd_exec="/opt/fmp/current/venv/bin/streamlit run dashboard.py",
        verify_rc=0,
        password_file=Path("/nonexistent"),
    )
    assert facts["systemd_cutover_proven"] is True
    assert facts["readonly_proven"] is False


def test_production_verify_workflow_runs_host_audit():
    text = Path(".github/workflows/platform_research_verify.yml").read_text(encoding="utf-8")
    assert "jobs.audit_host_dashboard" in text
    assert text.index("verify_tlt_monitor --live") < text.index("jobs.audit_host_dashboard")
    assert "qc_research.verify_csfml_v1 --live" in text
    assert "--require-present" not in text
    assert "--require-readonly" in text
    assert "--env-file /etc/fmp/fmp-dashboard.env" in text
    assert "jobs or change systemd" in text
    assert "source /root/FMP_SCREENER/.env" not in text
    assert ". /root/FMP_SCREENER/.env" not in text
    assert text.count("/etc/fmp/fmp-dashboard.env") >= 2
    assert "--dry-run" in text
    ingest = text.split("ingest_platform_artifacts", 1)[1].split("verify_tlt_monitor", 1)[0]
    assert "--dry-run" in ingest
    assert "--verify-monitor" in ingest
    assert "does not ingest or write PostgreSQL" in text
    assert "FMP_IDENTITY_ENV_ONLY=1" in text
    assert "FMP_DASHBOARD_ENV=/etc/fmp/fmp-dashboard.env" in text


def test_everyday_deploy_runs_host_audit_before_restart():
    deploy = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "jobs.audit_host_dashboard" in deploy
    assert "--require-readonly" in deploy
    assert "--verify-rc" in deploy
    assert "/var/lib/fmp/deploy/host_audit.json" in deploy
    assert deploy.index("jobs.report_deploy_identity") < deploy.index("jobs.audit_host_dashboard")
    assert deploy.index("jobs.audit_host_dashboard") < deploy.index("systemctl restart fmp-dashboard")
    assert "host dashboard audit failed" in deploy
    audit_block = deploy.split("Auditing host Streamlit identity", 1)[1].split(
        "Verifying official CSFML V1 identity", 1
    )[0]
    assert "/etc/fmp/fmp-dashboard.env" in audit_block
    assert "--env-file /etc/fmp/fmp-dashboard.env" in audit_block
    assert "unset DATABASE_URL" in audit_block
    assert "source /root/FMP_SCREENER/.env" not in audit_block
    assert ". /root/FMP_SCREENER/.env" not in audit_block
    assert "jobs.cutover_dashboard_systemd" in deploy
    assert deploy.index("jobs.cutover_dashboard_systemd") < deploy.index("systemctl restart fmp-dashboard")
    assert "--apply" not in deploy
