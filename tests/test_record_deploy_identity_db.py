"""Sanitized deploy identity persists to PostgreSQL, never to Streamlit host files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from jobs.record_deploy_identity_db import (
    SecretBearingIdentity,
    insert_record,
    main,
    sanitize_record,
)

ROOT = Path(__file__).resolve().parents[1]


def _record(**overrides):
    payload = {
        "recorded_at": "2026-09-10T02:00:00+00:00",
        "git_sha": "abc123def456",
        "checkout": "/root/FMP_SCREENER",
        "deploy_mode": "git_pull",
        "immutable_release_rc": 0,
        "immutable_current_present": False,
        "systemd_still_git_pull": True,
        "systemd_cutover_proven": False,
        "readonly_verify_rc": 3,
        "readonly_proven": False,
        "dashboard_readonly_url_set": True,
        "dashboard_readonly_password_file_present": False,
        "writer_fallback": False,
        "writer_env_keys_present": [],
        "provider_fetch": False,
        "csfml_v1_label_integrity": "CANNOT_RULE_OUT",
        "csfml_v1_rerun_authorized": False,
    }
    payload.update(overrides)
    return payload


def test_sanitize_record_keeps_booleans_and_key_names_only():
    record = sanitize_record(_record(writer_env_keys_present=["DATABASE_URL", "DB_PASSWORD"]))
    assert record["git_sha"] == "abc123def456"
    assert record["readonly_proven"] is False
    assert record["writer_env_keys_present"] == "DATABASE_URL,DB_PASSWORD"
    assert record["csfml_v1_label_integrity"] == "CANNOT_RULE_OUT"


def test_sanitize_record_refuses_urls_and_invalid_key_names():
    with pytest.raises(SecretBearingIdentity):
        sanitize_record(_record(checkout="postgresql://writer:secret@127.0.0.1/fmp"))
    with pytest.raises(SecretBearingIdentity):
        sanitize_record(_record(writer_env_keys_present=["not a key"]))
    with pytest.raises(ValueError, match="git_sha"):
        sanitize_record(_record(git_sha="main"))


def test_load_record_and_cli_refuse_missing_or_secret_json(tmp_path, monkeypatch):
    monkeypatch.delenv("FMP_STREAMLIT_READONLY", raising=False)
    missing = tmp_path / "missing.json"
    assert main(["--from", str(missing)]) == 2
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps(_record(checkout="postgres://x")), encoding="utf-8")
    assert main(["--from", str(secret)]) == 4
    monkeypatch.setenv("FMP_STREAMLIT_READONLY", "1")
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps(_record()), encoding="utf-8")
    assert main(["--from", str(ok)]) == 4


def test_deploy_persists_identity_in_writer_subshell_before_dashboard_env():
    deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "jobs.record_deploy_identity_db" in deploy
    assert "/var/lib/fmp/deploy/current.json" in deploy
    block = deploy.split("Persisting sanitized deploy identity", 1)[1]
    block = block.split("Persisting sanitized research live identity", 1)[0]
    assert "/etc/fmp/fmp-writer.env" in block
    assert "/root/FMP_SCREENER/.env" in block
    assert block.index("/etc/fmp/fmp-writer.env") < block.index("/root/FMP_SCREENER/.env")
    assert "/etc/fmp/fmp-dashboard.env" not in block
    assert "(" in block and ")" in block


def test_streamlit_reads_identity_from_ops_view_not_host_json():
    ui = (ROOT / "market_intelligence" / "pages_ui.py").read_text(encoding="utf-8")
    read_models = (ROOT / "market_intelligence" / "read_models.py").read_text(encoding="utf-8")
    page = (ROOT / "pages" / "15_Data_Health.py").read_text(encoding="utf-8")
    assert "dashboard_readonly_proven" in ui
    assert "ops_status" in read_models
    assert "/var/lib/fmp/deploy" not in ui
    assert "/var/lib/fmp/deploy" not in read_models
    assert "/var/lib/fmp/deploy" not in page
    sql = (ROOT / "db" / "migrations" / "022_deploy_host_identity.sql").read_text(encoding="utf-8")
    assert "dashboard_readonly_proven" in sql
    assert "deploy_git_sha" in sql
    assert "postgresql://" not in sql
    added = (ROOT / "db" / "migrations" / "023_streamlit_readonly_identity.sql").read_text(encoding="utf-8")
    assert "streamlit_readonly" in added
    assert "dashboard_streamlit_readonly" in added
    assert "postgresql://" not in added
    live = (ROOT / "db" / "migrations" / "024_research_live_identity.sql").read_text(encoding="utf-8")
    assert "csfml_v1_live_present" in live
    assert "tlt_v0_live_identity_ok" in live
    assert "csfml_v1_live_present" in ui
    assert "tlt_v0_live_present" in ui
    assert "postgresql://" not in live
    assert "/var/lib/fmp/deploy" not in live
    live_stage1 = (ROOT / "db" / "migrations" / "025_stage1_live_identity.sql").read_text(encoding="utf-8")
    assert "stage1_live_present" in live_stage1
    assert "stage1_live_present" in ui
    assert "postgresql://" not in live_stage1
    assert "/var/lib/fmp/deploy" not in live_stage1


def test_insert_record_is_visible_on_ops_view(pg_engine, monkeypatch):
    from market_intelligence.read_models import ops_status

    monkeypatch.delenv("FMP_STREAMLIT_READONLY", raising=False)
    record = sanitize_record(_record(readonly_proven=False, systemd_still_git_pull=True))
    insert_record(record, engine=pg_engine)
    with pg_engine.connect() as conn:
        ops = ops_status(conn)
        rows = conn.execute(text("SELECT git_sha FROM mi_deploy_host_identity")).fetchall()
    assert rows and rows[0][0] == "abc123def456"
    assert ops["deploy_git_sha"] == "abc123def456"
    assert ops["dashboard_readonly_proven"] is False
    assert ops["systemd_still_git_pull"] is True
    assert ops["csfml_v1_label_integrity"] == "CANNOT_RULE_OUT"
    assert ops["csfml_v1_rerun_authorized"] is False
    assert "secret" not in json.dumps(ops)
    assert "postgresql://" not in json.dumps(ops)
