"""Windows-local fetch-eod command. Read-only historical bars, no orders.

Exit codes:
  0  FULL_SUCCESS — every requested symbol returned valid bars
  1  PARTIAL_SUCCESS — some requested symbols failed; successful bars may still be posted
  2  FAILED — TWS down or zero successful symbols
  3  config/delivery error
  4  eod.lock contention
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any

from ibkr_collector import DEFAULT_EOD_CLIENT_ID, DEFAULT_TWS_HOST, DEFAULT_TWS_PORT
from ibkr_collector.config import load_config
from ibkr_collector.delivery import DeliveryError, IngestClient
from ibkr_collector.historical import (
    ADJUSTMENT_BASIS_ADJUSTED_LAST,
    BACKFILL_DURATION,
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_LOCK,
    INCREMENTAL_DURATION,
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
    if backfill:
        return "backfill"
    if symbols == list(PHASE3_SYMBOLS):
        return "smoke"
    if symbols == list(UNIVERSE_SYMBOLS):
        return "full"
    return "incremental"


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
    session, client, error = connect_historical_session(host=host, port=port, client_id=client_id)
    if session is None:
        sys.stderr.write("TWS unavailable: {0}\n".format(error))
        return EXIT_FAILED
    try:
        results = fetch_universe(session, symbols, duration=duration)
    finally:
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
        delivery.finalize_equity_bars(
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
    sys.stdout.write(json.dumps({"posted_bars": posted, "chunks": len(chunks), "batch_id": batch_id, "finalized": True}, sort_keys=True) + "\n")
    return coverage_exit
