"""Stage 1 QuantConnect backtest detail + equity sync helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from qc_research.parsing import (
    extract_stage1_metadata,
    is_failed_status,
    is_stage1_name,
    normalize_statistics,
    parse_equity_chart,
    parse_number,
)


def json_param(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)


def existing_backtest_map(conn, strategy_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT
                backtest_id,
                name,
                status,
                research_run_id,
                research_suite_version,
                research_test_type
            FROM backtests
            WHERE strategy_id = :strategy_id
            """
        ),
        {"strategy_id": strategy_id},
    ).mappings()
    return {row["backtest_id"]: dict(row) for row in rows}


def equity_point_counts(conn, strategy_id: str) -> dict[str, int]:
    rows = conn.execute(
        text(
            """
            SELECT backtest_id, COUNT(*) AS n
            FROM backtest_equity_points
            WHERE strategy_id = :strategy_id
            GROUP BY backtest_id
            """
        ),
        {"strategy_id": strategy_id},
    ).mappings()
    return {row["backtest_id"]: int(row["n"]) for row in rows}


def needs_detail_read(existing: dict[str, Any] | None, backtest: dict[str, Any]) -> bool:
    name = backtest.get("name") or ""
    status = str(backtest.get("status") or "").lower()
    completed = "completed" in status
    failed = is_failed_status(status, backtest)
    if not is_stage1_name(name):
        return False
    if not completed and not failed:
        return True
    if existing is None:
        return True
    if not existing.get("research_run_id"):
        return True
    return False


def needs_equity_curve(existing: dict[str, Any] | None, backtest: dict[str, Any], point_count: int) -> bool:
    name = backtest.get("name") or ""
    status = str(backtest.get("status") or "").lower()
    if not is_stage1_name(name):
        return False
    if "completed" not in status:
        return False
    if is_failed_status(status, backtest):
        return False
    return point_count < 2


def empty_to_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def stage1_upsert_fields(detail: dict[str, Any], name: str | None) -> dict[str, Any]:
    meta = extract_stage1_metadata(detail, name=name)
    failed = is_failed_status(detail.get("status"), detail)
    metrics = normalize_statistics(detail, failed=failed)
    objective_name = meta.get("objective_name") or "sharpe_ratio"
    objective_value = metrics.get(objective_name)
    research_guide = detail.get("researchGuide") or detail.get("research_guide") or meta.get("thresholds")
    raw_stats = detail.get("statistics") or detail.get("Statistics") or {}
    return {
        **metrics,
        "research_suite_version": empty_to_none(meta.get("research_suite_version")),
        "research_run_id": empty_to_none(meta.get("research_run_id")),
        "research_experiment_id": empty_to_none(meta.get("research_experiment_id")),
        "research_test_type": empty_to_none(meta.get("research_test_type")),
        "research_phase": empty_to_none(meta.get("research_phase")),
        "research_window_id": empty_to_none(meta.get("research_window_id")),
        "research_git_commit": empty_to_none(meta.get("research_git_commit")),
        "research_is_holdout": meta.get("research_is_holdout"),
        "research_dirty": meta.get("research_dirty"),
        "train_start": empty_to_none(meta.get("train_start")),
        "train_end": empty_to_none(meta.get("train_end")),
        "test_start": empty_to_none(meta.get("test_start")),
        "test_end": empty_to_none(meta.get("test_end")),
        "parameters_json": meta.get("parameters") or {},
        "objective_name": objective_name,
        "objective_value": objective_value,
        "raw_statistics_json": raw_stats if isinstance(raw_stats, dict) else {"value": raw_stats},
        "research_guide_json": research_guide if research_guide is not None else meta.get("thresholds"),
        "backtest_start": detail.get("startDate") or detail.get("created"),
        "backtest_end": detail.get("endDate") or detail.get("completedDate"),
        "error_message": empty_to_none(detail.get("error") or detail.get("stacktrace")),
        "failed": failed,
    }


def upsert_research_run(conn, strategy_id: str, fields: dict[str, Any]) -> None:
    run_id = fields.get("research_run_id")
    if not run_id:
        return
    holdout = bool(fields.get("research_is_holdout"))
    conn.execute(
        text(
            """
            INSERT INTO research_runs (
                research_run_id,
                strategy_id,
                suite_version,
                git_commit,
                dirty,
                first_seen_at,
                last_seen_at,
                holdout_accessed,
                holdout_access_count,
                metadata_json
            )
            VALUES (
                :research_run_id,
                :strategy_id,
                :suite_version,
                :git_commit,
                :dirty,
                NOW(),
                NOW(),
                :holdout_accessed,
                :holdout_increment,
                CAST(:metadata_json AS jsonb)
            )
            ON CONFLICT (research_run_id)
            DO UPDATE SET
                last_seen_at = NOW(),
                suite_version = COALESCE(EXCLUDED.suite_version, research_runs.suite_version),
                git_commit = COALESCE(EXCLUDED.git_commit, research_runs.git_commit),
                dirty = COALESCE(EXCLUDED.dirty, research_runs.dirty),
                holdout_accessed = research_runs.holdout_accessed OR EXCLUDED.holdout_accessed,
                holdout_access_count = research_runs.holdout_access_count
                    + CASE
                        WHEN EXCLUDED.holdout_accessed
                             AND research_runs.holdout_accessed IS FALSE
                        THEN 1
                        ELSE 0
                      END
            """
        ),
        {
            "research_run_id": run_id,
            "strategy_id": strategy_id,
            "suite_version": fields.get("research_suite_version"),
            "git_commit": fields.get("research_git_commit"),
            "dirty": fields.get("research_dirty"),
            "holdout_accessed": holdout,
            "holdout_increment": 1 if holdout else 0,
            "metadata_json": json_param(
                {
                    "objective_name": fields.get("objective_name"),
                    "primary_seen_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
        },
    )


def insert_equity_points(conn, strategy_id: str, backtest_id: str, points: list[dict[str, Any]]) -> int:
    inserted = 0
    for point in points:
        timestamp = point.get("timestamp")
        if timestamp is None:
            continue
        if isinstance(timestamp, (int, float)):
            timestamp = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        conn.execute(
            text(
                """
                INSERT INTO backtest_equity_points (
                    backtest_id,
                    strategy_id,
                    timestamp,
                    equity,
                    period_return,
                    series_name
                )
                VALUES (
                    :backtest_id,
                    :strategy_id,
                    :timestamp,
                    :equity,
                    :period_return,
                    :series_name
                )
                ON CONFLICT (backtest_id, timestamp, series_name)
                DO UPDATE SET
                    equity = EXCLUDED.equity,
                    period_return = EXCLUDED.period_return
                """
            ),
            {
                "backtest_id": backtest_id,
                "strategy_id": strategy_id,
                "timestamp": timestamp,
                "equity": point.get("equity"),
                "period_return": point.get("period_return"),
                "series_name": point.get("series_name") or "Equity",
            },
        )
        inserted += 1
    return inserted


def list_metrics_from_summary(backtest: dict[str, Any]) -> dict[str, Any]:
    """Lightweight metrics from /backtests/list includeStatistics."""
    failed = is_failed_status(backtest.get("status"), backtest)
    metrics = normalize_statistics(backtest, failed=failed)
    return metrics
