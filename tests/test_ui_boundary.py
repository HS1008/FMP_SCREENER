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
    assert "strip_writer_database_env" in watchlist


def test_provider_fetch_opt_in(monkeypatch):
    monkeypatch.setenv("STREAMLIT_ALLOW_PROVIDER_FETCH", "1")
    assert provider_fetch_allowed() is True
    refuse_provider_fetch("FMP")
