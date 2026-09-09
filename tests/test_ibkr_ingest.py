"""PostgreSQL + HTTP tests for private IBKR ingest (no TWS, no orders)."""

from __future__ import annotations

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
