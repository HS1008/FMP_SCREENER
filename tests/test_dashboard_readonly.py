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


def test_provision_script_applies_sql_via_admin_or_peer_not_writer():
    script = (ROOT / "scripts" / "provision_dashboard_readonly.sh").read_text(encoding="utf-8")
    assert "MI_ADMIN_DATABASE_URL" in script
    assert "ADMIN_DATABASE_URL" in script
    assert "postgres_peer" in script
    assert "sudo -n -u postgres" in script
    assert "-f -" in script
    assert "The dashboard writer cannot CREATE ROLE" in script
    assert "--scrub-streamlit-writer" in script
    for line in script.splitlines():
        if "dashboard_readonly.sql" in line:
            assert "DATABASE_URL" not in line
            assert "DB_USER" not in line


def test_dashboard_readonly_grants_cover_monitor_tables():
    import re

    sql = (ROOT / "db" / "roles" / "dashboard_readonly.sql").read_text(encoding="utf-8")
    sources = [
        (ROOT / "pages" / "strategy_monitor.py").read_text(encoding="utf-8"),
        (ROOT / "qc_research" / "read_models" / "monitor_queries.py").read_text(encoding="utf-8"),
        (ROOT / "qc_research" / "research_library.py").read_text(encoding="utf-8"),
        (ROOT / "qc_research" / "tlt_duration_momentum.py").read_text(encoding="utf-8"),
        (ROOT / "qc_research" / "ml_monitor_ui.py").read_text(encoding="utf-8"),
    ]
    tables = set()
    for source in sources:
        blocks = re.findall(r'"""(.*?)"""', source, flags=re.S)
        blocks += re.findall(r"'''(.*?)'''", source, flags=re.S)
        for block in blocks:
            if "SELECT" not in block.upper():
                continue
            tables.update(re.findall(r"\bFROM\s+([a-z_][a-z0-9_]*)", block, flags=re.I))
            tables.update(re.findall(r"\bJOIN\s+([a-z_][a-z0-9_]*)", block, flags=re.I))
    tables -= {"bounded_q"}
    assert tables
    missing = sorted(name for name in tables if name not in sql)
    assert not missing, missing


def test_dashboard_readonly_sql_sets_read_only_defaults():
    sql = (ROOT / "db" / "roles" / "dashboard_readonly.sql").read_text(encoding="utf-8")
    assert "default_transaction_read_only = on" in sql
    assert "CONNECTION LIMIT 20" in sql
    assert "GRANT SELECT ON TABLE" in sql
    assert "REVOKE CREATE ON SCHEMA public FROM dashboard_readonly" in sql
    assert "repairing grants and memberships" in sql
    assert "has_schema_privilege('dashboard_readonly', 'public', 'CREATE')" in sql
    assert "pg_auth_members" in sql
    assert "strategies" in sql
    assert "research_runs" in sql
    assert "backtests" in sql
    assert "holdout_exposures" in sql
    assert "strategy_specs" in sql
    assert "research_pair_diagnostics" in sql
    assert "research_fixed_income_metrics" in sql
    assert "research_risk_metrics" in sql


def test_verify_job_exits_3_when_url_unset(monkeypatch, capsys):
    monkeypatch.delenv("DASHBOARD_READONLY_URL", raising=False)
    from jobs.verify_dashboard_readonly import run

    assert run() == 3
    captured = capsys.readouterr()
    assert "dashboard readonly verify failed (config)" in captured.out
    assert "skipped" not in captured.out


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
    script = (ROOT / "scripts" / "verify_dashboard_identity.sh").read_text(encoding="utf-8")
    assert "python3" in script
    assert "PYTHON_BIN" in script
    assert "/root/FMP_SCREENER/.env" not in script
    assert "/etc/fmp/fmp-dashboard.env" in script
    assert "clear_inherited_writer_env" in script
    assert "refuse_writer_keys_in_dashboard_env" in script
    assert (
        "clear_inherited_writer_env\nload_dashboard_env\nrefuse_writer_keys_in_dashboard_env"
        in script
    )


def test_identity_script_refuses_writer_fallback_on_deploy():
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
    assert result.returncode == 4
    assert "writer_fallback_refused" in result.stdout
    assert "skipped_explicit_writer_fallback" not in result.stdout


def test_identity_script_refuses_provider_fetch_on_deploy():
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "verify_dashboard_identity.sh")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT),
            "FMP_IDENTITY_ENV_ONLY": "1",
            "STREAMLIT_ALLOW_PROVIDER_FETCH": "1",
        },
        check=False,
    )
    assert result.returncode == 5
    assert "provider_fetch_refused" in result.stdout


def test_identity_script_refuses_missing_streamlit_readonly(tmp_path):
    dashboard = tmp_path / "fmp-dashboard.env"
    dashboard.write_text(
        "DASHBOARD_READONLY_URL=postgresql://dashboard_readonly:x@127.0.0.1/fmp\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "verify_dashboard_identity.sh")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT),
            "FMP_IDENTITY_ENV_ONLY": "1",
            "FMP_DASHBOARD_ENV": str(dashboard),
        },
        check=False,
    )
    assert result.returncode == 4
    assert "streamlit_readonly_missing" in result.stdout
    assert "postgresql" not in result.stdout.lower()


def test_identity_script_clears_inherited_writer_keys(tmp_path):
    dashboard = tmp_path / "fmp-dashboard.env"
    dashboard.write_text("DASHBOARD_READONLY_URL=\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "verify_dashboard_identity.sh")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT),
            "FMP_IDENTITY_ENV_ONLY": "1",
            "FMP_DASHBOARD_ENV": str(dashboard),
            "DATABASE_URL": "postgresql://writer:secret@127.0.0.1/fmp",
            "DB_USER": "writer",
            "MARKET_INTELLIGENCE_DATABASE_URL": "postgresql://mi:secret@127.0.0.1/fmp",
        },
        check=False,
    )
    assert result.returncode == 3
    assert "DASHBOARD_READONLY_URL required" in result.stdout
    assert "writer_fallback_refused" not in result.stdout
    assert "postgresql" not in result.stdout.lower()


def test_identity_script_refuses_writer_keys_in_dashboard_env(tmp_path):
    dashboard = tmp_path / "fmp-dashboard.env"
    dashboard.write_text(
        "DASHBOARD_READONLY_URL=postgresql://dashboard_readonly:x@127.0.0.1/fmp\n"
        "DATABASE_URL=postgresql://writer:secret@127.0.0.1/fmp\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "verify_dashboard_identity.sh")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT),
            "FMP_IDENTITY_ENV_ONLY": "1",
            "FMP_DASHBOARD_ENV": str(dashboard),
        },
        check=False,
    )
    assert result.returncode == 4
    assert "writer_fallback_refused" in result.stdout
    assert "dashboard env must not carry writer database keys" in result.stdout
    assert "postgresql" not in result.stdout.lower()


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
        [psql, "-d", admin_url, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(path)],
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
    assert "repairing grants and memberships" in second.stdout
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


def test_verify_job_covers_monitor_tables():
    from jobs.verify_dashboard_readonly import (
        CORE_MUTATION_PROBES,
        OPTIONAL_MUTATION_PROBES,
        OPTIONAL_SELECTS,
        REQUIRED_SELECTS,
    )

    text = "\n".join(sql for sql, _label in CORE_MUTATION_PROBES)
    assert "INSERT INTO backtests" in text
    assert "INSERT INTO research_artifacts" in text
    assert "INSERT INTO strategies" in text
    assert "INSERT INTO backtest_equity_points" in text
    assert "INSERT INTO ml_trials" in text
    assert "INSERT INTO ml_models" in text
    assert "INSERT INTO research_experiments" in text
    assert "INSERT INTO holdout_exposures" in text
    assert "INSERT INTO strategy_specs" in text
    assert "INSERT INTO research_pair_diagnostics" in text
    assert "INSERT INTO research_fixed_income_metrics" in text
    assert "INSERT INTO research_risk_metrics" in text
    assert "CREATE TABLE dashboard_readonly_probe" in text
    assert "TRUNCATE TABLE research_runs" in text
    required = "\n".join(REQUIRED_SELECTS)
    assert "FROM backtests" in required
    assert "FROM research_artifacts" in required
    assert "FROM ml_trials" in required
    optional = "\n".join(OPTIONAL_SELECTS)
    assert "mi_v_ops_status" in optional
    live = "\n".join(sql for sql, _label in OPTIONAL_MUTATION_PROBES)
    assert "live_snapshots" in live
    assert "positions" in live
    assert "ml_signal_points" in live
    assert "research_oos_windows" in live
    job = (ROOT / "jobs" / "verify_dashboard_readonly.py").read_text(encoding="utf-8")
    assert "readonly_monitor_tables=denied" in job


def test_dashboard_readonly_role_selects_and_denies_writes(dashboard_ro_engine):
    from jobs.verify_dashboard_readonly import CORE_MUTATION_PROBES

    engine, _url = dashboard_ro_engine
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM research_runs")).scalar() >= 0
        assert conn.execute(text("SELECT COUNT(*) FROM strategies")).scalar() >= 0
        assert conn.execute(text("SELECT COUNT(*) FROM backtests")).scalar() >= 0
        assert conn.execute(text("SELECT COUNT(*) FROM research_artifacts")).scalar() >= 0
        assert conn.execute(text("SELECT COUNT(*) FROM ml_trials")).scalar() >= 0
    for sql, _label in CORE_MUTATION_PROBES:
        with pytest.raises(Exception) as excinfo:
            with engine.begin() as conn:
                conn.execute(text(sql))
        message = str(excinfo.value).lower()
        assert "permission denied" in message or "read-only transaction" in message, sql


def test_dashboard_readonly_privileges_hold_when_session_default_overridden(dashboard_ro_engine):
    from jobs.verify_dashboard_readonly import CORE_MUTATION_PROBES

    engine, _url = dashboard_ro_engine
    for sql, _label in CORE_MUTATION_PROBES:
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


def test_provision_script_skips_without_password_file(tmp_path):
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "provision_dashboard_readonly.sh"),
            "--root",
            str(ROOT),
            "--pw-file",
            str(tmp_path / "missing.pw"),
            "--dashboard-env",
            str(tmp_path / "dash.env"),
            "--systemd-env",
            str(tmp_path / "systemd.env"),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "dashboard_readonly=skipped" in result.stdout
    assert "postgresql" not in result.stdout.lower()
    assert "postgresql" not in result.stderr.lower()


def test_provision_script_require_fails_without_password_file(tmp_path):
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "provision_dashboard_readonly.sh"),
            "--root",
            str(ROOT),
            "--pw-file",
            str(tmp_path / "missing.pw"),
            "--require",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
        check=False,
    )
    assert result.returncode == 3
    assert "password file absent" in result.stdout


def test_provision_script_creates_role_and_materializes_url(pg_engine, pg_database, pg_admin_url, tmp_path):
    """Run the real host script against disposable PostgreSQL. Never print the URL."""
    _ = pg_engine
    if shutil.which("psql") is None:
        pytest.fail("psql client is required for dashboard_readonly provision tests")
    password = "dash_{0}".format(uuid.uuid4().hex)
    pw_file = tmp_path / "dashboard_readonly.pw"
    pw_file.write_text(password + "\n", encoding="utf-8")
    os.chmod(pw_file, 0o600)
    dashboard_env = tmp_path / "dash.env"
    dashboard_env.write_text(
        "# test env\nFMP_API_KEY=not-a-db-secret\n"
        "MARKET_INTELLIGENCE_DATABASE_URL=postgresql://writer:keep-for-cli@127.0.0.1/fmp\n",
        encoding="utf-8",
    )
    systemd_env = tmp_path / "etc" / "fmp-dashboard.env"
    systemd_env.parent.mkdir(parents=True, exist_ok=True)
    systemd_env.write_text(
        "DATABASE_URL=postgresql://writer:strip-me@127.0.0.1/fmp\nDB_USER=writer\nFMP_API_KEY=keep-systemd\n",
        encoding="utf-8",
    )
    admin_on_test_db = make_url(pg_admin_url).set(
        database=make_url(pg_database).database,
        drivername="postgresql",
    ).render_as_string(hide_password=False)
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "ADMIN_DATABASE_URL": admin_on_test_db,
        "DATABASE_URL": admin_on_test_db,
        "DASHBOARD_READONLY_URL": "",
        "DASHBOARD_ALLOW_WRITER_FALLBACK": "",
        "FMP_IDENTITY_ENV_ONLY": "1",
    }
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "provision_dashboard_readonly.sh"),
            "--root",
            str(ROOT),
            "--pw-file",
            str(pw_file),
            "--dashboard-env",
            str(dashboard_env),
            "--systemd-env",
            str(systemd_env),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        check=False,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    try:
        assert result.returncode == 0, result.stderr
        assert "dashboard_readonly=provisioned" in result.stdout
        assert "dashboard_readonly_sql_via=admin_url" in result.stdout
        assert password not in combined
        assert "postgresql://" not in combined
        materialized = dashboard_env.read_text(encoding="utf-8")
        assert "DASHBOARD_READONLY_URL=postgresql://dashboard_readonly:" in materialized
        assert "FMP_API_KEY=not-a-db-secret" in materialized
        assert "MARKET_INTELLIGENCE_DATABASE_URL=postgresql://writer:keep-for-cli@127.0.0.1/fmp" in materialized
        systemd_text = systemd_env.read_text(encoding="utf-8")
        assert "DASHBOARD_READONLY_URL=postgresql://dashboard_readonly:" in systemd_text
        assert "FMP_API_KEY=keep-systemd" in systemd_text
        assert "systemd_env=writer_keys_removed" in result.stdout
        for line in systemd_text.splitlines():
            key = line.split("=", 1)[0].lstrip("#").strip()
            assert key not in {
                "DATABASE_URL",
                "MARKET_INTELLIGENCE_DATABASE_URL",
                "DB_PASSWORD",
                "DB_HOST",
                "DB_USER",
                "DB_NAME",
                "DB_PORT",
                "DASHBOARD_ALLOW_WRITER_FALLBACK",
                "STREAMLIT_ALLOW_PROVIDER_FETCH",
            }
        url = None
        for line in materialized.splitlines():
            if line.startswith("DASHBOARD_READONLY_URL="):
                url = line.split("=", 1)[1]
                break
        assert url
        verify_env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "FMP_IDENTITY_ENV_ONLY": "1",
            "DASHBOARD_READONLY_URL": url,
            "DASHBOARD_ALLOW_WRITER_FALLBACK": "",
            "FMP_STREAMLIT_READONLY": "1",
        }
        verify = subprocess.run(
            ["bash", str(ROOT / "scripts" / "verify_dashboard_identity.sh")],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=verify_env,
            check=False,
            timeout=60,
        )
        assert verify.returncode == 0, verify.stderr
        assert "dashboard_readonly_verify=ok" in verify.stdout
        assert password not in verify.stdout + verify.stderr
    finally:
        for engine_url in (pg_database, pg_admin_url):
            engine = create_engine(engine_url, isolation_level="AUTOCOMMIT", future=True)
            with engine.connect() as conn:
                exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_readonly'")).scalar()
                if exists:
                    conn.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE usename = 'dashboard_readonly' AND pid <> pg_backend_pid()"
                        )
                    )
                    conn.execute(text("DROP OWNED BY dashboard_readonly"))
            engine.dispose()
        admin = create_engine(pg_admin_url, isolation_level="AUTOCOMMIT", future=True)
        with admin.connect() as conn:
            conn.execute(text("DROP ROLE IF EXISTS dashboard_readonly"))
        admin.dispose()


def test_constraint_error_is_not_treated_as_readonly():
    from jobs.verify_dashboard_readonly import mutation_is_privilege_denial, sqlstate_of

    class _Orig:
        pgcode = "23502"

    class _Exc(Exception):
        def __init__(self):
            super().__init__("null value in column violates not-null constraint")
            self.orig = _Orig()

    exc = _Exc()
    assert sqlstate_of(exc) == "23502"
    assert mutation_is_privilege_denial(exc) is False


def test_polluted_preexisting_role_is_repaired_or_fails_closed(
    pg_engine, pg_database, pg_admin_url, tmp_path
):
    role = "dashboard_ro_{0}".format(uuid.uuid4().hex[:8])
    writer = "fmp_writer_{0}".format(uuid.uuid4().hex[:8])
    password = "dash_{0}".format(uuid.uuid4().hex)
    admin_on_test_db = make_url(pg_admin_url).set(
        database=make_url(pg_database).database,
        drivername="postgresql",
    ).render_as_string(hide_password=False)
    admin = create_engine(admin_on_test_db, isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as conn:
            conn.execute(text('CREATE ROLE "{0}" NOLOGIN'.format(writer)))
            conn.execute(
                text(
                    "CREATE ROLE \"{0}\" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "INHERIT PASSWORD '{1}'".format(role, password)
                )
            )
            conn.execute(text('GRANT "{0}" TO "{1}"'.format(writer, role)))
            conn.execute(text('GRANT INSERT, UPDATE, DELETE ON research_runs TO "{0}"'.format(role)))
            conn.execute(text('GRANT USAGE, UPDATE ON ALL SEQUENCES IN SCHEMA public TO "{0}"'.format(role)))
        first = _provision_dashboard_role(admin_on_test_db, role, password, tmp_path)
        if first.returncode != 0:
            combined = first.stdout + first.stderr
            assert "CREATE on schema public" in combined or "role memberships" in combined or "owns" in combined
            return
        url = make_url(pg_database).set(username=role, password=password).render_as_string(
            hide_password=False
        )
        from jobs.verify_dashboard_readonly import run

        import os as _os
        _os.environ["DASHBOARD_READONLY_URL"] = url
        assert run() == 0
        with admin.connect() as conn:
            insert_ok = conn.execute(
                text("SELECT has_table_privilege(:r, 'research_runs', 'INSERT')"),
                {"r": role},
            ).scalar()
            member = conn.execute(
                text(
                    "SELECT COUNT(*) FROM pg_auth_members m "
                    "JOIN pg_roles u ON u.oid = m.member "
                    "JOIN pg_roles r ON r.oid = m.roleid "
                    "WHERE u.rolname = :u AND r.rolname = :w"
                ),
                {"u": role, "w": writer},
            ).scalar()
        assert insert_ok is False
        assert int(member or 0) == 0
    finally:
        with admin.connect() as conn:
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = :u"), {"u": role})
            conn.execute(text('DROP OWNED BY "{0}"'.format(role)))
            conn.execute(text('DROP ROLE IF EXISTS "{0}"'.format(role)))
            conn.execute(text('DROP ROLE IF EXISTS "{0}"'.format(writer)))
        admin.dispose()


def test_public_create_inheritance_fails_closed(pg_engine, pg_database, pg_admin_url, tmp_path):
    role = "dashboard_ro_{0}".format(uuid.uuid4().hex[:8])
    password = "dash_{0}".format(uuid.uuid4().hex)
    admin_on_test_db = make_url(pg_admin_url).set(
        database=make_url(pg_database).database,
        drivername="postgresql",
    ).render_as_string(hide_password=False)
    admin = create_engine(admin_on_test_db, isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as conn:
            conn.execute(text("GRANT CREATE ON SCHEMA public TO PUBLIC"))
        first = _provision_dashboard_role(admin_on_test_db, role, password, tmp_path)
        combined = first.stdout + first.stderr
        assert first.returncode != 0
        assert "CREATE on schema public" in combined
        assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in combined
    finally:
        with admin.connect() as conn:
            conn.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
            conn.execute(text('DROP OWNED BY "{0}"'.format(role)))
            conn.execute(text('DROP ROLE IF EXISTS "{0}"'.format(role)))
        admin.dispose()


def test_constraint_insert_false_positive_fails_verify(pg_engine, pg_database, pg_admin_url, monkeypatch):
    role = "dash_insert_{0}".format(uuid.uuid4().hex[:8])
    password = "dash_{0}".format(uuid.uuid4().hex)
    admin = create_engine(pg_admin_url, isolation_level="AUTOCOMMIT", future=True)
    dbname = make_url(pg_database).database
    try:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "CREATE ROLE \"{0}\" LOGIN NOSUPERUSER PASSWORD '{1}'".format(role, password)
                )
            )
            conn.execute(text('GRANT CONNECT ON DATABASE "{0}" TO "{1}"'.format(dbname, role)))
        with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("GRANT USAGE ON SCHEMA public TO \"{0}\"".format(role)))
            conn.execute(text("GRANT SELECT, INSERT ON research_runs TO \"{0}\"".format(role)))
            conn.execute(text("ALTER ROLE \"{0}\" SET default_transaction_read_only = on".format(role)))
        url = make_url(pg_database).set(username=role, password=password).render_as_string(
            hide_password=False
        )
        monkeypatch.setenv("DASHBOARD_READONLY_URL", url)
        from jobs.verify_dashboard_readonly import run

        assert run() == 2
    finally:
        with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = :u"), {"u": role})
            conn.execute(text('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{0}"'.format(role)))
            conn.execute(text('DROP OWNED BY "{0}"'.format(role)))
        with admin.connect() as conn:
            conn.execute(text('DROP ROLE IF EXISTS "{0}"'.format(role)))
        admin.dispose()
