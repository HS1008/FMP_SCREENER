"""Yahoo volatility analytics, ingest parsing, and Options page boundary. No live Yahoo."""

from __future__ import annotations

import math
import statistics
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from market_intelligence import yahoo_vol as vol
from market_intelligence.ingest_yahoo_vol import fetch_yahoo_closes, ingest_yahoo_vol

ROOT = Path(__file__).resolve().parents[1]


def test_realized_vol_20_sample_stdev_annualized():
    # Known 21 closes → 20 log returns with sample stdev = 0.01 exactly when returns are [0.01]*20.
    # Constant returns → sample stdev 0; use a single-step impulse then flat to keep math simple.
    closes = [100.0]
    for _ in range(19):
        closes.append(closes[-1] * math.exp(0.01))
    closes.append(closes[-1] * math.exp(0.02))  # last return differs → nonzero sample stdev
    out = vol.realized_vol_20(closes)
    assert out["status"] == "OK"
    assert out["window"] == 20
    assert out["underlying"] == "GSPC"
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    expected = statistics.stdev(returns) * math.sqrt(252) * 100.0
    assert out["value"] == pytest.approx(expected, rel=1e-9)


def test_realized_vol_20_incomplete_and_nulls():
    assert vol.realized_vol_20([1, 2, None, 3] * 3)["status"] == "INCOMPLETE"
    assert vol.realized_vol_20([100.0] * 10)["value"] is None
    assert vol.realized_vol_20([])["reason"] == "fewer_than_21_closes"


def test_vix_minus_rv_null_handling():
    rv_ok = {"status": "OK", "value": 12.0, "reason": None}
    assert vol.vix_minus_rv(None, rv_ok)["value"] is None
    assert vol.vix_minus_rv(20.0, {"status": "INCOMPLETE", "value": None, "reason": "x"})["value"] is None
    spread = vol.vix_minus_rv(20.0, rv_ok)
    assert spread["status"] == "OK"
    assert spread["value"] == pytest.approx(8.0)
    assert spread["metric_id"] == "VIX_MINUS_GSPC_RV20"
    assert spread["underlying"] == "GSPC"


def test_term_structure_slope_labels_not_contango():
    levels = {"^VIX9D": 14.0, "^VIX": 15.0, "^VIX3M": 16.0, "^VIX6M": 17.0, "^VIX1Y": 18.0}
    curve = vol.term_structure(levels)
    assert curve["curve_state"] == "upward_sloping"
    assert curve["curve_state"] not in {"contango", "backwardation"}
    flat = vol.term_structure({"^VIX9D": 15.0, "^VIX": 15.02, "^VIX3M": None, "^VIX6M": None, "^VIX1Y": 15.01})
    assert flat["curve_state"] == "flat"
    down = vol.term_structure({"^VIX9D": 20.0, "^VIX": 18.0, "^VIX3M": 16.0, "^VIX6M": 15.0, "^VIX1Y": 14.0})
    assert down["curve_state"] == "downward_sloping"
    missing = vol.term_structure({})
    assert missing["curve_state"] is None
    assert all(p["level"] is None for p in missing["points"])
    assert "Never labeled contango/backwardation" in curve["note"]


def test_positive_number_rejects_zero_nan():
    assert vol.positive_number(0) is None
    assert vol.positive_number(-1) is None
    assert vol.positive_number(float("nan")) is None
    assert vol.positive_number("12.5") == pytest.approx(12.5)


def test_fetch_yahoo_closes_parses_history_frame():
    idx = pd.to_datetime(["2026-09-22", "2026-09-23"])
    frame = pd.DataFrame({"Close": [15.2, 16.1]}, index=idx)

    class _Ticker:
        def history(self, **_kwargs):
            return frame

    with patch("yfinance.Ticker", return_value=_Ticker()):
        rows = fetch_yahoo_closes("^VIX", start=date(2026, 9, 1), end=date(2026, 9, 23))
    assert rows == [(date(2026, 9, 22), 15.2), (date(2026, 9, 23), 16.1)]


def test_fetch_yahoo_closes_empty_on_blank():
    class _Ticker:
        def history(self, **_kwargs):
            return pd.DataFrame()

    with patch("yfinance.Ticker", return_value=_Ticker()):
        assert fetch_yahoo_closes("^SKEW", start=date(2026, 9, 1), end=date(2026, 9, 23)) == []


def test_ingest_yahoo_vol_writes_metrics_with_mocked_fetch():
    engine = MagicMock()
    begin = MagicMock()
    engine.begin.return_value.__enter__.return_value = begin
    engine.begin.return_value.__exit__.return_value = False

    def fake_fetch(ticker, *, start, end):
        day = date(2026, 9, 23)
        if ticker == "^GSPC":
            return [(day.fromordinal(day.toordinal() - 21 + i), 5000.0 + i) for i in range(22)]
        if ticker == "^VIX":
            return [(day, 18.5)]
        if ticker == "^SKEW":
            return [(day, 135.0)]
        # tenors
        return [(day, 17.0 + hash(ticker) % 5)]

    with (
        patch("market_intelligence.ingest_yahoo_vol.fetch_yahoo_closes", side_effect=fake_fetch),
        patch("market_intelligence.ingest_yahoo_vol.start_run", return_value="run-1"),
        patch("market_intelligence.ingest_yahoo_vol.finish_run"),
        patch("market_intelligence.ingest_yahoo_vol.record_freshness"),
        patch("market_intelligence.ingest_yahoo_vol._ensure_source"),
        patch("market_intelligence.ingest_yahoo_vol._write_rows", return_value=1) as write,
    ):
        report = ingest_yahoo_vol(engine, today=date(2026, 9, 23))
    assert report["failed"] is False
    assert report["sections"]["vix"]["status"] == "SUCCEEDED"
    assert report["sections"]["skew"]["status"] == "SUCCEEDED"
    assert write.called


def test_options_page_shows_yahoo_core_without_provider_imports(monkeypatch):
    yahoo = {
        "status": "OK",
        "vix": {"metric_id": "VIX_SPOT", "value": 18.5, "status": "OK", "as_of": "2026-09-23"},
        "skew": {"metric_id": "SKEW_INDEX", "value": 135.0, "status": "OK", "as_of": "2026-09-23"},
        "spread": {"metric_id": "VIX_MINUS_GSPC_RV20", "value": 4.0, "status": "OK", "as_of": "2026-09-23"},
        "slope": {"metric_id": "VIX_INDEX_FRONT_TO_BACK", "value": 1.2, "status": "OK", "as_of": "2026-09-23"},
        "rv20": {"metric_id": "GSPC_REALIZED_VOL_20D", "value": 14.5, "status": "OK", "as_of": "2026-09-23"},
        "curve_state": "upward_sloping",
        "unavailable_tenors": [],
        "tenors": [
            {"tenor": "9D", "yahoo_ticker": "^VIX9D", "value": 17.0, "as_of": "2026-09-23", "status": "OK"},
            {"tenor": "1M", "yahoo_ticker": "^VIX", "value": 18.5, "as_of": "2026-09-23", "status": "OK"},
            {"tenor": "3M", "yahoo_ticker": "^VIX3M", "value": 19.0, "as_of": "2026-09-23", "status": "OK"},
            {"tenor": "6M", "yahoo_ticker": "^VIX6M", "value": 19.5, "as_of": "2026-09-23", "status": "OK"},
            {"tenor": "1Y", "yahoo_ticker": "^VIX1Y", "value": 20.0, "as_of": "2026-09-23", "status": "OK"},
        ],
        "history": {
            "VIX_SPOT": [{"as_of": "2026-09-22", "value": 18.0}, {"as_of": "2026-09-23", "value": 18.5}],
            "SKEW_INDEX": [{"as_of": "2026-09-22", "value": 134.0}, {"as_of": "2026-09-23", "value": 135.0}],
            "VIX_MINUS_GSPC_RV20": [{"as_of": "2026-09-23", "value": 4.0}],
            "GSPC_REALIZED_VOL_20D": [{"as_of": "2026-09-23", "value": 14.5}],
        },
    }

    def fake_cached(fn_name, *args, **kwargs):
        if fn_name == "options_volatility_context":
            return {
                "status": "OK",
                "symbols": [],
                "vix": None,
                "yahoo_core": yahoo,
                "reason": "No published OpenBB/Cboe snapshots",
            }
        if fn_name == "options_chain_details":
            return []
        raise RuntimeError(fn_name)

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_cached)
    page = (ROOT / "pages" / "21_Options_Volatility.py").read_text(encoding="utf-8")
    ui = (ROOT / "market_intelligence" / "pages_ui.py").read_text(encoding="utf-8")
    for blob in (page, ui):
        assert "yfinance" not in blob
        assert "ingest_yahoo_vol" not in blob
        assert "cboe_client" not in blob
    at = AppTest.from_file(str(ROOT / "pages" / "21_Options_Volatility.py"), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    text = " ".join(str(getattr(el, "value", el)) for kind in ("title", "subheader", "caption", "markdown", "info") for el in getattr(at, kind))
    assert "Cboe SKEW Index" in text
    assert "Core volatility" in text
    assert "25Δ SPX skew" not in text or "Options chains" in text
    assert at.metric


def test_options_page_nulls_stay_unavailable(monkeypatch):
    def fake_cached(fn_name, *args, **kwargs):
        if fn_name == "options_volatility_context":
            return {
                "status": "UNAVAILABLE",
                "symbols": [],
                "vix": None,
                "yahoo_core": {"status": "UNAVAILABLE", "reason": "No Yahoo volatility metrics published yet."},
                "reason": "empty",
            }
        raise RuntimeError(fn_name)

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_cached)
    at = AppTest.from_file(str(ROOT / "pages" / "21_Options_Volatility.py"), default_timeout=30)
    at.run()
    assert not at.exception
    text = " ".join(str(el.value) for el in at.info)
    assert "unavailable" in text.lower() or "No Yahoo" in text
