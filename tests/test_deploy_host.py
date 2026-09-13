"""Versioned host deploy: auto-deploy on main, migrate once, pin SSH."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from jobs.apply_migrations import MigrationDriftError, apply_migrations, migration_sha256
from sqlalchemy import create_engine, text
import pytest


ROOT = Path(__file__).resolve().parents[1]


def _pointer_target(path: Path | str) -> str:
    host = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")
    start = host.index("pointer_target()")
    end = host.index("\n}\n", start) + 3
    script = host[start:end] + 'pointer_target "$1"\n'
    result = subprocess.run(
        ["bash", "-c", script, "pointer_target", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.rstrip("\n")


def test_deploy_yml_is_thin_auto_deploy_with_pinned_ssh():
    deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    host = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")
    assert "push:" in deploy
    assert "branches:" in deploy
    assert "- main" in deploy
    assert "ubuntu-latest" in deploy
    assert "workflow_dispatch" not in deploy
    assert "self-hosted" not in deploy
    assert "ssh-keyscan -H" not in deploy
    assert "ssh-keyscan -t" not in deploy
    assert "DO_SSH_KNOWN_HOSTS" in deploy
    assert "StrictHostKeyChecking=yes" in deploy
    assert "bash -s -- --sha" in deploy
    assert "flock -n" in host
    assert "concurrency:" in deploy
    assert "cancel-in-progress: false" in deploy
    assert "git pull --ff-only origin main" not in deploy
    assert "git pull --ff-only origin main" not in host
    assert "python -m jobs.apply_migrations" not in deploy
    assert "systemctl restart fmp-dashboard" not in deploy
    assert deploy.count("python -m jobs.apply_migrations") == 0
    assert host.count("-m jobs.apply_migrations") == 1
    assert "Applying database migrations ONCE" in host
    assert "MIGRATIONS_BACKFILL_SHA256" not in host
    assert "MERGING TO MAIN IS A PRODUCTION DEPLOY EVENT" in deploy


def test_deploy_host_compares_pointers_with_existence_preserving_resolver():
    host = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")
    assert "pointer_target()" in host
    assert host.count('CURRENT_BEFORE="$(pointer_target "$CURRENT_LINK")"') == 1
    assert host.count('PREVIOUS_BEFORE="$(pointer_target "$PREVIOUS_LINK")"') == 1
    assert host.count('$(pointer_target "$CURRENT_LINK")') == 2
    assert host.count('$(pointer_target "$PREVIOUS_LINK")') == 2
    assert 'readlink -f "$CURRENT_LINK"' not in host
    assert "FAIL: stage-only must not move current/previous pointers" in host


def test_pointer_target_absent_current_is_empty_even_when_parent_exists(tmp_path):
    missing = tmp_path / "opt" / "fmp" / "current"
    missing.parent.mkdir(parents=True)
    assert missing.exists() is False
    raw = subprocess.run(
        ["readlink", "-f", str(missing)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert raw == str(missing.resolve())
    assert _pointer_target(missing) == ""


def test_pointer_target_unchanged_symlink_is_stable(tmp_path):
    target = tmp_path / "releases" / "abc"
    target.mkdir(parents=True)
    current = tmp_path / "current"
    current.symlink_to(target)
    assert _pointer_target(current) == str(target.resolve())
    assert _pointer_target(current) == _pointer_target(current)


def test_pointer_target_detects_retargeted_symlink(tmp_path):
    first = tmp_path / "releases" / "old"
    second = tmp_path / "releases" / "new"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    current = tmp_path / "current"
    current.symlink_to(first)
    before = _pointer_target(current)
    current.unlink()
    current.symlink_to(second)
    assert before != _pointer_target(current)


def test_deploy_host_migrates_once_from_staged_sha():
    host = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")
    assert "activate=skipped" in host
    assert "checkout --detach" in host
    assert host.index("Staging immutable release") < host.index("-m jobs.apply_migrations")
    assert host.index("Applying database migrations ONCE") < host.index(
        "-m jobs.apply_migrations"
    )
    assert host.index("-m jobs.apply_migrations") < host.index(
        "provision_dashboard_readonly.sh"
    )
    assert "no activate" in host
    assert "/etc/fmp/secrets/dashboard_readonly.pw" in host
    assert "dashboard_readonly_pw=migrated_to_etc_fmp_secrets" in host
    assert "refusing to mutate the shared checkout venv first" in host


def test_first_deploy_legacy_filename_only_state(tmp_path):
    engine = create_engine("sqlite:///{0}".format(tmp_path / "oldprod.db"))
    staged = tmp_path / "migrations"
    staged.mkdir()
    first = staged / "001_a.sql"
    first.write_text("CREATE TABLE IF NOT EXISTS demo (id int);\n", encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, applied_at TEXT, sha256 TEXT)"
            )
        )
        conn.execute(text("INSERT INTO schema_migrations (filename) VALUES ('001_a.sql')"))
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": "migration_checksum_baseline_v1",
                "baseline_git_sha": "2ed4da99df28d06e7730e56601de440e870dc823",
                "migrations": [
                    {
                        "filename": "001_a.sql",
                        "sha256": migration_sha256(first),
                        "baseline_git_sha": "2ed4da99df28d06e7730e56601de440e870dc823",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    applied = apply_migrations(staged, engine=engine, baseline_path=baseline)
    assert applied == ["001_a.sql (skipped)"]
    again = apply_migrations(staged, engine=engine, baseline_path=baseline)
    assert again == ["001_a.sql (skipped)"]
    first.write_text("CREATE TABLE IF NOT EXISTS demo (id int);\n-- drift\n", encoding="utf-8")
    with pytest.raises(MigrationDriftError):
        apply_migrations(staged, engine=engine, baseline_path=baseline)


def test_systemd_unit_requires_dashboard_env():
    example = (ROOT / "deploy" / "fmp-dashboard.service.example").read_text(encoding="utf-8")
    assert "EnvironmentFile=__ENV_FILE__" in example
    assert "EnvironmentFile=-" not in example
    assert "User=fmp-dashboard" in example or "User=__USER__" in example
    from jobs.cutover_dashboard_systemd import PROPOSED_UNIT

    assert "EnvironmentFile=/etc/fmp/fmp-dashboard.env" in PROPOSED_UNIT
    assert "EnvironmentFile=-" not in PROPOSED_UNIT
    assert "User=fmp-dashboard" in PROPOSED_UNIT


def test_release_docs_describe_auto_deploy():
    docs = (ROOT / "docs" / "RELEASE_CUTOVER_PLAN.md").read_text(encoding="utf-8")
    assert "PRODUCTION DEPLOY EVENT" in docs
    assert "push -> main" in docs or "push to main" in docs.lower()
    assert "ubuntu-latest" in docs
    assert "self-hosted deploy runner" not in docs.lower() or "not self-hosted" in docs.lower()
    assert "DO_SSH_KNOWN_HOSTS" in docs
    assert "prepare → validate → activate" in docs or "prepare/validate/activate" in docs.lower() or "Prepare:" in docs
    assert "last_verified.sha" in docs
    assert "deploy_release.sh --rollback" in docs
