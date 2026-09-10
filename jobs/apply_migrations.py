"""Apply idempotent SQL migrations before Streamlit restarts.

Default behavior:
  - create schema_migrations
  - skip filenames already recorded
  - fail if an already-applied file's sha256 drifted
  - execute only unapplied migrations
  - insert the filename and sha256 only after successful execution

``--recheck`` re-executes already-applied SQL and requires
``MIGRATIONS_RECHECK_OK=1``. Changed SQL requires a new migration filename.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

from sqlalchemy import text


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


class MigrationDriftError(RuntimeError):
    """An already-applied migration file changed contents."""


def migration_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_migrations_table(conn) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                sha256 TEXT
            )
            """
        )
    )
    conn.execute(text("ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS sha256 TEXT"))


def applied_filenames(conn) -> set[str]:
    rows = conn.execute(text("SELECT filename FROM schema_migrations")).fetchall()
    return {row[0] for row in rows}


def split_sql_statements(sql: str) -> list[str]:
    statements = []
    buf: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(buf).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            buf = []
    tail = "\n".join(buf).strip().rstrip(";").strip()
    if tail:
        statements.append(tail)
    return statements


def pending_migration_files(
    files: list[Path],
    already: set[str],
    *,
    recheck: bool = False,
) -> list[Path]:
    if recheck:
        return list(files)
    return [path for path in files if path.name not in already]


def apply_migrations(
    migrations_dir: Path | None = None,
    *,
    recheck: bool = False,
    engine=None,
) -> list[str]:
    directory = migrations_dir or MIGRATIONS_DIR
    if not directory.is_dir():
        raise SystemExit("No migrations directory at {0}".format(directory))

    files = sorted(path for path in directory.glob("*.sql") if path.is_file())
    applied = []
    if engine is None:
        from db.connection import engine as default_engine

        engine = default_engine
    with engine.begin() as conn:
        ensure_migrations_table(conn)
        already = applied_filenames(conn)
        pending = pending_migration_files(files, already, recheck=recheck)
        skipped = [path for path in files if path not in pending]
        for path in skipped:
            recorded = conn.execute(
                text("SELECT sha256 FROM schema_migrations WHERE filename = :filename"),
                {"filename": path.name},
            ).scalar()
            digest = migration_sha256(path)
            if recorded and recorded != digest:
                raise MigrationDriftError(
                    "Migration drift: {0} changed after apply (recorded {1}, file {2}). "
                    "Add a new migration filename instead of editing applied SQL.".format(
                        path.name, recorded, digest
                    )
                )
            if not recorded:
                conn.execute(
                    text("UPDATE schema_migrations SET sha256 = :sha256 WHERE filename = :filename"),
                    {"filename": path.name, "sha256": digest},
                )
            applied.append("{0} (skipped)".format(path.name))
        for path in pending:
            sql = path.read_text(encoding="utf-8")
            for statement in split_sql_statements(sql):
                conn.execute(text(statement))
            digest = migration_sha256(path)
            if path.name not in already:
                conn.execute(
                    text(
                        """
                        INSERT INTO schema_migrations (filename, sha256)
                        VALUES (:filename, :sha256)
                        ON CONFLICT (filename) DO UPDATE SET sha256 = EXCLUDED.sha256
                        """
                    ),
                    {"filename": path.name, "sha256": digest},
                )
            applied.append(path.name if not recheck else "{0} (rechecked)".format(path.name))
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply FMP_SCREENER database migrations.")
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="Re-execute already applied SQL files. Not the default.",
    )
    args = parser.parse_args(argv)
    if args.recheck and (os.environ.get("MIGRATIONS_RECHECK_OK") or "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        print("FAIL: --recheck requires MIGRATIONS_RECHECK_OK=1")
        return 3
    applied = apply_migrations(recheck=bool(args.recheck))
    print("Migrations:")
    for name in applied:
        print("  {0}".format(name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
