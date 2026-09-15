"""Deterministic EIA ingest behavior (no live EIA_API_KEY required)."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from market_intelligence.ingest_eia import api_key_from_env, ingest_eia


def test_api_key_from_env_treats_blank_as_missing():
    assert api_key_from_env({"EIA_API_KEY": ""}) is None
    assert api_key_from_env({"EIA_API_KEY": "  "}) is None
    assert api_key_from_env({"EIA_API_KEY": "abc"}) == "abc"


@pytest.mark.usefixtures("pg_engine")
def test_missing_eia_key_is_skipped_configuration_not_failed(pg_engine):
    report = ingest_eia(pg_engine, env={}, today=date(2026, 9, 14))
    assert report.status == "SKIPPED"
    assert report.failed is False
    assert "EIA_API_KEY missing" in (report.error or "")
    from sqlalchemy import text

    with pg_engine.connect() as conn:
        fresh = conn.execute(
            text(
                "SELECT transport_status, last_error_redacted FROM mi_data_freshness "
                "WHERE source_id = 'EIA_ENERGY' AND dataset = 'petroleum_and_gas_statistics'"
            )
        ).mappings().one()
    assert fresh["transport_status"] == "SKIPPED"
    assert "opendata" in (fresh["last_error_redacted"] or "")


@pytest.mark.usefixtures("pg_engine")
def test_eia_ingest_writes_series_rows_from_fixture_payload(pg_engine, monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return (
                b'{"response":{"data":[{"period":"2026-09-04","value":420.5,"units":"million barrels"},'
                b'{"period":"2026-08-28","value":418.0,"units":"million barrels"}]}}'
            )

    monkeypatch.setattr("market_intelligence.ingest_eia.urllib.request.urlopen", lambda *a, **k: _Resp())
    report = ingest_eia(pg_engine, env={"EIA_API_KEY": "test-key"}, today=date(2026, 9, 14))
    assert report.failed is False
    assert report.status == "SUCCEEDED"
    assert report.rows_written >= 2
    assert report.latest_observation == date(2026, 9, 4)
    from sqlalchemy import text

    with pg_engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM mi_eia_observations WHERE source_id='EIA_ENERGY'")).scalar()
    assert int(n) >= 2
