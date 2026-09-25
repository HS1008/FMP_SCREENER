"""Cboe volatility adapter, metrics, and read-only Options page. No live Cboe calls."""

from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path

import pytest

from jobs.validate_cboe_live import EXIT_FAIL, EXIT_OK, validation_outcome
from market_intelligence import cboe_analytics as vol
from market_intelligence.cboe_client import (
    API_ROOT_DELAYED,
    API_ROOT_LIVE,
    UNDERLYING_QUOTES,
    CboeAuthError,
    CboeClient,
    CboeError,
    CboeMalformedPayload,
    CboeRateLimitError,
    CboeTrialLimitError,
    parse_cboe_body,
    probe_status,
    request_point_cost,
)
from market_intelligence.ingest_cboe import fetch_index_quotes

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 9, 24)


class _Resp:
    def __init__(self, body: bytes, headers=None, status=200):
        self._body = body
        self.headers = headers or {}
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(code: int, body: str = ""):
    import urllib.error

    return urllib.error.HTTPError("https://id.livevol.com/connect/token", code, "err", hdrs=None, fp=_Bytes(body.encode()))


class _Bytes:
    def __init__(self, raw: bytes):
        self._raw = raw

    def read(self, _n=-1):
        data, self._raw = self._raw, b""
        return data

    def close(self):
        return None


def _client(handler):
    return CboeClient("client-id", "client-secret", opener=handler, sleep=lambda _s: None, clock=lambda: 0.0)


def test_token_success_and_cache():
    calls = {"n": 0}

    def opener(request, timeout):
        calls["n"] += 1
        assert request.get_method() == "POST"
        assert request.get_header("Authorization").startswith("Basic ")
        assert b"client_secret" not in request.data
        assert b"grant_type=client_credentials" in request.data
        assert b"scope=api.allaccess" in request.data
        return _Resp(b'{"access_token":"tok-1","expires_in":3600,"token_type":"Bearer"}')

    client = _client(opener)
    assert client.access_token() == "tok-1"
    assert client.access_token() == "tok-1"
    assert calls["n"] == 1


def test_token_refresh_after_expiry():
    clock = {"t": 0.0}
    calls = {"n": 0}

    def opener(request, timeout):
        calls["n"] += 1
        token = "tok-{0}".format(calls["n"])
        return _Resp(json.dumps({"access_token": token, "expires_in": 30}).encode())

    client = CboeClient("client-id", "client-secret", opener=opener, sleep=lambda _s: None, clock=lambda: clock["t"])
    assert client.access_token() == "tok-1"
    clock["t"] = 100.0
    assert client.access_token() == "tok-2"
    assert calls["n"] == 2


def test_bad_credentials():
    def opener(request, timeout):
        raise _http_error(401, "invalid_client")

    with pytest.raises(CboeAuthError) as exc:
        _client(opener).access_token()
    assert exc.value.capability == "AUTH_FAILED"


def test_rate_limit_retries_then_raises():
    calls = {"n": 0}

    def opener(request, timeout):
        calls["n"] += 1
        raise _http_error(429, "rate limit")

    with pytest.raises(CboeRateLimitError):
        _client(opener).access_token()
    assert calls["n"] == 3


def test_trial_limit_does_not_retry():
    calls = {"n": 0}

    def opener(request, timeout):
        calls["n"] += 1
        raise _http_error(400, "trial point limit exceeded")

    with pytest.raises(CboeTrialLimitError):
        _client(opener).access_token()
    assert calls["n"] == 1


def test_malformed_token_payload():
    def opener(request, timeout):
        return _Resp(b"not-json")

    with pytest.raises(CboeMalformedPayload):
        _client(opener).access_token()


def test_probe_configuration_states():
    status, _reason, enabled = probe_status({})
    assert status == "CONFIGURATION_REQUIRED" and enabled is False
    status, _reason, enabled = probe_status({"CBOE_CLIENT_ID": "a", "CBOE_CLIENT_SECRET": "b"})
    assert status == "DISABLED" and enabled is False
    status, _reason, enabled = probe_status({"CBOE_CLIENT_ID": "a", "CBOE_CLIENT_SECRET": "b", "MI_CBOE_ENABLED": "1"})
    assert status == "CONFIGURED" and enabled is True


def _underlying_row(symbol, level, prev=None, iv30=None, stamp="15:59:00.000"):
    return {
        "symbol": symbol,
        "timestamp": stamp,
        "underlying_last_trade_price": level,
        "underlying_prev_day_close": prev,
        "iv30": iv30,
    }


def test_vix_and_term_structure_parsing():
    rows = [
        _underlying_row("VIX9D", 14.0, 13.0),
        _underlying_row("VIX", 16.0, 15.5, iv30=None),
        _underlying_row("VIX3M", 18.0, 18.2),
        _underlying_row("VIX6M", 19.0),
        _underlying_row("VIX1Y", 20.0),
        _underlying_row("SPX", 5700.0, iv30=15.5),
    ]
    snaps = vol.index_snapshots(rows, quote_date=AS_OF)
    assert snaps["VIX"]["level"] == 16.0
    assert snaps["VIX"]["observation_ts"]
    curve = vol.term_structure(snaps)
    assert curve["construction"] == "vix_index_tenor"
    assert curve["label"] == "VIX index term structure"
    assert [p["tenor"] for p in curve["points"]] == ["9D", "1M", "3M", "6M", "1Y"]
    assert curve["front_to_back_slope"] == pytest.approx(6.0)
    assert curve["curve_state"] == "upward_sloping"
    assert "contango" not in json.dumps(curve).lower()


def test_null_level_is_not_zero():
    snaps = vol.index_snapshots([{"symbol": "VIX", "underlying_last_trade_price": None, "timestamp": "15:00:00"}], quote_date=AS_OF)
    assert snaps["VIX"]["level"] is None


def _contract(side, strike, delta, iv, expiry="2026-10-24", bid=1.0, ask=1.2):
    return {
        "root": "SPX",
        "option_type": side,
        "strike": strike,
        "delta": delta,
        "mid_iv": iv,
        "expiry": expiry,
        "option": "SPX261024{0}{1:08d}".format(side, int(strike * 1000)),
        "option_bid": bid,
        "option_ask": ask,
        "open_interest": 10,
    }


def test_skew_selects_nearest_25_delta():
    payload = {
        "timestamp": "15:45:00.000",
        "symbol": "SPX",
        "underlying_last_trade_price": 5700,
        "options": [
            _contract("P", 5400, -0.10, 0.30),
            _contract("P", 5500, -0.24, 0.22),
            _contract("P", 5450, -0.40, 0.28),
            _contract("C", 5900, 0.24, 0.16),
            _contract("C", 6000, 0.10, 0.18),
        ],
    }
    result = vol.skew_from_chain(payload, as_of=AS_OF)
    assert result["status"] == "OK"
    assert result["put"]["strike"] == 5500
    assert result["call"]["strike"] == 5900
    assert result["put_iv"] == pytest.approx(22.0)
    assert result["call_iv"] == pytest.approx(16.0)
    assert result["skew"] == pytest.approx(6.0)
    assert result["expiry"] == "2026-10-24"
    assert result["dte"] == 30


def test_expiry_tie_prefers_later():
    chosen = vol.choose_expiry([date(2026, 10, 14), date(2026, 11, 3)], AS_OF)
    # 20 days vs 40 days, equal distance, later wins
    assert chosen == date(2026, 11, 3)


def test_missing_put_fails_closed():
    payload = {"timestamp": "15:45:00", "options": [_contract("C", 5900, 0.25, 0.16)]}
    result = vol.skew_from_chain(payload, as_of=AS_OF)
    assert result["status"] == "INCOMPLETE"
    assert result["skew"] is None
    assert result["reason"] == "missing_put"


def test_missing_delta_is_skipped():
    payload = {
        "options": [
            _contract("P", 5500, None, 0.22),
            _contract("C", 5900, 0.25, 0.16),
        ]
    }
    result = vol.skew_from_chain(payload, as_of=AS_OF)
    assert result["status"] == "INCOMPLETE"
    assert result["reason"] == "missing_put"


def test_missing_iv_is_not_zero():
    bad = _contract("P", 5500, -0.25, None)
    bad["iv"] = None
    bad["mid_iv"] = None
    assert vol.provider_iv(bad) is None
    assert vol.select_25_delta([bad, _contract("C", 5900, 0.25, 0.16)], side="P") is None


def test_rv20_and_spreads_are_distinct():
    closes = []
    price = 100.0
    for i in range(21):
        closes.append((date(2026, 8, 3 + i), price))
        price *= 1.01 if i % 2 == 0 else 0.995
    rv = vol.realized_vol_20(closes)
    assert rv["status"] == "OK"
    assert rv["value"] > 0
    iv30 = vol.iv_rv_spread(iv30=18.0, vix=16.0, rv=rv["value"])
    assert iv30["metric_id"] == "SPX_IV30_MINUS_SPX_RV20"
    vix_only = vol.iv_rv_spread(iv30=None, vix=16.0, rv=rv["value"])
    assert vix_only["metric_id"] == "VIX_MINUS_SPX_RV20"
    assert iv30["metric_id"] != vix_only["metric_id"]
    short = vol.realized_vol_20(closes[:5])
    assert short["value"] is None and short["status"] == "INCOMPLETE"


def test_options_page_is_read_only_source():
    page = (ROOT / "pages" / "21_Options_Volatility.py").read_text(encoding="utf-8")
    ui = (ROOT / "market_intelligence" / "pages_ui.py").read_text(encoding="utf-8")
    render = ui.split("def render_options_volatility()", 1)[1].split("\ndef _overview_displayed_dates", 1)[0]
    core = ui.split("def _render_cboe_core_panel", 1)[1].split("\ndef _render_volatility_panel", 1)[0]
    for blob in (page, render, core):
        assert "cboe_client" not in blob
        assert "urllib" not in blob
        assert "INSERT " not in blob and "UPDATE " not in blob
        assert "execute(" not in blob
    assert "load_optional" in render
    assert "mi_v_cboe" not in render  # the page goes through the read model, not SQL
    assert "never labeled contango or backwardation" in core.lower()


def test_options_page_renders_null_as_blank(monkeypatch):
    from streamlit.testing.v1 import AppTest

    core = {
        "available": True,
        "reason": None,
        "latest": [
            {"metric_id": "VIX_SPOT", "as_of": "2026-09-24", "value": None, "status": "INCOMPLETE", "units": "vol_points", "detail_json": {}, "ingested_at": "2026-09-24T20:00:00+00:00", "provider_observation_ts": None},
            {"metric_id": "SPX_25D_SKEW", "as_of": "2026-09-24", "value": None, "status": "INCOMPLETE", "units": "vol_points", "detail_json": {"reason": "missing_put"}},
        ],
        "history": [],
        "health": [{"freshness_dataset": "vix", "freshness_status": "STALE", "transport_status": "PARTIAL", "provider": "Cboe"}],
    }
    ctx = {
        "status": "OK",
        "reason": None,
        "symbols": [],
        "vix": None,
        "cboe_core": core,
        "last_attempts": [],
    }

    def _fake_load_optional(name, *args, **kwargs):
        if name == "options_volatility_context":
            return {"available": True, "data": ctx, "error": None}
        return {"available": False, "data": kwargs.get("default"), "error": "unused"}

    monkeypatch.setattr("market_intelligence.pages_ui.load_optional", _fake_load_optional)
    at = AppTest.from_file(str(ROOT / "pages" / "21_Options_Volatility.py"), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    text_blob = " ".join(str(item.value) for item in list(at.metric) + list(at.info) + list(at.caption) + list(at.warning))
    assert "—" in text_blob or "unavailable" in text_blob.lower()
    assert "0.00" not in text_blob
    assert "not a live quote" in text_blob.lower() or "not a live" in text_blob.lower()


def _token_then(body: bytes, headers=None):
    def opener(request, timeout):
        if request.get_method() == "POST":
            return _Resp(b'{"access_token":"tok-1","expires_in":3600,"token_type":"Bearer"}')
        return _Resp(body, headers=headers)

    return opener


def test_underlying_quotes_accepts_documented_xml():
    xml = (
        b'<symbols xmlns:i="http://www.w3.org/2001/XMLSchema-instance">'
        b"<symbol><symbol>VIX</symbol>"
        b"<underlying_last_trade_price>16.5</underlying_last_trade_price>"
        b"<timestamp>15:59:00.000</timestamp></symbol></symbols>"
    )
    client = _client(_token_then(xml, {"Content-Type": "application/xml"}))
    rows = client.underlying_quotes(["VIX"], AS_OF, session_date=AS_OF)
    assert rows[0]["symbol"] == "VIX"
    assert rows[0]["underlying_last_trade_price"] == 16.5
    assert client.requests_made == 1
    assert client.points_used == 8


def test_underlying_quotes_decodes_gzip_json():
    raw = gzip.compress(b'[{"symbol":"VIX","underlying_last_trade_price":16.5}]')
    client = _client(_token_then(raw, {"Content-Type": "application/json", "Content-Encoding": "gzip"}))
    rows = client.underlying_quotes(["VIX"], AS_OF, session_date=AS_OF)
    assert rows[0]["symbol"] == "VIX"
    assert rows[0]["underlying_last_trade_price"] == 16.5


def test_html_body_is_unavailable_with_diagnostic():
    with pytest.raises(CboeMalformedPayload) as exc:
        parse_cboe_body(b"<html>blocked</html>", content_type="text/html")
    assert exc.value.capability == "UNAVAILABLE"
    assert "HTML" in str(exc.value)


def test_empty_body_is_unavailable():
    with pytest.raises(CboeMalformedPayload) as exc:
        parse_cboe_body(b"", content_type="application/json")
    assert "empty" in str(exc.value)


def test_delayed_same_session_costs_eight_points():
    assert request_point_cost(UNDERLYING_QUOTES, historical=False, api_root=API_ROOT_DELAYED) == 8
    assert request_point_cost(UNDERLYING_QUOTES, historical=True, api_root=API_ROOT_DELAYED) == 3
    assert request_point_cost(UNDERLYING_QUOTES, historical=False, api_root=API_ROOT_LIVE) == 4


def test_fetch_index_quotes_skips_null_vix_to_prior_session():
    class _Fake:
        def underlying_quotes(self, symbols, quote_date, *, session_date):
            if quote_date == AS_OF:
                return [{"symbol": "VIX", "underlying_last_trade_price": None, "timestamp": "15:59:00"}]
            return [{"symbol": "VIX", "underlying_last_trade_price": 17.2, "timestamp": "15:59:00"}]

    snaps, quote_day = fetch_index_quotes(_Fake(), AS_OF)
    assert quote_day == date(2026, 9, 23)
    assert snaps["VIX"]["level"] == 17.2


def test_fetch_index_quotes_skips_transport_error():
    class _Fake:
        def underlying_quotes(self, symbols, quote_date, *, session_date):
            if quote_date == AS_OF:
                raise CboeError("UNAVAILABLE", "Cboe response was not JSON")
            return [{"symbol": "VIX", "underlying_last_trade_price": 18.0, "timestamp": "15:59:00"}]

    snaps, quote_day = fetch_index_quotes(_Fake(), AS_OF)
    assert quote_day < AS_OF
    assert snaps["VIX"]["level"] == 18.0


def test_live_validation_requires_ok_metrics():
    status, code = validation_outcome(
        {
            "auth": "SUCCEEDED",
            "views_ok": True,
            "metrics_ok": [],
            "metrics_incomplete": [{"metric_id": "SPX_REALIZED_VOL_20D", "status": "INCOMPLETE"}],
            "entitlement": "UNAVAILABLE",
        }
    )
    assert status == "FAILED" and code == EXIT_FAIL
    status, code = validation_outcome(
        {
            "auth": "SUCCEEDED",
            "views_ok": True,
            "metrics_ok": ["VIX_SPOT"],
            "entitlement": "READY",
        }
    )
    assert status == "OK" and code == EXIT_OK
    status, code = validation_outcome(
        {
            "auth": "SUCCEEDED",
            "views_ok": True,
            "metrics_ok": ["VIX_SPOT"],
            "entitlement": "UNAVAILABLE",
        }
    )
    assert status == "PARTIAL" and code == EXIT_OK
