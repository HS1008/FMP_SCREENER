"""Market Intelligence refresh orchestrator.

    python -m jobs.market_intelligence_refresh --fred --build-analytics
    python -m jobs.market_intelligence_refresh --all-configured
    python -m jobs.market_intelligence_refresh --all-configured --dry-run
    python -m jobs.market_intelligence_refresh --probe-config

Exit codes:
    0  success (all requested configured sources succeeded; disabled sources skipped)
    2  partial failure: at least one configured source failed; successful work is kept
    3  configuration error (no writer DB, unknown flags)
    75 lock contention (another Market Intelligence writer holds the advisory lock)

``--dry-run`` validates configuration, contracts and the planned operations without any
provider call or database mutation. ``--probe-config`` is the explicit read-only probe: it
reports which providers are configured (never their secrets) and whether the writer DB
is reachable, without writing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from typing import Any

from market_intelligence import CODE_VERSION
from market_intelligence.catalog import CATALOG, CATALOG_VERSION, FRED_SOURCE_ID
from market_intelligence.fmp_mode import fmp_free_mode, legacy_fmp_enabled, treasury_enabled
from market_intelligence.fred_client import api_key_from_env
from market_intelligence.locking import EXIT_LOCK_CONTENTION, LockContention, writer_lock
from market_intelligence.nulls import strict_dumps

logger = logging.getLogger("market_intelligence.refresh")

EXIT_OK = 0
EXIT_PARTIAL = 2
EXIT_CONFIG = 3

LEGACY_SOURCE_ID = "FMP_LEGACY"


def _legacy_root(env: dict[str, str]) -> str | None:
    return (env.get("MARKET_INTELLIGENCE_PRECOMPUTED_ROOT") or "").strip() or None


def plan(args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    """Planned operations and source configuration status (no secrets, no calls)."""
    fred_configured = api_key_from_env(env) is not None
    legacy_root = _legacy_root(env)
    if legacy_root is None:
        try:
            import config  # noqa: WPS433 - legacy config is safe (no provider calls at import)

            legacy_root = str(config.OUTPUT_DIR / "precomputed")
        except Exception:  # noqa: BLE001
            legacy_root = None
    legacy_configured = bool(legacy_root and os.path.isdir(legacy_root))
    want_all = bool(args.all_configured)
    steps: list[dict[str, Any]] = []
    if args.fred or want_all:
        steps.append(
            {
                "step": "fred",
                "source_id": FRED_SOURCE_ID,
                "configured": fred_configured,
                "action": ("ingest" if fred_configured else "skip_unconfigured") if (want_all or fred_configured) else "fail_unconfigured",
                "mode": args.mode,
                "series": list(args.series) if args.series else [s.series_id for s in CATALOG],
                "catalog_version": CATALOG_VERSION,
            }
        )
    from market_intelligence.finra_catalog import FINRA_QUERY_SOURCE_ID
    from market_intelligence.finra_client import configured_from_env as finra_configured_from_env

    finra_configured = finra_configured_from_env(env)
    if args.finra or want_all:
        steps.append(
            {
                "step": "finra",
                "source_id": FINRA_QUERY_SOURCE_ID,
                "configured": finra_configured,
                "action": ("ingest" if finra_configured else "skip_unconfigured") if (want_all or finra_configured) else "fail_unconfigured",
                "mode": args.mode,
            }
        )
    if args.legacy_sector or (want_all and legacy_fmp_enabled(env)):
        # Explicit --legacy-sector is a human opt-in for that run; --all-configured stays FMP-free.
        legacy_allowed = bool(args.legacy_sector) or legacy_fmp_enabled(env)
        steps.append(
            {
                "step": "legacy_sector",
                "source_id": LEGACY_SOURCE_ID,
                "configured": legacy_configured and legacy_allowed,
                "root": legacy_root,
                "action": (
                    "ingest"
                    if legacy_configured and legacy_allowed
                    else ("skip_unconfigured" if want_all or fmp_free_mode(env) else "fail_unconfigured")
                ),
            }
        )
    if args.treasury or want_all:
        ust_on = treasury_enabled(env)
        steps.append(
            {
                "step": "treasury",
                "source_id": "TREASURY",
                "configured": ust_on,
                "action": "ingest" if ust_on else ("skip_unconfigured" if want_all else "fail_unconfigured"),
            }
        )
    from market_intelligence.equity_eod import adapter_from_env

    equity_adapter = adapter_from_env(env)
    if args.equity or want_all:
        equity_ok = equity_adapter.access_status == "CONFIGURED"
        steps.append(
            {
                "step": "equity",
                "source_id": "EQUITY_EOD",
                "configured": equity_ok,
                "provider": equity_adapter.source_id,
                "action": "ingest" if equity_ok else ("skip_unconfigured" if want_all else "fail_unconfigured"),
                "reason": equity_adapter.reason,
            }
        )
    if args.build_analytics or want_all:
        steps.append({"step": "build_analytics", "action": "compute"})
    if args.build_morning or want_all:
        steps.append({"step": "build_morning", "action": "compute"})
    from market_intelligence.adapters import probe_all

    return {
        "code_version": CODE_VERSION,
        "dry_run": bool(args.dry_run),
        "writer_configured": _writer_configured(env),
        "steps": steps,
        "external_adapters": {sid: {"access_status": p.access_status, "enabled": p.enabled, "reason": p.reason} for sid, p in probe_all(env).items()},
    }


def _writer_configured(env: dict[str, str]) -> bool:
    from market_intelligence.writer_db import writer_url

    if env is os.environ:
        return writer_url() is not None
    return writer_url(env) is not None or bool(env.get("DATABASE_URL")) or bool(env.get("DB_HOST"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Market Intelligence refresh")
    parser.add_argument("--fred", action="store_true", help="Ingest the FRED catalog")
    parser.add_argument("--finra", action="store_true", help="Ingest FINRA Query API corporate-bond aggregates")
    parser.add_argument("--legacy-sector", action="store_true", help="Ingest legacy precomputed sector bundles (no FMP calls)")
    parser.add_argument("--treasury", action="store_true", help="Ingest official Treasury daily XML par yields")
    parser.add_argument("--equity", action="store_true", help="Ingest independent equity/ETF daily bars")
    parser.add_argument("--build-analytics", action="store_true", help="Recompute versioned analytics")
    parser.add_argument("--build-morning", action="store_true", help="Build and publish a morning context snapshot")
    parser.add_argument("--all-configured", action="store_true", help="Run every configured step; disabled sources are explicit skips")
    parser.add_argument("--dry-run", action="store_true", help="Validate configuration and plan only (no provider calls, no DB writes)")
    parser.add_argument("--probe-config", action="store_true", help="Read-only probe of configuration and DB reachability")
    parser.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    parser.add_argument("--series", nargs="*", default=None, help="Restrict FRED ingestion to these series ids")
    parser.add_argument("--as-of", default=None, help="Analytics as-of date / FRED retrieval date (YYYY-MM-DD); default today. The morning snapshot is always current-only.")
    parser.add_argument("--backfill-analytics-from", default=None, metavar="YYYY-MM-DD", help="Bounded, idempotent history backfill: compute metrics for every stored observation date on/after this date (latest-revised vintage, labeled as such)")
    parser.add_argument("--wait-lock", action="store_true", help="Wait for the writer lock instead of failing fast")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON status")
    return parser


def run(argv: list[str] | None = None, *, engine=None, fred_client_factory=None, env: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if not any((args.fred, args.finra, args.legacy_sector, args.treasury, args.equity, args.build_analytics, args.build_morning, args.all_configured, args.probe_config)):
        parser.error("choose at least one of --fred/--finra/--legacy-sector/--treasury/--equity/--build-analytics/--build-morning/--all-configured/--probe-config")
    the_plan = plan(args, env)
    status: dict[str, Any] = {"plan": the_plan, "results": {}, "status": "PLANNED"}

    if args.probe_config:
        status["probe"] = _probe(engine)
        status["status"] = "PROBED"
        _emit(status, args.json)
        return EXIT_OK

    if args.dry_run:
        status["status"] = "DRY_RUN_VALIDATED"
        _emit(status, args.json)
        return EXIT_OK

    if not the_plan["writer_configured"] and engine is None:
        status["status"] = "CONFIGURATION_REQUIRED"
        status["error"] = "writer database not configured"
        _emit(status, args.json)
        return EXIT_CONFIG

    if engine is None:
        from market_intelligence.writer_db import writer_engine

        engine = writer_engine()

    try:
        with writer_lock(engine, wait=args.wait_lock):
            exit_code = _execute(args, the_plan, status, engine, fred_client_factory, env)
    except LockContention as exc:
        status["status"] = "LOCK_CONTENTION"
        status["error"] = str(exc)
        _emit(status, args.json)
        return EXIT_LOCK_CONTENTION
    _emit(status, args.json)
    return exit_code


def _probe(engine) -> dict[str, Any]:
    from market_intelligence.finra_client import configured_from_env as finra_configured_from_env

    probe: dict[str, Any] = {
        "fred_api_key_present": api_key_from_env() is not None,
        "finra_credentials_present": finra_configured_from_env(),
    }
    try:
        if engine is None:
            from market_intelligence.writer_db import writer_engine

            engine = writer_engine()
        from sqlalchemy import text

        with engine.connect() as conn:
            probe["db_reachable"] = bool(conn.execute(text("SELECT 1")).scalar())
            probe["mi_tables"] = int(conn.execute(text("SELECT COUNT(*) FROM information_schema.tables WHERE table_name LIKE 'mi\\_%'")).scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        probe["db_reachable"] = False
        probe["db_error"] = exc.__class__.__name__
    return probe


def _execute(args, the_plan, status, engine, fred_client_factory, env) -> int:
    from market_intelligence.store import RUN_FAILED, RUN_SKIPPED, RUN_SUCCEEDED, finish_run, start_run, upsert_source_registry

    failures = 0
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    fred_key = api_key_from_env(env)
    from market_intelligence.finra_catalog import FINRA_QUERY_SOURCE_ID, FINRA_TRACE_SOURCE_ID
    from market_intelligence.finra_client import configured_from_env as finra_configured_from_env

    finra_key = finra_configured_from_env(env)
    legacy_step = next((s for s in the_plan["steps"] if s["step"] == "legacy_sector"), None)
    enabled = {FRED_SOURCE_ID: fred_key is not None, LEGACY_SOURCE_ID: bool(legacy_step and legacy_step["configured"] and legacy_fmp_enabled(env)), FINRA_QUERY_SOURCE_ID: finra_key}
    access = {
        FRED_SOURCE_ID: "CONFIGURED" if fred_key else "CONFIGURATION_REQUIRED",
        LEGACY_SOURCE_ID: "CONFIGURED" if enabled[LEGACY_SOURCE_ID] else ("RETIRED_OPTIONAL" if fmp_free_mode(env) else "CONFIGURATION_REQUIRED"),
        FINRA_QUERY_SOURCE_ID: "CONFIGURED" if finra_key else "CONFIGURATION_REQUIRED",
        FINRA_TRACE_SOURCE_ID: "ENTITLEMENT_REQUIRED",
    }
    from market_intelligence.adapters import IBKR_SOURCE_ID, probe_all

    adapter_status = probe_all(env)
    for source_id, probe in adapter_status.items():
        if source_id == IBKR_SOURCE_ID:
            continue  # Windows collector owns this row; FRED refresh must not clobber it
        if source_id in {FINRA_QUERY_SOURCE_ID, FINRA_TRACE_SOURCE_ID}:
            continue
        enabled[source_id] = False
        access[source_id] = probe.access_status
    status["external_adapters"] = {sid: {"access_status": p.access_status, "reason": p.reason} for sid, p in adapter_status.items()}
    with engine.begin() as conn:
        upsert_source_registry(conn, enabled=enabled, access=access, preserve={IBKR_SOURCE_ID})
        from market_intelligence.ingest_finra import ensure_finra_sources, record_individual_trace_limitation

        ensure_finra_sources(conn, query_enabled=bool(finra_key), query_access=access[FINRA_QUERY_SOURCE_ID])
        record_individual_trace_limitation(conn)
        parent_run_id = start_run(conn, source_id="ORCHESTRATOR", dataset="market_intelligence_refresh")

    for step in the_plan["steps"]:
        name = step["step"]
        if step.get("action") == "skip_unconfigured":
            status["results"][name] = {"status": RUN_SKIPPED, "reason": "source not configured"}
            with engine.begin() as conn:
                rid = start_run(conn, source_id=step["source_id"], dataset=name, parent_run_id=parent_run_id)
                finish_run(conn, rid, status=RUN_SKIPPED, details={"reason": "not configured"})
            continue
        if step.get("action") == "fail_unconfigured":
            status["results"][name] = {"status": RUN_FAILED, "reason": "explicitly requested source is not configured"}
            failures += 1
            continue
        try:
            if name == "fred":
                from market_intelligence.fred_client import FredClient
                from market_intelligence.ingest_fred import ingest_fred_catalog

                client = fred_client_factory() if fred_client_factory else FredClient(fred_key)
                report = ingest_fred_catalog(engine, client, series_ids=args.series or None, mode=args.mode, parent_run_id=parent_run_id, today=as_of)
                status["results"][name] = report.as_dict()
                if report.failed:
                    failures += 1
            elif name == "finra":
                from market_intelligence.finra_client import FinraClient, credentials_from_env
                from market_intelligence.ingest_finra import ingest_finra

                client_id, client_secret = credentials_from_env(env)
                client = FinraClient(client_id, client_secret)
                report = ingest_finra(engine, client, parent_run_id=parent_run_id, today=as_of, mode=args.mode)
                status["results"][name] = report.as_dict()
                if report.failed:
                    failures += 1
            elif name == "legacy_sector":
                from market_intelligence.legacy_bridge import ingest_precomputed_root

                report = ingest_precomputed_root(engine, step["root"], parent_run_id=parent_run_id)
                status["results"][name] = report.as_dict()
                if report.failed_bundles:
                    failures += 1
            elif name == "treasury":
                from market_intelligence.ingest_treasury import ingest_treasury

                report = ingest_treasury(engine, parent_run_id=parent_run_id, today=as_of)
                status["results"][name] = report.as_dict()
                if report.failed:
                    failures += 1
            elif name == "equity":
                from market_intelligence.equity_eod import ingest_equity_eod

                report = ingest_equity_eod(engine, parent_run_id=parent_run_id, today=as_of, env=env)
                status["results"][name] = report.as_dict()
                if report.failed:
                    failures += 1
            elif name == "build_analytics":
                from market_intelligence.analytics import build_analytics, last_analytics_run_at

                history_start = date.fromisoformat(args.backfill_analytics_from) if getattr(args, "backfill_analytics_from", None) else None
                with engine.begin() as conn:
                    # Revision-aware incremental mode: recompute from the earliest observation
                    # date rewritten since the last successful analytics run (None -> first run,
                    # which only computes the latest date unless a backfill start is given).
                    since = None if history_start else last_analytics_run_at(conn)
                    rid = start_run(conn, source_id="ANALYTICS", dataset="metric_snapshots", parent_run_id=parent_run_id)
                    report = build_analytics(conn, as_of=as_of, run_id=rid, history_start=history_start, since=since)
                    details = report.as_dict()
                    details["mode"] = "backfill" if history_start else ("incremental_since_{0}".format(since.isoformat()) if since else "latest_only")
                    finish_run(conn, rid, status=RUN_SUCCEEDED, counts={"inserted": report.metrics_written + report.credit_written}, details=details)
                status["results"][name] = details
            elif name == "build_morning":
                from market_intelligence.morning_context import build_and_publish

                result = build_and_publish(engine, parent_run_id=parent_run_id, extra_params={"analytics_as_of": as_of.isoformat() if as_of else None})
                status["results"][name] = result.as_dict()
        except Exception as exc:  # noqa: BLE001 - keep other steps running
            logger.exception("step %s failed", name)
            status["results"][name] = {"status": RUN_FAILED, "error": exc.__class__.__name__}
            failures += 1

    final = RUN_SUCCEEDED if failures == 0 else "PARTIAL"
    with engine.begin() as conn:
        finish_run(conn, parent_run_id, status=final, details={"failures": failures, "steps": [s["step"] for s in the_plan["steps"]]})
    status["status"] = final
    status["parent_run_id"] = parent_run_id
    status["failures"] = failures
    return EXIT_OK if failures == 0 else EXIT_PARTIAL


def _emit(status: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(strict_dumps(status, indent=2))
        return
    print("Market Intelligence refresh: {0}".format(status.get("status")))
    for step in status.get("plan", {}).get("steps", []):
        print("  plan {0}: {1}".format(step["step"], step.get("action")))
    for name, result in status.get("results", {}).items():
        summary = result.get("status") or result.get("series_failed")
        print("  {0}: {1}".format(name, json.dumps(result if len(strict_dumps(result)) < 400 else {"status": summary}, default=str)))
    if status.get("error"):
        print("  error: {0}".format(status["error"]))


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
