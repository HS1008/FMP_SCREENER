"""Public CFTC COT client + ingest (no live network in the default unit test)."""

from __future__ import annotations

from datetime import date

import pytest

from market_intelligence.cftc_client import CftcClient
from market_intelligence.ingest_cftc import ingest_cftc


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._payload


def test_cftc_client_normalizes_net_positioning():
    payload = b"""[{
        "market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
        "report_date_as_yyyy_mm_dd": "2026-09-08T00:00:00.000",
        "open_interest_all": "100",
        "noncomm_positions_long_all": "60",
        "noncomm_positions_short_all": "40",
        "comm_positions_long_all": "25",
        "comm_positions_short_all": "30",
        "commodity_name": "GOLD",
        "futonly_or_combined": "FutOnly"
    }]"""

    def opener(request, timeout):
        assert "6dca-aqww.json" in request.full_url
        return _FakeResponse(payload)

    rows = CftcClient(opener=opener).latest_rows()
    assert rows[0]["report_date"] == "2026-09-08"
    assert rows[0]["noncomm_net"] == 20
    assert rows[0]["open_interest"] == 100


@pytest.mark.usefixtures("pg_engine")
def test_cftc_ingest_writes_watchlist_rows(pg_engine):
    class _Client:
        def latest_rows(self):
            return [
                {
                    "market": "GOLD - COMMODITY EXCHANGE INC.",
                    "report_date": "2026-09-08",
                    "open_interest": 100,
                    "noncomm_long": 60,
                    "noncomm_short": 40,
                    "noncomm_net": 20,
                    "comm_long": 25,
                    "comm_short": 30,
                    "commodity_name": "GOLD",
                    "report_type": "FutOnly",
                }
            ]

    report = ingest_cftc(pg_engine, _Client(), today=date(2026, 9, 14))
    assert report.failed is False
    assert report.rows_written == 1
    assert report.latest_observation == date(2026, 9, 8)
    from sqlalchemy import text

    with pg_engine.connect() as conn:
        row = conn.execute(text("SELECT market, noncomm_net FROM mi_v_cftc_cot_current")).mappings().one()
    assert row["market"].startswith("GOLD")
    assert int(row["noncomm_net"]) == 20


@pytest.mark.usefixtures("pg_engine")
def test_cftc_empty_payload_is_failed_not_healthy(pg_engine):
    class _Empty:
        def latest_rows(self):
            return []

    report = ingest_cftc(pg_engine, _Empty(), today=date(2026, 9, 14))
    assert report.failed is True
    assert report.status == "FAILED"
    assert report.rows_written == 0
    assert "zero publishable" in (report.error or "")
    from sqlalchemy import text

    with pg_engine.connect() as conn:
        fresh = conn.execute(
            text(
                "SELECT transport_status, freshness_status FROM mi_data_freshness "
                "WHERE source_id = 'CFTC_COT' AND dataset = 'commitment_of_traders'"
            )
        ).mappings().one()
    assert fresh["transport_status"] == "FAILED"
