"""Ingest a hash-verified ``sector_internals_v1`` artifact into canonical PostgreSQL.

    python -m jobs.ingest_pit_sector_internals --artifact path/to/internals.json [--validate-only] [--json]

The artifact is produced locally by the isolated quant-strategies MarketIntelligenceResearch
producer (``python -m research.market_intelligence.cli internals ...``). This job reads that file
only: no QuantConnect, no provider calls, no constituent-level data. Honours the shared writer lock.
Exit codes: 0 ok, 2 rejected, 3 configuration, 75 lock contention.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from market_intelligence.locking import EXIT_LOCK_CONTENTION, LockContention, writer_lock
from market_intelligence.nulls import strict_dumps
from market_intelligence.pit_sector import ArtifactRejected, ingest_artifact, load_artifact, record_rejection

EXIT_REJECTED = 2
EXIT_CONFIGURATION = 3


def run(argv: list[str] | None = None, *, engine=None) -> int:
    parser = argparse.ArgumentParser(description="PIT sector internals consumer (sector_internals_v1)")
    parser.add_argument("--artifact", required=True, help="Path to a sector_internals_v1 JSON artifact")
    parser.add_argument("--validate-only", action="store_true", help="Verify hash/schema/date gate; no DB writes")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--wait-lock", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    path = Path(args.artifact)
    if not path.is_file():
        print(strict_dumps({"status": "CONFIGURATION_REQUIRED", "reason": "artifact file missing", "artifact": str(path)}, indent=2))
        return EXIT_CONFIGURATION
    try:
        artifact = load_artifact(path)
    except (ArtifactRejected, ValueError) as exc:
        # Rejected before any DB access; when a writer is available the rejection is also recorded below.
        if not args.validate_only and engine is not None:
            with engine.begin() as conn:
                record_rejection(conn, reason=str(exc), source_ref=path.name)
        print(strict_dumps({"status": "REJECTED", "reason": str(exc), "artifact": path.name}, indent=2))
        return EXIT_REJECTED
    if args.validate_only:
        print(strict_dumps({"status": "VALIDATED", "artifact_sha256": artifact["artifact_sha256"], "rows": len(artifact["rows"]), "provenance": artifact["provenance"], "boundary": artifact["boundary"]}, indent=2))
        return 0
    if engine is None:
        from market_intelligence.writer_db import WriterConfigurationError, writer_engine

        try:
            engine = writer_engine()
        except WriterConfigurationError as exc:
            print(strict_dumps({"status": "CONFIGURATION_REQUIRED", "reason": str(exc)}, indent=2))
            return EXIT_CONFIGURATION
    try:
        with writer_lock(engine, wait=args.wait_lock):
            from market_intelligence.store import upsert_source_registry

            with engine.begin() as conn:
                upsert_source_registry(conn, enabled={"QC_MARKET_INTELLIGENCE": True}, access={"QC_MARKET_INTELLIGENCE": "CONFIGURED"})
            try:
                with engine.begin() as conn:
                    report = ingest_artifact(conn, artifact, source_ref=path.name)
            except ArtifactRejected as exc:
                with engine.begin() as conn:
                    run_id = record_rejection(conn, reason=str(exc), source_ref=path.name)
                print(strict_dumps({"status": "REJECTED", "reason": str(exc), "run_id": run_id}, indent=2))
                return EXIT_REJECTED
    except LockContention as exc:
        print(strict_dumps({"status": "LOCK_CONTENTION", "reason": str(exc)}, indent=2))
        return EXIT_LOCK_CONTENTION
    payload = report.as_dict()
    print(strict_dumps(payload, indent=2) if args.json else "PIT sector internals: {0} ({1} rows: {2} inserted, {3} revised, {4} unchanged; provenance {5})".format(payload["status"], payload["rows_received"], payload["rows_inserted"], payload["rows_revised"], payload["rows_unchanged"], payload["provenance"]))
    return 0


if __name__ == "__main__":
    sys.exit(run())
