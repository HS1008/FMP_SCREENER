"""Running-service identity is separate from a configured read-only URL."""

from __future__ import annotations

import json
from pathlib import Path

from jobs.audit_host_dashboard import collect_facts, write_facts
from jobs.observe_running_dashboard import observe_running_dashboard, unproven_observation


def test_missing_host_is_unproven_not_a_pass():
    facts = unproven_observation()
    assert facts["observed_running_identity"] == "unproven"
    assert facts["observed_pid"] is None
    assert facts["observed_code_sha"] == "unproven"


def test_recorded_observation_uses_injected_unit_and_proc(tmp_path):
    proc = tmp_path / "proc" / "42"
    proc.mkdir(parents=True)
    (proc / "cwd").symlink_to(tmp_path / "opt" / "fmp" / "current")
    (proc / "exe").symlink_to(tmp_path / "venv" / "bin" / "python")
    (proc / "cmdline").write_text("streamlit\x00run\x00dashboard.py\x00", encoding="utf-8")
    (proc / "environ").write_bytes(b"FMP_STREAMLIT_READONLY=1\0DASHBOARD_READONLY_URL=secret\0HOME=/root\0")
    current = tmp_path / "releases" / ("a" * 40)
    current.mkdir(parents=True)
    link = tmp_path / "current"
    link.symlink_to(current)
    facts = observe_running_dashboard(
        unit="fmp-dashboard",
        proc_root=tmp_path / "proc",
        current_link=link,
        systemctl_properties={
            "MainPID": "42",
            "FragmentPath": "/etc/systemd/system/fmp-dashboard.service",
            "WorkingDirectory": "/opt/fmp/current",
            "ExecStart": "/opt/fmp/current/venv/bin/streamlit run dashboard.py",
            "EnvironmentFiles": "/etc/fmp/fmp-dashboard.env (ignore)",
        },
    )
    assert facts["observed_running_identity"] == "recorded"
    assert facts["observed_pid"] == 42
    assert facts["observed_cmdline_has_streamlit"] is True
    assert facts["observed_code_sha"] == "a" * 40
    assert facts["observed_env_file_paths"] == ["/etc/fmp/fmp-dashboard.env"]
    assert facts["observed_readonly_url_key_present"] is True
    assert facts["observed_streamlit_readonly_key_present"] is True
    assert facts["observed_writer_env_key_names_present"] == []
    text = json.dumps(facts)
    assert "secret" not in text
    assert "postgresql://" not in text


def test_audit_reports_configured_and_observed_separately(tmp_path):
    pw = tmp_path / "dashboard_readonly.pw"
    pw.write_text("x\n", encoding="utf-8")
    facts = collect_facts(
        env={"DASHBOARD_READONLY_URL": "postgresql://dashboard_readonly:secret@127.0.0.1/fmp", "FMP_STREAMLIT_READONLY": "1"},
        password_file=pw,
        current_link=tmp_path / "missing-current",
        systemd_exec="/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py",
        verify_rc=0,
        running_observation=unproven_observation(),
    )
    assert facts["readonly_proven"] is True
    assert facts["configured_readonly_url_set"] is True
    assert facts["observed_running_identity"] == "unproven"
    assert facts["running_service_identity_proven"] is False
    path = tmp_path / "host_audit.json"
    write_facts(facts, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "secret" not in path.read_text(encoding="utf-8")
    assert payload["observed_running_identity"] == "unproven"
    assert payload["readonly_proven"] is True


def test_cutover_apply_is_not_a_live_install():
    text = Path("jobs/cutover_dashboard_systemd.py").read_text(encoding="utf-8")
    assert "Does not switch the live unit" in text
    assert "refused_no_systemd_mutate" in text or "systemd_mutated" in text
