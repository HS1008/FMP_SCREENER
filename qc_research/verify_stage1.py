"""Read-only query-back of official Stage 1 identity.

Does not launch QuantConnect. Does not change economic_gate or authorize a rerun.
Live query-back uses DASHBOARD_READONLY_URL only.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Mapping

from qc_research.contracts.sealed_results import (
    MINIMUM_STAGE1_PINS,
    official_stage1_identity_blockers,
)


OFFICIAL_RUN_ID = "STAGE1_SPYTrend_c04553d8"
OFFICIAL_RUN_SQL = """
    SELECT
        research_run_id,
        strategy_id,
        git_commit,
        run_status,
        expected_experiment_count,
        completed_count,
        failed_count,
        skipped_count,
        holdout_accessed,
        holdout_access_count
    FROM research_runs
    WHERE research_run_id = :run_id
    LIMIT 1
"""


def evaluate_stage1_row(row: Mapping[str, Any] | None) -> dict[str, Any]:
    pin = dict(MINIMUM_STAGE1_PINS[OFFICIAL_RUN_ID])
    report: dict[str, Any] = {
        "present": False,
        "identity_ok": False,
        "blockers": [],
        "research_run_id": OFFICIAL_RUN_ID,
        "strategy_id": pin["strategy_id"],
        "git_commit_pinned": False,
        "holdout_accessed": False,
        "expected_experiment_count": pin["expected_experiment_count"],
    }
    if not row:
        report["blockers"] = ["official_run_missing"]
        return report
    blockers = official_stage1_identity_blockers(
        strategy_id=row.get("strategy_id"),
        research_run_id=OFFICIAL_RUN_ID,
        row=row,
    )
    commit = str(row.get("git_commit") or "").strip()
    report.update(
        {
            "present": True,
            "identity_ok": not blockers,
            "blockers": blockers,
            "git_commit_pinned": commit == str(pin.get("git_commit") or ""),
            "holdout_accessed": bool(row.get("holdout_accessed")),
        }
    )
    return report


def query_stage1_row(conn, *, run_id: str) -> dict[str, Any] | None:
    from sqlalchemy import text

    row = conn.execute(text(OFFICIAL_RUN_SQL), {"run_id": run_id}).mappings().first()
    return dict(row) if row else None


def verify_exit_code(report: Mapping[str, Any], *, require_present: bool) -> int:
    if report.get("present") and not report.get("identity_ok"):
        print("stage1=identity_refused")
        return 2
    if require_present and not report.get("present"):
        print("stage1=official_run_missing")
        return 3
    if report.get("identity_ok"):
        print("stage1=identity_ok")
        return 0
    print("stage1=not_ingested")
    return 0


def write_live_report(
    report: Mapping[str, Any],
    path: str,
    *,
    code_root: str = "",
) -> None:
    from datetime import datetime, timezone
    from pathlib import Path

    from jobs.audit_host_dashboard import write_facts

    write_facts(
        {
            "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "code_root": str(code_root or "").strip(),
            "present": bool(report.get("present")),
            "identity_ok": bool(report.get("identity_ok")),
            "blockers": [str(item) for item in (report.get("blockers") or [])],
            "git_commit_pinned": bool(report.get("git_commit_pinned")),
            "holdout_accessed": bool(report.get("holdout_accessed")),
            "expected_experiment_count": report.get("expected_experiment_count"),
            "research_run_id": report.get("research_run_id"),
            "strategy_id": report.get("strategy_id"),
        },
        Path(path),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify official Stage 1 Postgres identity")
    parser.add_argument("--live", action="store_true", default=False)
    parser.add_argument("--require-present", action="store_true", default=False)
    parser.add_argument("--out", default="")
    parser.add_argument("--code-root", default="")
    args = parser.parse_args(argv)
    pin = dict(MINIMUM_STAGE1_PINS[OFFICIAL_RUN_ID])
    if not args.live:
        print(
            json.dumps(
                {
                    "research_run_id": OFFICIAL_RUN_ID,
                    "strategy_id": pin["strategy_id"],
                    "git_commit": pin["git_commit"],
                    "expected_experiment_count": pin["expected_experiment_count"],
                }
            )
        )
        print("stage1=pin_only")
        return 0

    from db.dashboard_engine import (
        dashboard_engine,
        load_streamlit_env,
        writer_fallback_allowed,
    )

    if writer_fallback_allowed():
        print("DASHBOARD_ALLOW_WRITER_FALLBACK is not a live Stage 1 verify path")
        return 4
    load_streamlit_env()
    engine = dashboard_engine()
    with engine.connect() as conn:
        row = query_stage1_row(conn, run_id=OFFICIAL_RUN_ID)
    report = evaluate_stage1_row(row)
    print(
        json.dumps(
            {
                "present": report["present"],
                "identity_ok": report["identity_ok"],
                "blockers": report["blockers"],
                "git_commit_pinned": report["git_commit_pinned"],
                "holdout_accessed": report["holdout_accessed"],
                "expected_experiment_count": report["expected_experiment_count"],
            }
        )
    )
    if args.out:
        write_live_report(report, args.out, code_root=args.code_root)
        print("stage1_live_written={0}".format(args.out))
    return verify_exit_code(report, require_present=args.require_present)


if __name__ == "__main__":
    raise SystemExit(main())
