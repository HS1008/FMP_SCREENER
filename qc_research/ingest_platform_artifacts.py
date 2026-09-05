#!/usr/bin/env python3
"""Ingest proven platform REAL_QC artifacts into PostgreSQL.

One command when DATABASE_URL / DB_* is available in an authorized
environment. Missing credentials skip (exit 0) and do not invent a database.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from qc_research.platform_ingest import (
    DEFAULT_ARTIFACT_ROOT,
    SKIP_NO_DATABASE,
    discover_platform_files,
    ingest_platform_files,
    live_postgres_configured,
    monitor_view_from_artifacts,
    normalize_platform_file,
    postgres_engine,
    require_live_postgres_ingest,
    verify_monitor_view,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest proven platform research artifacts")
    parser.add_argument(
        "--root",
        default=str(DEFAULT_ARTIFACT_ROOT),
        help="Directory or JSON file of platform_artifact_v1 / smoke records",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate and wrap without touching PostgreSQL")
    parser.add_argument(
        "--verify-monitor",
        action="store_true",
        help="Build the Strategy Monitor read model from ingested / wrapped payloads",
    )
    ns = parser.parse_args(argv)
    root = Path(ns.root)
    paths = discover_platform_files(root)
    if not paths:
        print("No platform artifacts found under {0}".format(root))
        return 1
    normalized: list[tuple[str, dict]] = []
    errors: list[str] = []
    for path in paths:
        try:
            normalized.extend(normalize_platform_file(path))
        except Exception as exc:
            errors.append("{0}: {1}".format(path, exc))
    if errors:
        print("Validation failed:")
        for item in errors:
            print("  {0}".format(item))
        return 1
    print("Prepared {0} artifact(s) from {1} file(s)".format(len(normalized), len(paths)))
    if ns.dry_run:
        if ns.verify_monitor:
            view = verify_monitor_view(monitor_view_from_artifacts(normalized))
            print(
                "Monitor dry-run ok strategy={0} run={1} provenance={2} intercept_only={3}".format(
                    view.get("strategy_id"),
                    view.get("research_run_id"),
                    view.get("provenance_kind"),
                    view.get("intercept_only_flag"),
                )
            )
        print("Dry-run complete. PostgreSQL was not contacted.")
        return 0
    if not live_postgres_configured():
        print(SKIP_NO_DATABASE)
        if ns.verify_monitor:
            view = verify_monitor_view(monitor_view_from_artifacts(normalized))
            print(
                "Monitor payload verified without live PostgreSQL strategy={0} run={1}".format(
                    view.get("strategy_id"),
                    view.get("research_run_id"),
                )
            )
        return 0
    require_live_postgres_ingest()
    engine = postgres_engine()
    with engine.begin() as conn:
        summary = ingest_platform_files(conn, paths, root=root)
    print(json.dumps({key: summary[key] for key in ("ingested", "skipped", "errors")}, indent=2))
    if summary["errors"]:
        return 1
    if ns.verify_monitor:
        view = verify_monitor_view(monitor_view_from_artifacts(normalized))
        print(
            "Monitor verified strategy={0} run={1} provenance={2} winner={3}".format(
                view.get("strategy_id"),
                view.get("research_run_id"),
                view.get("provenance_kind"),
                view.get("winner_backtest_id"),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
