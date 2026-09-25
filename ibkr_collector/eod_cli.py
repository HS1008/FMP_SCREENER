"""Windows-local fetch-eod command. Read-only historical bars, no orders.

Exit codes:
  0  FULL_SUCCESS — local fetch FULL_SUCCESS and server SUCCEEDED/COMPLETE/promotion_eligible
  1  PARTIAL_SUCCESS or COVERAGE_MISMATCH — successful bars may still be stored; not fully fresh
  2  FAILED — TWS down, zero successful symbols, or server FAILED/EMPTY
  3  config/delivery error
  4  eod.lock contention
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any, Mapping

from ibkr_collector import DEFAULT_EOD_CLIENT_ID, DEFAULT_TWS_HOST, DEFAULT_TWS_PORT
from ibkr_collector.config import load_config
from ibkr_collector.delivery import DeliveryError, IngestClient
from ibkr_collector.historical import (
    ADJUSTMENT_BASIS_ADJUSTED_LAST,
    BACKFILL_DURATION,
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_FULL_SUCCESS,
    EXIT_LOCK,
    EXIT_PARTIAL_SUCCESS,
    INCREMENTAL_DURATION,
    OVERALL_FULL_SUCCESS,
    PHASE3_SYMBOLS,
    WHAT_TO_SHOW_ADJUSTED,
    connect_historical_session,
    coverage_summary,
    exit_code_for_coverage,
    fetch_universe,
)
from ibkr_collector.lock import InstanceLock
from ibkr_collector.logging_setup import setup_logging
from ibkr_collector.secrets_win import read_ingest_token
from market_intelligence.equity_eod import EQUITY_SOURCE_ID
from market_intelligence.ibkr_eod import IBKR_PROVIDER, results_to_ingest_payload
from market_intelligence.taxonomy import UNIVERSE_SYMBOLS


def acquire_eod_lock(cfg) -> InstanceLock | None:
    """Separate from the quote collector lock so quotes and EOD can run together."""
    lock = InstanceLock(cfg.eod_lock_path)
    if not lock.acquire():
        return None
    return lock


def request_mode_for(*, backfill: bool, symbols: list[str]) -> str:
    """Label the collector run. History window is unchanged: 2 Y only with --backfill, else 1 W.

    Scheduled ``fetch-eod`` against the dashboard universe is incremental, not a
    one-off full-universe validation.
    """
    if backfill:
        return "backfill"
    if symbols == list(PHASE3_SYMBOLS):
        return "smoke"
    return "incremental"


def exit_code_for_finalize(*, local_summary: Mapping[str, Any], server: Mapping[str, Any]) -> int:
    """Combine local TWS coverage with the authoritative server finalize payload."""
    local_full = local_summary.get("overall") == OVERALL_FULL_SUCCESS
    run_status = str(server.get("run_status") or "").upper()
    coverage = str(server.get("coverage_status") or "").upper()
    promotion = bool(server.get("promotion_eligible"))
    if run_status == "SUCCEEDED" and coverage == "COMPLETE" and promotion and local_full:
        return EXIT_FULL_SUCCESS
    if run_status == "FAILED" or coverage in {"FAILED", "EMPTY"}:
        return EXIT_FAILED
    if coverage in {"PARTIAL", "COVERAGE_MISMATCH"} or run_status == "PARTIAL":
        return EXIT_PARTIAL_SUCCESS
    if server.get("ok") is False:
        return EXIT_FAILED
    local_exit = exit_code_for_coverage(local_summary)
    if local_exit != EXIT_FULL_SUCCESS:
        return local_exit
    if not promotion:
        return EXIT_PARTIAL_SUCCESS
    return EXIT_PARTIAL_SUCCESS


def run_fetch_eod(args: Any) -> int:
    cfg = load_config()
    setup_logging(cfg.log_dir)
    lock = acquire_eod_lock(cfg)
    if lock is None:
        sys.stderr.write("fetch-eod already running (eod.lock)\n")
        return EXIT_LOCK
    try:
        return _run_fetch_eod(args, cfg)
    finally:
        lock.release()


def _run_fetch_eod(args: Any, cfg) -> int:
    host = args.host or cfg.tws_host or DEFAULT_TWS_HOST
    port = args.port or cfg.tws_port or DEFAULT_TWS_PORT
    client_id = args.client_id or DEFAULT_EOD_CLIENT_ID
    if host not in {"127.0.0.1", "localhost", "::1"}:
        sys.stderr.write("fetch-eod refuses non-localhost TWS hosts\n")
        return EXIT_CONFIG
    symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()] if args.symbols else list(UNIVERSE_SYMBOLS)
    duration = BACKFILL_DURATION if args.backfill else INCREMENTAL_DURATION
    from ibkr_collector.budget import acquire, release

    budget = acquire("eod", 1)
    if not budget.allowed:
        sys.stderr.write("shared TWS budget denied for EOD ({0})\n".format(budget.reason))
        return EXIT_FAILED
    session, client, error = connect_historical_session(host=host, port=port, client_id=client_id)
    if session is None:
        release("eod", 1)
        sys.stderr.write("TWS unavailable: {0}\n".format(error))
        return EXIT_FAILED
    try:
        results = fetch_universe(session, symbols, duration=duration)
    finally:
        release("eod", 1)
        try:
            client.disconnect()
        except Exception:
            pass
    summary = coverage_summary(results, requested=symbols)
    summary["request_mode"] = request_mode_for(backfill=bool(args.backfill), symbols=symbols)
    sys.stdout.write(json.dumps(summary, sort_keys=True, default=str) + "\n")
    coverage_exit = exit_code_for_coverage(summary)
    if args.dry_run:
        return coverage_exit
    payload = results_to_ingest_payload(
        results,
        collector_id=cfg.collector_id,
        requested=symbols,
        request_mode=summary["request_mode"],
    )
    batch_id = str(uuid.uuid4())
    payload["batch_id"] = batch_id
    delivery = IngestClient(cfg.ingest_url, read_ingest_token())
    if not delivery.configured():
        sys.stderr.write("ingest token or URL missing; bars not posted\n")
        return EXIT_CONFIG
    chunks = [payload["bars"][i : i + 400] for i in range(0, len(payload["bars"]), 400)]
    posted = 0
    try:
        if chunks:
            for index, chunk in enumerate(chunks, start=1):
                part = dict(payload)
                part["bars"] = chunk
                part["batch_id"] = batch_id
                part["chunk_index"] = index
                part["chunk_count"] = len(chunks)
                part["finalize"] = False
                delivery.send_equity_bars(part)
                posted += len(chunk)
        finalize_response = delivery.finalize_equity_bars(
            {
                "collector_id": cfg.collector_id,
                "batch_id": batch_id,
                "coverage": payload["coverage"],
                "request_mode": summary["request_mode"],
                "provider": IBKR_PROVIDER,
                "source_id": EQUITY_SOURCE_ID,
                "what_to_show": WHAT_TO_SHOW_ADJUSTED,
                "adjustment_basis": ADJUSTMENT_BASIS_ADJUSTED_LAST,
            }
        )
    except DeliveryError as exc:
        sys.stderr.write("ingest delivery failed: {0}\n".format(exc))
        return EXIT_CONFIG
    exit_code = exit_code_for_finalize(local_summary=summary, server=finalize_response or {})
    sys.stdout.write(
        json.dumps(
            {
                "posted_bars": posted,
                "chunks": len(chunks),
                "batch_id": batch_id,
                "finalized": True,
                "local_fetch_status": summary.get("overall"),
                "server_run_status": (finalize_response or {}).get("run_status"),
                "server_coverage_status": (finalize_response or {}).get("coverage_status"),
                "promotion_eligible": (finalize_response or {}).get("promotion_eligible"),
                "exit_code": exit_code,
            },
            sort_keys=True,
            default=str,
        )
        + "\n"
    )
    return exit_code
