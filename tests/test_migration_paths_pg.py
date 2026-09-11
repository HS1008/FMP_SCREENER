"""Migration paths on disposable PostgreSQL 16, end to end through the gateway migration.

Scenarios (each on its own throw-away database):

A. fresh empty database -> every migration applies once, hashes recorded, second apply no-op;
B. simulated production: migrations 001-019 recorded the way the pre-checksum runner on
   ``main`` recorded them (filename-only rows, no sha256 column) -> the new runner backfills
   only trusted baseline hashes, applies 020-027, records their hashes, and is idempotent;
C. an already-applied file changed on disk -> the runner fails closed and applies nothing;
D. the read-only role provisioned from ``db/roles/*.sql`` can SELECT every gateway view and
   cannot mutate or read raw research tables (SQLSTATE 42501 / 25006).
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from jobs.apply_migrations import MigrationDriftError, apply_migrations, load_checksum_baseline, migration_sha256
from tests.conftest import STRATEGIES_TABLE_SQL

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"
GATEWAY_VIEWS = ("mi_v_strategy_nonholdout_runs", "mi_v_strategy_experiments", "mi_v_strategy_oos_windows", "mi_v_strategy_artifact_status")

# What the pre-checksum runner on production main created (jobs/apply_migrations.py @ 2ed4da9).
LEGACY_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _split(sql: str) -> list[str]:
    from jobs.apply_migrations import split_sql_statements

    return split_sql_statements(sql)


@pytest.fixture
def scratch_db(pg_admin_url):
    name = "fmp_mig_path_{0}".format(uuid.uuid4().hex[:8])
    admin = create_engine(pg_admin_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text('CREATE DATABASE "{0}"'.format(name)))
    url = make_url(pg_admin_url).set(database=name)
    engine = create_engine(url, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text(STRATEGIES_TABLE_SQL))
        yield engine, url
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n AND pid <> pg_backend_pid()"), {"n": name})
            conn.execute(text('DROP DATABASE IF EXISTS "{0}"'.format(name)))
        admin.dispose()


def _recorded(engine) -> dict[str, str | None]:
    with engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(text("SELECT filename, sha256 FROM schema_migrations"))}


def _all_files() -> list[Path]:
    return sorted(p for p in MIGRATIONS.glob("*.sql") if p.is_file())


def test_gateway_migration_is_the_next_number_and_last():
    names = [p.name for p in _all_files()]
    numbers = [int(n[:3]) for n in names]
    assert numbers == list(range(1, len(names) + 1)), "migration numbers must be contiguous"
    assert names[-1] == "027_ai_gateway_strategy_views.sql"
    assert not any(n.startswith("020_ai_gateway") for n in names), "the concurrent PR's duplicate 020 must not survive"


def test_scenario_a_fresh_database_applies_everything_once_with_hashes(scratch_db):
    engine, _url = scratch_db
    first = apply_migrations(engine=engine)
    assert first and not any(name.endswith("(skipped)") for name in first)
    assert first[-1] == "027_ai_gateway_strategy_views.sql"
    recorded = _recorded(engine)
    for path in _all_files():
        assert recorded[path.name] == migration_sha256(path)
    second = apply_migrations(engine=engine)
    assert all(name.endswith("(skipped)") for name in second) and len(second) == len(first)
    with engine.connect() as conn:
        views = {r[0] for r in conn.execute(text("SELECT table_name FROM information_schema.views WHERE table_schema = 'public'"))}
    assert set(GATEWAY_VIEWS) <= views


def test_scenario_b_simulated_production_001_019_upgrades_through_027(scratch_db):
    engine, _url = scratch_db
    baseline = load_checksum_baseline()
    legacy = [p for p in _all_files() if p.name < "020"]
    assert [p.name for p in legacy] == sorted(baseline["by_name"])
    # Replay production history: the old runner executed 001-019 and recorded filename-only rows.
    with engine.begin() as conn:
        conn.execute(text(LEGACY_SCHEMA_MIGRATIONS_SQL))
        for path in legacy:
            for statement in _split(path.read_text(encoding="utf-8")):
                conn.execute(text(statement))
            conn.execute(text("INSERT INTO schema_migrations (filename) VALUES (:f) ON CONFLICT DO NOTHING"), {"f": path.name})
    with engine.connect() as conn:
        cols = {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'schema_migrations'"))}
    assert "sha256" not in cols

    upgraded = apply_migrations(engine=engine)
    skipped = [n for n in upgraded if n.endswith("(skipped)")]
    applied = [n for n in upgraded if not n.endswith("(skipped)")]
    assert len(skipped) == 19
    assert applied == [p.name for p in _all_files() if p.name >= "020"]
    assert applied[-1] == "027_ai_gateway_strategy_views.sql"
    recorded = _recorded(engine)
    # Historical rows adopted the trusted baseline hash (not a bless of whatever was on disk: the
    # values are equal here only because the files are the committed baseline files).
    for path in legacy:
        assert recorded[path.name] == baseline["by_name"][path.name] == migration_sha256(path)
    for path in _all_files():
        if path.name >= "020":
            assert recorded[path.name] == migration_sha256(path)
    again = apply_migrations(engine=engine)
    assert all(n.endswith("(skipped)") for n in again)


def test_scenario_c_changed_applied_sql_fails_closed_and_applies_nothing(scratch_db, tmp_path):
    engine, _url = scratch_db
    staged = tmp_path / "migrations"
    staged.mkdir()
    for path in _all_files():
        shutil.copy(path, staged / path.name)
    apply_migrations(staged, engine=engine)
    target = staged / "026_treasury_equity_eod.sql"
    target.write_text(target.read_text(encoding="utf-8") + "\n-- drift\nCREATE TABLE IF NOT EXISTS mi_drift_marker (x INT);\n", encoding="utf-8")
    before = _recorded(engine)
    with pytest.raises(MigrationDriftError):
        apply_migrations(staged, engine=engine)
    assert _recorded(engine) == before
    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('mi_drift_marker')")).scalar() is None


def test_scenario_c_unknown_null_sha_row_is_refused_not_blessed(scratch_db):
    engine, _url = scratch_db
    apply_migrations(engine=engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE schema_migrations SET sha256 = NULL WHERE filename = '027_ai_gateway_strategy_views.sql'"))
    # 027 is not part of the trusted 001-019 baseline: a NULL hash on it cannot be adopted.
    with pytest.raises(MigrationDriftError):
        apply_migrations(engine=engine)


def test_scenario_d_readonly_role_covers_gateway_views_and_cannot_mutate(scratch_db, pg_admin_url, tmp_path):
    from tests.test_mi_pipeline import provision_role_with_psql

    engine, url = scratch_db
    apply_migrations(engine=engine)
    role = "mig_ro_{0}".format(uuid.uuid4().hex[:8])
    password = "pw-{0}".format(uuid.uuid4().hex[:10])
    admin_on_db = make_url(pg_admin_url).set(database=url.database, drivername="postgresql").render_as_string(hide_password=False)
    result = provision_role_with_psql(admin_on_db, role, password, tmp_path)
    assert result.returncode == 0, result.stderr
    ro = create_engine(url.set(username=role, password=password), future=True, connect_args={"options": "-c default_transaction_read_only=on"})
    try:
        with ro.connect() as conn:
            for view in GATEWAY_VIEWS:
                conn.execute(text("SELECT * FROM {0} LIMIT 1".format(view)))
            grants = {
                r[0]
                for r in conn.execute(text("SELECT table_name FROM information_schema.role_table_grants WHERE grantee = :r AND privilege_type = 'SELECT'"), {"r": role})
            }
        assert set(GATEWAY_VIEWS) <= grants
        assert all(name.startswith("mi_v_") for name in grants), sorted(n for n in grants if not n.startswith("mi_v_"))
        with ro.connect() as conn:
            writer_grants = conn.execute(
                text("SELECT COUNT(*) FROM information_schema.role_table_grants WHERE grantee = :r AND privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER')"),
                {"r": role},
            ).scalar()
        assert writer_grants == 0
        for sql in (
            "INSERT INTO research_runs (research_run_id, strategy_id) VALUES ('x', 'y')",
            "SELECT * FROM research_artifacts LIMIT 1",
            "SELECT * FROM backtests LIMIT 1",
            "UPDATE mi_source_registry SET enabled = TRUE",
            "CREATE TABLE escalate (x INT)",
        ):
            with ro.connect() as conn:
                with pytest.raises(Exception) as exc:
                    conn.execute(text(sql))
                    conn.commit()
            assert getattr(getattr(exc.value, "orig", None), "pgcode", None) in {"42501", "25006"}, sql
    finally:
        ro.dispose()
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = :r AND pid <> pg_backend_pid()"), {"r": role})
            conn.execute(text('DROP OWNED BY "{0}"'.format(role)))
        admin = create_engine(pg_admin_url, isolation_level="AUTOCOMMIT", future=True)
        with admin.connect() as conn:
            conn.execute(text('DROP ROLE IF EXISTS "{0}"'.format(role)))
        admin.dispose()


def test_baseline_file_matches_committed_001_019_bytes():
    baseline = load_checksum_baseline()
    for name, digest in baseline["by_name"].items():
        assert hashlib.sha256((MIGRATIONS / name).read_bytes()).hexdigest() == digest
    assert baseline["baseline_git_sha"] == "2ed4da99df28d06e7730e56601de440e870dc823"
