"""Adversarial PostgreSQL regressions for the IBKR EQUITY_EOD batch protocol.

No TWS. No orders. Collector HTTP + CollectorStoreAdapter refresh only.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from ibkr_ingest.validate import PayloadError, validate_equity_bar_batch
from market_intelligence.equity_eod import CollectorStoreAdapter, ingest_equity_eod
from market_intelligence.equity_eod_batch import (
    BatchProtocolError,
    finalize_equity_eod_batch,
    stage_equity_bars,
)
from market_intelligence.taxonomy import UNIVERSE_SYMBOLS
from ibkr_ingest.app import app, set_engine_for_tests
from tests.test_ibkr_ingest import _eod_bar, _eod_batch

T0 = "2026-09-08"
T1 = "2026-09-11"
UNIVERSE = list(UNIVERSE_SYMBOLS)


@pytest.fixture
def ingest_client(mi_db, monkeypatch):
    from fastapi.testclient import TestClient

    set_engine_for_tests(mi_db)
    monkeypatch.setenv("IBKR_INGEST_TOKEN", "test-ingest-token")
    monkeypatch.setenv("IBKR_INGEST_DATABASE_URL", "postgresql://unused")
    client = TestClient(app)
    yield client
    set_engine_for_tests(None)


def _headers():
    return {"Authorization": "Bearer test-ingest-token"}


def _coverage_complete(symbols=None):
    symbols = list(symbols or UNIVERSE)
    return {
        "requested": symbols,
        "successful": symbols,
        "failed": {},
        "coverage_ratio": 1.0,
        "overall": "COMPLETE",
        "coverage_status": "COMPLETE",
    }


def _coverage_partial(missing="NVDA"):
    ok = [s for s in UNIVERSE if s != missing]
    return {
        "requested": list(UNIVERSE),
        "successful": ok,
        "failed": {missing: "timeout"},
        "coverage_ratio": len(ok) / float(len(UNIVERSE)),
        "overall": "PARTIAL_SUCCESS",
        "coverage_status": "PARTIAL",
    }


def _coverage_failed():
    return {
        "requested": list(UNIVERSE),
        "successful": [],
        "failed": {s: "timeout" for s in UNIVERSE},
        "coverage_ratio": 0.0,
        "overall": "FAILED",
        "coverage_status": "EMPTY",
    }


def _finalize_body(batch_id, coverage, **extra):
    body = {
        "collector_id": extra.pop("collector_id", "harin-laptop"),
        "batch_id": batch_id,
        "coverage": coverage,
        "request_mode": extra.pop("request_mode", "full"),
        "provider": extra.pop("provider", "IBKR"),
        "source_id": extra.pop("source_id", "EQUITY_EOD"),
        "what_to_show": extra.pop("what_to_show", "ADJUSTED_LAST"),
        "adjustment_basis": extra.pop("adjustment_basis", "IBKR_ADJUSTED_LAST"),
    }
    body.update(extra)
    return body


def _universe_bars(day, close=100.0):
    return [_eod_bar(symbol=sym, day=day, close=close + (i * 0.01), con_id=1000 + i) for i, sym in enumerate(UNIVERSE)]


def _stage(engine, bars, *, batch_id, chunk_index, chunk_count, coverage=None, request_mode="full", finalize=False):
    req = validate_equity_bar_batch(
        _eod_batch(
            bars,
            batch_id=batch_id,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            coverage=coverage or {},
            finalize=finalize,
            request_mode=request_mode,
        )
    )
    with engine.begin() as conn:
        return stage_equity_bars(conn, req)


def _finalize(engine, batch_id, coverage, **extra):
    with engine.begin() as conn:
        return finalize_equity_eod_batch(conn, **_finalize_body(batch_id, coverage, **extra))


def _snapshot_as_ofs(engine):
    with engine.connect() as conn:
        return [
            row[0]
            for row in conn.execute(
                text("SELECT DISTINCT as_of FROM mi_sector_snapshots WHERE source_id='EQUITY_EOD' AND dataset='ETF_RS_VS_SPY' ORDER BY as_of")
            )
        ]


def _run_statuses(engine, batch_id):
    with engine.connect() as conn:
        run_id = conn.execute(text("SELECT ingestion_run_id FROM mi_equity_eod_batches WHERE batch_id=:id"), {"id": batch_id}).scalar()
        statuses = [row[0] for row in conn.execute(text("SELECT status FROM mi_ingestion_runs WHERE source_id='EQUITY_EOD' AND dataset='equity_etf_daily_bars' ORDER BY started_at"))]
        one = conn.execute(text("SELECT status FROM mi_ingestion_runs WHERE run_id=:r"), {"r": run_id}).scalar() if run_id else None
    return run_id, one, statuses


def test_full_universe_complete_publishes_succeeded_run(ingest_client, mi_db):
    batch_id = str(uuid.uuid4())
    body = _eod_batch(
        _universe_bars(T0),
        batch_id=batch_id,
        chunk_index=1,
        chunk_count=1,
        coverage=_coverage_complete(),
        request_mode="full",
        finalize=True,
    )
    r = ingest_client.post("/v1/equity_bars", headers=_headers(), json=body)
    assert r.status_code == 200, r.text
    assert r.json()["coverage_status"] == "COMPLETE"
    assert r.json()["snapshots"] > 0
    with mi_db.connect() as conn:
        snaps = conn.execute(text("SELECT COUNT(*) FROM mi_sector_snapshots WHERE source_id='EQUITY_EOD' AND as_of=:d"), {"d": T0}).scalar()
        run_status = conn.execute(text("SELECT status FROM mi_ingestion_runs WHERE run_id=:r"), {"r": r.json()["run_id"]}).scalar()
        fresh = conn.execute(text("SELECT transport_status, coverage_status, metadata_status FROM mi_data_freshness WHERE source_id='EQUITY_EOD' AND dataset='equity_etf_daily_bars'")).mappings().one()
    assert snaps > 0
    assert run_status == "SUCCEEDED"
    assert fresh["transport_status"] == "OK"
    assert fresh["coverage_status"] == "COMPLETE"
    assert fresh["metadata_status"] == "VALIDATED"


def test_partial_45_of_46_does_not_publish_or_overwrite(ingest_client, mi_db):
    first_id = str(uuid.uuid4())
    complete = ingest_client.post(
        "/v1/equity_bars",
        headers=_headers(),
        json=_eod_batch(_universe_bars(T0, 200.0), batch_id=first_id, chunk_index=1, chunk_count=1, coverage=_coverage_complete(), finalize=True),
    )
    assert complete.status_code == 200 and complete.json()["snapshots"] > 0
    t0_snaps = _snapshot_as_ofs(mi_db)
    assert t0_snaps == [date(2026, 9, 8)]
    with mi_db.connect() as conn:
        t0_sha = conn.execute(
            text("SELECT artifact_sha256 FROM mi_sector_snapshots WHERE source_id='EQUITY_EOD' AND dataset='ETF_RS_VS_SPY' AND sector_key='Technology' AND as_of=:d"),
            {"d": T0},
        ).scalar()

    partial_id = str(uuid.uuid4())
    missing = "NVDA"
    bars = [_eod_bar(symbol=s, day=T1, close=300.0, con_id=2000 + i) for i, s in enumerate(UNIVERSE) if s != missing]
    r = ingest_client.post(
        "/v1/equity_bars",
        headers=_headers(),
        json=_eod_batch(bars, batch_id=partial_id, chunk_index=1, chunk_count=1, coverage=_coverage_partial(missing), finalize=True),
    )
    assert r.status_code == 200, r.text
    assert r.json()["coverage_status"] == "PARTIAL"
    assert r.json()["snapshots"] == 0
    assert r.json().get("run_status") == "PARTIAL"
    assert _snapshot_as_ofs(mi_db) == t0_snaps
    with mi_db.connect() as conn:
        still = conn.execute(
            text("SELECT artifact_sha256, as_of FROM mi_sector_snapshots WHERE source_id='EQUITY_EOD' AND dataset='ETF_RS_VS_SPY' AND sector_key='Technology' AND as_of=:d"),
            {"d": T0},
        ).mappings().one()
        fresh = conn.execute(text("SELECT coverage_status, metadata_status, latest_observation_date FROM mi_data_freshness WHERE source_id='EQUITY_EOD' AND dataset='equity_etf_daily_bars'")).mappings().one()
        bars_kept = conn.execute(text("SELECT COUNT(*) FROM mi_market_bars WHERE source_id='EQUITY_EOD' AND bar_date=:d"), {"d": T1}).scalar()
        nvda_t1 = conn.execute(text("SELECT COUNT(*) FROM mi_market_bars WHERE source_id='EQUITY_EOD' AND instrument_id='NVDA' AND bar_date=:d"), {"d": T1}).scalar()
    assert still["artifact_sha256"] == t0_sha
    assert fresh["coverage_status"] == "PARTIAL"
    assert fresh["metadata_status"] == "PARTIAL_COVERAGE"
    assert fresh["latest_observation_date"] == date(2026, 9, 8)
    assert bars_kept == len(UNIVERSE) - 1
    assert nvda_t1 == 0


def test_zero_of_46_records_failed_without_snapshots(ingest_client, mi_db):
    batch_id = str(uuid.uuid4())
    r = ingest_client.post("/v1/equity_bars/finalize", headers=_headers(), json=_finalize_body(batch_id, _coverage_failed()))
    assert r.status_code == 200, r.text
    assert r.json()["coverage_status"] == "EMPTY"
    assert r.json()["snapshots"] == 0
    with mi_db.connect() as conn:
        snaps = conn.execute(text("SELECT COUNT(*) FROM mi_sector_snapshots WHERE source_id='EQUITY_EOD'")).scalar()
        status = conn.execute(text("SELECT status FROM mi_ingestion_runs WHERE run_id=:r"), {"r": r.json()["run_id"]}).scalar()
        state = conn.execute(text("SELECT state FROM mi_equity_eod_batches WHERE batch_id=:id"), {"id": batch_id}).scalar()
    assert snaps == 0
    assert status == "FAILED"
    assert state == "FAILED"


def test_unknown_complete_and_partial_finalize_rejected(ingest_client):
    headers = _headers()
    complete = ingest_client.post(
        "/v1/equity_bars/finalize",
        headers=headers,
        json=_finalize_body(str(uuid.uuid4()), _coverage_complete()),
    )
    assert complete.status_code == 400
    assert "unknown batch_id" in complete.json()["detail"]
    partial = ingest_client.post(
        "/v1/equity_bars/finalize",
        headers=headers,
        json=_finalize_body(str(uuid.uuid4()), _coverage_partial()),
    )
    assert partial.status_code == 400
    assert "unknown batch_id" in partial.json()["detail"]
    fake_complete_zero = dict(_coverage_failed())
    fake_complete_zero["coverage_status"] = "COMPLETE"
    fake_complete_zero["overall"] = "COMPLETE"
    rejected = ingest_client.post(
        "/v1/equity_bars/finalize",
        headers=headers,
        json=_finalize_body(str(uuid.uuid4()), fake_complete_zero),
    )
    assert rejected.status_code == 400


def test_one_logical_run_stays_attempted_until_finalize(mi_db):
    batch_id = str(uuid.uuid4())
    first = _stage(mi_db, [_eod_bar(day=T0)], batch_id=batch_id, chunk_index=1, chunk_count=2)
    second = _stage(mi_db, [_eod_bar(day=T1, close=651.0)], batch_id=batch_id, chunk_index=2, chunk_count=2)
    assert first["run_id"] == second["run_id"]
    run_id, status, statuses = _run_statuses(mi_db, batch_id)
    assert run_id == first["run_id"]
    assert status == "ATTEMPTED"
    assert statuses == ["ATTEMPTED"]
    done = _finalize(mi_db, batch_id, _coverage_partial())
    assert done["run_id"] == run_id
    assert done["run_status"] == "PARTIAL"
    _, status, statuses = _run_statuses(mi_db, batch_id)
    assert status == "PARTIAL"
    assert statuses == ["PARTIAL"]


def test_duplicate_chunk_same_hash_is_idempotent(ingest_client, mi_db):
    batch_id = str(uuid.uuid4())
    body = _eod_batch([_eod_bar()], batch_id=batch_id, chunk_index=1, chunk_count=2, finalize=False)
    first = ingest_client.post("/v1/equity_bars", headers=_headers(), json=body)
    again = ingest_client.post("/v1/equity_bars", headers=_headers(), json=body)
    assert first.status_code == 200 and again.status_code == 200
    assert again.json().get("finalized") is False
    with mi_db.connect() as conn:
        chunks = conn.execute(text("SELECT COUNT(*) FROM mi_equity_eod_batch_chunks WHERE batch_id=:id"), {"id": batch_id}).scalar()
        received = conn.execute(text("SELECT received_chunks FROM mi_equity_eod_batches WHERE batch_id=:id"), {"id": batch_id}).scalar()
        runs = conn.execute(text("SELECT COUNT(*) FROM mi_ingestion_runs WHERE source_id='EQUITY_EOD' AND dataset='equity_etf_daily_bars'")).scalar()
    assert chunks == 1
    assert received == 1
    assert runs == 1


def test_duplicate_chunk_changed_hash_rejected(ingest_client):
    batch_id = str(uuid.uuid4())
    ingest_client.post("/v1/equity_bars", headers=_headers(), json=_eod_batch([_eod_bar(close=650.0)], batch_id=batch_id, chunk_index=1, chunk_count=2))
    mutated = ingest_client.post("/v1/equity_bars", headers=_headers(), json=_eod_batch([_eod_bar(close=651.0)], batch_id=batch_id, chunk_index=1, chunk_count=2))
    assert mutated.status_code == 409


def test_reordered_chunks_then_finalize(ingest_client, mi_db):
    batch_id = str(uuid.uuid4())
    second = ingest_client.post(
        "/v1/equity_bars",
        headers=_headers(),
        json=_eod_batch([_eod_bar(day=T1)], batch_id=batch_id, chunk_index=2, chunk_count=2, finalize=False),
    )
    first = ingest_client.post(
        "/v1/equity_bars",
        headers=_headers(),
        json=_eod_batch([_eod_bar(day=T0, close=649.0)], batch_id=batch_id, chunk_index=1, chunk_count=2, finalize=False),
    )
    assert second.status_code == 200 and first.status_code == 200
    done = ingest_client.post("/v1/equity_bars/finalize", headers=_headers(), json=_finalize_body(batch_id, _coverage_partial()))
    assert done.status_code == 200
    assert done.json()["coverage_status"] == "PARTIAL"


def test_finalize_before_all_chunks_rejected(ingest_client):
    batch_id = str(uuid.uuid4())
    ingest_client.post("/v1/equity_bars", headers=_headers(), json=_eod_batch([_eod_bar()], batch_id=batch_id, chunk_index=1, chunk_count=2))
    early = ingest_client.post("/v1/equity_bars/finalize", headers=_headers(), json=_finalize_body(batch_id, _coverage_complete(["SPY"])))
    assert early.status_code == 409
    assert "finalize before all chunks" in early.json()["detail"]


def test_duplicate_finalization_is_idempotent(ingest_client, mi_db):
    batch_id = str(uuid.uuid4())
    ingest_client.post(
        "/v1/equity_bars",
        headers=_headers(),
        json=_eod_batch(_universe_bars(T0), batch_id=batch_id, chunk_index=1, chunk_count=1, coverage=_coverage_complete(), finalize=True),
    )
    payload = _finalize_body(batch_id, _coverage_complete())
    again = ingest_client.post("/v1/equity_bars/finalize", headers=_headers(), json=payload)
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["snapshots"] == 0
    assert _snapshot_as_ofs(mi_db) == [date(2026, 9, 8)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "YAHOO"),
        ("source_id", "FMP_LEGACY"),
        ("what_to_show", "TRADES"),
        ("adjustment_basis", "SPLIT_ADJUSTED_UNKNOWN_DIVIDEND"),
        ("request_mode", "backfill"),
        ("collector_id", "other-host"),
    ],
)
def test_finalize_rejects_metadata_substitution(ingest_client, field, value):
    batch_id = str(uuid.uuid4())
    posted = ingest_client.post(
        "/v1/equity_bars",
        headers=_headers(),
        json=_eod_batch([_eod_bar()], batch_id=batch_id, chunk_index=1, chunk_count=1, finalize=False, request_mode="full"),
    )
    assert posted.status_code == 200
    body = _finalize_body(batch_id, _coverage_partial(), **{field: value})
    if field in {"provider", "source_id", "what_to_show", "adjustment_basis"}:
        # HTTP validator rejects illegal IBKR provenance before the batch binder.
        r = ingest_client.post("/v1/equity_bars/finalize", headers=_headers(), json=body)
        assert r.status_code == 400
        return
    r = ingest_client.post("/v1/equity_bars/finalize", headers=_headers(), json=body)
    assert r.status_code == 409
    assert field in r.json()["detail"]


def test_trades_cannot_be_mislabeled_adjusted_last(mi_db):
    batch_id = str(uuid.uuid4())
    _stage(mi_db, [_eod_bar()], batch_id=batch_id, chunk_index=1, chunk_count=1)
    with pytest.raises(BatchProtocolError, match="what_to_show"):
        _finalize(mi_db, batch_id, _coverage_partial(), what_to_show="TRADES")
    with pytest.raises(PayloadError, match="what_to_show"):
        validate_equity_bar_batch(_eod_batch([_eod_bar(what_to_show="TRADES")]))


def test_open_batch_do_refresh_cannot_promote_staged_bars(mi_db, monkeypatch):
    t0_id = str(uuid.uuid4())
    _stage(mi_db, _universe_bars(T0, 110.0), batch_id=t0_id, chunk_index=1, chunk_count=1, coverage=_coverage_complete(), finalize=False)
    done = _finalize(mi_db, t0_id, _coverage_complete())
    assert done["snapshots"] > 0
    assert _snapshot_as_ofs(mi_db) == [date(2026, 9, 8)]

    open_id = str(uuid.uuid4())
    chunk_count = 58
    for index, symbol in enumerate(UNIVERSE[:30], start=1):
        _stage(
            mi_db,
            [_eod_bar(symbol=symbol, day=T1, close=400.0 + index, con_id=3000 + index)],
            batch_id=open_id,
            chunk_index=index,
            chunk_count=chunk_count,
            request_mode="full",
        )
    with mi_db.connect() as conn:
        state = conn.execute(text("SELECT state, received_chunks FROM mi_equity_eod_batches WHERE batch_id=:id"), {"id": open_id}).mappings().one()
        run_status = conn.execute(text("SELECT status FROM mi_ingestion_runs WHERE run_id=(SELECT ingestion_run_id FROM mi_equity_eod_batches WHERE batch_id=:id)"), {"id": open_id}).scalar()
    assert state["state"] == "OPEN"
    assert int(state["received_chunks"]) == 30
    assert run_status == "ATTEMPTED"

    connected = []

    def _boom(*_args, **_kwargs):
        connected.append(True)
        raise AssertionError("TWS socket opened during DigitalOcean refresh")

    monkeypatch.setattr("ibkr_collector.historical.connect_historical_session", _boom)
    monkeypatch.setattr("market_intelligence.ibkr_eod.connect_historical_session", _boom)
    report = ingest_equity_eod(
        mi_db,
        CollectorStoreAdapter(),
        today=date(2026, 9, 11),
        lookback_days=10,
        env={"MI_EQUITY_PROVIDER": "ibkr"},
    )
    assert connected == []
    assert report.status == "SKIPPED"
    assert report.snapshots_written == 0
    assert report.latest_observation == date(2026, 9, 8)
    assert _snapshot_as_ofs(mi_db) == [date(2026, 9, 8)]
    with mi_db.connect() as conn:
        fresh = conn.execute(text("SELECT latest_observation_date, coverage_status, metadata_status, coverage_json FROM mi_data_freshness WHERE source_id='EQUITY_EOD' AND dataset='equity_etf_daily_bars'")).mappings().one()
    assert fresh["latest_observation_date"] == date(2026, 9, 8)
    assert fresh["coverage_status"] == "COMPLETE"
    assert fresh["metadata_status"] == "IN_PROGRESS"
    assert fresh["coverage_json"]["latest_collection_attempt"]["state"] == "OPEN"
    assert fresh["coverage_json"]["snapshot_promotion"] == "blocked_open_batch"

    remaining = UNIVERSE[30:]
    next_index = 31
    for symbol in remaining:
        _stage(mi_db, [_eod_bar(symbol=symbol, day=T1, close=500.0, con_id=4000 + next_index)], batch_id=open_id, chunk_index=next_index, chunk_count=chunk_count)
        next_index += 1
    while next_index <= chunk_count:
        _stage(mi_db, [_eod_bar(symbol="SPY", day="2026-09-10", close=111.0 + next_index)], batch_id=open_id, chunk_index=next_index, chunk_count=chunk_count)
        next_index += 1
    promoted = _finalize(mi_db, open_id, _coverage_complete())
    assert promoted["coverage_status"] == "COMPLETE"
    assert promoted["run_status"] == "SUCCEEDED"
    assert promoted["snapshots"] > 0
    assert _snapshot_as_ofs(mi_db) == [date(2026, 9, 8), date(2026, 9, 11)]


def test_remote_gateway_still_redacts_ibkr_values():
    from ai_gateway.config import remote_value_sources
    from market_intelligence.export_policy import EXPORT_MODE_EXTERNAL, filter_for_export

    assert "IBKR" not in remote_value_sources({})
    assert "EQUITY_EOD" not in remote_value_sources({})
    remote = filter_for_export(
        {
            "sector_key": "Technology",
            "source_id": "EQUITY_EOD",
            "provider": "IBKR",
            "export_scope": "INTERNAL_ONLY",
            "ret_1d": 0.012,
        },
        export_mode=EXPORT_MODE_EXTERNAL,
        remote_value_sources=("IBKR",),
    )
    assert remote.get("restricted") is True
    assert "0.012" not in str(remote)


def test_streamlit_pages_remain_db_only():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    forbidden = (
        "market_intelligence.equity_eod",
        "market_intelligence.ibkr_eod",
        "ibkr_collector.historical",
        "ibkr_collector.eod_cli",
        "ai_gateway",
    )
    for rel in (
        "market_intelligence/pages_ui.py",
        "market_intelligence/ui.py",
        "dashboard.py",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text
