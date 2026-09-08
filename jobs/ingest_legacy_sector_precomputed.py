"""Ingest saved legacy FMP precomputed bundles into canonical sector snapshots.

    python -m jobs.ingest_legacy_sector_precomputed [--root outputs/precomputed] [--dry-run] [--json]

Reads Parquet/JSON only. Never calls FMP, never imports nightly_refresh/data_loader, and
honours the shared Market Intelligence writer lock. Exit codes match
``jobs.market_intelligence_refresh`` (0 ok, 2 partial, 3 configuration, 75 lock contention).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from market_intelligence.legacy_bridge import discover_bundles, ingest_precomputed_root
from market_intelligence.locking import EXIT_LOCK_CONTENTION, LockContention, writer_lock
from market_intelligence.nulls import strict_dumps


def default_root() -> Path:
    import config  # legacy config module only (paths); no provider calls at import

    return Path(config.OUTPUT_DIR) / "precomputed"


def run(argv: list[str] | None = None, *, engine=None) -> int:
    parser = argparse.ArgumentParser(description="Legacy precomputed sector bundle bridge")
    parser.add_argument("--root", default=None, help="Precomputed root (default: config.OUTPUT_DIR/precomputed)")
    parser.add_argument("--dry-run", action="store_true", help="List discovered bundles only; no DB writes")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--wait-lock", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    root = Path(args.root) if args.root else default_root()
    if not root.is_dir():
        print(strict_dumps({"status": "CONFIGURATION_REQUIRED", "reason": "precomputed root missing", "root": str(root)}, indent=2))
        return 3
    if args.dry_run:
        plan = [{"kind": kind, "path": str(path), "context": ctx} for kind, path, ctx in discover_bundles(root)]
        print(strict_dumps({"status": "DRY_RUN_VALIDATED", "root": str(root), "bundles": plan}, indent=2))
        return 0
    if engine is None:
        from market_intelligence.writer_db import WriterConfigurationError, writer_engine

        try:
            engine = writer_engine()
        except WriterConfigurationError as exc:
            print(strict_dumps({"status": "CONFIGURATION_REQUIRED", "reason": str(exc)}, indent=2))
            return 3
    try:
        with writer_lock(engine, wait=args.wait_lock):
            from market_intelligence.store import upsert_source_registry

            with engine.begin() as conn:
                upsert_source_registry(conn, enabled={"FMP_LEGACY": True}, access={"FMP_LEGACY": "CONFIGURED"})
            report = ingest_precomputed_root(engine, root)
    except LockContention as exc:
        print(strict_dumps({"status": "LOCK_CONTENTION", "reason": str(exc)}, indent=2))
        return EXIT_LOCK_CONTENTION
    payload = report.as_dict()
    payload["status"] = "SUCCEEDED" if not report.failed_bundles else "PARTIAL"
    print(strict_dumps(payload, indent=2) if args.json else "Legacy bridge: {0} ({1} bundles, {2} failed, {3} quarantined)".format(payload["status"], payload["bundles_processed"], payload["bundles_failed"], len(payload["quarantined"])))
    return 0 if not report.failed_bundles else 2


if __name__ == "__main__":
    sys.exit(run())
