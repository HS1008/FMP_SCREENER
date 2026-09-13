"""Durable multi-chunk EQUITY_EOD batch protocol.

A multi-chunk IBKR backfill is ONE logical ingestion attempt:

    first chunk -> create batch + freeze expected universe + one run (ATTEMPTED)
    later chunks -> upsert bars and batch observations; run stays ATTEMPTED
    FINALIZE -> server-observed coverage + claim agreement -> SUCCEEDED / PARTIAL / FAILED

Collector coverage is a claim. COMPLETE and snapshot promotion require durable
observations from THIS batch on the latest received date for every frozen
expected symbol. Older canonical mi_market_bars rows cannot satisfy coverage.
"""

from __future__ import annotations

import hashlib
from datetime import date as date_cls
from typing import Any, Mapping

from sqlalchemy import text

from ibkr_ingest.validate import EquityBarIngestRequest
from market_intelligence.nulls import strict_dumps
from market_intelligence.store import (
    RUN_ATTEMPTED,
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_SUCCEEDED,
    TRANSPORT_OK,
    finish_run,
    record_freshness,
    start_run,
    utcnow,
)

BATCH_OPEN = "OPEN"
BATCH_FINALIZED = "FINALIZED"
BATCH_FAILED = "FAILED"
COVERAGE_COMPLETE = "COMPLETE"
COVERAGE_PARTIAL = "PARTIAL"
COVERAGE_EMPTY = "EMPTY"
COVERAGE_MISMATCH = "COVERAGE_MISMATCH"

_ZERO_SUCCESS_OVERALL = frozenset({"FAILED", "EMPTY", "FAILURE"})
_METADATA_FIELDS = (
    "collector_id",
    "provider",
    "source_id",
    "what_to_show",
    "adjustment_basis",
    "request_mode",
)


class BatchProtocolError(ValueError):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def chunk_hash(records: list[Mapping[str, Any]]) -> str:
    payload = strict_dumps(list(records))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def observation_row_hash(raw: Mapping[str, Any], *, provider: str, what_to_show: str, adjustment_basis: str) -> str:
    day = raw["bar_date"]
    if hasattr(day, "isoformat"):
        day = day.isoformat()
    payload = {
        "symbol": str(raw["symbol"]),
        "bar_date": str(day)[:10],
        "adj_close": float(raw["adj_close"]),
        "close": float(raw["close"]),
        "open": raw.get("open"),
        "high": raw.get("high"),
        "low": raw.get("low"),
        "volume": raw.get("volume"),
        "con_id": raw.get("con_id"),
        "provider": provider,
        "what_to_show": what_to_show,
        "adjustment_basis": adjustment_basis,
    }
    return hashlib.sha256(strict_dumps(payload).encode("utf-8")).hexdigest()


def freeze_expected_universe() -> tuple[list[str], str, str]:
    from market_intelligence.taxonomy import TAXONOMY_VERSION, UNIVERSE_SYMBOLS

    symbols = list(UNIVERSE_SYMBOLS)
    digest = hashlib.sha256(strict_dumps(symbols).encode("utf-8")).hexdigest()
    return symbols, digest, TAXONOMY_VERSION


def _as_symbol_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    return []


def coverage_status_of(coverage: Mapping[str, Any] | None) -> tuple[str, float, dict[str, Any]]:
    body = dict(coverage or {})
    requested = body.get("requested")
    successful = body.get("successful")
    failed = body.get("failed")
    if isinstance(successful, list):
        success_n = len(successful)
    else:
        success_n = int(successful or 0)
    if isinstance(failed, dict):
        failed_n = len(failed)
    elif isinstance(failed, list):
        failed_n = len(failed)
    else:
        failed_n = int(failed or 0)
    if requested is None:
        requested_n = success_n + failed_n
    elif isinstance(requested, list):
        requested_n = len(requested)
    else:
        requested_n = int(requested)
    ratio = (success_n / float(requested_n)) if requested_n else 0.0
    if requested_n <= 0 or success_n <= 0:
        status = COVERAGE_EMPTY
    elif failed_n == 0 and success_n == requested_n:
        status = COVERAGE_COMPLETE
    else:
        status = COVERAGE_PARTIAL
    body.setdefault("requested", requested_n)
    body.setdefault("successful_count", success_n)
    body.setdefault("failed_count", failed_n)
    body.setdefault("coverage_ratio", ratio)
    body.setdefault("coverage_status", status)
    return status, ratio, body


def proven_zero_success_failed_attempt(coverage: Mapping[str, Any] | None) -> bool:
    """Unknown-batch FINALIZE is allowed only for an explicitly failed empty attempt."""
    if not coverage:
        return False
    claimed = str(coverage.get("coverage_status") or "").upper()
    if claimed in {COVERAGE_COMPLETE, COVERAGE_PARTIAL}:
        return False
    overall = str(coverage.get("overall") or "").upper()
    if overall not in _ZERO_SUCCESS_OVERALL:
        return False
    raw_ratio = coverage.get("coverage_ratio")
    if raw_ratio is not None:
        try:
            if float(raw_ratio) != 0.0:
                return False
        except (TypeError, ValueError):
            return False
    status, ratio, body = coverage_status_of(coverage)
    requested_raw = body.get("requested")
    if isinstance(requested_raw, list):
        requested_n = len(requested_raw)
    else:
        requested_n = int(requested_raw or 0)
    success_n = int(body.get("successful_count") or 0)
    failed_n = int(body.get("failed_count") or 0)
    if status != COVERAGE_EMPTY:
        return False
    if requested_n <= 0 or success_n != 0 or failed_n != requested_n:
        return False
    if abs(float(ratio)) > 1e-12:
        return False
    return True


def published_complete_snapshot_as_of(conn):
    """Latest sector snapshot date. Snapshots are written only for verified COMPLETE batches."""
    row = conn.execute(
        text(
            """
            SELECT MAX(as_of) FROM mi_sector_snapshots
            WHERE source_id = 'EQUITY_EOD' AND dataset = 'ETF_RS_VS_SPY'
            """
        )
    ).scalar()
    if row is None:
        return None
    if isinstance(row, date_cls):
        return row
    return date_cls.fromisoformat(str(row)[:10])


def latest_open_equity_batch(conn) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT * FROM mi_equity_eod_batches
            WHERE state = :state
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ),
        {"state": BATCH_OPEN},
    ).mappings().first()
    return dict(row) if row is not None else None


def _require_metadata_match(batch: Mapping[str, Any], fields: Mapping[str, Any]) -> None:
    for name in _METADATA_FIELDS:
        if name not in fields:
            continue
        expected = batch[name]
        got = fields[name]
        if expected != got:
            raise BatchProtocolError("{0} does not match batch".format(name))
    if "chunk_count" in fields and fields["chunk_count"] is not None:
        if int(batch["chunk_count"]) != int(fields["chunk_count"]):
            raise BatchProtocolError("chunk_count does not match batch")


def _lock_batch(conn, batch_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT * FROM mi_equity_eod_batches WHERE batch_id = :id FOR UPDATE"),
        {"id": batch_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def _insert_batch_row(conn, values: Mapping[str, Any]) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mi_equity_eod_batches (
                batch_id, collector_id, source_id, provider, what_to_show, adjustment_basis,
                request_mode, chunk_count, state, coverage_json, ingestion_run_id,
                expected_symbols_json, expected_symbols_sha256, universe_version
            ) VALUES (
                :id, :cid, :src, :prov, :wts, :adj, :mode, :n, :state, CAST(:cov AS JSONB), :run,
                CAST(:expected AS JSONB), :esh, :uver
            )
            ON CONFLICT (batch_id) DO NOTHING
            """
        ),
        dict(values),
    )


def _ensure_batch(conn, req: EquityBarIngestRequest) -> dict[str, Any]:
    existing = _lock_batch(conn, req.batch_id)
    if existing is None:
        symbols, digest, universe_version = freeze_expected_universe()
        _insert_batch_row(
            conn,
            {
                "id": req.batch_id,
                "cid": req.collector_id,
                "src": req.source_id,
                "prov": req.provider,
                "wts": req.what_to_show,
                "adj": req.adjustment_basis,
                "mode": req.request_mode,
                "n": req.chunk_count,
                "state": BATCH_OPEN,
                "cov": strict_dumps(req.coverage or {}),
                "run": None,
                "expected": strict_dumps(symbols),
                "esh": digest,
                "uver": universe_version,
            },
        )
        existing = _lock_batch(conn, req.batch_id)
        if existing is None:
            raise BatchProtocolError("failed to create batch", status_code=500)
    if existing["state"] == BATCH_FAILED:
        raise BatchProtocolError("batch {0} is failed".format(req.batch_id))
    if existing["state"] == BATCH_FINALIZED:
        return existing
    _require_metadata_match(
        existing,
        {
            "collector_id": req.collector_id,
            "provider": req.provider,
            "source_id": req.source_id,
            "what_to_show": req.what_to_show,
            "adjustment_basis": req.adjustment_basis,
            "request_mode": req.request_mode,
            "chunk_count": req.chunk_count,
        },
    )
    return existing


def _logical_run_id(conn, batch: Mapping[str, Any], *, source_id: str) -> str:
    existing = batch.get("ingestion_run_id")
    if existing:
        status = conn.execute(text("SELECT status FROM mi_ingestion_runs WHERE run_id = :r"), {"r": existing}).scalar()
        if status in {RUN_ATTEMPTED, RUN_SUCCEEDED, RUN_PARTIAL, RUN_FAILED}:
            return str(existing)
    run_id = start_run(conn, source_id=source_id, dataset="equity_etf_daily_bars")
    conn.execute(
        text("UPDATE mi_equity_eod_batches SET ingestion_run_id = :run, updated_at = NOW() WHERE batch_id = :id AND state = :open"),
        {"run": run_id, "id": batch["batch_id"], "open": BATCH_OPEN},
    )
    return run_id


def _record_observations(conn, req: EquityBarIngestRequest) -> None:
    for raw in req.records:
        day = raw["bar_date"]
        if not isinstance(day, date_cls):
            day = date_cls.fromisoformat(str(day)[:10])
        digest = observation_row_hash(
            raw,
            provider=req.provider,
            what_to_show=req.what_to_show,
            adjustment_basis=req.adjustment_basis,
        )
        inserted = conn.execute(
            text(
                """
                INSERT INTO mi_equity_eod_batch_observations (
                    batch_id, symbol, bar_date, chunk_index, row_hash, con_id,
                    provider, what_to_show, adjustment_basis
                ) VALUES (
                    :id, :sym, :day, :i, :h, :con, :prov, :wts, :adj
                )
                ON CONFLICT (batch_id, symbol, bar_date) DO NOTHING
                RETURNING symbol
                """
            ),
            {
                "id": req.batch_id,
                "sym": str(raw["symbol"]),
                "day": day,
                "i": req.chunk_index,
                "h": digest,
                "con": raw.get("con_id"),
                "prov": req.provider,
                "wts": req.what_to_show,
                "adj": req.adjustment_basis,
            },
        ).fetchone()
        if inserted is not None:
            continue
        prior = conn.execute(
            text(
                """
                SELECT row_hash, provider, what_to_show, adjustment_basis
                FROM mi_equity_eod_batch_observations
                WHERE batch_id = :id AND symbol = :sym AND bar_date = :day
                """
            ),
            {"id": req.batch_id, "sym": str(raw["symbol"]), "day": day},
        ).mappings().one()
        if prior["row_hash"] != digest:
            raise BatchProtocolError("contradictory observation for {0} {1}".format(raw["symbol"], day))
        if prior["provider"] != req.provider or prior["what_to_show"] != req.what_to_show or prior["adjustment_basis"] != req.adjustment_basis:
            raise BatchProtocolError("observation provenance does not match batch")


def record_chunk(conn, req: EquityBarIngestRequest) -> dict[str, Any]:
    digest = chunk_hash(req.records)
    batch = _ensure_batch(conn, req)
    if batch["state"] == BATCH_FINALIZED:
        prior = conn.execute(
            text("SELECT chunk_hash FROM mi_equity_eod_batch_chunks WHERE batch_id=:id AND chunk_index=:i"),
            {"id": req.batch_id, "i": req.chunk_index},
        ).scalar()
        if prior == digest:
            return {
                "duplicate": True,
                "chunk_hash": digest,
                "state": BATCH_FINALIZED,
                "run_id": batch.get("ingestion_run_id"),
            }
        raise BatchProtocolError("batch already finalized")
    inserted = conn.execute(
        text(
            """
            INSERT INTO mi_equity_eod_batch_chunks (batch_id, chunk_index, chunk_count, chunk_hash, bar_count)
            VALUES (:id, :i, :n, :h, :bars)
            ON CONFLICT (batch_id, chunk_index) DO NOTHING
            RETURNING chunk_index
            """
        ),
        {"id": req.batch_id, "i": req.chunk_index, "n": req.chunk_count, "h": digest, "bars": len(req.records)},
    ).fetchone()
    if inserted is None:
        prior = conn.execute(
            text("SELECT chunk_hash FROM mi_equity_eod_batch_chunks WHERE batch_id=:id AND chunk_index=:i"),
            {"id": req.batch_id, "i": req.chunk_index},
        ).scalar()
        if prior != digest:
            raise BatchProtocolError("chunk {0} hash mismatch".format(req.chunk_index))
        return {
            "duplicate": True,
            "chunk_hash": digest,
            "state": batch["state"],
            "run_id": batch.get("ingestion_run_id"),
        }
    conn.execute(
        text(
            """
            UPDATE mi_equity_eod_batches
            SET received_chunks = received_chunks + 1,
                bars_received = bars_received + :bars,
                coverage_json = CASE WHEN CAST(:cov AS TEXT) = '{}' THEN coverage_json ELSE CAST(:cov AS JSONB) END,
                updated_at = NOW()
            WHERE batch_id = :id AND state = :open
            """
        ),
        {"id": req.batch_id, "bars": len(req.records), "cov": strict_dumps(req.coverage or {}), "open": BATCH_OPEN},
    )
    return {
        "duplicate": False,
        "chunk_hash": digest,
        "state": BATCH_OPEN,
        "run_id": batch.get("ingestion_run_id"),
    }


def _refuse_provider_mix(conn, *, source_id: str, provider: str) -> None:
    existing = conn.execute(
        text(
            """
            SELECT DISTINCT provider FROM mi_market_bars
            WHERE source_id = :s AND provider IS NOT NULL AND provider <> :p
            LIMIT 1
            """
        ),
        {"s": source_id, "p": provider},
    ).scalar()
    if existing:
        raise BatchProtocolError("refusing to mix provider {0} with existing {1}".format(provider, existing), status_code=400)


def stage_equity_bars(conn, req: EquityBarIngestRequest) -> dict[str, Any]:
    from market_intelligence.equity_eod import EquityBar, upsert_bars

    _refuse_provider_mix(conn, source_id=req.source_id, provider=req.provider)
    chunk_meta = record_chunk(conn, req)
    if chunk_meta["state"] != BATCH_FINALIZED:
        _record_observations(conn, req)
    batch = _lock_batch(conn, req.batch_id)
    if batch is None:
        raise BatchProtocolError("unknown batch_id", status_code=400)
    run_id = _logical_run_id(conn, batch, source_id=req.source_id)
    bars: list[EquityBar] = []
    for raw in req.records:
        day = raw["bar_date"]
        if not isinstance(day, date_cls):
            day = date_cls.fromisoformat(str(day)[:10])
        bars.append(
            EquityBar(
                instrument_id=str(raw["symbol"]),
                bar_date=day,
                adj_close=float(raw["adj_close"]),
                close=float(raw["close"]),
                open_price=raw.get("open"),
                high=raw.get("high"),
                low=raw.get("low"),
                volume=raw.get("volume"),
                adjustment_basis=req.adjustment_basis,
                provider_symbol=str(raw.get("provider_symbol") or raw["symbol"]),
                source_id=req.source_id,
                con_id=raw.get("con_id"),
                provider=req.provider,
            )
        )
    retrieved = utcnow()
    counts = upsert_bars(conn, bars, run_id=run_id, retrieved_at=retrieved, provider=req.provider)
    return {
        "received": len(bars),
        "inserted": counts.get("inserted", 0),
        "unchanged": counts.get("unchanged", 0),
        "snapshots": 0,
        "finalized": False,
        "batch_id": req.batch_id,
        "chunk_index": req.chunk_index,
        "chunk_count": req.chunk_count,
        "chunk_hash": chunk_meta["chunk_hash"],
        "duplicate_chunk": chunk_meta["duplicate"],
        "run_id": run_id,
        "run_status": RUN_ATTEMPTED,
        "latest_observation": max((b.bar_date for b in bars), default=None),
    }


def _expected_symbols_of(batch: Mapping[str, Any]) -> list[str]:
    stored = batch.get("expected_symbols_json")
    if isinstance(stored, list) and stored:
        return [str(item) for item in stored]
    if isinstance(stored, str) and stored:
        import json

        parsed = json.loads(stored)
        if isinstance(parsed, list) and parsed:
            return [str(item) for item in parsed]
    symbols, _, _ = freeze_expected_universe()
    return symbols


def reconcile_batch_coverage(conn, batch: Mapping[str, Any], claimed_coverage: Mapping[str, Any] | None) -> dict[str, Any]:
    """Derive COMPLETE from persisted batch observations, not from canonical history."""
    expected = _expected_symbols_of(batch)
    expected_set = set(expected)
    rows = conn.execute(
        text(
            """
            SELECT symbol, bar_date FROM mi_equity_eod_batch_observations
            WHERE batch_id = :id
            """
        ),
        {"id": batch["batch_id"]},
    ).all()
    observed_pairs: list[tuple[str, date_cls]] = []
    for symbol, day in rows:
        if not isinstance(day, date_cls):
            day = date_cls.fromisoformat(str(day)[:10])
        observed_pairs.append((str(symbol), day))
    observed_symbols = sorted({symbol for symbol, _day in observed_pairs})
    latest = max((day for _symbol, day in observed_pairs), default=None)
    present_on_latest = sorted({symbol for symbol, day in observed_pairs if latest is not None and day == latest and symbol in expected_set})
    missing_on_latest = [symbol for symbol in expected if symbol not in set(present_on_latest)]
    missing_symbols = [symbol for symbol in expected if symbol not in set(observed_symbols)]
    unexpected_symbols = [symbol for symbol in observed_symbols if symbol not in expected_set]
    observed_latest_date_count = len(present_on_latest)
    expected_count = len(expected)
    observed_ratio = (observed_latest_date_count / float(expected_count)) if expected_count else 0.0
    if expected_count <= 0 or latest is None or observed_latest_date_count <= 0:
        server_status = COVERAGE_EMPTY
    elif observed_latest_date_count == expected_count:
        server_status = COVERAGE_COMPLETE
    else:
        server_status = COVERAGE_PARTIAL
    claimed_status, claimed_ratio, claimed_body = coverage_status_of(claimed_coverage)
    claimed_successful = _as_symbol_list((claimed_coverage or {}).get("successful_symbols"))
    if not claimed_successful:
        claimed_successful = _as_symbol_list((claimed_coverage or {}).get("successful"))
    claim_complete = claimed_status == COVERAGE_COMPLETE
    server_complete = server_status == COVERAGE_COMPLETE
    claim_lists_expected = (not claimed_successful) or (set(claimed_successful) == expected_set)
    promotion_eligible = bool(server_complete and claim_complete and claim_lists_expected and int(claimed_body.get("failed_count") or 0) == 0)
    reasons: list[str] = []
    if not server_complete:
        reasons.append("server_observed_incomplete_on_latest_date")
        if missing_on_latest:
            reasons.append("missing_on_latest_date:" + ",".join(missing_on_latest[:8]))
    if claim_complete and not server_complete:
        reasons.append("collector_claim_complete_disagrees_with_batch_evidence")
    if server_complete and not claim_complete:
        reasons.append("collector_claim_is_not_complete")
    if claim_complete and claimed_successful and set(claimed_successful) != expected_set:
        reasons.append("claimed_successful_set_does_not_match_frozen_universe")
    mismatch = (claim_complete != server_complete) or (claim_complete and not promotion_eligible)
    return {
        "expected_symbols": expected,
        "expected_count": expected_count,
        "observed_symbols": observed_symbols,
        "observed_count": len(observed_symbols),
        "missing_symbols": missing_symbols,
        "unexpected_symbols": unexpected_symbols,
        "latest_observed_date_any_symbol": latest.isoformat() if latest else None,
        "symbols_present_on_latest_observed_date": present_on_latest,
        "missing_on_latest_observed_date": missing_on_latest,
        "observed_latest_date_count": observed_latest_date_count,
        "observed_coverage_ratio": observed_ratio,
        "claimed_coverage": claimed_body,
        "claimed_coverage_status": claimed_status,
        "claimed_coverage_ratio": claimed_ratio,
        "claim_vs_observed_match": (not mismatch) and (claimed_status == server_status or (claimed_status == COVERAGE_EMPTY and server_status == COVERAGE_EMPTY)),
        "server_coverage_status": server_status,
        "promotion_eligible": promotion_eligible,
        "reasons": reasons,
        "universe_version": batch.get("universe_version"),
        "expected_symbols_sha256": batch.get("expected_symbols_sha256"),
    }


def _create_zero_success_failed_batch(
    conn,
    *,
    batch_id: str,
    collector_id: str,
    coverage: Mapping[str, Any],
    request_mode: str,
    provider: str,
    source_id: str,
    what_to_show: str,
    adjustment_basis: str,
) -> dict[str, Any]:
    run_id = start_run(conn, source_id=source_id, dataset="equity_etf_daily_bars")
    symbols, digest, universe_version = freeze_expected_universe()
    _insert_batch_row(
        conn,
        {
            "id": batch_id,
            "cid": collector_id,
            "src": source_id,
            "prov": provider,
            "wts": what_to_show,
            "adj": adjustment_basis,
            "mode": request_mode,
            "n": 0,
            "state": BATCH_OPEN,
            "cov": strict_dumps(dict(coverage)),
            "run": run_id,
            "expected": strict_dumps(symbols),
            "esh": digest,
            "uver": universe_version,
        },
    )
    batch = _lock_batch(conn, batch_id)
    if batch is None:
        raise BatchProtocolError("failed to create batch", status_code=500)
    return batch


def _duplicate_finalize_payload(batch: Mapping[str, Any], batch_id: str) -> dict[str, Any]:
    coverage = batch.get("coverage_status")
    state = batch["state"]
    if state == BATCH_FAILED or coverage in {COVERAGE_EMPTY, None} and state != BATCH_FINALIZED:
        run_status = RUN_FAILED
        ok = False
        state = BATCH_FAILED
        coverage = coverage or COVERAGE_EMPTY
    elif coverage == COVERAGE_COMPLETE:
        run_status = RUN_SUCCEEDED
        ok = True
        state = BATCH_FINALIZED
    elif coverage == COVERAGE_MISMATCH:
        run_status = RUN_PARTIAL
        ok = True
        state = BATCH_FINALIZED
    else:
        run_status = RUN_PARTIAL
        ok = True
        state = BATCH_FINALIZED
        coverage = coverage or COVERAGE_PARTIAL
    return {
        "ok": ok,
        "finalized": True,
        "duplicate": True,
        "batch_id": batch_id,
        "state": state,
        "snapshots": 0,
        "coverage_status": coverage,
        "run_id": batch.get("finalized_run_id") or batch.get("ingestion_run_id"),
        "run_status": run_status,
        "latest_observation": None,
    }


def finalize_equity_eod_batch(
    conn,
    *,
    batch_id: str,
    collector_id: str,
    coverage: Mapping[str, Any] | None,
    request_mode: str,
    provider: str,
    source_id: str,
    what_to_show: str,
    adjustment_basis: str,
) -> dict[str, Any]:
    from market_intelligence.equity_eod import EQUITY_SOURCE_ID, load_adj_closes, write_snapshots
    from market_intelligence.store import upsert_source_registry

    batch = _lock_batch(conn, batch_id)
    if batch is None:
        if not proven_zero_success_failed_attempt(coverage):
            raise BatchProtocolError("unknown batch_id", status_code=400)
        batch = _create_zero_success_failed_batch(
            conn,
            batch_id=batch_id,
            collector_id=collector_id,
            coverage=coverage or {},
            request_mode=request_mode,
            provider=provider,
            source_id=source_id,
            what_to_show=what_to_show,
            adjustment_basis=adjustment_basis,
        )
    else:
        _require_metadata_match(
            batch,
            {
                "collector_id": collector_id,
                "provider": provider,
                "source_id": source_id,
                "what_to_show": what_to_show,
                "adjustment_basis": adjustment_basis,
                "request_mode": request_mode,
            },
        )
    if batch["state"] in {BATCH_FINALIZED, BATCH_FAILED}:
        return _duplicate_finalize_payload(batch, batch_id)
    expected_chunks = int(batch["chunk_count"] or 0)
    received = int(batch["received_chunks"] or 0)
    if expected_chunks > 0 and received < expected_chunks:
        raise BatchProtocolError("finalize before all chunks: {0}/{1}".format(received, expected_chunks))
    if expected_chunks > 0:
        indexes = {int(r[0]) for r in conn.execute(text("SELECT chunk_index FROM mi_equity_eod_batch_chunks WHERE batch_id = :id"), {"id": batch_id})}
        missing = [i for i in range(1, expected_chunks + 1) if i not in indexes]
        if missing:
            raise BatchProtocolError("missing chunks: {0}".format(missing))
    observed = reconcile_batch_coverage(conn, batch, coverage)
    claimed_status = observed["claimed_coverage_status"]
    server_status = observed["server_coverage_status"]
    promotion_eligible = bool(observed["promotion_eligible"])
    verified_as_of = None
    if observed["latest_observed_date_any_symbol"]:
        verified_as_of = date_cls.fromisoformat(observed["latest_observed_date_any_symbol"])
    published_as_of = published_complete_snapshot_as_of(conn)
    cov_body = {
        "claimed_coverage": observed["claimed_coverage"],
        "server_observed_coverage": {k: observed[k] for k in observed if k != "claimed_coverage"},
        "request_mode": request_mode,
        "provider": provider,
        "what_to_show": what_to_show,
        "adjustment_basis": adjustment_basis,
        "source_id": source_id,
        "collector_id": collector_id,
        "batch_id": batch_id,
        "expected_symbols_sha256": observed["expected_symbols_sha256"],
        "universe_version": observed["universe_version"],
    }
    upsert_source_registry(
        conn,
        [
            {
                "source_id": EQUITY_SOURCE_ID,
                "provider": provider,
                "dataset": "equity_etf_daily_bars",
                "source_url": "",
                "expected_cadence": "D",
                "usage_scope": "INTERNAL_ONLY",
                "attribution": "Interactive Brokers ADJUSTED_LAST daily bars via Windows collector.",
                "terms_notes": "INTERNAL_ONLY. Remote AI export is not authorized. TWS stays on the collector host.",
                "units_metadata": {"price": "IBKR_ADJUSTED_LAST"},
            }
        ],
        enabled={EQUITY_SOURCE_ID: True},
        access={EQUITY_SOURCE_ID: "CONFIGURED"},
    )
    run_id = _logical_run_id(conn, batch, source_id=EQUITY_SOURCE_ID)
    snapshots = 0
    if promotion_eligible and verified_as_of is not None:
        expected_symbols = observed["expected_symbols"]
        prices = load_adj_closes(conn, expected_symbols, source_id=EQUITY_SOURCE_ID, provider=provider)
        snapshots = write_snapshots(
            conn,
            as_of=verified_as_of,
            prices=prices,
            source_id=EQUITY_SOURCE_ID,
            run_id=run_id,
            adjustment_basis=adjustment_basis,
            provider=provider,
        )
        freshness_as_of = verified_as_of
        transport = TRANSPORT_OK
        run_status = RUN_SUCCEEDED
        metadata = "VALIDATED"
        batch_state = BATCH_FINALIZED
        coverage_status = COVERAGE_COMPLETE
        ratio = 1.0
    elif server_status == COVERAGE_EMPTY and claimed_status == COVERAGE_EMPTY:
        freshness_as_of = published_as_of
        transport = "FAILED"
        run_status = RUN_FAILED
        metadata = "INCOMPLETE"
        batch_state = BATCH_FAILED
        coverage_status = COVERAGE_EMPTY
        ratio = 0.0
    elif not observed["claim_vs_observed_match"] or (claimed_status == COVERAGE_COMPLETE and not promotion_eligible) or (server_status == COVERAGE_COMPLETE and claimed_status != COVERAGE_COMPLETE):
        freshness_as_of = published_as_of
        transport = TRANSPORT_OK if server_status != COVERAGE_EMPTY else "FAILED"
        run_status = RUN_FAILED if server_status == COVERAGE_EMPTY else RUN_PARTIAL
        metadata = COVERAGE_MISMATCH
        batch_state = BATCH_FAILED if server_status == COVERAGE_EMPTY else BATCH_FINALIZED
        coverage_status = COVERAGE_MISMATCH
        ratio = float(observed["observed_coverage_ratio"])
    else:
        freshness_as_of = published_as_of
        transport = TRANSPORT_OK
        run_status = RUN_PARTIAL
        metadata = "PARTIAL_COVERAGE"
        batch_state = BATCH_FINALIZED
        coverage_status = COVERAGE_PARTIAL
        ratio = float(observed["observed_coverage_ratio"])
    cov_body["logical_ingestion_status"] = run_status
    cov_body["snapshot_promotion"] = "published" if snapshots else "blocked"
    cov_body["coverage_status"] = coverage_status
    cov_body["promotion_eligible"] = promotion_eligible
    record_freshness(
        conn,
        source_id=EQUITY_SOURCE_ID,
        dataset="equity_etf_daily_bars",
        cadence="D",
        transport_status=transport,
        latest_observation=freshness_as_of,
        success=promotion_eligible,
        error_redacted=None if promotion_eligible else ("; ".join(observed["reasons"]) or "universe coverage incomplete"),
        run_id=run_id,
        latest_observation_retrieved_at=utcnow() if promotion_eligible else None,
        metadata_status=metadata,
        series_id="SPY",
        coverage_status=coverage_status,
        coverage_ratio=ratio,
        coverage_json=cov_body,
    )
    finish_run(
        conn,
        run_id,
        status=run_status,
        counts={"received": int(batch["bars_received"] or 0)},
        details={
            "collector_id": collector_id,
            "batch_id": batch_id,
            "snapshots": snapshots,
            "coverage_status": coverage_status,
            "server_coverage_status": server_status,
            "finalized": True,
            "logical_batch": True,
            "promotion_eligible": promotion_eligible,
        },
    )
    conn.execute(
        text(
            """
            UPDATE mi_equity_eod_batches
            SET state = :state,
                coverage_json = CAST(:cov AS JSONB),
                coverage_ratio = :ratio,
                coverage_status = :cstatus,
                finalized_run_id = :run,
                finalized_at = NOW(),
                updated_at = NOW()
            WHERE batch_id = :id AND state = :open
            """
        ),
        {
            "state": batch_state,
            "cov": strict_dumps(cov_body),
            "ratio": ratio,
            "cstatus": coverage_status,
            "run": run_id,
            "id": batch_id,
            "open": BATCH_OPEN,
        },
    )
    return {
        "ok": promotion_eligible or (run_status == RUN_PARTIAL),
        "finalized": True,
        "duplicate": False,
        "batch_id": batch_id,
        "state": batch_state,
        "snapshots": snapshots,
        "coverage_status": coverage_status,
        "coverage_ratio": ratio,
        "run_id": run_id,
        "run_status": run_status,
        "latest_observation": (verified_as_of.isoformat() if promotion_eligible and verified_as_of else (published_as_of.isoformat() if published_as_of else None)),
        "received": int(batch["bars_received"] or 0),
        "inserted": 0,
        "unchanged": 0,
        "promotion_eligible": promotion_eligible,
        "missing_on_latest_observed_date": observed["missing_on_latest_observed_date"],
    }
