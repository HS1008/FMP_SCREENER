"""PostgreSQL SELECT loaders used by Strategy Monitor.

Read-only. Missing engine stays empty. Research query failures raise so a
down or permission-denied database cannot look like an empty library.
Paper/live tables stay optional and do not fail the research page.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError


def read_sql(engine, sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
    if engine is None:
        return pd.DataFrame()
    return pd.read_sql(text(sql), engine, params=params or {})


def read_sql_allow_missing_relation(
    engine, sql: str, params: dict[str, Any] | None = None
) -> pd.DataFrame:
    """Empty only when the relation or column is absent. Other DB errors raise."""
    try:
        return read_sql(engine, sql, params)
    except ProgrammingError:
        return pd.DataFrame()


def read_sql_optional(engine, sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
    """Paper/live tables. Query errors stay empty and do not fail research."""
    if engine is None:
        return pd.DataFrame()
    try:
        return pd.read_sql(text(sql), engine, params=params or {})
    except SQLAlchemyError:
        return pd.DataFrame()


def as_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


PLATFORM_RUN_IDS_SQL = """
        SELECT DISTINCT a.research_run_id
        FROM research_artifacts a
        LEFT JOIN research_runs r ON r.research_run_id = a.research_run_id
        WHERE COALESCE(r.research_kind, '') IS DISTINCT FROM 'stage2_ml'
          AND (
                r.strategy_id = :strategy_id
             OR r.research_lineage_id = :strategy_id
             OR r.research_lineage_id = :lineage
             OR a.payload_json->>'strategy_id' = :strategy_id
             OR a.payload_json->'payload'->>'strategy_id' = :strategy_id
             OR a.payload_json->'identity'->>'strategy_id' = :strategy_id
             OR a.payload_json->'payload'->'identity'->>'strategy_id' = :strategy_id
             OR a.payload_json->>'research_lineage_id' = :strategy_id
             OR a.payload_json->'payload'->>'research_lineage_id' = :strategy_id
             OR a.research_run_id LIKE :run_prefix
          )
        ORDER BY 1
        """


def load_platform_run_ids(engine, strategy_id: str) -> list[str]:
    if not strategy_id:
        return []
    lineage_rows = read_sql(
        engine,
        """
        SELECT research_lineage_id
        FROM research_runs
        WHERE strategy_id = :strategy_id
          AND research_kind = 'platform_research'
        ORDER BY last_seen_at DESC NULLS LAST
        LIMIT 1
        """,
        {"strategy_id": strategy_id},
    )
    lineage = strategy_id
    if lineage_rows is not None and not lineage_rows.empty:
        value = lineage_rows.iloc[0].get("research_lineage_id")
        if value:
            lineage = str(value)
    rows = read_sql(
        engine,
        PLATFORM_RUN_IDS_SQL,
        {
            "strategy_id": strategy_id,
            "lineage": lineage,
            "run_prefix": "PLATFORM_{0}_%".format(strategy_id),
        },
    )
    if rows is None or rows.empty:
        return []
    return [str(value) for value in rows["research_run_id"].dropna().astype(str).tolist() if value]


def load_stage2_trials(engine, research_run_id: str) -> pd.DataFrame:
    return read_sql(
        engine,
        """
        SELECT *
        FROM ml_trials
        WHERE research_run_id = :research_run_id
        ORDER BY outer_window_id, trial_id
        """,
        {"research_run_id": research_run_id},
    )


def load_stage2_models(engine, research_run_id: str) -> pd.DataFrame:
    return read_sql(
        engine,
        """
        SELECT *
        FROM ml_models
        WHERE research_run_id = :research_run_id
        ORDER BY outer_window_id
        """,
        {"research_run_id": research_run_id},
    )


def load_stage2_feature_diagnostics(engine, research_run_id: str) -> pd.DataFrame:
    return read_sql(
        engine,
        """
        SELECT *
        FROM ml_feature_diagnostics
        WHERE research_run_id = :research_run_id
        ORDER BY outer_window_id, coefficient_rank NULLS LAST
        """,
        {"research_run_id": research_run_id},
    )


def load_stage2_signal_points(engine, research_run_id: str) -> pd.DataFrame:
    return read_sql(
        engine,
        """
        SELECT *
        FROM ml_signal_points
        WHERE research_run_id = :research_run_id
        ORDER BY timestamp
        """,
        {"research_run_id": research_run_id},
    )


def load_stage2_run_ids(engine, strategy_id: str) -> list[str]:
    if engine is None:
        return []
    rows = read_sql(
        engine,
        """
        SELECT DISTINCT research_run_id
        FROM research_runs
        WHERE research_kind = 'stage2_ml'
          AND strategy_id = :strategy_id
        UNION
        SELECT DISTINCT research_run_id
        FROM research_artifacts
        WHERE research_run_id LIKE :prefix
        ORDER BY 1
        """,
        {
            "strategy_id": strategy_id,
            "prefix": "STAGE2_{0}_%".format(strategy_id),
        },
    )
    if rows is None or rows.empty:
        return []
    return [str(value) for value in rows["research_run_id"].dropna().astype(str).tolist() if value]


def load_stage2_artifact_payload(engine, research_run_id: str, artifact_type: str) -> dict[str, Any] | None:
    rows = read_sql(
        engine,
        """
        SELECT payload_json
        FROM research_artifacts
        WHERE research_run_id = :research_run_id
          AND artifact_type = :artifact_type
        ORDER BY synced_at DESC NULLS LAST
        LIMIT 1
        """,
        {"research_run_id": research_run_id, "artifact_type": artifact_type},
    )
    if rows is None or rows.empty:
        return None
    return as_payload(rows.iloc[0].get("payload_json"))


def _execute_one(engine, sql: str, params: dict[str, Any] | None = None):
    if engine is None:
        return None
    with engine.connect() as conn:
        return conn.execute(text(sql), params or {}).mappings().first()


def _execute_one_optional(engine, sql: str, params: dict[str, Any] | None = None):
    """Paper/live row. Query errors stay empty and do not fail research."""
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params or {}).mappings().first()
    except SQLAlchemyError:
        return None


STRATEGIES_SQL = """
        SELECT
            strategy_id,
            name,
            environment,
            status,
            qc_project_id,
            qc_deployment_id,
            qc_research_project_id,
            qc_research_project_name,
            git_commit,
            rules_json,
            created_at,
            updated_at
        FROM strategies
        ORDER BY name
        """

PLATFORM_STRATEGY_ROWS_SQL = """
            SELECT DISTINCT ON (strategy_id)
                strategy_id,
                strategy_id AS name,
                'research' AS environment,
                CASE
                    WHEN COALESCE(run_status, '') IN ('', 'HUMAN_REVIEW_REQUIRED', 'RESEARCH_COMPLETE')
                    THEN 'COMPLETE'
                    ELSE run_status
                END AS status,
                NULL::varchar AS qc_project_id,
                NULL::varchar AS qc_deployment_id,
                NULL::varchar AS qc_research_project_id,
                NULL::varchar AS qc_research_project_name,
                NULL::varchar AS git_commit,
                NULL::jsonb AS rules_json,
                first_seen_at AS created_at,
                last_seen_at AS updated_at,
                research_mode,
                research_kind,
                asset_class
            FROM research_runs
            WHERE research_kind = 'platform_research'
              AND strategy_id IS NOT NULL
              AND strategy_id <> ''
            ORDER BY strategy_id, last_seen_at DESC NULLS LAST
            """

STRATEGY_LABELS_SQL = """
            SELECT DISTINCT ON (strategy_id)
                strategy_id,
                research_mode,
                research_kind,
                asset_class,
                delivery_status,
                last_seen_at
            FROM research_runs
            WHERE strategy_id IS NOT NULL
              AND strategy_id <> ''
            ORDER BY strategy_id, last_seen_at DESC NULLS LAST
            """

STRATEGY_LABELS_FALLBACK_SQL = """
                SELECT DISTINCT ON (strategy_id)
                    strategy_id,
                    research_mode,
                    research_kind,
                    asset_class,
                    last_seen_at
                FROM research_runs
                WHERE strategy_id IS NOT NULL
                  AND strategy_id <> ''
                ORDER BY strategy_id, last_seen_at DESC NULLS LAST
                """

STRATEGY_BY_ID_SQL = """
            SELECT
                strategy_id,
                name,
                environment,
                status,
                qc_project_id,
                qc_deployment_id,
                qc_research_project_id,
                qc_research_project_name,
                git_commit,
                rules_json,
                created_at,
                updated_at
            FROM strategies
            WHERE strategy_id = :strategy_id
            """

LATEST_SNAPSHOT_SQL = """
        SELECT
            timestamp,
            equity,
            cash,
            holdings_value,
            daily_return,
            total_return,
            drawdown,
            status
        FROM live_snapshots
        WHERE strategy_id = :strategy_id
          AND equity > 0
        ORDER BY timestamp DESC
        LIMIT 1
    """

EQUITY_HISTORY_SQL = """
            SELECT
                timestamp,
                equity,
                cash,
                holdings_value,
                total_return,
                drawdown
            FROM live_snapshots
            WHERE strategy_id = :strategy_id
            ORDER BY timestamp ASC
        """

LATEST_POSITIONS_SQL = """
            SELECT
                symbol,
                quantity,
                price,
                market_value,
                weight,
                timestamp
            FROM positions
            WHERE strategy_id = :strategy_id
              AND timestamp = (
                  SELECT MAX(timestamp)
                  FROM positions
                  WHERE strategy_id = :strategy_id
              )
            ORDER BY ABS(market_value) DESC
        """

ORDERS_SQL = """
            SELECT
                timestamp,
                symbol,
                direction,
                quantity,
                order_type,
                status,
                fill_price,
                qc_order_id
            FROM orders
            WHERE strategy_id = :strategy_id
            ORDER BY timestamp DESC NULLS LAST
            LIMIT 50
        """

TRADES_SQL = """
            SELECT
                symbol,
                entry_time,
                exit_time,
                quantity,
                entry_price,
                exit_price,
                pnl
            FROM trades
            WHERE strategy_id = :strategy_id
            ORDER BY exit_time DESC NULLS LAST
            LIMIT 50
        """

BACKTESTS_SQL = """
        SELECT
            backtest_id,
            strategy_id,
            name,
            status,
            created_at,
            sharpe_ratio,
            sortino_ratio,
            alpha,
            beta,
            cagr,
            max_drawdown,
            net_profit,
            win_rate,
            loss_rate,
            trade_count,
            psr,
            research_suite_version,
            research_run_id,
            research_experiment_id,
            research_test_type,
            research_phase,
            research_window_id,
            research_git_commit,
            research_is_holdout,
            research_dirty,
            train_start,
            train_end,
            test_start,
            test_end,
            parameters_json,
            objective_name,
            objective_value,
            raw_statistics_json,
            research_guide_json,
            research_thresholds_json,
            research_primary_parameter,
            research_selection_summary_json,
            research_lineage_id,
            economic_parameter_count,
            research_metadata_count,
            backtest_start,
            backtest_end,
            error_message
        FROM backtests
        WHERE strategy_id = :strategy_id
        ORDER BY created_at DESC NULLS LAST
    """

BACKTESTS_FALLBACK_SQL = """
                SELECT
                    backtest_id,
                    name,
                    status,
                    created_at,
                    sharpe_ratio,
                    sortino_ratio,
                    alpha,
                    beta,
                    cagr,
                    max_drawdown,
                    net_profit,
                    win_rate,
                    loss_rate,
                    trade_count,
                    psr
                FROM backtests
                WHERE strategy_id = :strategy_id
                ORDER BY created_at DESC NULLS LAST
            """

BACKTEST_EQUITY_SQL = """
                SELECT
                    timestamp,
                    equity,
                    period_return,
                    series_name
                FROM backtest_equity_points
                WHERE backtest_id = :backtest_id
                ORDER BY timestamp ASC
            """

RESEARCH_RUN_SQL = """
                    SELECT
                        research_run_id,
                        strategy_id,
                        suite_version,
                        git_commit,
                        dirty,
                        first_seen_at,
                        last_seen_at,
                        holdout_accessed,
                        holdout_access_count,
                        config_json,
                        research_lineage_id,
                        expected_experiment_count,
                        synced_experiment_count,
                        completed_count,
                        failed_count,
                        skipped_count,
                        run_status,
                        holdout_exposure_status,
                        holdout_start,
                        holdout_end,
                        orchestrator_summary_json
                    FROM research_runs
                    WHERE research_run_id = :run_id
                """


def enrich_strategy_research_labels(engine, strategies: pd.DataFrame) -> pd.DataFrame:
    if strategies is None or strategies.empty:
        return strategies
    work = strategies.copy()
    for column in ("research_mode", "research_kind", "asset_class", "delivery_status"):
        if column not in work.columns:
            work[column] = None
    meta = read_sql_allow_missing_relation(engine, STRATEGY_LABELS_SQL)
    if meta is None or meta.empty:
        meta = read_sql_allow_missing_relation(engine, STRATEGY_LABELS_FALLBACK_SQL)
    if meta is None or meta.empty:
        return work
    work = work.drop(
        columns=[
            col
            for col in ("research_mode", "research_kind", "asset_class", "delivery_status", "last_seen_at")
            if col in work.columns
        ],
        errors="ignore",
    )
    return work.merge(meta, on="strategy_id", how="left")


def load_strategies_frame(engine) -> pd.DataFrame:
    registered = read_sql(engine, STRATEGIES_SQL)
    extra = read_sql_allow_missing_relation(engine, PLATFORM_STRATEGY_ROWS_SQL)
    if extra is None or extra.empty:
        combined = registered
    elif registered is None or registered.empty:
        combined = extra
    else:
        have = set(registered["strategy_id"].astype(str))
        add = extra[~extra["strategy_id"].astype(str).isin(have)]
        combined = registered if add.empty else pd.concat([registered, add], ignore_index=True)
    return enrich_strategy_research_labels(engine, combined)


def load_strategy_row(engine, strategy_id: str):
    rows = read_sql(engine, STRATEGY_BY_ID_SQL, {"strategy_id": strategy_id})
    if rows is None or rows.empty:
        return None
    return rows.iloc[0]


def load_latest_snapshot_row(engine, strategy_id: str):
    return _execute_one_optional(engine, LATEST_SNAPSHOT_SQL, {"strategy_id": strategy_id})


def load_equity_history_frame(engine, strategy_id: str) -> pd.DataFrame:
    return read_sql_optional(engine, EQUITY_HISTORY_SQL, {"strategy_id": strategy_id})


def load_latest_positions_frame(engine, strategy_id: str) -> pd.DataFrame:
    return read_sql_optional(engine, LATEST_POSITIONS_SQL, {"strategy_id": strategy_id})


def load_orders_frame(engine, strategy_id: str) -> pd.DataFrame:
    return read_sql_optional(engine, ORDERS_SQL, {"strategy_id": strategy_id})


def load_trades_frame(engine, strategy_id: str) -> pd.DataFrame:
    return read_sql_optional(engine, TRADES_SQL, {"strategy_id": strategy_id})


def load_backtests_frame(engine, strategy_id: str) -> pd.DataFrame:
    rows = read_sql_allow_missing_relation(engine, BACKTESTS_SQL, {"strategy_id": strategy_id})
    if rows is None or rows.empty:
        return read_sql(engine, BACKTESTS_FALLBACK_SQL, {"strategy_id": strategy_id})
    return rows


def load_backtest_equity_frame(engine, backtest_id: str) -> pd.DataFrame:
    return read_sql(engine, BACKTEST_EQUITY_SQL, {"backtest_id": backtest_id})


def load_research_run_row(engine, run_id: str):
    return _execute_one(engine, RESEARCH_RUN_SQL, {"run_id": run_id})


OPS_IDENTITY_SQL = """
        SELECT
            dashboard_streamlit_readonly,
            dashboard_readonly_proven,
            systemd_cutover_proven,
            systemd_still_git_pull,
            deploy_git_sha
        FROM mi_v_ops_status
        LIMIT 1
        """


def load_ops_identity(engine) -> dict[str, Any] | None:
    """Deploy identity from mi_v_ops_status. Missing view stays empty."""
    rows = read_sql_optional(engine, OPS_IDENTITY_SQL)
    if rows is None or rows.empty:
        return None
    return {str(key): rows.iloc[0][key] for key in rows.columns}


def _tri_state(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        if bool(pd.isna(value)):
            return "unknown"
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes"}:
        return "yes"
    if text in {"false", "f", "0", "no"}:
        return "no"
    if text in {"none", "nan", ""}:
        return "unknown"
    return "unknown"


def format_ops_identity_caption(row: dict[str, Any] | None) -> str | None:
    """Human caption for Strategy Monitor. None when the ops view is absent."""
    if not row:
        return None
    sha = str(row.get("deploy_git_sha") or "").strip()
    if sha.lower() in {"none", "nan"}:
        sha = ""
    sha_text = sha[:12] if sha else "unrecorded"
    return (
        "Host identity: Streamlit read-only={0}; dashboard read-only proven={1}; "
        "systemd cutover={2}; still git-pull unit={3}; deploy SHA={4}."
    ).format(
        _tri_state(row.get("dashboard_streamlit_readonly")),
        _tri_state(row.get("dashboard_readonly_proven")),
        _tri_state(row.get("systemd_cutover_proven")),
        _tri_state(row.get("systemd_still_git_pull")),
        sha_text,
    )
