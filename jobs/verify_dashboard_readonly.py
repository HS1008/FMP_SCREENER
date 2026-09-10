"""Verify the Streamlit identity can SELECT and cannot mutate.

Never prints URLs or passwords. Exit 0 ok, 2 failed, 3 config.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text


def _url() -> str:
    return (os.environ.get("DASHBOARD_READONLY_URL") or "").strip()


def run() -> int:
    url = _url()
    if not url:
        print("DASHBOARD_READONLY_URL unset; dashboard readonly verify skipped")
        return 3
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg2://", 1)
        if url.startswith("postgresql://") and "+psycopg2" not in url
        else url
    )
    with engine.connect() as conn:
        conn.execute(text("SELECT COUNT(*) FROM research_runs"))
        conn.execute(text("SELECT COUNT(*) FROM strategies"))
        denied = []
        for statement, label in (
            ("INSERT INTO research_runs (research_run_id, strategy_id) VALUES ('x', 'x')", "INSERT"),
            ("UPDATE research_runs SET strategy_id = strategy_id WHERE FALSE", "UPDATE"),
            ("DELETE FROM research_runs WHERE FALSE", "DELETE"),
            ("CREATE TABLE dashboard_readonly_probe (id int)", "CREATE"),
        ):
            try:
                conn.execute(text(statement))
                conn.rollback()
                denied.append(label)
            except Exception:
                conn.rollback()
        if denied:
            print("READONLY_VERIFY_FAILED mutations_succeeded={0}".format(",".join(denied)))
            return 2
    print("readonly_select=ok")
    print("readonly_insert=denied")
    print("readonly_update=denied")
    print("readonly_delete=denied")
    print("readonly_create=denied")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify dashboard readonly identity")
    parser.parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
