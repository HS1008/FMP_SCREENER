"""FINRA Query API client unit tests (no live network, no secrets)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from market_intelligence.finra_catalog import CORPORATE_BREADTH, FINRA_API_BASE, FINRA_TOKEN_URL
from market_intelligence.finra_client import FinraClient, FinraError, credentials_from_env, redact


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, status: int, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = ""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, headers=None, timeout=None, json=None):
        return self.request("POST", url, headers=headers, json=json, timeout=timeout)

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "headers": dict(headers or {})})
        if not self._responses:
            raise AssertionError("unexpected request {0} {1}".format(method, url))
        return self._responses.pop(0)


def test_credentials_accept_both_secret_name_pairs():
    assert credentials_from_env({"FINRA_API_CLIENT_ID": "id", "FINRA_API_CLIENT_SECRET": "secret"}) == ("id", "secret")
    assert credentials_from_env({"FINRA_CLIENT_ID": "a", "FINRA_CLIENT_SECRET": "b"}) == ("a", "b")


def test_redact_masks_basic_bearer_and_secrets():
    text = redact("Authorization: Bearer abcdef Authorization: Basic xyz id=client-id", "client-id", "abcdef")
    assert "abcdef" not in text and "client-id" not in text
    assert "[REDACTED]" in text


def test_authenticate_success_versus_401_configuration():
    session = FakeSession([FakeResponse(200, {"access_token": "tok", "expires_in": 3600})])
    client = FinraClient("id", "secret", session=session, sleep=lambda _s: None)
    assert client.authenticate() == "tok"
    session401 = FakeSession([FakeResponse(401, {"error": "invalid_client"})])
    bad = FinraClient("id", "secret", session=session401, sleep=lambda _s: None)
    with pytest.raises(FinraError) as exc:
        bad.authenticate()
    assert exc.value.status == 401
    assert exc.value.capability == "CONFIGURATION_REQUIRED"


def test_dataset_403_is_entitlement_not_auth_success():
    session = FakeSession(
        [
            FakeResponse(200, {"access_token": "tok", "expires_in": 3600}),
            FakeResponse(403, {"message": "forbidden"}),
        ]
    )
    client = FinraClient("id", "secret", session=session, sleep=lambda _s: None)
    probe = client.probe_dataset(CORPORATE_BREADTH, retrieved_at="2026-09-09T00:00:00+00:00")
    assert probe.capability_status == "ENTITLEMENT_REQUIRED"
    assert probe.http_status == 403
    assert probe.record_count is None


def test_pagination_and_inclusive_date_window():
    page1 = [{"tradeReportDate": "2026-01-02", "productCategory": "all securities", "totalVolume": 1, "totalTrades": 1}]
    page2 = [{"tradeReportDate": "2026-01-03", "productCategory": "all securities", "totalVolume": 2, "totalTrades": 2}]
    session = FakeSession(
        [
            FakeResponse(200, {"access_token": "tok", "expires_in": 3600}),
            FakeResponse(200, page1),
            FakeResponse(200, page2),
            FakeResponse(200, []),
        ]
    )
    client = FinraClient("id", "secret", session=session, sleep=lambda _s: None)
    rows = client.query_all(CORPORATE_BREADTH, start=date(2026, 1, 2), end=date(2026, 1, 3), limit=1, max_pages=5)
    assert len(rows) == 2
    data_calls = [c for c in session.calls if c["url"].startswith(FINRA_API_BASE)]
    assert data_calls[0]["json"]["compareFilters"] == [
        {"compareType": "greater", "fieldName": "tradeReportDate", "fieldValue": "2026-01-01"},
        {"compareType": "lesser", "fieldName": "tradeReportDate", "fieldValue": "2026-01-04"},
    ]
    assert data_calls[1]["json"]["offset"] == 1


def test_pagination_truncation_is_an_error_not_silent_success():
    page = [{"tradeReportDate": "2026-01-02", "productCategory": "all securities", "totalVolume": 1}]
    session = FakeSession(
        [
            FakeResponse(200, {"access_token": "tok", "expires_in": 3600}),
            FakeResponse(200, page),
        ]
    )
    client = FinraClient("id", "secret", session=session, sleep=lambda _s: None)
    with pytest.raises(FinraError) as exc:
        client.query_all(CORPORATE_BREADTH, start=date(2026, 1, 2), end=date(2026, 1, 3), limit=1, max_pages=1)
    assert "pagination incomplete" in str(exc.value).lower()
    assert exc.value.capability == "TEMPORARILY_UNAVAILABLE"


def test_malformed_array_entries_reject_the_page():
    session = FakeSession(
        [
            FakeResponse(200, {"access_token": "tok", "expires_in": 3600}),
            FakeResponse(
                200,
                [
                    {"tradeReportDate": "2026-01-02", "productCategory": "all securities", "totalVolume": 1},
                    "garbage",
                    None,
                    {"tradeReportDate": "2026-01-03", "productCategory": "all securities", "totalVolume": 2},
                ],
            ),
        ]
    )
    client = FinraClient("id", "secret", session=session, sleep=lambda _s: None)
    with pytest.raises(FinraError) as exc:
        client.query_page(CORPORATE_BREADTH, limit=5, offset=0)
    assert "malformed array entries" in str(exc.value).lower()
    assert exc.value.capability == "TEMPORARILY_UNAVAILABLE"


def test_wrapped_list_with_malformed_entries_is_also_rejected():
    session = FakeSession(
        [
            FakeResponse(200, {"access_token": "tok", "expires_in": 3600}),
            FakeResponse(200, {"data": [{"tradeReportDate": "2026-01-02"}, 3]}),
        ]
    )
    client = FinraClient("id", "secret", session=session, sleep=lambda _s: None)
    with pytest.raises(FinraError) as exc:
        client.query_page(CORPORATE_BREADTH, limit=5, offset=0)
    assert "malformed" in str(exc.value).lower()


def test_unexpected_object_payload_is_not_an_empty_success():
    session = FakeSession(
        [
            FakeResponse(200, {"access_token": "tok", "expires_in": 3600}),
            FakeResponse(200, {"unexpected": True}),
        ]
    )
    client = FinraClient("id", "secret", session=session, sleep=lambda _s: None)
    with pytest.raises(FinraError) as exc:
        client.query_page(CORPORATE_BREADTH, limit=5, offset=0)
    assert "unexpected" in str(exc.value).lower()


def test_client_source_does_not_call_traqs_or_guess_trace_tape():
    text = (ROOT / "market_intelligence" / "finra_client.py").read_text(encoding="utf-8")
    ingest = (ROOT / "market_intelligence" / "ingest_finra.py").read_text(encoding="utf-8")
    assert "apidownload.finratraqs.org" not in text
    assert "apidownload.finratraqs.org" not in ingest
    assert FINRA_TOKEN_URL.startswith("https://ews.fip.finra.org/")
    assert "Does not call TRAQS" in text
