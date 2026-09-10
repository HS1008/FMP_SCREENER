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
