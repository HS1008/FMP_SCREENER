"""dashboard_readonly role denies writes on disposable PostgreSQL.

Provisions a unique role from db/roles/dashboard_readonly.sql using the same
psql pattern as Market Intelligence read-only tests. Never prints URLs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_readonly_sql_sets_read_only_defaults():
    sql = (ROOT / "db" / "roles" / "dashboard_readonly.sql").read_text(encoding="utf-8")
    assert "default_transaction_read_only = on" in sql
    assert "CONNECTION LIMIT 20" in sql
    assert "GRANT SELECT ON TABLE" in sql
    assert "REVOKE CREATE ON SCHEMA public FROM dashboard_readonly" in sql
    assert "strategies" in sql
    assert "research_runs" in sql
    assert "backtests" in sql


def test_verify_job_exits_3_when_url_unset(monkeypatch):
    monkeypatch.delenv("DASHBOARD_READONLY_URL", raising=False)
    from jobs.verify_dashboard_readonly import run

    assert run() == 3


def test_identity_script_fails_closed_without_url_or_fallback():
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "verify_dashboard_identity.sh")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT),
            "FMP_IDENTITY_ENV_ONLY": "1",
        },
        check=False,
    )
    assert result.returncode == 3
    assert "DASHBOARD_READONLY_URL required" in result.stdout
    assert "postgresql" not in result.stdout.lower()


def test_identity_script_allows_explicit_writer_fallback():
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "verify_dashboard_identity.sh")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT),
            "FMP_IDENTITY_ENV_ONLY": "1",
            "DASHBOARD_ALLOW_WRITER_FALLBACK": "1",
        },
        check=False,
    )
    assert result.returncode == 0
    assert "skipped_explicit_writer_fallback" in result.stdout


def _provision_dashboard_role(admin_url: str, role: str, password: str | None, tmp_dir: Path) -> subprocess.CompletedProcess:
    psql = shutil.which("psql")
    if psql is None:
        pytest.fail("psql client is required for dashboard_readonly role tests")
    sql = (ROOT / "db" / "roles" / "dashboard_readonly.sql").read_text(encoding="utf-8").replace(
        "dashboard_readonly", role
    )
    if password is not None:
        sql = "\\set ro_password '{0}'\n".format(password) + sql
    path = tmp_dir / "{0}.sql".format(role)
    path.write_text(sql, encoding="utf-8")
    return subprocess.run(
        [psql, admin_url, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.fixture
def dashboard_ro_engine(pg_engine, pg_database, pg_admin_url, tmp_path):
    """Unique dashboard_readonly-shaped role on the disposable migrated database."""
    role = "dashboard_ro_{0}".format(uuid.uuid4().hex[:8])
    password = "dash_{0}".format(uuid.uuid4().hex)
    admin_on_test_db = make_url(pg_admin_url).set(
        database=make_url(pg_database).database,
        drivername="postgresql",
    ).render_as_string(hide_password=False)
    first = _provision_dashboard_role(admin_on_test_db, role, password, tmp_path)
    assert first.returncode == 0, first.stderr
    second = _provision_dashboard_role(admin_on_test_db, role, None, tmp_path)
    assert second.returncode == 0, second.stderr
    assert "refreshing grants only" in second.stdout
    with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
        admin.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
    url = make_url(pg_database).set(username=role, password=password)
    engine = create_engine(url, future=True, pool_pre_ping=True)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1
    yield engine, str(url.render_as_string(hide_password=False))
    engine.dispose()
    with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = :u"), {"u": role})
        conn.execute(text('DROP OWNED BY "{0}"'.format(role)))
    admin = create_engine(pg_admin_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text('DROP ROLE IF EXISTS "{0}"'.format(role)))
    admin.dispose()


def test_dashboard_readonly_role_selects_and_denies_writes(dashboard_ro_engine):
    engine, _url = dashboard_ro_engine
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM research_runs")).scalar() >= 0
        assert conn.execute(text("SELECT COUNT(*) FROM strategies")).scalar() >= 0
        assert conn.execute(text("SELECT COUNT(*) FROM backtests")).scalar() >= 0
    for sql in (
        "INSERT INTO research_runs (research_run_id, strategy_id) VALUES ('x', 'x')",
        "UPDATE research_runs SET strategy_id = strategy_id WHERE FALSE",
        "DELETE FROM research_runs WHERE FALSE",
        "CREATE TABLE dashboard_readonly_probe (id int)",
    ):
        with pytest.raises(Exception) as excinfo:
            with engine.begin() as conn:
                conn.execute(text(sql))
        message = str(excinfo.value).lower()
        assert "permission denied" in message or "read-only transaction" in message, sql


def test_dashboard_readonly_privileges_hold_when_session_default_overridden(dashboard_ro_engine):
    engine, _url = dashboard_ro_engine
    for sql in (
        "INSERT INTO research_runs (research_run_id, strategy_id) VALUES ('x', 'x')",
        "UPDATE research_runs SET strategy_id = strategy_id WHERE FALSE",
        "DELETE FROM research_runs WHERE FALSE",
        "CREATE TABLE dashboard_readonly_probe_rw (id int)",
    ):
        with pytest.raises(Exception) as excinfo:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text("SET default_transaction_read_only = off"))
                assert conn.execute(text("SHOW transaction_read_only")).scalar() == "off"
                conn.execute(text(sql))
        assert "permission denied" in str(excinfo.value).lower(), sql


def test_verify_job_against_provisioned_dashboard_role(dashboard_ro_engine, monkeypatch):
    _engine, url = dashboard_ro_engine
    monkeypatch.setenv("DASHBOARD_READONLY_URL", url)
    from jobs.verify_dashboard_readonly import run

    assert run() == 0
