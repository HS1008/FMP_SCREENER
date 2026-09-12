"""Durable multi-chunk EQUITY_EOD batch protocol.

Chunks may upsert bars immediately. Snapshots and successful freshness are
written only on FINALIZE after every expected chunk has arrived. Retries of
the same (batch_id, chunk_index, chunk_hash) are idempotent.
"""

from __future__ import annotations

import hashlib
from datetime import date as date_cls
from typing import Any, Mapping

from sqlalchemy import text

from ibkr_ingest.validate import EquityBarIngestRequest
from market_intelligence.nulls import strict_dumps
from market_intelligence.store import RUN_SUCCEEDED, TRANSPORT_OK, finish_run, record_freshness, start_run, utcnow

BATCH_OPEN = "OPEN"
BATCH_FINALIZED = "FINALIZED"
BATCH_FAILED = "FAILED"
COVERAGE_COMPLETE = "COMPLETE"
COVERAGE_PARTIAL = "PARTIAL"
COVERAGE_EMPTY = "EMPTY"


class BatchProtocolError(ValueError):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def chunk_hash(records: list[Mapping[str, Any]]) -> str:
    payload = strict_dumps(list(records))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def _ensure_batch(conn, req: EquityBarIngestRequest) -> dict[str, Any]:
    existing = conn.execute(
        text("SELECT * FROM mi_equity_eod_batches WHERE batch_id = :id"),
        {"id": req.batch_id},
    ).mappings().first()
    if existing is None:
        conn.execute(
            text(
                """
                INSERT INTO mi_equity_eod_batches (
                    batch_id, collector_id, source_id, provider, what_to_show, adjustment_basis,
                    request_mode, chunk_count, state, coverage_json
                ) VALUES (
                    :id, :cid, :src, :prov, :wts, :adj, :mode, :n, :state, CAST(:cov AS JSONB)
                )
                """
            ),
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
            },
        )
        return conn.execute(text("SELECT * FROM mi_equity_eod_batches WHERE batch_id = :id"), {"id": req.batch_id}).mappings().one()
    if existing["state"] == BATCH_FAILED:
        raise BatchProtocolError("batch {0} is failed".format(req.batch_id))
    if existing["collector_id"] != req.collector_id:
        raise BatchProtocolError("collector_id does not match batch")
    if int(existing["chunk_count"]) != int(req.chunk_count):
        raise BatchProtocolError("chunk_count does not match batch")
    if existing["provider"] != req.provider or existing["source_id"] != req.source_id:
        raise BatchProtocolError("provider/source_id does not match batch")
    if existing["what_to_show"] != req.what_to_show or existing["adjustment_basis"] != req.adjustment_basis:
        raise BatchProtocolError("adjustment provenance does not match batch")
    return existing


def record_chunk(conn, req: EquityBarIngestRequest) -> dict[str, Any]:
    digest = chunk_hash(req.records)
    batch = _ensure_batch(conn, req)
    if batch["state"] == BATCH_FINALIZED:
        prior = conn.execute(
            text("SELECT chunk_hash FROM mi_equity_eod_batch_chunks WHERE batch_id=:id AND chunk_index=:i"),
            {"id": req.batch_id, "i": req.chunk_index},
        ).scalar()
        if prior == digest:
            return {"duplicate": True, "chunk_hash": digest, "state": BATCH_FINALIZED}
        raise BatchProtocolError("batch already finalized")
    prior = conn.execute(
        text("SELECT chunk_hash, bar_count FROM mi_equity_eod_batch_chunks WHERE batch_id=:id AND chunk_index=:i"),
        {"id": req.batch_id, "i": req.chunk_index},
    ).mappings().first()
    if prior is not None:
        if prior["chunk_hash"] != digest:
            raise BatchProtocolError("chunk {0} hash mismatch".format(req.chunk_index))
        return {"duplicate": True, "chunk_hash": digest, "state": batch["state"]}
    conn.execute(
        text(
            """
            INSERT INTO mi_equity_eod_batch_chunks (batch_id, chunk_index, chunk_count, chunk_hash, bar_count)
            VALUES (:id, :i, :n, :h, :bars)
            """
        ),
        {"id": req.batch_id, "i": req.chunk_index, "n": req.chunk_count, "h": digest, "bars": len(req.records)},
    )
    conn.execute(
        text(
            """
            UPDATE mi_equity_eod_batches
            SET received_chunks = received_chunks + 1,
                bars_received = bars_received + :bars,
                coverage_json = CASE WHEN CAST(:cov AS TEXT) = '{}' THEN coverage_json ELSE CAST(:cov AS JSONB) END,
                updated_at = NOW()
            WHERE batch_id = :id
            """
        ),
        {"id": req.batch_id, "bars": len(req.records), "cov": strict_dumps(req.coverage or {})},
    )
    return {"duplicate": False, "chunk_hash": digest, "state": BATCH_OPEN}


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
    run_id = start_run(conn, source_id=req.source_id, dataset="equity_etf_daily_bars")
    counts = upsert_bars(conn, bars, run_id=run_id, retrieved_at=retrieved, provider=req.provider)
    conn.execute(
        text("UPDATE mi_equity_eod_batches SET ingestion_run_id = COALESCE(ingestion_run_id, :run), updated_at = NOW() WHERE batch_id = :id"),
        {"run": run_id, "id": req.batch_id},
    )
    finish_run(
        conn,
        run_id,
        status=RUN_SUCCEEDED,
        counts=counts,
        details={"collector_id": req.collector_id, "batch_id": req.batch_id, "chunk_index": req.chunk_index, "chunk_count": req.chunk_count, "staged": True},
    )
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
        "latest_observation": max((b.bar_date for b in bars), default=None),
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
    from market_intelligence.taxonomy import UNIVERSE_SYMBOLS

    batch = conn.execute(text("SELECT * FROM mi_equity_eod_batches WHERE batch_id = :id"), {"id": batch_id}).mappings().first()
    if batch is None:
        if not coverage:
            raise BatchProtocolError("unknown batch_id", status_code=400)
        conn.execute(
            text(
                """
                INSERT INTO mi_equity_eod_batches (
                    batch_id, collector_id, source_id, provider, what_to_show, adjustment_basis,
                    request_mode, chunk_count, state, coverage_json
                ) VALUES (
                    :id, :cid, :src, :prov, :wts, :adj, :mode, 0, :state, CAST(:cov AS JSONB)
                )
                """
            ),
            {
                "id": batch_id,
                "cid": collector_id,
                "src": source_id,
                "prov": provider,
                "wts": what_to_show,
                "adj": adjustment_basis,
                "mode": request_mode,
                "state": BATCH_OPEN,
                "cov": strict_dumps(dict(coverage or {})),
            },
        )
        batch = conn.execute(text("SELECT * FROM mi_equity_eod_batches WHERE batch_id = :id"), {"id": batch_id}).mappings().one()
    if batch["collector_id"] != collector_id:
        raise BatchProtocolError("collector_id does not match batch")
    if batch["state"] == BATCH_FINALIZED:
        return {
            "ok": True,
            "finalized": True,
            "duplicate": True,
            "batch_id": batch_id,
            "snapshots": 0,
            "coverage_status": batch["coverage_status"],
            "run_id": batch["finalized_run_id"],
            "latest_observation": None,
        }
    expected = int(batch["chunk_count"] or 0)
    received = int(batch["received_chunks"] or 0)
    if expected > 0 and received < expected:
        raise BatchProtocolError("finalize before all chunks: {0}/{1}".format(received, expected))
    if expected > 0:
        indexes = {int(r[0]) for r in conn.execute(text("SELECT chunk_index FROM mi_equity_eod_batch_chunks WHERE batch_id = :id"), {"id": batch_id})}
        missing = [i for i in range(1, expected + 1) if i not in indexes]
        if missing:
            raise BatchProtocolError("missing chunks: {0}".format(missing))
    cov_status, ratio, cov_body = coverage_status_of(coverage or batch["coverage_json"] or {})
    cov_body["request_mode"] = request_mode
    cov_body["provider"] = provider
    cov_body["what_to_show"] = what_to_show
    cov_body["adjustment_basis"] = adjustment_basis
    cov_body["batch_id"] = batch_id
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
    run_id = start_run(conn, source_id=EQUITY_SOURCE_ID, dataset="equity_etf_daily_bars")
    prices = load_adj_closes(conn, list(UNIVERSE_SYMBOLS), source_id=EQUITY_SOURCE_ID, provider=provider)
    as_of = None
    for series in prices.values():
        if series:
            last = max(series)
            if as_of is None or last > as_of:
                as_of = last
    snapshots = 0
    if as_of is not None and cov_status != COVERAGE_EMPTY:
        snapshots = write_snapshots(
            conn,
            as_of=as_of,
            prices=prices,
            source_id=EQUITY_SOURCE_ID,
            run_id=run_id,
            adjustment_basis=adjustment_basis,
            provider=provider,
        )
    complete = cov_status == COVERAGE_COMPLETE
    metadata = "VALIDATED" if complete else ("PARTIAL_COVERAGE" if cov_status == COVERAGE_PARTIAL else "INCOMPLETE")
    record_freshness(
        conn,
        source_id=EQUITY_SOURCE_ID,
        dataset="equity_etf_daily_bars",
        cadence="D",
        transport_status=TRANSPORT_OK if as_of is not None else "FAILED",
        latest_observation=as_of,
        success=complete,
        error_redacted=None if complete else "universe coverage incomplete",
        run_id=run_id,
        latest_observation_retrieved_at=utcnow() if as_of is not None else None,
        metadata_status=metadata,
        series_id="SPY",
        coverage_status=cov_status,
        coverage_ratio=ratio,
        coverage_json=cov_body,
    )
    finish_run(
        conn,
        run_id,
        status=RUN_SUCCEEDED,
        details={"collector_id": collector_id, "batch_id": batch_id, "snapshots": snapshots, "coverage_status": cov_status, "finalized": True},
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
                updated_at = NOW(),
                request_mode = :mode
            WHERE batch_id = :id
            """
        ),
        {
            "state": BATCH_FINALIZED,
            "cov": strict_dumps(cov_body),
            "ratio": ratio,
            "cstatus": cov_status,
            "run": run_id,
            "mode": request_mode,
            "id": batch_id,
        },
    )
    return {
        "ok": True,
        "finalized": True,
        "duplicate": False,
        "batch_id": batch_id,
        "snapshots": snapshots,
        "coverage_status": cov_status,
        "coverage_ratio": ratio,
        "run_id": run_id,
        "latest_observation": as_of.isoformat() if as_of else None,
        "received": int(batch["bars_received"] or 0),
        "inserted": 0,
        "unchanged": 0,
    }
