"""PostgreSQL + HTTP tests for private IBKR ingest (no TWS, no orders)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from ibkr_ingest.app import app, set_engine_for_tests


@pytest.fixture
def ingest_client(mi_db, monkeypatch):
    from fastapi.testclient import TestClient

    set_engine_for_tests(mi_db)
    monkeypatch.setenv("IBKR_INGEST_TOKEN", "test-ingest-token")
    monkeypatch.setenv("IBKR_INGEST_DATABASE_URL", "postgresql://unused")
    client = TestClient(app)
    yield client
    set_engine_for_tests(None)


def test_health_is_unauthenticated(ingest_client):
    assert ingest_client.get("/health").json() == {"status": "ok"}


def test_rejects_missing_token_and_mutations_of_unknown_routes(ingest_client):
    r = ingest_client.post("/v1/heartbeat", json={"collector_id": "c", "reported_state": "CONNECTED"})
    assert r.status_code == 401
    r = ingest_client.get("/v1/quotes", headers={"Authorization": "Bearer test-ingest-token"})
    assert r.status_code == 405


def test_heartbeat_and_stale_observation(ingest_client, mi_db):
    headers = {"Authorization": "Bearer test-ingest-token"}
    body = {"collector_id": "harin-laptop", "reported_state": "WAITING_FOR_TWS", "client_id": 71, "watchlist": ["SPY"]}
    r = ingest_client.post("/v1/heartbeat", headers=headers, json=body)
    assert r.status_code == 200 and r.json()["ok"] is True
    with mi_db.connect() as conn:
        row = conn.execute(text("SELECT reported_state, observed_state FROM mi_v_ibkr_collector_status WHERE collector_id='harin-laptop'")).mappings().one()
        assert row["reported_state"] == "WAITING_FOR_TWS"
        assert row["observed_state"] == "WAITING_FOR_TWS"
        conn.execute(
            text("UPDATE mi_collector_status SET last_heartbeat_at = NOW() - INTERVAL '5 minutes' WHERE collector_id='harin-laptop'")
        )
        conn.commit()
        stale = conn.execute(text("SELECT observed_state, heartbeat_age_seconds FROM mi_v_ibkr_collector_status WHERE collector_id='harin-laptop'")).mappings().one()
        assert stale["observed_state"] == "COLLECTOR_OFFLINE"
        assert stale["heartbeat_age_seconds"] >= 90


def test_quote_ingest_is_idempotent_and_keeps_nulls(ingest_client, mi_db):
    headers = {"Authorization": "Bearer test-ingest-token"}
    ingest_client.post("/v1/heartbeat", headers=headers, json={"collector_id": "harin-laptop", "reported_state": "CONNECTED", "client_id": 71, "watchlist": ["SPY"]})
    quote = {
        "record_id": "rec-spy-1",
        "symbol": "SPY",
        "sec_type": "STK",
        "con_id": 756733,
        "currency": "USD",
        "exchange": "SMART",
        "quote_ts": "2026-09-09T14:00:00+00:00",
        "retrieved_at": "2026-09-09T14:00:01+00:00",
        "bid": 500.1,
        "ask": 500.2,
        "last_price": None,
        "close_price": 499.9,
        "market_data_type": "DELAYED",
        "quote_status": "PARTIAL",
        "last_callback_at": "2026-09-09T13:59:58+00:00",
        "provenance": {"collector_id": "harin-laptop", "client_id": 71, "tws_host": "127.0.0.1"},
    }
    r1 = ingest_client.post("/v1/quotes", headers=headers, json={"collector_id": "harin-laptop", "quotes": [quote]})
    r2 = ingest_client.post("/v1/quotes", headers=headers, json={"collector_id": "harin-laptop", "quotes": [quote]})
    assert r1.status_code == 200 and r1.json()["inserted"] == 1
    assert r2.status_code == 200 and r2.json()["unchanged"] == 1 and r2.json()["inserted"] == 0
    with mi_db.connect() as conn:
        row = conn.execute(text("SELECT last_price, bid, close_price, market_data_type, con_id FROM mi_v_ibkr_quotes_latest")).mappings().one()
        assert row["last_price"] is None
        assert float(row["bid"]) == 500.1
        assert float(row["close_price"]) == 499.9
        assert row["market_data_type"] == "DELAYED"
        assert int(row["con_id"]) == 756733
        ingest = conn.execute(text("SELECT last_ingest_ok_at FROM mi_v_ibkr_collector_status WHERE collector_id='harin-laptop'")).scalar()
        assert ingest is not None
        callback = conn.execute(text("SELECT last_callback_at FROM mi_v_ibkr_quotes_latest")).scalar()
        assert callback is not None


def test_mixed_batch_quarantines_poison_and_keeps_valid(ingest_client, mi_db):
    headers = {"Authorization": "Bearer test-ingest-token"}
    good = {
        "record_id": "good-1",
        "symbol": "SPY",
        "sec_type": "STK",
        "quote_ts": "2026-09-09T14:01:00+00:00",
        "market_data_type": "DELAYED",
        "bid": 1.0,
        "ask": 1.1,
    }
    poison = {"record_id": "bad-1", "symbol": "SPY", "quote_ts": "2026-09-09T14:01:00+00:00", "place_order": True}
    later = {
        "record_id": "good-2",
        "symbol": "QQQ",
        "sec_type": "STK",
        "quote_ts": "2026-09-09T14:01:01+00:00",
        "market_data_type": "DELAYED",
        "bid": 2.0,
        "ask": 2.1,
    }
    r = ingest_client.post("/v1/quotes", headers=headers, json={"collector_id": "harin-laptop", "quotes": [good, poison, later]})
    assert r.status_code == 200
    body = r.json()
    assert body["inserted"] == 2
    assert body["rejected"] == 1
    outcomes = {row["record_id"]: row["outcome"] for row in body["results"]}
    assert outcomes["good-1"] == "committed"
    assert outcomes["bad-1"] == "rejected"
    assert outcomes["good-2"] == "committed"
    with mi_db.connect() as conn:
        symbols = {row[0] for row in conn.execute(text("SELECT i.display_name FROM mi_v_ibkr_quotes_latest q JOIN mi_market_instruments i ON i.instrument_id = q.instrument_id"))}
        assert symbols == {"SPY", "QQQ"}
        asset = conn.execute(text("SELECT asset_type FROM mi_market_instruments WHERE display_name='SPY'")).scalar()
        assert asset == "EQUITY"


def test_savepoint_isolates_invalid_timestamp(mi_db):
    from market_intelligence.ibkr_store import ingest_quotes

    records = [
        {
            "record_id": "ok-ts",
            "symbol": "SPY",
            "sec_type": "STK",
            "quote_ts": "2026-09-09T14:02:00+00:00",
            "market_data_type": "DELAYED",
            "bid": 1.0,
        },
        {
            "record_id": "bad-ts",
            "symbol": "QQQ",
            "sec_type": "STK",
            "quote_ts": "not-a-timestamp",
            "market_data_type": "DELAYED",
            "bid": 2.0,
        },
        {
            "record_id": "ok-ts-2",
            "symbol": "IWM",
            "sec_type": "STK",
            "quote_ts": "2026-09-09T14:02:01+00:00",
            "market_data_type": "DELAYED",
            "bid": 3.0,
        },
    ]
    with mi_db.begin() as conn:
        result = ingest_quotes(conn, records, collector_id="harin-laptop")
    assert result["inserted"] == 2
    assert result["rejected"] == 1
    assert [row["outcome"] for row in result["results"]] == ["committed", "rejected", "committed"]


def test_equity_bar_ingest_writes_eod_provenance(ingest_client, mi_db):
    headers = {"Authorization": "Bearer test-ingest-token"}
    body = {
        "collector_id": "harin-laptop",
        "provider": "IBKR",
        "source_id": "EQUITY_EOD",
        "what_to_show": "ADJUSTED_LAST",
        "adjustment_basis": "IBKR_ADJUSTED_LAST",
        "bars": [
            {
                "symbol": "SPY",
                "con_id": 756733,
                "sec_type": "STK",
                "exchange": "SMART",
                "primary_exchange": "ARCA",
                "currency": "USD",
                "bar_date": "2026-09-10",
                "open": 649.0,
                "high": 652.0,
                "low": 648.0,
                "close": 650.0,
                "adj_close": 650.0,
                "volume": 1000,
                "adjustment_basis": "IBKR_ADJUSTED_LAST",
                "what_to_show": "ADJUSTED_LAST",
                "provider_symbol": "SPY",
            }
        ],
    }
    r = ingest_client.post("/v1/equity_bars", headers=headers, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 1
    with mi_db.connect() as conn:
        row = conn.execute(
            text("SELECT adj_close_price, adjustment_basis, con_id, source_id FROM mi_market_bars WHERE instrument_id='SPY'")
        ).mappings().one()
        assert float(row["adj_close_price"]) == 650.0
        assert row["adjustment_basis"] == "IBKR_ADJUSTED_LAST"
        assert int(row["con_id"]) == 756733
        assert row["source_id"] == "EQUITY_EOD"
        assert r.json()["finalized"] is True


def test_equity_bar_batch_rejects_orders_and_unknown_basis():
    from ibkr_ingest.validate import PayloadError, validate_equity_bar_batch

    with pytest.raises(PayloadError):
        validate_equity_bar_batch({"collector_id": "x", "place_order": True, "bars": []})
    with pytest.raises(PayloadError, match="adjustment_basis"):
        validate_equity_bar_batch(
            {
                "collector_id": "x",
                "provider": "IBKR",
                "source_id": "EQUITY_EOD",
                "what_to_show": "ADJUSTED_LAST",
                "adjustment_basis": "IBKR_ADJUSTED_LAST",
                "bars": [
                    {
                        "symbol": "SPY",
                        "con_id": 1,
                        "exchange": "SMART",
                        "primary_exchange": "ARCA",
                        "currency": "USD",
                        "bar_date": "2026-09-10",
                        "close": 1,
                        "adj_close": 1,
                        "adjustment_basis": "ADJUSTED_CLOSE",
                        "what_to_show": "ADJUSTED_LAST",
                    }
                ],
            }
        )


def test_quote_batch_limit(ingest_client):
    headers = {"Authorization": "Bearer test-ingest-token"}
    quotes = [
        {
            "record_id": "r{0}".format(i),
            "symbol": "SPY",
            "quote_ts": "2026-09-09T14:00:00+00:00",
            "market_data_type": "DELAYED",
        }
        for i in range(101)
    ]
    r = ingest_client.post("/v1/quotes", headers=headers, json={"collector_id": "x", "quotes": quotes})
    assert r.status_code == 400


def _eod_bar(symbol="SPY", day="2026-09-10", close=650.0, con_id=756733, **extra):
    row = {
        "symbol": symbol,
        "con_id": con_id,
        "sec_type": "STK",
        "exchange": "SMART",
        "primary_exchange": extra.pop("primary_exchange", "ARCA"),
        "currency": "USD",
        "bar_date": day,
        "open": close - 1.0,
        "high": close + 1.0,
        "low": close - 2.0,
        "close": close,
        "adj_close": close,
        "volume": 1000,
        "adjustment_basis": "IBKR_ADJUSTED_LAST",
        "what_to_show": "ADJUSTED_LAST",
        "provider_symbol": symbol,
    }
    row.update(extra)
    return row


def _eod_batch(bars, *, batch_id=None, chunk_index=None, chunk_count=None, coverage=None, finalize=None, **extra):
    body = {
        "collector_id": "harin-laptop",
        "provider": "IBKR",
        "source_id": "EQUITY_EOD",
        "what_to_show": "ADJUSTED_LAST",
        "adjustment_basis": "IBKR_ADJUSTED_LAST",
        "request_mode": extra.pop("request_mode", "full"),
        "bars": bars,
        "coverage": coverage or {},
    }
    if batch_id:
        body["batch_id"] = batch_id
    if chunk_index is not None:
        body["chunk_index"] = chunk_index
    if chunk_count is not None:
        body["chunk_count"] = chunk_count
    if finalize is not None:
        body["finalize"] = finalize
    body.update(extra)
    return body


def test_equity_ingest_rejects_false_provenance_and_future_bars():
    from ibkr_ingest.validate import PayloadError, validate_equity_bar_batch

    completed = date(2026, 9, 10)
    with pytest.raises(PayloadError, match="provider"):
        validate_equity_bar_batch(_eod_batch([_eod_bar()], **{"provider": "YAHOO"}), completed_session=completed)
    with pytest.raises(PayloadError, match="source_id"):
        validate_equity_bar_batch(_eod_batch([_eod_bar()], **{"source_id": "FMP_LEGACY"}), completed_session=completed)
    trades = _eod_bar(what_to_show="TRADES")
    with pytest.raises(PayloadError, match="what_to_show"):
        validate_equity_bar_batch(_eod_batch([trades]), completed_session=completed)
    missing_basis = _eod_bar()
    del missing_basis["adjustment_basis"]
    with pytest.raises(PayloadError, match="adjustment_basis"):
        validate_equity_bar_batch(_eod_batch([missing_basis]), completed_session=completed)
    with pytest.raises(PayloadError, match="future"):
        validate_equity_bar_batch(_eod_batch([_eod_bar(day="2099-01-01")]), completed_session=completed)
    with pytest.raises(PayloadError, match="contradictory"):
        validate_equity_bar_batch(
            _eod_batch([_eod_bar(close=100.0), _eod_bar(close=101.0)]),
            completed_session=completed,
        )


def test_interrupted_duplicate_reordered_and_idempotent_chunks(ingest_client, mi_db):
    import uuid

    from sqlalchemy import text

    headers = {"Authorization": "Bearer test-ingest-token"}
    batch_id = str(uuid.uuid4())
    first = _eod_batch(
        [_eod_bar(day="2026-09-09", close=649.0)],
        batch_id=batch_id,
        chunk_index=1,
        chunk_count=2,
        coverage={"requested": 2, "successful": ["SPY"], "failed": {"NVDA": "timeout"}},
        request_mode="full",
    )
    second = _eod_batch(
        [_eod_bar(day="2026-09-10", close=650.0)],
        batch_id=batch_id,
        chunk_index=2,
        chunk_count=2,
        coverage={"requested": 2, "successful": ["SPY"], "failed": {"NVDA": "timeout"}},
        request_mode="full",
    )
    r1 = ingest_client.post("/v1/equity_bars", headers=headers, json=first)
    assert r1.status_code == 200, r1.text
    assert r1.json()["finalized"] is False
    with mi_db.connect() as conn:
        snaps = conn.execute(text("SELECT COUNT(*) FROM mi_sector_snapshots WHERE source_id='EQUITY_EOD'")).scalar()
        fresh = conn.execute(text("SELECT coverage_status, metadata_status FROM mi_data_freshness WHERE source_id='EQUITY_EOD' AND dataset='equity_etf_daily_bars'")).mappings().first()
    assert snaps == 0
    assert fresh is None or fresh["coverage_status"] != "COMPLETE"
    early = ingest_client.post(
        "/v1/equity_bars/finalize",
        headers=headers,
        json={
            "collector_id": "harin-laptop",
            "batch_id": batch_id,
            "coverage": first["coverage"],
            "request_mode": "full",
            "provider": "IBKR",
            "source_id": "EQUITY_EOD",
            "what_to_show": "ADJUSTED_LAST",
            "adjustment_basis": "IBKR_ADJUSTED_LAST",
        },
    )
    assert early.status_code == 409
    dup = ingest_client.post("/v1/equity_bars", headers=headers, json=first)
    assert dup.status_code == 200 and dup.json().get("finalized") is False
    reordered = ingest_client.post("/v1/equity_bars", headers=headers, json=second)
    assert reordered.status_code == 200, reordered.text
    done = ingest_client.post(
        "/v1/equity_bars/finalize",
        headers=headers,
        json={
            "collector_id": "harin-laptop",
            "batch_id": batch_id,
            "coverage": second["coverage"],
            "request_mode": "full",
            "provider": "IBKR",
            "source_id": "EQUITY_EOD",
            "what_to_show": "ADJUSTED_LAST",
            "adjustment_basis": "IBKR_ADJUSTED_LAST",
        },
    )
    assert done.status_code == 200, done.text
    assert done.json()["coverage_status"] == "PARTIAL"
    again = ingest_client.post(
        "/v1/equity_bars/finalize",
        headers=headers,
        json={
            "collector_id": "harin-laptop",
            "batch_id": batch_id,
            "coverage": second["coverage"],
            "request_mode": "full",
            "provider": "IBKR",
            "source_id": "EQUITY_EOD",
            "what_to_show": "ADJUSTED_LAST",
            "adjustment_basis": "IBKR_ADJUSTED_LAST",
        },
    )
    assert again.status_code == 200 and again.json()["duplicate"] is True
    with mi_db.connect() as conn:
        bars = conn.execute(text("SELECT COUNT(*) FROM mi_market_bars WHERE source_id='EQUITY_EOD' AND instrument_id='SPY'")).scalar()
        freshness = conn.execute(text("SELECT transport_status, coverage_status, metadata_status FROM mi_data_freshness WHERE source_id='EQUITY_EOD' AND dataset='equity_etf_daily_bars'")).mappings().one()
        health = conn.execute(text("SELECT coverage_status FROM mi_v_source_health WHERE source_id='EQUITY_EOD'")).scalar()
    assert bars == 2
    assert freshness["transport_status"] == "OK"
    assert freshness["coverage_status"] == "PARTIAL"
    assert freshness["metadata_status"] == "PARTIAL_COVERAGE"
    assert health == "PARTIAL"


def test_mismatched_chunk_hash_is_rejected(ingest_client):
    import uuid

    headers = {"Authorization": "Bearer test-ingest-token"}
    batch_id = str(uuid.uuid4())
    first = _eod_batch([_eod_bar(close=650.0)], batch_id=batch_id, chunk_index=1, chunk_count=2)
    ingest_client.post("/v1/equity_bars", headers=headers, json=first)
    mutated = _eod_batch([_eod_bar(close=651.0)], batch_id=batch_id, chunk_index=1, chunk_count=2)
    r = ingest_client.post("/v1/equity_bars", headers=headers, json=mutated)
    assert r.status_code == 409


def test_empty_universe_finalize_records_failed_coverage(ingest_client, mi_db):
    import uuid

    from sqlalchemy import text

    headers = {"Authorization": "Bearer test-ingest-token"}
    batch_id = str(uuid.uuid4())
    coverage = {
        "requested": ["SPY", "XLK"],
        "successful": [],
        "failed": {"SPY": "timeout", "XLK": "no_entitlement"},
        "coverage_ratio": 0.0,
        "overall": "FAILED",
    }
    r = ingest_client.post(
        "/v1/equity_bars/finalize",
        headers=headers,
        json={
            "collector_id": "harin-laptop",
            "batch_id": batch_id,
            "coverage": coverage,
            "request_mode": "full",
            "provider": "IBKR",
            "source_id": "EQUITY_EOD",
            "what_to_show": "ADJUSTED_LAST",
            "adjustment_basis": "IBKR_ADJUSTED_LAST",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["coverage_status"] == "EMPTY"
    with mi_db.connect() as conn:
        fresh = conn.execute(
            text("SELECT transport_status, coverage_status, metadata_status FROM mi_data_freshness WHERE source_id='EQUITY_EOD' AND dataset='equity_etf_daily_bars'")
        ).mappings().one()
    assert fresh["coverage_status"] == "EMPTY"
    assert fresh["metadata_status"] == "INCOMPLETE"
    assert fresh["transport_status"] == "FAILED"
