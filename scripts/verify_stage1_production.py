"""Stage 1 production verification against the deployed PostgreSQL database.

Uses the application's SQLAlchemy engine (db.connection) so PostgreSQL
credentials never enter the shell. Never prints secrets.

Exit 0 on overall PASS, 1 on FAIL.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from qc_research.holdout import (
    STATUS_EXPOSED_PRIOR_TO_STAGE1,
    periods_overlap,
)

STRATEGY_ID = "SPYTrend"
REQUIRED_MIGRATION = "001_stage1_research.sql"
REQUIRED_RESEARCH_PROJECT_MIGRATION = "002_research_project.sql"
HOLDOUT_START = date(2023, 1, 1)
EXPECTED_RESEARCH_PROJECT_NAME = "SPYTrendResearch"
EXPECTED_RESEARCH_PROJECT_ID = "35732039"
EXPECTED_SMOKE_START = date(2017, 1, 1)
EXPECTED_SMOKE_END = date(2018, 12, 31)

REQUIRED_TABLES = (
    "backtests",
    "backtest_equity_points",
    "research_runs",
    "holdout_exposures",
    "schema_migrations",
)

REQUIRED_BACKTEST_COLUMNS = (
    "research_run_id",
    "research_test_type",
    "research_selection_summary_json",
    "research_lineage_id",
    "backtest_start",
    "backtest_end",
)

SECRET_ENV_NAMES = (
    "DB_PASSWORD",
    "DATABASE_URL",
    "QC_API_TOKEN",
    "QC_USER_ID",
    "DO_SSH_KEY",
    "SSH_PRIVATE_KEY",
    "FMP_API_KEY",
    "CHATGPT_API_TOKEN",
)

# Exact untracked paths allowed on the DigitalOcean checkout. Not globbed.
# Do not delete these files; they are server/cron operational artifacts.
ALLOWED_SERVER_ONLY_FILES = frozenset(
    {
        "test_db.py",
        "update_dashboard.sh",
    }
)


def redact(text: str) -> str:
    """Strip credential material from error strings before printing."""
    redacted = str(text)
    for name in SECRET_ENV_NAMES:
        value = os.getenv(name)
        if value:
            redacted = redacted.replace(value, "[{0}_REDACTED]".format(name))
    redacted = re.sub(r":([^:@/\s]+)@", ":***@", redacted)
    redacted = re.sub(
        r"BEGIN (OPENSSH|RSA|EC) PRIVATE KEY.*?END \1 PRIVATE KEY",
        "[PRIVATE_KEY_REDACTED]",
        redacted,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return redacted


def parse_git_status_porcelain(output: str) -> list[tuple[str, str]]:
    """Parse ``git status --porcelain`` into (xy, path) pairs.

    Does not read file contents. Rename destinations are the path used.
    """
    entries: list[tuple[str, str]] = []
    for raw in (output or "").splitlines():
        line = raw.rstrip("\n")
        if not line:
            continue
        xy = line[:2]
        rest = line[3:] if len(line) > 2 else ""
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        path = rest.strip().strip('"')
        if path:
            entries.append((xy, path))
    return entries


def evaluate_working_tree(porcelain: str) -> dict[str, Any]:
    """Allow only known server-only untracked files; fail on any tracked drift."""
    tracked: list[str] = []
    allowed_untracked: list[str] = []
    unexpected_untracked: list[str] = []
    for xy, path in parse_git_status_porcelain(porcelain):
        if xy == "!!":
            continue
        if xy == "??":
            if path in ALLOWED_SERVER_ONLY_FILES:
                allowed_untracked.append(path)
            else:
                unexpected_untracked.append(path)
            continue
        tracked.append("{0} {1}".format(xy, path))

    failures: list[str] = []
    if tracked:
        failures.append(
            "tracked working-tree changes: {0}".format("; ".join(tracked))
        )
    if unexpected_untracked:
        failures.append(
            "unexpected untracked files: {0}".format(
                ", ".join(unexpected_untracked)
            )
        )
    ok = not failures
    return {
        "ok": ok,
        "tracked_status": "CLEAN" if not tracked else "DIRTY",
        "tracked": tracked,
        "allowed_untracked": allowed_untracked,
        "unexpected_untracked": unexpected_untracked,
        "working_tree_check": "PASS" if ok else "FAIL",
        "failures": failures,
    }


def format_working_tree_report(result: dict[str, Any]) -> str:
    known = list(result.get("allowed_untracked") or [])
    unexpected = list(result.get("unexpected_untracked") or [])
    lines = [
        "Working tree tracked files: {0}".format(
            result.get("tracked_status") or "DIRTY"
        ),
        "",
        "Known server-only files:",
    ]
    if known:
        for name in known:
            lines.append("  {0}".format(name))
    else:
        lines.append("  NONE")
    lines.append("")
    if unexpected:
        lines.append("Unexpected untracked files:")
        for name in unexpected:
            lines.append("  {0}".format(name))
    else:
        lines.append("Unexpected untracked files: NONE")
    lines.extend(
        [
            "",
            "Overall working-tree check:",
            "{0}".format(result.get("working_tree_check") or "FAIL"),
        ]
    )
    return "\n".join(lines)


def inspect_git_working_tree(repo_root: Path | None = None) -> dict[str, Any]:
    import subprocess

    root = Path(repo_root or ROOT)
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(root),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return {
            "ok": False,
            "tracked_status": "DIRTY",
            "tracked": [],
            "allowed_untracked": [],
            "unexpected_untracked": [],
            "working_tree_check": "FAIL",
            "failures": [
                "git status --porcelain failed: {0}".format(
                    redact(completed.stderr or completed.stdout or "unknown error")
                )
            ],
        }
    return evaluate_working_tree(completed.stdout)


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def check_schema(
    tables: set[str],
    backtest_columns: set[str],
    migrations: set[str],
) -> list[str]:
    failures = []
    missing_tables = [name for name in REQUIRED_TABLES if name not in tables]
    if missing_tables:
        failures.append("missing tables: {0}".format(", ".join(missing_tables)))
    missing_cols = [
        name for name in REQUIRED_BACKTEST_COLUMNS if name not in backtest_columns
    ]
    if missing_cols:
        failures.append(
            "backtests missing columns: {0}".format(", ".join(missing_cols))
        )
    if REQUIRED_MIGRATION not in migrations:
        failures.append("schema_migrations missing {0}".format(REQUIRED_MIGRATION))
    if REQUIRED_RESEARCH_PROJECT_MIGRATION not in migrations:
        failures.append(
            "schema_migrations missing {0}".format(REQUIRED_RESEARCH_PROJECT_MIGRATION)
        )
    return failures


def evaluate_live_parser(portfolio: dict[str, Any] | None, raw_result: Any) -> dict[str, Any]:
    """Check equity > 0 and the q/p/v holdings regression."""
    from jobs.sync_quantconnect import (
        _extract_holdings,
        _extract_portfolio,
        _safe_float,
        snapshot_is_malformed,
    )

    result = {
        "ok": False,
        "equity_parser": "FAIL",
        "positions_parser": "FAIL",
        "equity": None,
        "cash": None,
        "holdings_value": None,
        "position_count": 0,
        "malformed": None,
        "failures": [],
    }
    if not isinstance(portfolio, dict):
        result["failures"].append("live portfolio parser returned no payload")
        return result

    equity = _safe_float(portfolio.get("equity"))
    cash = _safe_float(portfolio.get("cash"))
    holdings_value = _safe_float(portfolio.get("holdings_value"))
    positions = portfolio.get("positions") or []
    result["equity"] = equity
    result["cash"] = cash
    result["holdings_value"] = holdings_value
    result["position_count"] = len(positions)

    if equity > 0:
        result["equity_parser"] = "PASS"
    else:
        result["failures"].append("equity is {0} (must be > 0)".format(equity))

    malformed = snapshot_is_malformed(portfolio, raw_result)
    result["malformed"] = malformed
    if malformed:
        result["failures"].append(malformed)

    raw_holdings = _extract_holdings(_extract_portfolio(raw_result or {}))
    if raw_holdings and abs(holdings_value) < 1e-9:
        result["failures"].append(
            "raw QuantConnect holdings are present but parsed holdings_value is 0"
        )
    else:
        result["positions_parser"] = "PASS"

    result["ok"] = not result["failures"]
    return result


def evaluate_legacy_and_holdout(
    backtests: list[dict[str, Any]],
    exposures: list[dict[str, Any]],
    *,
    holdout_start: date = HOLDOUT_START,
    holdout_end: date | None = None,
) -> dict[str, Any]:
    end = holdout_end or date.today()
    dated = []
    undated_legacy = 0
    overlapping = []
    for row in backtests:
        start = row.get("backtest_start")
        finish = row.get("backtest_end")
        is_legacy = not row.get("research_run_id")
        if start and finish:
            dated.append(row)
            if periods_overlap(start, finish, holdout_start, end):
                overlapping.append(row)
        elif is_legacy:
            undated_legacy += 1

    exposed = [
        row
        for row in exposures
        if str(row.get("status") or "") == STATUS_EXPOSED_PRIOR_TO_STAGE1
        and str(row.get("strategy_id") or "") == STRATEGY_ID
    ]

    failures = []
    dates_status = "PASS"
    if not dated:
        dates_status = "FAIL"
        failures.append(
            "no SPYTrend backtests have backtest_start/backtest_end populated"
        )
    elif undated_legacy and not overlapping:
        dates_status = "FAIL"
        failures.append(
            "legacy SPYTrend rows remain without historical dates after hydration"
        )

    holdout_status = "MISSING"
    if exposed:
        holdout_status = STATUS_EXPOSED_PRIOR_TO_STAGE1
    if overlapping and not exposed:
        holdout_status = "FAIL"
        failures.append(
            "historical SPYTrend backtest overlaps 2023-01-01 onward but "
            "holdout_exposures is missing EXPOSED_PRIOR_TO_STAGE1"
        )
    elif not overlapping and not exposed:
        holdout_status = "FAIL"
        failures.append(
            "no overlapping 2023+ SPYTrend backtest found after hydration"
        )

    return {
        "ok": not failures,
        "dates_status": dates_status,
        "holdout_status": holdout_status,
        "historical_count": len(overlapping),
        "dated_count": len(dated),
        "undated_legacy": undated_legacy,
        "failures": failures,
    }


def format_money(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return "${0:,.2f}".format(float(value))
    except (TypeError, ValueError):
        return "—"


def format_report(
    *,
    git_sha: str,
    working_tree: str,
    streamlit: str,
    migration: str,
    schema: str,
    live_status: str,
    live: dict[str, Any],
    backtests: dict[str, Any],
    overall: str,
    working_tree_detail: dict[str, Any] | None = None,
    research: dict[str, Any] | None = None,
    smoke: dict[str, Any] | None = None,
    stage1_run: dict[str, Any] | None = None,
) -> str:
    lines = [
        "STAGE 1 PRODUCTION VERIFICATION",
        "",
        "Deployment",
        "  Git SHA: {0}".format(git_sha or "UNKNOWN"),
        "  Working tree: {0}".format(working_tree),
        "  Streamlit: {0}".format(streamlit),
        "",
    ]
    if working_tree_detail:
        lines.extend(format_working_tree_report(working_tree_detail).splitlines())
        lines.append("")
    lines.extend(
        [
            "Database",
            "  Migration: {0}".format(migration),
            "  Stage 1 schema: {0}".format(schema),
            "",
            "Paper monitoring",
            "  SPYTrend status: {0}".format(live_status or "UNKNOWN"),
            "  Equity: {0}".format(format_money(live.get("equity"))),
            "  Cash: {0}".format(format_money(live.get("cash"))),
            "  Holdings value: {0}".format(format_money(live.get("holdings_value"))),
            "  Positions: {0}".format(live.get("position_count", 0)),
            "  Equity parser: {0}".format(live.get("equity_parser", "FAIL")),
            "  Positions parser: {0}".format(live.get("positions_parser", "FAIL")),
            "",
            "Backtests",
            "  Legacy dates hydrated: {0}".format(backtests.get("dates_status", "FAIL")),
            "  Historical backtests found: {0}".format(
                backtests.get("historical_count", 0)
            ),
            "",
            "Holdout",
            "  Status: {0}".format(backtests.get("holdout_status", "MISSING")),
            "",
        ]
    )
    research = research or {}
    smoke = smoke or {}
    if research or smoke:
        lines.extend(
            [
                "Research project",
                "  Name: {0}".format(research.get("name") or "—"),
                "  ID: {0}".format(research.get("project_id") or "—"),
                "  Discovery: {0}".format(research.get("status") or "—"),
                "",
                "Smoke ingest",
                "  Rows: {0}".format(smoke.get("count", 0)),
                "  Backtest ID: {0}".format(smoke.get("backtest_id") or "—"),
                "  Dates: {0} → {1}".format(
                    smoke.get("start") or "—", smoke.get("end") or "—"
                ),
                "  research_test_type: {0}".format(smoke.get("research_test_type") or "—"),
                "  research_is_holdout: {0}".format(smoke.get("research_is_holdout")),
                "  Equity points: {0}".format(smoke.get("equity_count", 0)),
                "  Equity years: {0}".format(smoke.get("equity_years") or "—"),
                "  Status: {0}".format(smoke.get("status") or "—"),
                "",
            ]
        )
    stage1_run = stage1_run or {}
    lines.extend(
        [
            "Stage 1 run",
            "  Present: {0}".format("YES" if stage1_run.get("present") else "NO"),
            "  Run ID: {0}".format(stage1_run.get("research_run_id") or "—"),
            "  run_status: {0}".format(stage1_run.get("run_status") or "—"),
            "  expected/completed/failed/skipped: {0}/{1}/{2}/{3}".format(
                stage1_run.get("expected_experiment_count")
                if stage1_run.get("expected_experiment_count") is not None
                else "—",
                stage1_run.get("completed_count")
                if stage1_run.get("completed_count") is not None
                else "—",
                stage1_run.get("failed_count")
                if stage1_run.get("failed_count") is not None
                else "—",
                stage1_run.get("skipped_count")
                if stage1_run.get("skipped_count") is not None
                else "—",
            ),
            "  Check: {0}".format(stage1_run.get("status") or "SKIP"),
            "",
        ]
    )
    lines.extend(["Overall:", "  {0}".format(overall)])
    return "\n".join(lines)


def load_schema(conn) -> tuple[set[str], set[str], set[str]]:
    tables = {
        row[0]
        for row in conn.execute(
            text(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                """
            )
        )
    }
    columns = {
        row[0]
        for row in conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'backtests'
                """
            )
        )
    }
    migrations = {
        row[0]
        for row in conn.execute(text("SELECT filename FROM schema_migrations"))
    }
    return tables, columns, migrations


def load_spytrend_backtests(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT
                backtest_id,
                name,
                strategy_id,
                research_run_id,
                research_test_type,
                research_is_holdout,
                backtest_start,
                backtest_end
            FROM backtests
            WHERE strategy_id = :strategy_id
            """
        ),
        {"strategy_id": STRATEGY_ID},
    ).mappings()
    return [dict(row) for row in rows]


def load_spytrend_strategy(conn) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT
                strategy_id,
                qc_project_id,
                qc_research_project_id,
                qc_research_project_name
            FROM strategies
            WHERE strategy_id = :strategy_id
            """
        ),
        {"strategy_id": STRATEGY_ID},
    ).mappings().first()
    return dict(row) if row else None


def load_spytrend_research_runs(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT
                research_run_id,
                strategy_id,
                git_commit,
                expected_experiment_count,
                synced_experiment_count,
                completed_count,
                failed_count,
                skipped_count,
                run_status,
                orchestrator_summary_json
            FROM research_runs
            WHERE strategy_id = :strategy_id
            ORDER BY last_seen_at DESC NULLS LAST
            """
        ),
        {"strategy_id": STRATEGY_ID},
    ).mappings()
    return [dict(row) for row in rows]


def evaluate_stage1_run(
    runs: list[dict[str, Any]] | None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a terminal STAGE1_* run when one exists.

    Pre-launch production (SMOKE only) has no STAGE1_* row; that is SKIP,
    not FAIL. After a Stage 1 suite and run_summary import, status must be
    COMPLETE or INCOMPLETE — never stuck IN_PROGRESS.
    """
    result: dict[str, Any] = {
        "status": "SKIP",
        "present": False,
        "research_run_id": None,
        "run_status": None,
        "expected_experiment_count": None,
        "completed_count": None,
        "failed_count": None,
        "skipped_count": None,
        "failures": [],
    }
    stage_runs = [
        row
        for row in runs or []
        if str(row.get("research_run_id") or "").startswith("STAGE1_")
    ]
    if not stage_runs:
        return result
    run = stage_runs[0]
    result["present"] = True
    result["research_run_id"] = run.get("research_run_id")
    result["run_status"] = run.get("run_status")
    result["expected_experiment_count"] = run.get("expected_experiment_count")
    result["completed_count"] = run.get("completed_count")
    result["failed_count"] = run.get("failed_count")
    result["skipped_count"] = run.get("skipped_count")
    failures: list[str] = []
    status = str(run.get("run_status") or "")
    if status not in {"COMPLETE", "INCOMPLETE"}:
        failures.append(
            "Stage 1 run_status is {0}; expected COMPLETE or INCOMPLETE "
            "after run_summary import".format(status or "missing")
        )
    expected = run.get("expected_experiment_count")
    try:
        expected_n = int(expected) if expected is not None else None
    except (TypeError, ValueError):
        expected_n = None
    if expected_n != 81:
        failures.append(
            "Stage 1 expected_experiment_count is {0}, expected 81".format(expected)
        )
    run_id = str(run.get("research_run_id") or "")
    smoke_mixed = [
        row
        for row in rows or []
        if str(row.get("research_run_id") or "") == run_id
        and str(row.get("research_test_type") or "").upper() == "SMOKE"
    ]
    if smoke_mixed:
        failures.append("SMOKE backtests are mixed into the STAGE1 research run")
    result["failures"] = failures
    result["status"] = "PASS" if not failures else "FAIL"
    return result


def load_equity_points(conn, backtest_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT timestamp
            FROM backtest_equity_points
            WHERE backtest_id = :backtest_id
            ORDER BY timestamp
            """
        ),
        {"backtest_id": backtest_id},
    ).mappings()
    return [dict(row) for row in rows]


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def evaluate_research_and_smoke(
    strategy: dict[str, Any] | None,
    rows: list[dict[str, Any]],
    equity_points: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Check research project discovery and SMOKE ingest. Never uses execution ID."""
    failures: list[str] = []
    name = str((strategy or {}).get("qc_research_project_name") or "").strip()
    project_id = (strategy or {}).get("qc_research_project_id")
    project_id_text = str(project_id).strip() if project_id not in (None, "") else ""
    execution_id = str((strategy or {}).get("qc_project_id") or "").strip()

    research = {
        "name": name or None,
        "project_id": project_id_text or None,
        "status": "FAIL",
        "failures": [],
    }
    if name != EXPECTED_RESEARCH_PROJECT_NAME:
        failures.append(
            "qc_research_project_name is {0!r}, expected {1!r}".format(
                name, EXPECTED_RESEARCH_PROJECT_NAME
            )
        )
    if not project_id_text:
        failures.append("qc_research_project_id is NULL; research project not discovered")
    elif project_id_text != EXPECTED_RESEARCH_PROJECT_ID:
        failures.append(
            "qc_research_project_id is {0}, expected {1}".format(
                project_id_text, EXPECTED_RESEARCH_PROJECT_ID
            )
        )
    if project_id_text and execution_id and project_id_text == execution_id:
        failures.append(
            "qc_research_project_id matches execution qc_project_id; no fallback allowed"
        )
    if not failures:
        research["status"] = "PASS"

    smoke_rows = [
        row
        for row in rows or []
        if str(row.get("research_test_type") or "").upper() == "SMOKE"
    ]
    smoke = {
        "count": len(smoke_rows),
        "backtest_id": None,
        "start": None,
        "end": None,
        "research_test_type": None,
        "research_is_holdout": None,
        "equity_count": 0,
        "equity_years": None,
        "status": "FAIL",
        "failures": [],
    }
    if not smoke_rows:
        failures.append("no SMOKE backtest ingested for SPYTrend")
    else:
        row = smoke_rows[0]
        start = _as_date(row.get("backtest_start"))
        end = _as_date(row.get("backtest_end"))
        holdout = row.get("research_is_holdout")
        smoke.update(
            {
                "backtest_id": row.get("backtest_id"),
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "research_test_type": row.get("research_test_type"),
                "research_is_holdout": holdout,
            }
        )
        if start != EXPECTED_SMOKE_START or end != EXPECTED_SMOKE_END:
            failures.append(
                "SMOKE dates are {0} → {1}, expected {2} → {3}".format(
                    start, end, EXPECTED_SMOKE_START, EXPECTED_SMOKE_END
                )
            )
        holdout_true = holdout in (True, "true", "t", "1", 1)
        if holdout_true:
            failures.append("SMOKE research_is_holdout must be false")

        points = list(equity_points or [])
        smoke["equity_count"] = len(points)
        years = []
        for point in points:
            stamp = point.get("timestamp")
            if isinstance(stamp, datetime):
                years.append(stamp.year)
            elif isinstance(stamp, date):
                years.append(stamp.year)
        if years:
            smoke["equity_years"] = "{0}-{1}".format(min(years), max(years))
        if len(points) <= 1:
            failures.append(
                "SMOKE equity curve has {0} points; expected > 1".format(len(points))
            )
        elif years and (min(years) < 2017 or max(years) > 2018):
            failures.append(
                "SMOKE equity timestamps are {0}, expected 2017-2018".format(
                    smoke["equity_years"]
                )
            )

    research_fail_keys = ("qc_research_project", "execution qc_project")
    research["failures"] = [
        item for item in failures if any(key in item for key in research_fail_keys)
    ]
    smoke["failures"] = [
        item for item in failures if not any(key in item for key in research_fail_keys)
    ]
    research["status"] = "PASS" if not research["failures"] else "FAIL"
    smoke["status"] = "PASS" if smoke_rows and not smoke["failures"] else "FAIL"
    return research, smoke


def load_holdout_exposures(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT strategy_id, research_lineage_id, status, backtest_id
            FROM holdout_exposures
            WHERE strategy_id = :strategy_id
            """
        ),
        {"strategy_id": STRATEGY_ID},
    ).mappings()
    return [dict(row) for row in rows]


def verify_live_spytrend() -> tuple[str, dict[str, Any]]:
    from jobs.sync_quantconnect import (
        get_live_portfolio,
        get_live_status,
        get_strategies,
        parse_portfolio,
    )

    strategies = get_strategies()
    target = None
    for row in strategies:
        if row["strategy_id"] == STRATEGY_ID or (row.get("name") or "") == STRATEGY_ID:
            target = row
            break
    if target is None:
        failed = evaluate_live_parser(None, None)
        failed["failures"].append("SPYTrend is not registered in strategies")
        return "MISSING", failed

    status = "UNKNOWN"
    try:
        live_result = get_live_status(target["qc_project_id"])
        status = str(live_result.get("status") or "UNKNOWN")
    except Exception as exc:
        failed = evaluate_live_parser(None, None)
        failed["failures"].append("live status error: {0}".format(redact(str(exc))))
        return status, failed

    try:
        raw = get_live_portfolio(target["qc_project_id"])
        portfolio = parse_portfolio(raw)
    except Exception as exc:
        failed = evaluate_live_parser(None, None)
        failed["failures"].append("live portfolio error: {0}".format(redact(str(exc))))
        return status, failed

    return status, evaluate_live_parser(portfolio, raw)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Stage 1 production schema, live parser, and holdout exposure.",
    )
    parser.add_argument("--git-sha", default=os.getenv("VERIFY_GIT_SHA", ""))
    parser.add_argument("--working-tree", default=os.getenv("VERIFY_WORKING_TREE", "UNKNOWN"))
    parser.add_argument("--streamlit", default=os.getenv("VERIFY_STREAMLIT", "UNKNOWN"))
    parser.add_argument("--migration", default=os.getenv("VERIFY_MIGRATION", "PASS"))
    parser.add_argument(
        "--working-tree-only",
        action="store_true",
        help=(
            "Inspect git status --porcelain with the server-file allowlist and "
            "exit. Does not query PostgreSQL or QuantConnect."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.working_tree_only:
        tree = inspect_git_working_tree()
        print(format_working_tree_report(tree))
        if tree.get("failures"):
            print()
            print("Failures:")
            for item in tree["failures"]:
                print("  - {0}".format(redact(item)))
        return 0 if tree.get("ok") else 1

    failures: list[str] = []
    schema_status = "FAIL"
    live_status = "UNKNOWN"
    live: dict[str, Any] = {
        "equity_parser": "FAIL",
        "positions_parser": "FAIL",
        "position_count": 0,
    }
    backtests: dict[str, Any] = {
        "dates_status": "FAIL",
        "holdout_status": "MISSING",
        "historical_count": 0,
    }
    research: dict[str, Any] = {"status": "FAIL"}
    smoke: dict[str, Any] = {"status": "FAIL", "count": 0}
    stage1_run: dict[str, Any] = {"status": "SKIP", "present": False}
    tree = inspect_git_working_tree()
    failures.extend(tree.get("failures") or [])
    working_tree_label = (
        "CLEAN"
        if tree.get("ok") and not tree.get("allowed_untracked")
        else ("CLEAN_WITH_SERVER_FILES" if tree.get("ok") else "DIRTY")
    )

    try:
        from db.connection import engine

        with engine.connect() as conn:
            tables, columns, migrations = load_schema(conn)
            schema_failures = check_schema(tables, columns, migrations)
            if schema_failures:
                failures.extend(schema_failures)
            else:
                schema_status = "PASS"

            rows = load_spytrend_backtests(conn)
            exposures = load_holdout_exposures(conn)
            backtests = evaluate_legacy_and_holdout(rows, exposures)
            failures.extend(backtests.get("failures") or [])
            strategy_row = load_spytrend_strategy(conn)
            smoke_id = None
            for row in rows:
                if str(row.get("research_test_type") or "").upper() == "SMOKE":
                    smoke_id = row.get("backtest_id")
                    break
            equity_points = load_equity_points(conn, str(smoke_id)) if smoke_id else []
            research, smoke = evaluate_research_and_smoke(
                strategy_row, rows, equity_points
            )
            failures.extend(research.get("failures") or [])
            failures.extend(smoke.get("failures") or [])
            try:
                run_rows = load_spytrend_research_runs(conn)
            except Exception as exc:
                failures.append(
                    "research_runs query error: {0}".format(redact(str(exc)))
                )
                run_rows = []
            stage1_run = evaluate_stage1_run(run_rows, rows)
            failures.extend(stage1_run.get("failures") or [])
    except Exception as exc:
        failures.append("database verification error: {0}".format(redact(str(exc))))

    try:
        live_status, live = verify_live_spytrend()
        failures.extend(live.get("failures") or [])
    except Exception as exc:
        failures.append("live verification error: {0}".format(redact(str(exc))))

    overall = "PASS" if not failures else "FAIL"
    report = format_report(
        git_sha=args.git_sha,
        working_tree=args.working_tree if args.working_tree != "UNKNOWN" else working_tree_label,
        streamlit=args.streamlit,
        migration=args.migration,
        schema=schema_status,
        live_status=live_status,
        live=live,
        backtests=backtests,
        overall=overall,
        working_tree_detail=tree,
        research=research,
        smoke=smoke,
        stage1_run=stage1_run,
    )
    print(report)
    if failures:
        print()
        print("Failures:")
        for item in failures:
            print("  - {0}".format(redact(item)))
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
