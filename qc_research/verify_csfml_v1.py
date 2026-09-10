"""Read-only query-back of official CSFML V1 identity.

Does not launch QuantConnect. Does not change economic_gate or authorize a rerun.
Live query-back uses DASHBOARD_READONLY_URL only.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Mapping

from qc_research.contracts.label_integrity import load_csfml_v1_label_integrity, official_csfml_v1_shas


OFFICIAL_RUN_SQL = """
    SELECT
        research_run_id,
        strategy_id,
        git_commit,
        holdout_accessed,
        holdout_access_count,
        economic_gate
    FROM research_runs
    WHERE research_run_id = :run_id
    LIMIT 1
"""


def evaluate_csfml_v1_row(
    row: Mapping[str, Any] | None,
    pin: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = dict(pin or load_csfml_v1_label_integrity())
    allowed = official_csfml_v1_shas(snapshot)
    report: dict[str, Any] = {
        "present": False,
        "identity_ok": False,
        "blockers": [],
        "research_run_id": snapshot["full_suite_run_id"],
        "strategy_id": snapshot["strategy_id"],
        "git_commit_pinned": False,
        "holdout_accessed": False,
        "economic_gate": None,
        "historical_v1_impact": snapshot["historical_v1_impact"],
        "rerun_authorized": bool(snapshot["rerun_authorized"]),
    }
    if not row:
        report["blockers"] = ["official_run_missing"]
        return report
    commit = str(row.get("git_commit") or "").strip()
    blockers: list[str] = []
    if commit not in allowed:
        blockers.append("git_commit_not_pinned")
    if row.get("holdout_accessed") in {True, 1, "true", "t"}:
        blockers.append("holdout_accessed")
    if int(row.get("holdout_access_count") or 0) != 0:
        blockers.append("holdout_access_count")
    gate = str(row.get("economic_gate") or "NOT_DEFINED")
    if gate != snapshot["economic_gate"]:
        blockers.append("economic_gate_changed")
    if str(row.get("strategy_id") or "") != snapshot["strategy_id"]:
        blockers.append("strategy_id_mismatch")
    if str(row.get("research_run_id") or "") != snapshot["full_suite_run_id"]:
        blockers.append("research_run_id_mismatch")
    report.update(
        {
            "present": True,
            "identity_ok": not blockers,
            "blockers": blockers,
            "git_commit_pinned": commit in allowed,
            "holdout_accessed": bool(row.get("holdout_accessed")),
            "economic_gate": gate,
        }
    )
    return report


def official_csfml_v1_identity_blockers(
    *,
    strategy_id: str | None,
    research_run_id: str | None,
    row: Mapping[str, Any] | None = None,
    engine: Any = None,
) -> list[str]:
    """Return blockers when the selected run is official CSFML V1.

    Empty when the run is not official V1 or the stored identity matches the
    pin. Query failures and a missing official row fail closed. Does not map
    blockers onto PASS/WATCH/FAIL or change economic_gate.
    """
    from qc_research.contracts.label_integrity import csfml_v1_integrity_caption

    if not csfml_v1_integrity_caption(strategy_id, research_run_id):
        return []
    pin = load_csfml_v1_label_integrity()
    record = row
    if record is None:
        if engine is None:
            return ["identity_query_failed"]
        try:
            with engine.connect() as conn:
                record = query_csfml_v1_row(conn, run_id=str(research_run_id))
        except Exception:
            return ["identity_query_failed"]
    report = evaluate_csfml_v1_row(record, pin)
    if report.get("identity_ok"):
        return []
    return [str(item) for item in (report.get("blockers") or ["identity_refused"])]


def query_csfml_v1_row(conn, *, run_id: str) -> dict[str, Any] | None:
    from sqlalchemy import text

    row = conn.execute(text(OFFICIAL_RUN_SQL), {"run_id": run_id}).mappings().first()
    return dict(row) if row else None


def verify_exit_code(report: Mapping[str, Any], *, require_present: bool) -> int:
    if report.get("present") and not report.get("identity_ok"):
        print("csfml_v1=identity_refused")
        return 2
    if require_present and not report.get("present"):
        print("csfml_v1=official_run_missing")
        return 3
    if report.get("identity_ok"):
        print("csfml_v1=identity_ok")
        return 0
    print("csfml_v1=not_ingested")
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
            "economic_gate": report.get("economic_gate"),
            "historical_v1_impact": report.get("historical_v1_impact"),
            "rerun_authorized": bool(report.get("rerun_authorized")),
            "research_run_id": report.get("research_run_id"),
        },
        Path(path),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify official CSFML V1 Postgres identity")
    parser.add_argument("--live", action="store_true", default=False)
    parser.add_argument("--require-present", action="store_true", default=False)
    parser.add_argument("--out", default="")
    parser.add_argument("--code-root", default="")
    args = parser.parse_args(argv)
    pin = load_csfml_v1_label_integrity()
    if not args.live:
        print(
            json.dumps(
                {
                    "historical_v1_impact": pin["historical_v1_impact"],
                    "rerun_authorized": pin["rerun_authorized"],
                    "economic_gate": pin["economic_gate"],
                    "full_suite_run_id": pin["full_suite_run_id"],
                }
            )
        )
        print("csfml_v1=pin_only")
        return 0

    from db.dashboard_engine import (
        dashboard_engine,
        load_streamlit_env,
        writer_fallback_allowed,
    )

    if writer_fallback_allowed():
        print("DASHBOARD_ALLOW_WRITER_FALLBACK is not a live CSFML V1 verify path")
        return 4
    load_streamlit_env()
    engine = dashboard_engine()
    with engine.connect() as conn:
        row = query_csfml_v1_row(conn, run_id=str(pin["full_suite_run_id"]))
    report = evaluate_csfml_v1_row(row, pin)
    print(
        json.dumps(
            {
                "present": report["present"],
                "identity_ok": report["identity_ok"],
                "blockers": report["blockers"],
                "git_commit_pinned": report["git_commit_pinned"],
                "holdout_accessed": report["holdout_accessed"],
                "economic_gate": report["economic_gate"],
                "historical_v1_impact": report["historical_v1_impact"],
                "rerun_authorized": report["rerun_authorized"],
            }
        )
    )
    if args.out:
        write_live_report(report, args.out, code_root=args.code_root)
        print("csfml_v1_live_written={0}".format(args.out))
    return verify_exit_code(report, require_present=args.require_present)


if __name__ == "__main__":
    raise SystemExit(main())
