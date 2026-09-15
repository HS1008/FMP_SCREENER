"""Focused provider integration regressions: SEC, OpenFIGI, protected env, EMMA Data Health filter."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest

from market_intelligence import adapters
from market_intelligence.openfigi_client import OpenFIGIClient, classify_mapping
from market_intelligence.pages_ui import _DATA_HEALTH_HIDDEN_SOURCES
from scripts.protected_env import upsert_if_placeholder


class _FakeResponse:
    def __init__(self, payload, *, url="https://data.sec.gov/submissions/CIK0000320193.json", headers=None):
        self._payload = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode("utf-8")
        self._url = url
        self.headers = headers or {}

    def read(self):
        return self._payload

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_sec_missing_user_agent_does_not_fetch():
    calls = []

    def opener(request, timeout=0):
        calls.append(request)
        raise AssertionError("must not fetch")

    adapter = adapters.EdgarAdapter(opener=opener, min_interval_s=0, sleeper=lambda _: None)
    with pytest.raises(adapters.AdapterDisabled):
        adapter.submissions("320193", env={})
    assert calls == []
    probe = adapter.probe({})
    assert probe.access_status == adapters.ACCESS_CONFIGURATION_REQUIRED


def test_sec_header_propagation_and_success():
    seen = {}

    def opener(request, timeout=0):
        seen["ua"] = request.get_header("User-agent") or request.get_header("User-Agent")
        return _FakeResponse({"cik": "0000320193", "name": "Apple Inc.", "filings": {"recent": {}}})

    env = {"SEC_USER_AGENT": "FMP Research ops@example.com", "MI_EDGAR_ENABLED": "1"}
    adapter = adapters.EdgarAdapter(opener=opener, min_interval_s=0, sleeper=lambda _: None)
    payload = adapter.submissions("320193", env=env)
    assert payload["cik"] == "0000320193"
    assert seen["ua"] == "FMP Research ops@example.com"


def test_sec_process_wide_rate_limit_uses_fake_clock():
    sleeps = []
    clock = {"t": 0.0}
    adapters._EDGAR_LAST_REQUEST_MONOTONIC = -1.0

    def now():
        return clock["t"]

    def sleeper(dt):
        sleeps.append(dt)
        clock["t"] += dt

    a = adapters.EdgarAdapter(opener=lambda *a, **k: None, min_interval_s=0.2, sleeper=sleeper, clock=now)
    b = adapters.EdgarAdapter(opener=lambda *a, **k: None, min_interval_s=0.2, sleeper=sleeper, clock=now)
    a._acquire_rate_slot()
    clock["t"] += 0.01
    b._acquire_rate_slot()
    assert any(x >= 0.19 for x in sleeps)


def test_sec_retry_after_respected_then_success():
    attempts = {"n": 0}
    sleeps = []

    def opener(request, timeout=0):
        attempts["n"] += 1
        if attempts["n"] == 1:
            import urllib.error

            raise urllib.error.HTTPError(
                request.full_url,
                429,
                "slow",
                {"Retry-After": "0.05"},
                BytesIO(b""),
            )
        return _FakeResponse({"cik": "0000320193"})

    env = {"SEC_USER_AGENT": "FMP Research ops@example.com", "MI_EDGAR_ENABLED": "1"}
    adapter = adapters.EdgarAdapter(opener=opener, min_interval_s=0, sleeper=sleeps.append, max_attempts=3)
    payload = adapter.submissions("320193", env=env)
    assert payload["cik"] == "0000320193"
    assert any(abs(x - 0.05) < 1e-9 for x in sleeps)


def test_sec_404_not_retried():
    attempts = {"n": 0}

    def opener(request, timeout=0):
        attempts["n"] += 1
        import urllib.error

        raise urllib.error.HTTPError(request.full_url, 404, "no", {}, BytesIO(b""))

    env = {"SEC_USER_AGENT": "FMP Research ops@example.com", "MI_EDGAR_ENABLED": "1"}
    adapter = adapters.EdgarAdapter(opener=opener, min_interval_s=0, sleeper=lambda _: None, max_attempts=3)
    with pytest.raises(RuntimeError, match="HTTP 404"):
        adapter.submissions("320193", env=env)
    assert attempts["n"] == 1


def test_openfigi_ambiguous_stays_unresolved():
    payload = [{"data": [{"figi": "BBG1", "ticker": "A"}, {"figi": "BBG2", "ticker": "B"}]}]

    def opener(request, timeout=0):
        assert request.get_header("X-openfigi-apikey") == "fake-figi-canary"
        return _FakeResponse(payload, url="https://api.openfigi.com/v3/mapping")

    rows = OpenFIGIClient("fake-figi-canary", opener=opener, min_interval_s=0).map_jobs(
        [{"idType": "TICKER", "idValue": "AAA", "exchCode": "US"}]
    )
    assert rows[0]["status"] == "AMBIGUOUS"
    assert rows[0]["chosen"] is None
    assert classify_mapping({"data": [{"figi": "X"}]}, None) == "MATCH"


def test_protected_env_sec_user_agent_allows_spaces_and_preserves():
    updated, action = upsert_if_placeholder("", "SEC_USER_AGENT", "FMP Research ops@example.com")
    assert action == "inserted"
    assert 'SEC_USER_AGENT="FMP Research ops@example.com"' in updated
    preserved, action2 = upsert_if_placeholder(updated, "SEC_USER_AGENT", "Other Agent other@example.com")
    assert action2 == "preserved"
    assert "Other Agent" not in preserved


def test_protected_env_refuses_empty_clobber_semantics_for_api_key():
    text = "EIA_API_KEY=already-real-looking-key\n"
    with pytest.raises(ValueError):
        upsert_if_placeholder(text, "EIA_API_KEY", "has space")
    updated, action = upsert_if_placeholder(text, "EIA_API_KEY", "new-key", rotate=False)
    assert action == "preserved"
    assert updated == text


def test_emma_hidden_from_data_health_constant():
    assert "MSRB_EMMA" in _DATA_HEALTH_HIDDEN_SOURCES


def test_eia_key_presence_is_configured_not_available():
    status = adapters.EiaEnergyAdapter().probe({"EIA_API_KEY": "fake-eia-canary"})
    assert status.access_status == adapters.ACCESS_CONFIGURED
    assert status.enabled is True


def test_ibkr_corp_bond_evidence_expiry(tmp_path: Path, monkeypatch):
    evidence = {
        "observed_at": "2000-01-01T00:00:00+00:00",
        "discovery_classification": "IDENTIFIER_RESOLVED_QUOTE_AVAILABLE",
        "quote_classification": "LIVE_BID_ASK",
        "ratings_classification": "RATINGS_ABSENT",
    }
    path = tmp_path / "bond_capability_latest.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    status = adapters.IBKRCorporateBondsAdapter().probe({"MI_IBKR_BOND_CAPABILITY_EVIDENCE": str(path)})
    assert status.access_status == adapters.ACCESS_ENTITLEMENT_REQUIRED
    assert "No fresh" in status.reason or "fresh" in status.reason.lower() or "did not resolve" in status.reason.lower() or "No fresh" in status.reason or status.capabilities.get("discovery") == "unproven_this_session"
