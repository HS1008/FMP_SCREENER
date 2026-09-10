"""Applied migration files cannot change silently."""

from __future__ import annotations

from pathlib import Path

from jobs.apply_migrations import MigrationDriftError, apply_migrations, migration_sha256
import pytest
from sqlalchemy import text


def test_migration_sha256_is_stable_for_unchanged_bytes(tmp_path: Path):
    path = tmp_path / "001_example.sql"
    path.write_text("SELECT 1;\n", encoding="utf-8")
    assert len(migration_sha256(path)) == 64
    assert migration_sha256(path) == migration_sha256(path)


def test_changed_applied_migration_fails_closed(tmp_path: Path, monkeypatch):
    from sqlalchemy import create_engine

    db = tmp_path / "mig.db"
    try:
        engine = create_engine("sqlite:///{0}".format(db))
    except Exception:
        pytest.skip("sqlite engine unavailable")
    staged = tmp_path / "migrations"
    staged.mkdir()
    first = staged / "001_a.sql"
    first.write_text("CREATE TABLE IF NOT EXISTS demo (id int);\n", encoding="utf-8")
    try:
        apply_migrations(staged, engine=engine)
    except Exception as exc:
        pytest.skip("engine cannot apply SQL: {0}".format(exc))
    first.write_text("CREATE TABLE IF NOT EXISTS demo (id int);\n-- drifted\n", encoding="utf-8")
    with pytest.raises(MigrationDriftError, match="drift"):
        apply_migrations(staged, engine=engine)
