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
HOLDOUT_START = date(2023, 1, 1)

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
) -> str:
    lines = [
        "STAGE 1 PRODUCTION VERIFICATION",
        "",
        "Deployment",
        "  Git SHA: {0}".format(git_sha or "UNKNOWN"),
        "  Working tree: {0}".format(working_tree),
        "  Streamlit: {0}".format(streamlit),
        "",
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
        "Overall:",
        "  {0}".format(overall),
    ]
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
                backtest_start,
                backtest_end
            FROM backtests
            WHERE strategy_id = :strategy_id
            """
        ),
        {"strategy_id": STRATEGY_ID},
    ).mappings()
    return [dict(row) for row in rows]


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
        working_tree=args.working_tree,
        streamlit=args.streamlit,
        migration=args.migration,
        schema=schema_status,
        live_status=live_status,
        live=live,
        backtests=backtests,
        overall=overall,
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
