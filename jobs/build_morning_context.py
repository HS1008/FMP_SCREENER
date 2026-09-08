"""Build and publish an immutable morning_context_v2 snapshot from canonical PostgreSQL.

    python -m jobs.build_morning_context [--generated-at 2026-09-08T11:00:00+00:00] [--json] [--print-body]

Current-only: the capture time is the database transaction timestamp. ``--cutoff`` is
accepted only as an assertion that the requested cutoff is not in the past; a historical
cutoff exits 4 with HISTORICAL_RECONSTRUCTION_UNSUPPORTED (use the stored snapshot index).
``--supersede-reason`` publishes a corrected successor and marks the previous snapshot
SUPERSEDED without touching its body.

Uses the backend writer identity, holds the shared Market Intelligence writer lock, reads
inside one REPEATABLE READ transaction, and never calls a provider or an LLM.
Exit codes: 0 ok, 3 configuration, 4 historical cutoff refused, 75 lock contention.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from market_intelligence.locking import EXIT_LOCK_CONTENTION, LockContention, writer_lock
from market_intelligence.morning_context import HistoricalReconstructionUnsupported, build_and_publish
from market_intelligence.nulls import strict_dumps


def run(argv: list[str] | None = None, *, engine=None) -> int:
    parser = argparse.ArgumentParser(description="Build morning context snapshot")
    parser.add_argument("--generated-at", default=None, help="Explicit ISO timestamp for deterministic replays")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--print-body", action="store_true", help="Print the full snapshot JSON body")
    parser.add_argument("--wait-lock", action="store_true")
    parser.add_argument("--created-by", default="backend")
    parser.add_argument("--cutoff", default=None, help="Requested cutoff (ISO); refused when earlier than the DB capture time")
    parser.add_argument("--supersede-reason", default=None, help="Publish a corrected successor and mark the current snapshot SUPERSEDED")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if engine is None:
        from market_intelligence.writer_db import WriterConfigurationError, writer_engine

        try:
            engine = writer_engine()
        except WriterConfigurationError as exc:
            print(strict_dumps({"status": "CONFIGURATION_REQUIRED", "reason": str(exc)}, indent=2))
            return 3
    generated_at = datetime.fromisoformat(args.generated_at) if args.generated_at else None
    requested_cutoff = datetime.fromisoformat(args.cutoff) if args.cutoff else None
    try:
        with writer_lock(engine, wait=args.wait_lock):
            result = build_and_publish(engine, generated_at=generated_at, created_by=args.created_by, requested_cutoff=requested_cutoff, supersede_reason=args.supersede_reason)
    except HistoricalReconstructionUnsupported as exc:
        print(strict_dumps({"status": "HISTORICAL_RECONSTRUCTION_UNSUPPORTED", "reason": str(exc)}, indent=2))
        return 4
    except LockContention as exc:
        print(strict_dumps({"status": "LOCK_CONTENTION", "reason": str(exc)}, indent=2))
        return EXIT_LOCK_CONTENTION
    if args.print_body:
        print(strict_dumps(result.body, indent=2))
    elif args.json:
        print(strict_dumps(result.as_dict(), indent=2))
    else:
        print("Morning context {0}: {1} ({2}) sha256={3}".format(result.as_dict()["status"], result.snapshot_id, result.completeness, result.snapshot_sha256))
    return 0


if __name__ == "__main__":
    sys.exit(run())
