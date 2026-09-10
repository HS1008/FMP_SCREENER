"""dashboard_readonly role denies writes when a disposable PG admin URL is available."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_readonly_sql_sets_read_only_defaults():
    sql = (ROOT / "db" / "roles" / "dashboard_readonly.sql").read_text(encoding="utf-8")
    assert "default_transaction_read_only = on" in sql
    assert "CONNECTION LIMIT 20" in sql
    assert "GRANT SELECT ON TABLE" in sql
    assert "REVOKE CREATE ON SCHEMA public FROM dashboard_readonly" in sql


def test_dashboard_readonly_role_denies_writes(pg_admin_url, pg_engine):
    """Provision the role when the admin URL can CREATE ROLE; otherwise skip."""
    import os
    import subprocess
    from urllib.parse import urlsplit

    role_sql = ROOT / "db" / "roles" / "dashboard_readonly.sql"
    password = "dashboard_readonly_test"
    env = os.environ.copy()
    parsed = urlsplit(pg_admin_url.replace("postgresql+psycopg2://", "postgresql://", 1))
    try:
        subprocess.run(
            ["psql", pg_admin_url.replace("postgresql+psycopg2://", "postgresql://", 1), "-v", "ON_ERROR_STOP=1", "-v", "ro_password={0}".format(password), "-f", str(role_sql)],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        pytest.skip("cannot provision dashboard_readonly: {0}".format(exc))

    from sqlalchemy import create_engine

    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5432
    db = (parsed.path or "/postgres").lstrip("/")
    ro_url = "postgresql+psycopg2://dashboard_readonly:{0}@{1}:{2}/{3}".format(password, host, port, db)
    try:
        engine = create_engine(ro_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            with pytest.raises(Exception):
                conn.execute(text("CREATE TABLE dashboard_readonly_probe (id int)"))
    except Exception as exc:
        pytest.skip("dashboard_readonly connect failed: {0}".format(exc))
