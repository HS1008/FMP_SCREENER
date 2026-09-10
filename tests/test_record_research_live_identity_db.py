"""Sanitized CSFML / TLT live query-back attaches to the latest deploy identity row."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobs.record_deploy_identity_db import SecretBearingIdentity, insert_record, sanitize_record
from jobs.record_research_live_identity_db import (
    build_record,
    load_live_report,
    main,
    update_latest,
)


ROOT = Path(__file__).resolve().parents[1]


def _identity(**overrides):
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


def _live(*, present: bool, identity_ok: bool, blockers=None):
    return {
        "present": present,
        "identity_ok": identity_ok,
        "blockers": blockers or [],
        "economic_gate": "NOT_DEFINED",
    }


def test_build_record_keeps_missing_files_null_and_refuses_secrets():
    record = build_record(csfml=None, tlt=_live(present=True, identity_ok=True))
    assert record["csfml_v1_live_present"] is None
    assert record["csfml_v1_provided"] is False
    assert record["tlt_v0_provided"] is True
    assert record["tlt_v0_live_present"] is True
    assert record["tlt_v0_live_identity_ok"] is True
    assert record["stage1_live_present"] is None
    assert record["stage1_provided"] is False
    missing = build_record(
        csfml=_live(present=False, identity_ok=False, blockers=["official_run_missing"]),
        tlt=None,
    )
    assert missing["csfml_v1_live_present"] is False
    assert missing["csfml_v1_live_identity_ok"] is False
    assert missing["csfml_v1_live_blockers"] == "official_run_missing"
    assert missing["tlt_v0_live_present"] is None
    with pytest.raises(SecretBearingIdentity):
        build_record(
            csfml=_live(
                present=True,
                identity_ok=False,
                blockers=["postgresql://writer:secret@127.0.0.1/fmp"],
            ),
            tlt=None,
        )


def test_load_live_report_and_cli_refuse_streamlit_and_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("FMP_STREAMLIT_READONLY", raising=False)
    missing = tmp_path / "missing.json"
    assert load_live_report(missing) is None
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps(_live(present=True, identity_ok=True, blockers=["postgres://x"])), encoding="utf-8")
    with pytest.raises(SecretBearingIdentity):
        load_live_report(secret)
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps(_live(present=False, identity_ok=False, blockers=["official_run_missing"])), encoding="utf-8")
    assert load_live_report(ok)["present"] is False
    monkeypatch.setenv("FMP_STREAMLIT_READONLY", "1")
    assert main(["--csfml", str(ok), "--tlt", str(missing)]) == 4


def test_update_latest_is_visible_on_ops_view(pg_engine, monkeypatch):
    from datetime import datetime, timezone
    from market_intelligence.read_models import ops_status

    monkeypatch.delenv("FMP_STREAMLIT_READONLY", raising=False)
    insert_record(
        sanitize_record(_identity(recorded_at=datetime.now(timezone.utc).isoformat())),
        engine=pg_engine,
    )
    record = build_record(
        csfml=_live(present=False, identity_ok=False, blockers=["official_run_missing"]),
        tlt=_live(present=True, identity_ok=True),
        stage1=_live(present=False, identity_ok=False, blockers=["official_run_missing"]),
    )
    assert update_latest(record, engine=pg_engine) == 1
    with pg_engine.connect() as conn:
        ops = ops_status(conn)
    assert ops["csfml_v1_label_integrity"] == "CANNOT_RULE_OUT"
    assert ops["csfml_v1_live_present"] is False
    assert ops["csfml_v1_live_identity_ok"] is False
    assert ops["csfml_v1_live_blockers"] == "official_run_missing"
    assert ops["tlt_v0_live_present"] is True
    assert ops["tlt_v0_live_identity_ok"] is True
    assert ops["tlt_v0_live_blockers"] is None
    assert ops["stage1_live_present"] is False
    assert ops["stage1_live_identity_ok"] is False
    assert ops["stage1_live_blockers"] == "official_run_missing"
    assert "postgresql://" not in json.dumps(ops)


def test_missing_sidecar_does_not_null_sibling_live_columns(pg_engine, monkeypatch):
    from datetime import datetime, timezone
    from market_intelligence.read_models import ops_status

    monkeypatch.delenv("FMP_STREAMLIT_READONLY", raising=False)
    insert_record(
        sanitize_record(_identity(recorded_at=datetime.now(timezone.utc).isoformat())),
        engine=pg_engine,
    )
    assert update_latest(
        build_record(
            csfml=_live(present=True, identity_ok=True),
            tlt=_live(present=True, identity_ok=True),
            stage1=_live(present=False, identity_ok=False, blockers=["official_run_missing"]),
        ),
        engine=pg_engine,
    ) == 1
    assert update_latest(
        build_record(
            csfml=None,
            tlt=None,
            stage1=_live(present=True, identity_ok=True),
        ),
        engine=pg_engine,
    ) == 1
    with pg_engine.connect() as conn:
        ops = ops_status(conn)
    assert ops["csfml_v1_live_present"] is True
    assert ops["csfml_v1_live_identity_ok"] is True
    assert ops["tlt_v0_live_present"] is True
    assert ops["tlt_v0_live_identity_ok"] is True
    assert ops["stage1_live_present"] is True
    assert ops["stage1_live_identity_ok"] is True
    assert ops["stage1_live_blockers"] is None


def test_deploy_persists_live_identity_after_query_back_in_writer_subshell():
    deploy = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")
    assert "jobs.record_research_live_identity_db" not in deploy
    assert "jobs.record_deploy_identity_db" in deploy
    assert "--csfml /var/lib/fmp/deploy/csfml_v1_live.json" in deploy
    assert "--tlt /var/lib/fmp/deploy/tlt_v0_live.json" in deploy
    assert "--stage1 /var/lib/fmp/deploy/stage1_live.json" in deploy
    assert "--allow-missing" in deploy
    assert "--require-present" not in deploy
    assert deploy.index("qc_research.verify_csfml_v1 --live") < deploy.index(
        "jobs.record_deploy_identity_db"
    )
    assert deploy.index("qc_research.verify_tlt_monitor --live") < deploy.index(
        "jobs.record_deploy_identity_db"
    )
    monitor = (ROOT / "pages" / "strategy_monitor.py").read_text(encoding="utf-8")
    assert "/var/lib/fmp/deploy" not in monitor
    ops_block = monitor.split("_ops_caption = format_ops_identity_caption", 1)[1].split(
        "DATABASE LOADERS", 1
    )[0]
    assert "st.stop()" not in ops_block
    assert "st.error(" not in ops_block
    assert deploy.index("jobs.audit_host_dashboard") < deploy.index("jobs.record_deploy_identity_db")
    assert deploy.index("qc_research.verify_stage1 --live") < deploy.index(
        "jobs.record_deploy_identity_db"
    )
    assert deploy.index("jobs.cutover_dashboard_systemd") < deploy.index(
        "jobs.record_deploy_identity_db"
    )
    persist = deploy.split("Persisting sanitized deploy identity", 1)[1].split(
        "Restarting Streamlit", 1
    )[0]
    assert "--csfml /var/lib/fmp/deploy/csfml_v1_live.json" in persist
    assert "--tlt /var/lib/fmp/deploy/tlt_v0_live.json" in persist
    assert "--stage1 /var/lib/fmp/deploy/stage1_live.json" in persist
    assert "jobs.record_research_live_identity_db" not in persist
    verify = (ROOT / ".github" / "workflows" / "stage1_verify.yml").read_text(encoding="utf-8")
    assert "jobs.record_research_live_identity_db" in verify
    assert "--stage1 /var/lib/fmp/deploy/stage1_live.json" in verify
    assert "--require-present" not in verify
    assert verify.index("qc_research.verify_stage1 --live") < verify.index(
        "jobs.record_research_live_identity_db"
    )
