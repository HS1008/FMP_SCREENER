"""Stage 1 QuantConnect backtest detail + equity sync helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text

from qc_research.dates import qc_simulation_dates
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


STAGE1_METRIC_COLUMNS = (
    "sharpe_ratio",
    "sortino_ratio",
    "alpha",
    "beta",
    "cagr",
    "max_drawdown",
    "net_profit",
    "win_rate",
    "loss_rate",
    "trade_count",
    "psr",
)


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
                research_test_type,
                backtest_start,
                backtest_end,
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


def needs_legacy_date_hydration(
    existing: dict[str, Any] | None,
    backtest: dict[str, Any] | None = None,
) -> bool:
    """One /backtests/read when a legacy row is missing official simulation dates."""
    name = ""
    if backtest:
        name = str(backtest.get("name") or "")
    if not name and existing:
        name = str(existing.get("name") or "")
    if is_stage1_name(name):
        return False
    if existing and existing.get("backtest_start") and existing.get("backtest_end"):
        return False
    return True


def _parameter_set(detail: dict[str, Any] | None) -> dict[str, Any]:
    detail = detail or {}
    for key in ("parameterSet", "parameters", "ParameterSet"):
        value = detail.get(key)
        if isinstance(value, dict):
            return value
    nested = detail.get("backtest")
    if isinstance(nested, dict):
        for key in ("parameterSet", "parameters"):
            value = nested.get(key)
            if isinstance(value, dict):
                return value
    return {}


def legacy_hydration_fields(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Official dates (and optional parameterSet) for a non-Stage-1 backtest.

    Does not attach Stage 1 research metadata.
    """
    dates = qc_simulation_dates(detail or {})
    params = _parameter_set(detail)
    strategy_params = {
        key: value
        for key, value in params.items()
        if not str(key).startswith("research_")
    }
    return {
        "backtest_start": dates["backtest_start"],
        "backtest_end": dates["backtest_end"],
        "parameters_json": strategy_params or None,
        "research_suite_version": None,
        "research_run_id": None,
        "research_test_type": None,
    }


def hydrate_legacy_and_classify(
    list_row: dict[str, Any],
    detail: dict[str, Any],
    *,
    holdout_start: Any,
    holdout_end: Any,
    strategy_id: str | None = None,
    research_lineage_id: str | None = None,
) -> dict[str, Any]:
    """Apply one detail read to a dateless legacy row and classify holdout exposure."""
    from qc_research.holdout import classify_rows

    fields = legacy_hydration_fields(detail)
    stored = dict(list_row)
    stored["backtest_start"] = fields["backtest_start"]
    stored["backtest_end"] = fields["backtest_end"]
    if fields.get("parameters_json"):
        stored["parameters_json"] = fields["parameters_json"]
    stored["research_suite_version"] = None
    stored["research_run_id"] = None
    stored["research_test_type"] = None
    classified = classify_rows(
        [stored],
        holdout_start=holdout_start,
        holdout_end=holdout_end,
        strategy_id=strategy_id or stored.get("strategy_id"),
        research_lineage_id=research_lineage_id or strategy_id or stored.get("strategy_id"),
    )
    return {"stored": stored, "classified": classified}


def merge_stage1_lightweight_metrics(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, Any]:
    """SQL COALESCE(EXCLUDED.col, backtests.col) equivalent for Stage 1.

    Incoming values must already be canonical. Missing incoming values keep
    previously stored detailed metrics.
    """
    merged = {}
    existing = existing or {}
    incoming = incoming or {}
    for column in STAGE1_METRIC_COLUMNS:
        value = incoming.get(column)
        if value is None:
            value = existing.get(column)
        merged[column] = value
    return merged


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
    research_guide = detail.get("researchGuide") or detail.get("research_guide")
    raw_stats = detail.get("statistics") or detail.get("Statistics") or {}
    dates = qc_simulation_dates(detail)
    expected = meta.get("expected_experiment_count")
    config_json = None
    nested = meta.get("research_meta") if isinstance(meta.get("research_meta"), dict) else {}
    if nested:
        config_json = {
            "thresholds": nested.get("thresholds") or meta.get("thresholds"),
            "primary_parameter": nested.get("primary_parameter")
            or meta.get("research_primary_parameter"),
            "selection": nested.get("selection"),
            "in_sample": nested.get("in_sample"),
            "validation": nested.get("validation"),
            "holdout": nested.get("holdout"),
            "parameter_grid": nested.get("parameter_grid"),
            "walk_forward": nested.get("walk_forward"),
            "default_parameters": nested.get("default_parameters"),
            "expected_experiment_count": nested.get("expected_experiment_count") or expected,
            "research_lineage_id": nested.get("research_lineage_id")
            or meta.get("research_lineage_id"),
        }
    elif meta.get("thresholds"):
        config_json = {
            "thresholds": meta.get("thresholds"),
            "primary_parameter": meta.get("research_primary_parameter"),
            "expected_experiment_count": expected,
            "research_lineage_id": meta.get("research_lineage_id"),
        }
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
        "research_lineage_id": empty_to_none(
            meta.get("research_lineage_id") or meta.get("research_strategy_id")
        ),
        "train_start": empty_to_none(meta.get("train_start")),
        "train_end": empty_to_none(meta.get("train_end")),
        "test_start": empty_to_none(meta.get("test_start")),
        "test_end": empty_to_none(meta.get("test_end")),
        "parameters_json": meta.get("parameters") or {},
        "objective_name": objective_name,
        "objective_value": objective_value,
        "raw_statistics_json": raw_stats if isinstance(raw_stats, dict) else {"value": raw_stats},
        "research_guide_json": research_guide if isinstance(research_guide, dict) else research_guide,
        "research_thresholds_json": meta.get("research_thresholds_json") or meta.get("thresholds"),
        "research_primary_parameter": empty_to_none(meta.get("research_primary_parameter")),
        "research_selection_summary_json": meta.get("research_selection_summary"),
        "economic_parameter_count": meta.get("economic_parameter_count"),
        "research_metadata_count": meta.get("research_metadata_count"),
        "expected_experiment_count": expected,
        "config_json": config_json,
        "backtest_start": dates["backtest_start"],
        "backtest_end": dates["backtest_end"],
        "backtest_start_source": dates["backtest_start_source"],
        "created": dates["created"],
        "error_message": empty_to_none(detail.get("error") or detail.get("stacktrace")),
        "failed": failed,
    }


def upsert_research_run(conn, strategy_id: str, fields: dict[str, Any]) -> None:
    run_id = fields.get("research_run_id")
    if not run_id:
        return
    from qc_research.parsing import is_smoke_test

    if is_smoke_test(fields) or str(fields.get("research_test_type") or "").upper() == "SMOKE":
        return
    holdout = bool(fields.get("research_is_holdout"))
    holdout_window = (fields.get("config_json") or {}).get("holdout") or {}
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
                config_json,
                metadata_json,
                research_lineage_id,
                expected_experiment_count,
                holdout_start,
                holdout_end
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
                CAST(:config_json AS jsonb),
                CAST(:metadata_json AS jsonb),
                :research_lineage_id,
                :expected_experiment_count,
                :holdout_start,
                :holdout_end
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
                      END,
                config_json = COALESCE(EXCLUDED.config_json, research_runs.config_json),
                research_lineage_id = COALESCE(EXCLUDED.research_lineage_id, research_runs.research_lineage_id),
                expected_experiment_count = COALESCE(
                    EXCLUDED.expected_experiment_count,
                    research_runs.expected_experiment_count
                ),
                holdout_start = COALESCE(EXCLUDED.holdout_start, research_runs.holdout_start),
                holdout_end = COALESCE(EXCLUDED.holdout_end, research_runs.holdout_end)
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
            "config_json": json_param(fields.get("config_json")),
            "research_lineage_id": fields.get("research_lineage_id") or strategy_id,
            "expected_experiment_count": fields.get("expected_experiment_count"),
            "holdout_start": holdout_window.get("start"),
            "holdout_end": holdout_window.get("end"),
            "metadata_json": json_param(
                {
                    "objective_name": fields.get("objective_name"),
                    "primary_parameter": fields.get("research_primary_parameter"),
                    "economic_parameter_count": fields.get("economic_parameter_count"),
                    "research_metadata_count": fields.get("research_metadata_count"),
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


def audit_holdout_exposures(conn, strategy_id: str) -> dict[str, Any] | None:
    """Record lineage-level holdout exposure without launching backtests."""
    from qc_research.holdout import (
        STATUS_EXPOSED_PRIOR_TO_STAGE1,
        classify_rows,
    )

    run_rows = conn.execute(
        text(
            """
            SELECT research_run_id, research_lineage_id, holdout_start, holdout_end,
                   config_json, expected_experiment_count
            FROM research_runs
            WHERE strategy_id = :strategy_id
            ORDER BY last_seen_at DESC
            """
        ),
        {"strategy_id": strategy_id},
    ).mappings()
    runs = [dict(row) for row in run_rows]
    if not runs:
        holdout_start = "2023-01-01"
        holdout_end = None
        lineage = strategy_id
    else:
        latest = runs[0]
        holdout_start = latest.get("holdout_start")
        holdout_end = latest.get("holdout_end")
        config = latest.get("config_json") or {}
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                config = {}
        holdout = (config or {}).get("holdout") or {}
        holdout_start = holdout_start or holdout.get("start") or "2023-01-01"
        holdout_end = holdout_end or holdout.get("end")
        lineage = latest.get("research_lineage_id") or strategy_id

    backtest_rows = conn.execute(
        text(
            """
            SELECT
                backtest_id, name, strategy_id, research_run_id,
                research_suite_version, research_test_type, research_lineage_id,
                research_git_commit, backtest_start, backtest_end,
                test_start, test_end, parameters_json
            FROM backtests
            WHERE strategy_id = :strategy_id
            """
        ),
        {"strategy_id": strategy_id},
    ).mappings()
    rows = [dict(row) for row in backtest_rows]
    if holdout_end is None:
        ends = [row.get("backtest_end") or row.get("test_end") for row in rows]
        holdout_end = max((str(item)[:10] for item in ends if item), default=None)

    classified = classify_rows(
        rows,
        holdout_start=holdout_start,
        holdout_end=holdout_end,
        strategy_id=strategy_id,
        research_lineage_id=lineage,
    )
    for row in classified["legacy_overlap_backtests"]:
        conn.execute(
            text(
                """
                INSERT INTO holdout_exposures (
                    strategy_id,
                    research_lineage_id,
                    holdout_start,
                    holdout_end,
                    status,
                    source,
                    backtest_id,
                    git_commit,
                    notes
                )
                VALUES (
                    :strategy_id,
                    :research_lineage_id,
                    :holdout_start,
                    :holdout_end,
                    :status,
                    :source,
                    :backtest_id,
                    :git_commit,
                    :notes
                )
                ON CONFLICT (strategy_id, research_lineage_id, holdout_start, backtest_id)
                DO UPDATE SET
                    last_seen_at = NOW(),
                    status = holdout_exposures.status
                """
            ),
            {
                "strategy_id": strategy_id,
                "research_lineage_id": lineage,
                "holdout_start": holdout_start,
                "holdout_end": holdout_end,
                "status": STATUS_EXPOSED_PRIOR_TO_STAGE1,
                "source": "legacy_overlap",
                "backtest_id": row.get("backtest_id"),
                "git_commit": row.get("research_git_commit"),
                "notes": (
                    "Historical backtest overlaps the configured holdout. "
                    "Marked EXPOSED_PRIOR_TO_STAGE1. Not deleted, hidden, or re-run."
                ),
            },
        )
    conn.execute(
        text(
            """
            UPDATE research_runs
            SET holdout_exposure_status = :status
            WHERE strategy_id = :strategy_id
              AND COALESCE(research_lineage_id, strategy_id) = :lineage
            """
        ),
        {"status": classified["status"], "strategy_id": strategy_id, "lineage": lineage},
    )
    return classified


IN_PROGRESS = "IN_PROGRESS"
COMPLETE = "COMPLETE"
INCOMPLETE = "INCOMPLETE"


def compute_research_run_progress(
    *,
    expected: int | None,
    row_statuses: list[str],
    orchestrator_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Authoritative progress for research_runs.

    80/81 stays IN_PROGRESS until the orchestrator summary finalizes skipped
    experiments. PASS/WATCH/FAIL are not assigned here.
    """
    completed = 0
    failed = 0
    db_skipped = 0
    for raw in row_statuses:
        status = str(raw or "").lower()
        if "completed" in status or status == "dry_run":
            completed += 1
        elif "fail" in status or "error" in status:
            failed += 1
        elif status == "skipped":
            db_skipped += 1
    summary = orchestrator_summary or {}
    skipped = int(summary.get("skipped_count") if summary.get("skipped_count") is not None else db_skipped)
    synced = len(row_statuses)
    expected_count = expected
    if expected_count is None and summary.get("expected_experiment_count") is not None:
        expected_count = int(summary.get("expected_experiment_count"))
    terminal = completed + failed + skipped
    summary_status = str(summary.get("run_status") or "")
    if summary_status in {COMPLETE, INCOMPLETE}:
        run_status = summary_status
    elif expected_count and terminal < int(expected_count):
        run_status = IN_PROGRESS
    elif expected_count and terminal >= int(expected_count):
        if failed or skipped or completed < int(expected_count):
            run_status = INCOMPLETE
        else:
            run_status = COMPLETE
    else:
        run_status = IN_PROGRESS
    return {
        "expected_experiment_count": expected_count,
        "synced_experiment_count": synced,
        "completed_count": completed,
        "failed_count": failed,
        "skipped_count": skipped,
        "run_status": run_status,
    }


def apply_run_summary(conn, payload: dict[str, Any]) -> None:
    """Upsert compact orchestrator run_summary.json into research_runs."""
    if not payload or not payload.get("research_run_id"):
        raise ValueError("run summary is missing research_run_id")
    run_id = payload["research_run_id"]
    conn.execute(
        text(
            """
            INSERT INTO research_runs (
                research_run_id,
                strategy_id,
                suite_version,
                git_commit,
                first_seen_at,
                last_seen_at,
                expected_experiment_count,
                synced_experiment_count,
                completed_count,
                failed_count,
                skipped_count,
                run_status,
                parent_research_run_id,
                source_research_run_id,
                orchestrator_summary_json
            )
            VALUES (
                :research_run_id,
                :strategy_id,
                'S1',
                :git_commit,
                NOW(),
                NOW(),
                :expected_experiment_count,
                :synced_experiment_count,
                :completed_count,
                :failed_count,
                :skipped_count,
                :run_status,
                :parent_research_run_id,
                :source_research_run_id,
                CAST(:summary AS jsonb)
            )
            ON CONFLICT (research_run_id)
            DO UPDATE SET
                last_seen_at = NOW(),
                expected_experiment_count = COALESCE(
                    EXCLUDED.expected_experiment_count,
                    research_runs.expected_experiment_count
                ),
                synced_experiment_count = EXCLUDED.synced_experiment_count,
                completed_count = EXCLUDED.completed_count,
                failed_count = EXCLUDED.failed_count,
                skipped_count = EXCLUDED.skipped_count,
                run_status = EXCLUDED.run_status,
                parent_research_run_id = COALESCE(
                    EXCLUDED.parent_research_run_id,
                    research_runs.parent_research_run_id
                ),
                source_research_run_id = COALESCE(
                    EXCLUDED.source_research_run_id,
                    research_runs.source_research_run_id
                ),
                orchestrator_summary_json = COALESCE(
                    EXCLUDED.orchestrator_summary_json,
                    research_runs.orchestrator_summary_json
                )
            """
        ),
        {
            "research_run_id": run_id,
            "strategy_id": payload.get("strategy_id") or "",
            "git_commit": payload.get("git_commit"),
            "expected_experiment_count": payload.get("expected_experiment_count"),
            "synced_experiment_count": payload.get("synced_experiment_count"),
            "completed_count": payload.get("completed_count"),
            "failed_count": payload.get("failed_count"),
            "skipped_count": payload.get("skipped_count"),
            "run_status": payload.get("run_status"),
            "parent_research_run_id": payload.get("parent_research_run_id"),
            "source_research_run_id": payload.get("source_research_run_id"),
            "summary": json_param(payload),
        },
    )


def refresh_research_run_progress(conn, strategy_id: str) -> list[dict[str, Any]]:
    """Recompute research_runs progress after a backtest sync."""
    run_rows = conn.execute(
        text(
            """
            SELECT
                research_run_id,
                expected_experiment_count,
                orchestrator_summary_json,
                run_status
            FROM research_runs
            WHERE strategy_id = :strategy_id
            """
        ),
        {"strategy_id": strategy_id},
    ).mappings()
    runs = {row["research_run_id"]: dict(row) for row in run_rows}
    backtest_rows = conn.execute(
        text(
            """
            SELECT research_run_id, status, research_test_type
            FROM backtests
            WHERE strategy_id = :strategy_id
              AND research_run_id IS NOT NULL
            """
        ),
        {"strategy_id": strategy_id},
    ).mappings()
    by_run: dict[str, list[str]] = {}
    for row in backtest_rows:
        if str(row.get("research_test_type") or "").upper() == "SMOKE":
            continue
        by_run.setdefault(row["research_run_id"], []).append(str(row.get("status") or ""))
    updated = []
    run_ids = set(runs) | set(by_run)
    for run_id in run_ids:
        meta = runs.get(run_id) or {}
        summary = meta.get("orchestrator_summary_json") or {}
        if isinstance(summary, str):
            try:
                summary = json.loads(summary)
            except ValueError:
                summary = {}
        progress = compute_research_run_progress(
            expected=meta.get("expected_experiment_count"),
            row_statuses=by_run.get(run_id) or [],
            orchestrator_summary=summary if isinstance(summary, dict) else {},
        )
        conn.execute(
            text(
                """
                UPDATE research_runs
                SET
                    synced_experiment_count = :synced_experiment_count,
                    completed_count = :completed_count,
                    failed_count = :failed_count,
                    skipped_count = :skipped_count,
                    run_status = :run_status
                WHERE research_run_id = :research_run_id
                """
            ),
            {
                "research_run_id": run_id,
                **progress,
            },
        )
        updated.append({"research_run_id": run_id, **progress})
    return updated


STAGE1_RESULTS_RELATIVE = "stage1_results"
STAGE1_RESULTS_OUTPUTS_RELATIVE = "outputs/stage1_results"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def discover_run_summary_paths(root: Path | None = None) -> list[Path]:
    """Find orchestrator run_summary.json files committed for automatic ingest.

    QuantConnect cannot represent skipped experiments. The orchestrator
    publishes run_summary.json to a deterministic tree that deploy pulls
    onto the droplet:

        stage1_results/<strategy_id>/<research_run_id>/run_summary.json
    """
    base = Path(root) if root is not None else repo_root()
    found: list[Path] = []
    seen: set[Path] = set()
    for relative in (STAGE1_RESULTS_RELATIVE, STAGE1_RESULTS_OUTPUTS_RELATIVE):
        directory = base / relative
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("run_summary.json")):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(path)
    return found


def load_run_summary_payload(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not payload.get("research_run_id"):
        raise ValueError("{0} is missing research_run_id".format(path))
    return payload


def import_run_summaries(
    conn,
    paths: Iterable[Path],
) -> list[dict[str, Any]]:
    """Apply each run_summary.json. Re-importing the same payload is idempotent."""
    imported: list[dict[str, Any]] = []
    seen: set[Path] = set()
    strategies: list[str] = []
    for raw in paths:
        path = Path(raw)
        resolved = path.resolve() if path.exists() else path
        if resolved in seen:
            continue
        seen.add(resolved)
        payload = load_run_summary_payload(path)
        apply_run_summary(conn, payload)
        strategy_id = payload.get("strategy_id")
        if strategy_id:
            strategies.append(str(strategy_id))
        imported.append(
            {
                "path": str(path),
                "research_run_id": payload.get("research_run_id"),
                "run_status": payload.get("run_status"),
            }
        )
    for strategy_id in dict.fromkeys(strategies):
        refresh_research_run_progress(conn, strategy_id)
    return imported

