"""Systemd cutover stays dry-run and fail-closed. Never mutates the live unit."""

from __future__ import annotations

import json
from pathlib import Path

from jobs.cutover_dashboard_systemd import (
    PROPOSED_UNIT,
    cutover_exit_code,
    evaluate_cutover,
    scan_systemd_env_file,
)


ROOT = Path(__file__).resolve().parents[1]


def _ready_tree(tmp_path: Path) -> dict[str, Path]:
    sha = "a" * 40
    release = tmp_path / "releases" / sha
    (release / "scripts").mkdir(parents=True)
    (release / "scripts" / "verify_dashboard_identity.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (release / "dashboard.py").write_text("# dashboard\n", encoding="utf-8")
    (release / "venv" / "bin").mkdir(parents=True)
    (release / "venv" / "bin" / "streamlit").write_text("#!/bin/sh\n", encoding="utf-8")
    (release / "qc_research").mkdir(parents=True)
    (release / "qc_research" / "ingest_platform_artifacts.py").write_text("# ingest\n", encoding="utf-8")
    current = tmp_path / "current"
    current.symlink_to(release)
    pw = tmp_path / "dashboard_readonly.pw"
    pw.write_text("x\n", encoding="utf-8")
    env_file = tmp_path / "fmp-dashboard.env"
    env_file.write_text(
        "DASHBOARD_READONLY_URL=postgresql://dashboard_readonly:secret@127.0.0.1/fmp\n"
        "FMP_STREAMLIT_READONLY=1\n",
        encoding="utf-8",
    )
    return {"current": current, "pw": pw, "env_file": env_file}


def test_scan_systemd_env_file_lists_writer_keys_not_values(tmp_path):
    path = tmp_path / "fmp-dashboard.env"
    path.write_text(
        "DASHBOARD_READONLY_URL=postgresql://dashboard_readonly:secret@127.0.0.1/fmp\n"
        "DATABASE_URL=postgresql://fmp:secret@127.0.0.1/fmp\n",
        encoding="utf-8",
    )
    scan = scan_systemd_env_file(path)
    assert scan["present"] is True
    assert scan["readonly_url_assignment"] is True
    assert scan["writer_keys_present"] == ["DATABASE_URL"]


def test_dry_run_is_ready_while_systemd_still_uses_git_pull(tmp_path):
    tree = _ready_tree(tmp_path)
    report = evaluate_cutover(
        env={
            "DASHBOARD_READONLY_URL": "postgresql://dashboard_readonly:secret@127.0.0.1/fmp",
            "FMP_STREAMLIT_READONLY": "1",
        },
        password_file=tree["pw"],
        current_link=tree["current"],
        systemd_exec="/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py",
        systemd_env_file=tree["env_file"],
        verify_rc=0,
    )
    assert report["ready"] is True
    assert report["blockers"] == []
    assert report["systemd_still_git_pull"] is True
    assert report["systemd_cutover_proven"] is False
    assert report["systemd_mutated"] is False
    assert report["mode"] == "dry_run"
    assert report["streamlit_venv_present"] is True
    assert report["ingest_module_present"] is True
    assert cutover_exit_code(report, require_ready=True, apply=False) == 0


def test_writer_keys_in_systemd_env_block_ready_and_exit_4(tmp_path):
    tree = _ready_tree(tmp_path)
    tree["env_file"].write_text(
        "DASHBOARD_READONLY_URL=postgresql://dashboard_readonly:secret@127.0.0.1/fmp\n"
        "DB_PASSWORD=secret\n",
        encoding="utf-8",
    )
    report = evaluate_cutover(
        env={
            "DASHBOARD_READONLY_URL": "postgresql://dashboard_readonly:secret@127.0.0.1/fmp",
            "FMP_STREAMLIT_READONLY": "1",
        },
        password_file=tree["pw"],
        current_link=tree["current"],
        systemd_exec="/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py",
        systemd_env_file=tree["env_file"],
        verify_rc=0,
    )
    assert report["ready"] is False
    assert "writer_keys_in_systemd_env" in report["blockers"]
    assert report["writer_keys_in_systemd_env"] == ["DB_PASSWORD"]
    assert cutover_exit_code(report, require_ready=False, apply=False) == 4


def test_apply_without_allow_flag_refuses_and_does_not_mutate(tmp_path):
    tree = _ready_tree(tmp_path)
    report = evaluate_cutover(
        env={
            "DASHBOARD_READONLY_URL": "postgresql://dashboard_readonly:secret@127.0.0.1/fmp",
            "FMP_STREAMLIT_READONLY": "1",
        },
        password_file=tree["pw"],
        current_link=tree["current"],
        systemd_exec="/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py",
        systemd_env_file=tree["env_file"],
        verify_rc=0,
        apply=True,
        allow_cutover=False,
    )
    assert report["ready"] is True
    assert report["apply_status"] == "refused_allow_flag"
    assert report["systemd_mutated"] is False
    assert cutover_exit_code(report, require_ready=False, apply=True) == 7


def test_apply_with_allow_flag_still_does_not_mutate_systemd(tmp_path):
    tree = _ready_tree(tmp_path)
    report = evaluate_cutover(
        env={
            "DASHBOARD_READONLY_URL": "postgresql://dashboard_readonly:secret@127.0.0.1/fmp",
            "FMP_STREAMLIT_READONLY": "1",
            "FMP_ALLOW_SYSTEMD_CUTOVER": "1",
        },
        password_file=tree["pw"],
        current_link=tree["current"],
        systemd_exec="/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py",
        systemd_env_file=tree["env_file"],
        verify_rc=0,
        apply=True,
        allow_cutover=True,
    )
    assert report["apply_status"] == "refused_no_systemd_mutate"
    assert report["systemd_mutated"] is False
    exec_start = [line for line in PROPOSED_UNIT.splitlines() if line.startswith("ExecStart=")][0]
    assert "/opt/fmp/current/venv/bin/streamlit" in exec_start
    assert "FMP_SCREENER" not in exec_start
    assert "WorkingDirectory=/opt/fmp/current" in PROPOSED_UNIT
    assert "Environment=FMP_STREAMLIT_READONLY=1" in PROPOSED_UNIT
    rw = [line for line in PROPOSED_UNIT.splitlines() if line.startswith("ReadWritePaths=")][0]
    assert rw == "ReadWritePaths=/var/lib/fmp /var/log/fmp"
    assert "/opt/fmp/current/outputs" not in PROPOSED_UNIT
    example = (ROOT / "deploy" / "fmp-dashboard.service.example").read_text(encoding="utf-8")
    assert "ReadWritePaths=/var/lib/fmp /var/log/fmp" in example
    assert "__ROOT__/outputs" not in example.split("ReadWritePaths=", 1)[1].splitlines()[0]


def test_missing_immutable_current_is_recorded_not_a_deploy_failure(tmp_path):
    pw = tmp_path / "dashboard_readonly.pw"
    pw.write_text("x\n", encoding="utf-8")
    env_file = tmp_path / "fmp-dashboard.env"
    env_file.write_text("DASHBOARD_READONLY_URL=postgresql://dashboard_readonly:x@127.0.0.1/fmp\n", encoding="utf-8")
    report = evaluate_cutover(
        env={"DASHBOARD_READONLY_URL": "postgresql://dashboard_readonly:x@127.0.0.1/fmp"},
        password_file=pw,
        current_link=tmp_path / "missing-current",
        systemd_exec="/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py",
        systemd_env_file=env_file,
        verify_rc=0,
    )
    assert report["ready"] is False
    assert "immutable_current_missing" in report["blockers"]
    assert cutover_exit_code(report, require_ready=False, apply=False) == 0
    assert cutover_exit_code(report, require_ready=True, apply=False) == 8


def test_missing_release_venv_blocks_cutover_ready(tmp_path):
    tree = _ready_tree(tmp_path)
    streamlit = tree["current"].resolve() / "venv" / "bin" / "streamlit"
    streamlit.unlink()
    report = evaluate_cutover(
        env={
            "DASHBOARD_READONLY_URL": "postgresql://dashboard_readonly:secret@127.0.0.1/fmp",
            "FMP_STREAMLIT_READONLY": "1",
        },
        password_file=tree["pw"],
        current_link=tree["current"],
        systemd_exec="/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py",
        systemd_env_file=tree["env_file"],
        verify_rc=0,
    )
    assert report["ready"] is False
    assert "streamlit_venv_missing" in report["blockers"]
    assert report["streamlit_venv_present"] is False
    assert cutover_exit_code(report, require_ready=False, apply=False) == 0
    assert cutover_exit_code(report, require_ready=True, apply=False) == 8


def test_cutover_source_never_calls_systemctl_mutate():
    text = (ROOT / "jobs" / "cutover_dashboard_systemd.py").read_text(encoding="utf-8")
    assert "systemctl" not in text
    assert "daemon-reload" not in text
    assert "subprocess" not in text


def test_cutover_reads_readonly_url_from_env_file_when_process_env_lacks_it(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_READONLY_URL", raising=False)
    monkeypatch.delenv("DASHBOARD_ALLOW_WRITER_FALLBACK", raising=False)
    monkeypatch.delenv("STREAMLIT_ALLOW_PROVIDER_FETCH", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fmp:secret@127.0.0.1/fmp")
    tree = _ready_tree(tmp_path)
    report = evaluate_cutover(
        password_file=tree["pw"],
        current_link=tree["current"],
        systemd_exec="/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py",
        systemd_env_file=tree["env_file"],
        verify_rc=0,
    )
    assert report["readonly_proven"] is True
    assert report["ready"] is True
    assert report["writer_env_keys_present"] == []
    assert "readonly_unproven" not in report["blockers"]
    assert "writer_identity" not in report["blockers"]


def test_everyday_deploy_records_cutover_dry_run_and_never_applies():
    deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "jobs.cutover_dashboard_systemd" in deploy
    assert "qc_research.verify_csfml_v1 --live" in deploy
    assert "--apply" not in deploy
    assert "--require-ready" not in deploy
    assert "FMP_ALLOW_SYSTEMD_CUTOVER" not in deploy
    assert "/var/lib/fmp/deploy/cutover_readiness.json" in deploy
    assert deploy.index("jobs.audit_host_dashboard") < deploy.index("jobs.cutover_dashboard_systemd")
    assert deploy.index("jobs.cutover_dashboard_systemd") < deploy.index("systemctl restart fmp-dashboard")
    cutover_block = deploy.split("Recording systemd cutover readiness", 1)[1].split(
        "Restarting Streamlit", 1
    )[0]
    assert "/etc/fmp/fmp-dashboard.env" in cutover_block
    assert "unset DATABASE_URL" in cutover_block
    assert "source /root/FMP_SCREENER/.env" not in cutover_block
    assert ". /root/FMP_SCREENER/.env" not in cutover_block
    docs = (ROOT / "docs" / "IMMUTABLE_DEPLOY.md").read_text(encoding="utf-8")
    assert "jobs.cutover_dashboard_systemd" in docs
    assert "--apply" in docs
    assert "prefer `/opt/fmp/current`" in docs
    assert "/opt/fmp/current/venv/bin/streamlit" in docs
    assert "Creating release venv" in (
        ROOT / "scripts" / "deploy_release.sh"
    ).read_text(encoding="utf-8")


def test_write_facts_strips_secrets_from_cutover_report(tmp_path):
    from jobs.audit_host_dashboard import write_facts

    tree = _ready_tree(tmp_path)
    report = evaluate_cutover(
        env={
            "DASHBOARD_READONLY_URL": "postgresql://dashboard_readonly:secret@127.0.0.1/fmp",
            "FMP_STREAMLIT_READONLY": "1",
        },
        password_file=tree["pw"],
        current_link=tree["current"],
        systemd_exec="/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py",
        systemd_env_file=tree["env_file"],
        verify_rc=0,
    )
    path = tmp_path / "cutover_readiness.json"
    write_facts(report, path)
    text = path.read_text(encoding="utf-8")
    assert "secret" not in text
    assert "postgresql://" not in text
    payload = json.loads(text)
    assert payload["ready"] is True
    assert payload["systemd_mutated"] is False
