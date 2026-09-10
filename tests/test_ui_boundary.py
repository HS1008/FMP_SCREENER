"""Streamlit provider-fetch default is refuse."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from qc_research.ui_boundary import provider_fetch_allowed, refuse_provider_fetch

ROOT = Path(__file__).resolve().parents[1]


def test_provider_fetch_denied_by_default(monkeypatch):
    monkeypatch.delenv("STREAMLIT_ALLOW_PROVIDER_FETCH", raising=False)
    assert provider_fetch_allowed() is False
    with pytest.raises(RuntimeError, match="FMP"):
        refuse_provider_fetch("FMP")


def test_eia_watchlist_uses_cache_only_when_fetch_denied(monkeypatch):
    monkeypatch.delenv("STREAMLIT_ALLOW_PROVIDER_FETCH", raising=False)
    import power_producer_watchlist as watchlist

    monkeypatch.setattr(
        watchlist.eia_wholesale,
        "load_cached_only",
        lambda: None,
    )

    def _fail(**kwargs):
        raise AssertionError("live EIA fetch must not run")

    monkeypatch.setattr(watchlist.eia_wholesale, "load_cached_or_fetch_eia_data", _fail)
    power, gas, sample = watchlist.load_market_data(force_refresh=True)
    assert power.empty and gas.empty and sample is False


def test_power_producer_strips_writer_after_dotenv():
    watchlist = (ROOT / "power_producer_watchlist.py").read_text(encoding="utf-8")
    assert "load_streamlit_env" in watchlist
    assert watchlist.count("load_dotenv(") == 0
    sector = (ROOT / "sector_dashboard_ui.py").read_text(encoding="utf-8")
    assert "load_streamlit_env" in sector
    assert "provider_fetch_allowed" in sector
    assert "def _streamlit_http_session" in sector
    assert sector.count("load_dotenv(") == 0
    scratch = (ROOT / "scratch_dashboard.py").read_text(encoding="utf-8")
    assert "load_streamlit_env" in scratch
    assert "provider_fetch_allowed" in scratch
    assert "def _cached_fmp_price_history" in scratch
    assert scratch.count("load_dotenv(") == 0
    launcher = (ROOT / "run_scratch_dashboard.py").read_text(encoding="utf-8")
    assert "load_streamlit_env" in launcher
    assert launcher.count("load_dotenv(") == 0


def test_streamlit_entrypoints_strip_writer_and_never_call_load_dotenv():
    allowed_dotenv = {ROOT / "db" / "dashboard_engine.py"}
    streamlit_roots = (
        ROOT / "dashboard.py",
        ROOT / "power_producer_watchlist.py",
        ROOT / "sector_dashboard_ui.py",
        ROOT / "scratch_dashboard.py",
        ROOT / "run_scratch_dashboard.py",
        *(ROOT / "pages").glob("*.py"),
    )
    for path in streamlit_roots:
        text = path.read_text(encoding="utf-8")
        assert "load_dotenv(" not in text, f"{path.name} must not call load_dotenv"
        assert (
            "strip_writer_database_env" in text or "load_streamlit_env" in text
        ), f"{path.name} must strip writer credentials"
        if path.parent.name == "pages":
            assert "load_streamlit_env" in text, f"{path.name} must clear writer-fallback flags"
    engine = (ROOT / "db" / "dashboard_engine.py").read_text(encoding="utf-8")
    assert "def load_streamlit_env" in engine
    assert "load_dotenv" in engine
    assert allowed_dotenv == {ROOT / "db" / "dashboard_engine.py"}  # only helper may reload .env
    dashboard = (ROOT / "dashboard.py").read_text(encoding="utf-8")
    main = dashboard.split("def main()", 1)[1].split("\n\n", 1)[0]
    assert "load_streamlit_env()" in main
    assert "if api_key and _background_warm_enabled():" in dashboard
    assert "def _background_warm_enabled" in dashboard


def test_provider_fetch_opt_in(monkeypatch):
    monkeypatch.setenv("STREAMLIT_ALLOW_PROVIDER_FETCH", "1")
    assert provider_fetch_allowed() is True
    refuse_provider_fetch("FMP")


def test_scratch_uses_yahoo_cache_when_fetch_denied(monkeypatch, tmp_path):
    monkeypatch.delenv("STREAMLIT_ALLOW_PROVIDER_FETCH", raising=False)
    import scratch_dashboard as scratch

    monkeypatch.setattr(scratch, "YAHOO_CACHE_DIR", tmp_path)
    dates = pd.bdate_range("2020-01-02", periods=10)
    scratch._write_yahoo_cache(
        "SPY", pd.Series([float(100 + i) for i in range(10)], index=dates)
    )

    def _boom(*_args, **_kwargs):
        raise AssertionError("live Yahoo fetch must not run")

    monkeypatch.setattr(scratch, "_provider_fetch_allowed", lambda: False)
    prices, err = scratch.fetch_yahoo_price_history(
        "SPY", date(2020, 1, 2), date(2020, 1, 20), force_refresh=True
    )
    assert err == ""
    assert len(prices) >= 5
    missing, miss_err = scratch.fetch_yahoo_price_history(
        "ZZZZ", date(2020, 1, 2), date(2020, 1, 20)
    )
    assert missing.empty
    assert "provider fetch disabled" in miss_err
    monkeypatch.setitem(
        __import__("sys").modules,
        "yfinance",
        type("YF", (), {"download": staticmethod(_boom), "Ticker": staticmethod(_boom)})(),
    )
    empty_holdings = scratch._parse_yahoo_fund_holdings("SPY")
    assert empty_holdings.empty


def test_scratch_uses_fmp_price_cache_when_fetch_denied(monkeypatch):
    monkeypatch.delenv("STREAMLIT_ALLOW_PROVIDER_FETCH", raising=False)
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    import data_loader
    import scratch_dashboard as scratch

    cached = pd.DataFrame(
        {
            "date": pd.bdate_range("2020-01-02", periods=10),
            "close": [float(100 + i) for i in range(10)],
        }
    )
    monkeypatch.setattr(data_loader, "_read_price_cache", lambda _sym: cached)

    def _fail(*_args, **_kwargs):
        raise AssertionError("live FMP fetch must not run")

    monkeypatch.setattr(data_loader, "get_price_history", _fail)
    monkeypatch.setattr(data_loader, "create_http_session", _fail)
    monkeypatch.setattr(data_loader, "_fmp_get", _fail)
    prices, err = scratch.fetch_fmp_price_history(
        "SPY", date(2020, 1, 2), date(2020, 1, 20), force_refresh=True
    )
    assert err == ""
    assert len(prices) >= 5
    holdings, hold_err = scratch.fetch_fmp_underlying_holdings("SPY")
    assert holdings.empty
    assert "provider fetch disabled" in hold_err
