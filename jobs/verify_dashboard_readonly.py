"""Verify the Streamlit identity can SELECT and cannot mutate.

Privilege denial must be proven. Constraint/schema errors are not read-only.
Never prints URLs or passwords. Exit 0 ok, 2 failed, 3 config.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from sqlalchemy import create_engine, text


REQUIRED_SELECTS = (
    "SELECT COUNT(*) FROM research_runs",
    "SELECT COUNT(*) FROM strategies",
    "SELECT COUNT(*) FROM backtests",
    "SELECT COUNT(*) FROM research_artifacts",
    "SELECT COUNT(*) FROM backtest_equity_points",
    "SELECT COUNT(*) FROM ml_trials",
    "SELECT COUNT(*) FROM ml_models",
    "SELECT COUNT(*) FROM research_experiments",
)

OPTIONAL_SELECTS = (
    "SELECT dashboard_streamlit_readonly FROM mi_v_ops_status LIMIT 1",
    "SELECT COUNT(*) FROM live_snapshots",
    "SELECT COUNT(*) FROM positions",
    "SELECT COUNT(*) FROM orders",
    "SELECT COUNT(*) FROM trades",
    "SELECT COUNT(*) FROM ml_feature_diagnostics",
    "SELECT COUNT(*) FROM ml_signal_points",
    "SELECT COUNT(*) FROM research_trials",
    "SELECT COUNT(*) FROM research_oos_windows",
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
    ("INSERT INTO strategies (strategy_id) VALUES ('x')", "INSERT_STRATEGIES"),
    ("UPDATE strategies SET strategy_id = strategy_id WHERE FALSE", "UPDATE_STRATEGIES"),
    ("DELETE FROM strategies WHERE FALSE", "DELETE_STRATEGIES"),
    (
        "INSERT INTO backtest_equity_points (backtest_id, strategy_id, timestamp) "
        "VALUES ('x', 'x', '2020-01-01')",
        "INSERT_EQUITY",
    ),
    ("UPDATE backtest_equity_points SET strategy_id = strategy_id WHERE FALSE", "UPDATE_EQUITY"),
    ("DELETE FROM backtest_equity_points WHERE FALSE", "DELETE_EQUITY"),
    (
        "INSERT INTO ml_trials (research_run_id, outer_window_id, trial_id) VALUES ('x', 'x', 'x')",
        "INSERT_ML_TRIALS",
    ),
    ("UPDATE ml_trials SET trial_id = trial_id WHERE FALSE", "UPDATE_ML_TRIALS"),
    ("DELETE FROM ml_trials WHERE FALSE", "DELETE_ML_TRIALS"),
    (
        "INSERT INTO ml_models (model_id, research_run_id) VALUES ('x', 'x')",
        "INSERT_ML_MODELS",
    ),
    ("UPDATE ml_models SET research_run_id = research_run_id WHERE FALSE", "UPDATE_ML_MODELS"),
    ("DELETE FROM ml_models WHERE FALSE", "DELETE_ML_MODELS"),
    (
        "INSERT INTO research_experiments (research_run_id, experiment_id) VALUES ('x', 'x')",
        "INSERT_EXPERIMENTS",
    ),
    (
        "UPDATE research_experiments SET experiment_id = experiment_id WHERE FALSE",
        "UPDATE_EXPERIMENTS",
    ),
    ("DELETE FROM research_experiments WHERE FALSE", "DELETE_EXPERIMENTS"),
    (
        "INSERT INTO holdout_exposures "
        "(strategy_id, research_lineage_id, holdout_start, status) "
        "VALUES ('x', 'x', '2020-01-01', 'x')",
        "INSERT_HOLDOUT",
    ),
    ("UPDATE holdout_exposures SET status = status WHERE FALSE", "UPDATE_HOLDOUT"),
    ("DELETE FROM holdout_exposures WHERE FALSE", "DELETE_HOLDOUT"),
    (
        "INSERT INTO strategy_specs (strategy_spec_hash, strategy_id, spec_json) "
        "VALUES ('x', 'x', '{}')",
        "INSERT_SPECS",
    ),
    ("UPDATE strategy_specs SET strategy_id = strategy_id WHERE FALSE", "UPDATE_SPECS"),
    ("DELETE FROM strategy_specs WHERE FALSE", "DELETE_SPECS"),
    (
        "INSERT INTO research_pair_diagnostics (research_run_id, pair_left, pair_right) "
        "VALUES ('x', 'x', 'x')",
        "INSERT_PAIRS",
    ),
    (
        "UPDATE research_pair_diagnostics SET pair_left = pair_left WHERE FALSE",
        "UPDATE_PAIRS",
    ),
    ("DELETE FROM research_pair_diagnostics WHERE FALSE", "DELETE_PAIRS"),
    (
        "INSERT INTO research_fixed_income_metrics (research_run_id, metric_name) "
        "VALUES ('x', 'x')",
        "INSERT_FI",
    ),
    (
        "UPDATE research_fixed_income_metrics SET metric_name = metric_name WHERE FALSE",
        "UPDATE_FI",
    ),
    ("DELETE FROM research_fixed_income_metrics WHERE FALSE", "DELETE_FI"),
    (
        "INSERT INTO research_risk_metrics (research_run_id, metric_name) VALUES ('x', 'x')",
        "INSERT_RISK",
    ),
    ("UPDATE research_risk_metrics SET metric_name = metric_name WHERE FALSE", "UPDATE_RISK"),
    ("DELETE FROM research_risk_metrics WHERE FALSE", "DELETE_RISK"),
    ("CREATE TABLE dashboard_readonly_probe (id int)", "CREATE"),
    ("TRUNCATE TABLE research_runs", "TRUNCATE"),
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
    (
        "INSERT INTO ml_feature_diagnostics (research_run_id, outer_window_id, feature_name) "
        "VALUES ('x', 'x', 'x')",
        "INSERT_FEATURE_DIAG",
    ),
    (
        "UPDATE ml_feature_diagnostics SET feature_name = feature_name WHERE FALSE",
        "UPDATE_FEATURE_DIAG",
    ),
    ("DELETE FROM ml_feature_diagnostics WHERE FALSE", "DELETE_FEATURE_DIAG"),
    (
        "INSERT INTO ml_signal_points (backtest_id, research_run_id, timestamp) "
        "VALUES ('x', 'x', '2020-01-01')",
        "INSERT_SIGNAL_POINTS",
    ),
    ("UPDATE ml_signal_points SET backtest_id = backtest_id WHERE FALSE", "UPDATE_SIGNAL_POINTS"),
    ("DELETE FROM ml_signal_points WHERE FALSE", "DELETE_SIGNAL_POINTS"),
    (
        "INSERT INTO research_trials (research_run_id, trial_id) VALUES ('x', 'x')",
        "INSERT_RESEARCH_TRIALS",
    ),
    ("UPDATE research_trials SET trial_id = trial_id WHERE FALSE", "UPDATE_RESEARCH_TRIALS"),
    ("DELETE FROM research_trials WHERE FALSE", "DELETE_RESEARCH_TRIALS"),
    (
        "INSERT INTO research_oos_windows (research_run_id, outer_window_id) VALUES ('x', 'x')",
        "INSERT_OOS_WINDOWS",
    ),
    (
        "UPDATE research_oos_windows SET outer_window_id = outer_window_id WHERE FALSE",
        "UPDATE_OOS_WINDOWS",
    ),
    ("DELETE FROM research_oos_windows WHERE FALSE", "DELETE_OOS_WINDOWS"),
)

DENIED_SQLSTATES = frozenset({"42501", "25006"})
DML_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
WRITER_ROLE_MARKERS = (
    "fmp_writer",
    "writer",
    "mi_writer",
    "market_intelligence",
    "postgres",
    "rds_superuser",
)


class ReadonlyVerifyError(RuntimeError):
    """Dashboard identity is not proven read-only."""


def _url() -> str:
    return (os.environ.get("DASHBOARD_READONLY_URL") or "").strip()


def expected_role_name() -> str:
    override = (os.environ.get("DASHBOARD_READONLY_ROLE") or "").strip()
    if override:
        return override
    url = _url()
    if "://" in url:
        rest = url.split("://", 1)[1]
        user = rest.split("@", 1)[0].split(":", 1)[0]
        if user:
            return user
    return "dashboard_readonly"


def sqlstate_of(exc: BaseException) -> str:
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "pgcode", None) or getattr(exc, "pgcode", None)
    if code:
        return str(code)
    message = str(exc).lower()
    if "permission denied" in message or "must be owner" in message:
        return "42501"
    if "read-only" in message or "cannot execute" in message and "read-only" in message:
        return "25006"
    return ""


def mutation_is_privilege_denial(exc: BaseException) -> bool:
    return sqlstate_of(exc) in DENIED_SQLSTATES


def _scalar(conn, statement: str, **params: Any) -> Any:
    return conn.execute(text(statement), params).scalar()


def prove_session_identity(conn, expected: str) -> None:
    current = str(_scalar(conn, "SELECT current_user") or "")
    session = str(_scalar(conn, "SELECT session_user") or "")
    if current != expected or session != expected:
        raise ReadonlyVerifyError(
            "identity mismatch current_user={0} session_user={1} expected={2}".format(
                current, session, expected
            )
        )
    txn = str(_scalar(conn, "SHOW transaction_read_only") or "").lower()
    default = str(_scalar(conn, "SHOW default_transaction_read_only") or "").lower()
    if txn not in {"on"} or default not in {"on"}:
        raise ReadonlyVerifyError(
            "transaction_read_only={0} default_transaction_read_only={1}".format(txn, default)
        )


def prove_privileges(conn, role: str) -> None:
    if _scalar(conn, "SELECT has_schema_privilege(:r, 'public', 'CREATE')", r=role):
        raise ReadonlyVerifyError(
            "role {0} has CREATE on schema public (direct or PUBLIC-inherited)".format(role)
        )
    tables = conn.execute(
        text(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type IN ('BASE TABLE', 'VIEW')
            """
        )
    ).fetchall()
    for (name,) in tables:
        for priv in DML_PRIVILEGES:
            if _scalar(
                conn,
                "SELECT has_table_privilege(:r, :t, :p)",
                r=role,
                t="public.{0}".format(name),
                p=priv,
            ):
                raise ReadonlyVerifyError(
                    "role {0} has {1} on {2}".format(role, priv, name)
                )
    sequences = conn.execute(
        text(
            """
            SELECT sequence_name FROM information_schema.sequences
            WHERE sequence_schema = 'public'
            """
        )
    ).fetchall()
    for (name,) in sequences:
        for priv in ("USAGE", "UPDATE"):
            if _scalar(
                conn,
                "SELECT has_sequence_privilege(:r, :s, :p)",
                r=role,
                s="public.{0}".format(name),
                p=priv,
            ):
                raise ReadonlyVerifyError(
                    "role {0} has {1} on sequence {2}".format(role, priv, name)
                )
    members = conn.execute(
        text(
            """
            SELECT r.rolname
            FROM pg_auth_members m
            JOIN pg_roles u ON u.oid = m.member
            JOIN pg_roles r ON r.oid = m.roleid
            WHERE u.rolname = :role
            """
        ),
        {"role": role},
    ).fetchall()
    dangerous = [
        str(row[0])
        for row in members
        if any(marker in str(row[0]).lower() for marker in WRITER_ROLE_MARKERS)
        or str(row[0]).lower() in {"pg_write_all_data", "pg_database_owner"}
    ]
    if dangerous:
        raise ReadonlyVerifyError(
            "role {0} is a member of writer/admin identities: {1}".format(
                role, ",".join(dangerous)
            )
        )


def _probe_mutation(conn, statement: str, label: str, *, optional: bool) -> None:
    conn.execute(text("SAVEPOINT readonly_probe"))
    try:
        conn.execute(text(statement))
    except Exception as exc:
        conn.execute(text("ROLLBACK TO SAVEPOINT readonly_probe"))
        conn.execute(text("RELEASE SAVEPOINT readonly_probe"))
        if mutation_is_privilege_denial(exc):
            return
        if optional and "does not exist" in str(exc).lower():
            return
        raise ReadonlyVerifyError(
            "mutation {0} failed with SQLSTATE {1}, not privilege/read-only denial".format(
                label, sqlstate_of(exc) or "unknown"
            )
        ) from exc
    conn.execute(text("ROLLBACK TO SAVEPOINT readonly_probe"))
    conn.execute(text("RELEASE SAVEPOINT readonly_probe"))
    raise ReadonlyVerifyError("mutation {0} succeeded".format(label))


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
    expected = expected_role_name()
    try:
        with engine.connect() as conn:
            prove_session_identity(conn, expected)
            prove_privileges(conn, expected)
            for statement in REQUIRED_SELECTS:
                conn.execute(text(statement))
            for statement in OPTIONAL_SELECTS:
                try:
                    conn.execute(text("SAVEPOINT optional_select"))
                    conn.execute(text(statement))
                    conn.execute(text("RELEASE SAVEPOINT optional_select"))
                except Exception:
                    conn.execute(text("ROLLBACK TO SAVEPOINT optional_select"))
                    conn.execute(text("RELEASE SAVEPOINT optional_select"))
            for statement, label in CORE_MUTATION_PROBES:
                _probe_mutation(conn, statement, label, optional=False)
            for statement, label in OPTIONAL_MUTATION_PROBES:
                _probe_mutation(conn, statement, label, optional=True)
            conn.rollback()
    except ReadonlyVerifyError as exc:
        print("READONLY_VERIFY_FAILED {0}".format(exc))
        return 2
    print("readonly_select=ok")
    print("readonly_insert=denied")
    print("readonly_update=denied")
    print("readonly_delete=denied")
    print("readonly_truncate=denied")
    print("readonly_create=denied")
    print("readonly_monitor_tables=denied")
    print("readonly_current_user={0}".format(expected))
    print("readonly_session_user={0}".format(expected))
    print("readonly_transaction_read_only=on")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify dashboard readonly identity")
    parser.parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
