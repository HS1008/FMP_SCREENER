"""Streamlit provider-fetch default is refuse."""

from __future__ import annotations

from pathlib import Path

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
    engine = (ROOT / "db" / "dashboard_engine.py").read_text(encoding="utf-8")
    assert "def load_streamlit_env" in engine
    assert "load_dotenv" in engine
    assert allowed_dotenv == {ROOT / "db" / "dashboard_engine.py"}  # only helper may reload .env


def test_provider_fetch_opt_in(monkeypatch):
    monkeypatch.setenv("STREAMLIT_ALLOW_PROVIDER_FETCH", "1")
    assert provider_fetch_allowed() is True
    refuse_provider_fetch("FMP")
