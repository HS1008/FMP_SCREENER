"""Verify the Streamlit identity can SELECT and cannot mutate.

Never prints URLs or passwords. Exit 0 ok, 2 failed, 3 config.
"""

from __future__ import annotations

import argparse
import os

from sqlalchemy import create_engine, text


REQUIRED_SELECTS = (
    "SELECT COUNT(*) FROM research_runs",
    "SELECT COUNT(*) FROM strategies",
    "SELECT COUNT(*) FROM backtests",
    "SELECT COUNT(*) FROM research_artifacts",
)

OPTIONAL_SELECTS = (
    "SELECT dashboard_streamlit_readonly FROM mi_v_ops_status LIMIT 1",
    "SELECT COUNT(*) FROM live_snapshots",
    "SELECT COUNT(*) FROM positions",
    "SELECT COUNT(*) FROM orders",
    "SELECT COUNT(*) FROM trades",
)

CORE_MUTATION_PROBES = (
    ("INSERT INTO research_runs (research_run_id, strategy_id) VALUES ('x', 'x')", "INSERT"),
    ("UPDATE research_runs SET strategy_id = strategy_id WHERE FALSE", "UPDATE"),
    ("DELETE FROM research_runs WHERE FALSE", "DELETE"),
    ("INSERT INTO backtests (backtest_id, strategy_id) VALUES ('x', 'x')", "INSERT_BACKTESTS"),
    ("UPDATE backtests SET strategy_id = strategy_id WHERE FALSE", "UPDATE_BACKTESTS"),
    ("DELETE FROM backtests WHERE FALSE", "DELETE_BACKTESTS"),
    ("INSERT INTO research_artifacts (artifact_key) VALUES ('x')", "INSERT_ARTIFACTS"),
    ("UPDATE research_artifacts SET artifact_type = artifact_type WHERE FALSE", "UPDATE_ARTIFACTS"),
    ("DELETE FROM research_artifacts WHERE FALSE", "DELETE_ARTIFACTS"),
    ("CREATE TABLE dashboard_readonly_probe (id int)", "CREATE"),
)

OPTIONAL_MUTATION_PROBES = (
    ("INSERT INTO live_snapshots (strategy_id) VALUES ('x')", "INSERT_LIVE"),
    ("UPDATE live_snapshots SET strategy_id = strategy_id WHERE FALSE", "UPDATE_LIVE"),
    ("DELETE FROM live_snapshots WHERE FALSE", "DELETE_LIVE"),
    ("INSERT INTO positions (strategy_id) VALUES ('x')", "INSERT_POSITIONS"),
    ("UPDATE positions SET strategy_id = strategy_id WHERE FALSE", "UPDATE_POSITIONS"),
    ("DELETE FROM positions WHERE FALSE", "DELETE_POSITIONS"),
    ("INSERT INTO orders (strategy_id) VALUES ('x')", "INSERT_ORDERS"),
    ("UPDATE orders SET strategy_id = strategy_id WHERE FALSE", "UPDATE_ORDERS"),
    ("DELETE FROM orders WHERE FALSE", "DELETE_ORDERS"),
    ("INSERT INTO trades (strategy_id) VALUES ('x')", "INSERT_TRADES"),
    ("UPDATE trades SET strategy_id = strategy_id WHERE FALSE", "UPDATE_TRADES"),
    ("DELETE FROM trades WHERE FALSE", "DELETE_TRADES"),
)


def _url() -> str:
    return (os.environ.get("DASHBOARD_READONLY_URL") or "").strip()


def run() -> int:
    url = _url()
    if not url:
        print("DASHBOARD_READONLY_URL unset; dashboard readonly verify failed (config)")
        return 3
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg2://", 1)
        if url.startswith("postgresql://") and "+psycopg2" not in url
        else url
    )
    with engine.connect() as conn:
        for statement in REQUIRED_SELECTS:
            conn.execute(text(statement))
        for statement in OPTIONAL_SELECTS:
            try:
                conn.execute(text(statement))
            except Exception:
                conn.rollback()
        denied = []
        for statement, label in CORE_MUTATION_PROBES + OPTIONAL_MUTATION_PROBES:
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
    print("readonly_monitor_tables=denied")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify dashboard readonly identity")
    parser.parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
