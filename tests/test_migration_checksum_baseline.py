"""Trusted checksum baseline adopts filename-only production rows without an env bless."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from jobs.apply_migrations import (
    BASELINE_PATH,
    MigrationDriftError,
    apply_migrations,
    load_checksum_baseline,
    migration_sha256,
)


ROOT = Path(__file__).resolve().parents[1]


def _engine(tmp_path: Path):
    try:
        return create_engine("sqlite:///{0}".format(tmp_path / "mig.db"))
    except Exception as exc:
        pytest.skip("sqlite engine unavailable: {0}".format(exc))


def _baseline_for(files: dict[str, str], tmp_path: Path) -> Path:
    rows = [
        {
            "filename": name,
            "sha256": migration_sha256(path),
            "baseline_git_sha": "2ed4da99df28d06e7730e56601de440e870dc823",
        }
        for name, path in files.items()
    ]
    target = tmp_path / "baseline.json"
    target.write_text(
        json.dumps(
            {
                "schema_version": "migration_checksum_baseline_v1",
                "baseline_git_sha": "2ed4da99df28d06e7730e56601de440e870dc823",
                "migrations": rows,
            }
        ),
        encoding="utf-8",
    )
    return target


def test_committed_baseline_covers_pre_checksum_main_migrations():
    payload = load_checksum_baseline()
    assert payload["baseline_git_sha"] == "2ed4da99df28d06e7730e56601de440e870dc823"
    names = set(payload["by_name"])
    assert "001_stage1_research.sql" in names
    assert "019_finra_identity_quarantine.sql" in names
    assert "020_ops_status_and_migration_checksum.sql" not in names
    for name, digest in payload["by_name"].items():
        path = ROOT / "db" / "migrations" / name
        assert path.is_file()
        assert migration_sha256(path) == digest
    source = (ROOT / "jobs" / "apply_migrations.py").read_text(encoding="utf-8")
    assert "MIGRATIONS_BACKFILL_SHA256" not in source
    assert "trusted baseline" in source


def test_clean_fresh_db_writes_hash_and_second_apply_is_noop(tmp_path):
    engine = _engine(tmp_path)
    staged = tmp_path / "migrations"
    staged.mkdir()
    first = staged / "001_a.sql"
    first.write_text("CREATE TABLE IF NOT EXISTS demo (id int);\n", encoding="utf-8")
    extra = staged / "002_b.sql"
    extra.write_text("CREATE TABLE IF NOT EXISTS demo2 (id int);\n", encoding="utf-8")
    baseline = _baseline_for({}, tmp_path)
    applied = apply_migrations(staged, engine=engine, baseline_path=baseline)
    assert applied == ["001_a.sql", "002_b.sql"]
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT filename, sha256 FROM schema_migrations ORDER BY filename")).fetchall()
    assert [row[0] for row in rows] == ["001_a.sql", "002_b.sql"]
    assert all(row[1] == migration_sha256(staged / row[0]) for row in rows)
    again = apply_migrations(staged, engine=engine, baseline_path=baseline)
    assert again == ["001_a.sql (skipped)", "002_b.sql (skipped)"]


def test_legacy_filename_only_rows_adopt_trusted_baseline(tmp_path):
    engine = _engine(tmp_path)
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
    baseline = _baseline_for({"001_a.sql": first}, tmp_path)
    applied = apply_migrations(staged, engine=engine, baseline_path=baseline)
    assert applied == ["001_a.sql (skipped)"]
    with engine.connect() as conn:
        recorded = conn.execute(
            text("SELECT sha256 FROM schema_migrations WHERE filename = '001_a.sql'")
        ).scalar()
    assert recorded == migration_sha256(first)
    again = apply_migrations(staged, engine=engine, baseline_path=baseline)
    assert again == ["001_a.sql (skipped)"]


def test_historical_hash_mismatch_refuses(tmp_path):
    engine = _engine(tmp_path)
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
    first.write_text("CREATE TABLE IF NOT EXISTS demo (id int);\n-- drifted\n", encoding="utf-8")
    trusted = tmp_path / "baseline.json"
    trusted.write_text(
        json.dumps(
            {
                "schema_version": "migration_checksum_baseline_v1",
                "baseline_git_sha": "2ed4da99df28d06e7730e56601de440e870dc823",
                "migrations": [
                    {
                        "filename": "001_a.sql",
                        "sha256": "0" * 64,
                        "baseline_git_sha": "2ed4da99df28d06e7730e56601de440e870dc823",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(MigrationDriftError, match="does not match trusted baseline"):
        apply_migrations(staged, engine=engine, baseline_path=trusted)


def test_unknown_historical_null_sha_refuses(tmp_path):
    engine = _engine(tmp_path)
    staged = tmp_path / "migrations"
    staged.mkdir()
    first = staged / "099_unknown.sql"
    first.write_text("CREATE TABLE IF NOT EXISTS demo (id int);\n", encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, applied_at TEXT, sha256 TEXT)"
            )
        )
        conn.execute(text("INSERT INTO schema_migrations (filename) VALUES ('099_unknown.sql')"))
    baseline = _baseline_for({}, tmp_path)
    with pytest.raises(MigrationDriftError, match="not in the trusted"):
        apply_migrations(staged, engine=engine, baseline_path=baseline)


def test_modified_already_hashed_migration_refuses(tmp_path):
    engine = _engine(tmp_path)
    staged = tmp_path / "migrations"
    staged.mkdir()
    first = staged / "001_a.sql"
    first.write_text("CREATE TABLE IF NOT EXISTS demo (id int);\n", encoding="utf-8")
    baseline = _baseline_for({}, tmp_path)
    apply_migrations(staged, engine=engine, baseline_path=baseline)
    first.write_text("CREATE TABLE IF NOT EXISTS demo (id int);\n-- drifted\n", encoding="utf-8")
    with pytest.raises(MigrationDriftError, match="drift"):
        apply_migrations(staged, engine=engine, baseline_path=baseline)


def test_new_migration_receives_hash_after_legacy_backfill(tmp_path):
    engine = _engine(tmp_path)
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
    second = staged / "002_new.sql"
    second.write_text("CREATE TABLE IF NOT EXISTS demo2 (id int);\n", encoding="utf-8")
    baseline = _baseline_for({"001_a.sql": first}, tmp_path)
    applied = apply_migrations(staged, engine=engine, baseline_path=baseline)
    assert "001_a.sql (skipped)" in applied
    assert "002_new.sql" in applied
    with engine.connect() as conn:
        rows = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT filename, sha256 FROM schema_migrations")).fetchall()
        }
    assert rows["001_a.sql"] == migration_sha256(first)
    assert rows["002_new.sql"] == migration_sha256(second)


def test_recheck_cli_still_requires_opt_in(monkeypatch):
    from jobs.apply_migrations import main

    monkeypatch.delenv("MIGRATIONS_RECHECK_OK", raising=False)
    assert main(["--recheck"]) == 3


def test_new_apply_does_not_overwrite_recorded_sha256_on_conflict():
    source = (ROOT / "jobs" / "apply_migrations.py").read_text(encoding="utf-8")
    insert_block = source.split("INSERT INTO schema_migrations", 1)[1].split("applied.append", 1)[0]
    assert "DO UPDATE SET sha256" not in insert_block
    assert "ON CONFLICT" not in insert_block
    assert BASELINE_PATH.is_file()
