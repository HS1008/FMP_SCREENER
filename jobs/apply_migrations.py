"""Apply idempotent SQL migrations before Streamlit restarts."""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import text

from db.connection import engine


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


def ensure_migrations_table(conn) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )


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


def apply_migrations(migrations_dir: Path | None = None) -> list[str]:
    directory = migrations_dir or MIGRATIONS_DIR
    if not directory.is_dir():
        raise SystemExit("No migrations directory at {0}".format(directory))

    files = sorted(path for path in directory.glob("*.sql") if path.is_file())
    applied = []
    with engine.begin() as conn:
        ensure_migrations_table(conn)
        already = applied_filenames(conn)
        for path in files:
            sql = path.read_text(encoding="utf-8")
            for statement in split_sql_statements(sql):
                conn.execute(text(statement))
            if path.name not in already:
                conn.execute(
                    text(
                        """
                        INSERT INTO schema_migrations (filename)
                        VALUES (:filename)
                        ON CONFLICT (filename) DO NOTHING
                        """
                    ),
                    {"filename": path.name},
                )
                applied.append(path.name)
            else:
                applied.append("{0} (rechecked)".format(path.name))
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply FMP_SCREENER database migrations.")
    parser.parse_args()
    applied = apply_migrations()
    print("Migrations:")
    for name in applied:
        print("  {0}".format(name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
