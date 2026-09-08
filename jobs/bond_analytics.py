"""Compute local bond analytics (bond_analytics_v1) for stored bonds with source-verified terms.

    python -m jobs.bond_analytics [--as-of YYYY-MM-DD] [--price-kind MID] [--dry-run] [--json]

Reads mi_bond_securities + mi_bond_quotes + the FRED par Treasury curve already in PostgreSQL,
writes mi_bond_analytics. No provider calls; bonds without verified terms or quotes are reported
as skips, never guessed. Exit codes: 0 ok, 3 configuration, 75 lock contention.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from market_intelligence.bonds import ANALYTICS_VERSION, compute_stored_bond_analytics
from market_intelligence.locking import EXIT_LOCK_CONTENTION, LockContention, writer_lock
from market_intelligence.nulls import strict_dumps
from market_intelligence.store import RUN_SUCCEEDED, finish_run, start_run


def run(argv: list[str] | None = None, *, engine=None) -> int:
    parser = argparse.ArgumentParser(description="Local bond analytics")
    parser.add_argument("--as-of", default=None, help="Pricing date (YYYY-MM-DD); default today")
    parser.add_argument("--price-kind", default="MID", choices=("MID", "BID", "ASK", "LAST"))
    parser.add_argument("--dry-run", action="store_true", help="Compute and report without writing")
    parser.add_argument("--wait-lock", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if engine is None:
        from market_intelligence.writer_db import WriterConfigurationError, writer_engine

        try:
            engine = writer_engine()
        except WriterConfigurationError as exc:
            print(strict_dumps({"status": "CONFIGURATION_REQUIRED", "reason": str(exc)}, indent=2))
            return 3
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    try:
        with writer_lock(engine, wait=args.wait_lock):
            if args.dry_run:
                with engine.connect() as conn:
                    tx = conn.begin()
                    report = compute_stored_bond_analytics(conn, as_of=as_of, price_kind=args.price_kind)
                    tx.rollback()
                status = "DRY_RUN"
            else:
                with engine.begin() as conn:
                    run_id = start_run(conn, source_id="ANALYTICS", dataset="bond_analytics")
                    report = compute_stored_bond_analytics(conn, as_of=as_of, price_kind=args.price_kind)
                    finish_run(conn, run_id, status=RUN_SUCCEEDED, counts={"inserted": report.computed}, details=report.as_dict())
                status = RUN_SUCCEEDED
    except LockContention as exc:
        print(strict_dumps({"status": "LOCK_CONTENTION", "reason": str(exc)}, indent=2))
        return EXIT_LOCK_CONTENTION
    payload = dict(report.as_dict(), status=status)
    if args.json:
        print(strict_dumps(payload, indent=2))
    else:
        print("Bond analytics {0} ({1}): computed={2} skipped={3} curve_points={4}".format(status, ANALYTICS_VERSION, report.computed, len(report.skipped), report.curve_points))
    return 0


if __name__ == "__main__":
    sys.exit(run())
