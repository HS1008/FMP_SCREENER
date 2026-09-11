"""Streamlit provider-fetch default is refuse."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import os

import pandas as pd
import pytest

from qc_research.ui_boundary import (
    ensure_streamlit_cache_dir,
    provider_fetch_allowed,
    refuse_provider_fetch,
    streamlit_filesystem_write_allowed,
)

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
        ROOT / "qc_research" / "preview_platform_monitor.py",
    )
    for path in streamlit_roots:
        text = path.read_text(encoding="utf-8")
        assert "load_dotenv(" not in text, f"{path.name} must not call load_dotenv"
        assert (
            "strip_writer_database_env" in text or "load_streamlit_env" in text
        ), f"{path.name} must strip writer credentials"
        if path.parent.name == "pages" or path.name == "preview_platform_monitor.py":
            assert "load_streamlit_env" in text, f"{path.name} must clear writer-fallback flags"
    engine = (ROOT / "db" / "dashboard_engine.py").read_text(encoding="utf-8")
    assert "def load_streamlit_env" in engine
    assert "load_dotenv" in engine
    assert allowed_dotenv == {ROOT / "db" / "dashboard_engine.py"}  # only helper may reload .env
    dashboard = (ROOT / "dashboard.py").read_text(encoding="utf-8")
    main = dashboard.split("def main()", 1)[1].split("\n\n", 1)[0]
    assert "load_streamlit_env()" in main
    assert "import data_loader" not in dashboard
    assert "import tech_rotation_engine" not in dashboard
    legacy = (ROOT / "legacy_fmp_dashboard.py").read_text(encoding="utf-8")
    assert "if api_key and _background_warm_enabled():" in legacy
    assert "def _background_warm_enabled" in legacy


FORBIDDEN_PROVIDER_IMPORTS = (
    "import yfinance",
    "from yfinance",
    "import ib_insync",
    "from ib_insync",
    "quantconnect.com",
    "from data_sources.fred",
    "import data_sources.fred",
    "from data_sources.finra",
    "EIA_API",
    # Post-FMP producers: ingestion belongs to jobs/, never to page render.
    "market_intelligence.treasury_xml",
    "market_intelligence.ingest_treasury",
    "market_intelligence.equity_eod",
    "market_intelligence.fred_client",
    "market_intelligence.finra_client",
    "ai_gateway",
    "urllib.request",
)


def test_market_intelligence_pages_read_postgresql_only():
    """MI page modules render DB reads; Treasury/FRED/FINRA/equity producers and the gateway are not imported."""
    modules = [
        ROOT / "market_intelligence" / "pages_ui.py",
        ROOT / "market_intelligence" / "ui.py",
        ROOT / "market_intelligence" / "page_registry.py",
        ROOT / "dashboard.py",
    ]
    for path in modules:
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_PROVIDER_IMPORTS:
            assert needle not in text, "{0} imports producer path {1}".format(path.name, needle)
        assert "financialmodelingprep" not in text.lower()
    ui = (ROOT / "market_intelligence" / "ui.py").read_text(encoding="utf-8")
    assert "Reloads cached database reads only" in ui


def test_production_streamlit_pages_do_not_import_provider_clients():
    production = [
        *(ROOT / "pages").glob("*.py"),
        ROOT / "qc_research" / "ml_monitor_ui.py",
        ROOT / "qc_research" / "research_readout.py",
        ROOT / "qc_research" / "preview_platform_monitor.py",
    ]
    for path in production:
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_PROVIDER_IMPORTS:
            assert needle not in text, "{0} imports provider path {1}".format(path.name, needle)
        assert "object_get(" not in text
    dashboard = (ROOT / "dashboard.py").read_text(encoding="utf-8")
    legacy = (ROOT / "legacy_fmp_dashboard.py").read_text(encoding="utf-8")
    assert "provider_fetch_allowed" in legacy or "refuse_provider_fetch" in legacy
    assert "fmp_free_mode" in dashboard
    assert "STREAMLIT_ALLOW_PROVIDER_FETCH" in (ROOT / "qc_research" / "ui_boundary.py").read_text(
        encoding="utf-8"
    )


def test_provider_fetch_opt_in(monkeypatch):
    monkeypatch.setenv("STREAMLIT_ALLOW_PROVIDER_FETCH", "1")
    assert provider_fetch_allowed() is True
    refuse_provider_fetch("FMP")


def test_scratch_uses_yahoo_cache_when_fetch_denied(monkeypatch, tmp_path):
    monkeypatch.delenv("STREAMLIT_ALLOW_PROVIDER_FETCH", raising=False)
    import scratch_dashboard as scratch

    # Streamlit import calls load_streamlit_env, which sets FMP_STREAMLIT_READONLY.
    monkeypatch.setattr(scratch, "streamlit_filesystem_write_allowed", lambda: True)
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


def test_streamlit_filesystem_write_refused_when_readonly(monkeypatch, tmp_path):
    monkeypatch.setenv("FMP_STREAMLIT_READONLY", "1")
    assert streamlit_filesystem_write_allowed() is False
    ensure_streamlit_cache_dir(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


def test_cli_filesystem_write_allowed_without_readonly(monkeypatch, tmp_path):
    monkeypatch.delenv("FMP_STREAMLIT_READONLY", raising=False)
    assert streamlit_filesystem_write_allowed() is True
    ensure_streamlit_cache_dir(tmp_path / "cache")
    assert (tmp_path / "cache").is_dir()


def test_load_api_key_does_not_reinject_writer_when_readonly(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FMP_API_KEY=from-checkout\n"
        "DATABASE_URL=postgresql://writer:secret@127.0.0.1/fmp\n"
        "STREAMLIT_ALLOW_PROVIDER_FETCH=1\n",
        encoding="utf-8",
    )
    import data_loader

    monkeypatch.setenv("FMP_STREAMLIT_READONLY", "1")
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("STREAMLIT_ALLOW_PROVIDER_FETCH", raising=False)
    monkeypatch.setattr(data_loader, "_repo_root", lambda: tmp_path)
    key = data_loader.load_api_key()
    assert key == "from-checkout"
    assert os.environ.get("DATABASE_URL") in {None, ""}
    assert os.environ.get("STREAMLIT_ALLOW_PROVIDER_FETCH") in {None, ""}


def test_streamlit_readonly_skips_price_cache_write(monkeypatch, tmp_path):
    monkeypatch.setenv("FMP_STREAMLIT_READONLY", "1")
    import data_loader

    cache = tmp_path / "prices"
    monkeypatch.setattr(data_loader.config, "CACHE_DIR", cache)
    merged = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-02", periods=5),
            "adjClose": [100.0 + i for i in range(5)],
            "symbol": ["SPY"] * 5,
        }
    )
    data_loader._write_price_history_cache(cache / "SPY.csv", merged, date(2024, 1, 8))
    data_loader._cache_path("SPY")
    assert not cache.exists()


def test_cli_still_writes_price_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("FMP_STREAMLIT_READONLY", raising=False)
    import data_loader

    cache = tmp_path / "prices"
    monkeypatch.setattr(data_loader.config, "CACHE_DIR", cache)
    merged = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-02", periods=5),
            "adjClose": [100.0 + i for i in range(5)],
            "symbol": ["SPY"] * 5,
        }
    )
    data_loader._write_price_history_cache(cache / "SPY.csv", merged, date(2024, 1, 8))
    assert (cache / "SPY.parquet").is_file() or (cache / "SPY.csv").is_file()


def test_streamlit_readonly_skips_profile_cache_write(monkeypatch, tmp_path):
    monkeypatch.setenv("FMP_STREAMLIT_READONLY", "1")
    import data_loader

    fund = tmp_path / "fundamentals"
    monkeypatch.setattr(data_loader.config, "FUNDAMENTALS_CACHE_DIR", fund)
    monkeypatch.setattr(
        data_loader,
        "_fmp_get",
        lambda *_args, **_kwargs: [{"symbol": "AAPL", "averageVolume": 1}],
    )
    row = data_loader.get_profile_snapshot(None, "k", "AAPL")
    assert row["averageVolume"] == 1
    assert not fund.exists()


def test_streamlit_readonly_skips_yahoo_and_underlying_cache_write(monkeypatch, tmp_path):
    monkeypatch.setenv("FMP_STREAMLIT_READONLY", "1")
    import scratch_dashboard as scratch

    yahoo = tmp_path / "yahoo"
    underlying = tmp_path / "underlying"
    monkeypatch.setattr(scratch, "YAHOO_CACHE_DIR", yahoo)
    monkeypatch.setattr(scratch, "UNDERLYING_CACHE_DIR", underlying)
    dates = pd.bdate_range("2020-01-02", periods=10)
    scratch._write_yahoo_cache(
        "SPY", pd.Series([float(100 + i) for i in range(10)], index=dates)
    )
    scratch._write_underlying_cache(
        "SPY",
        pd.DataFrame(
            {"underlying": ["AAPL"], "name": ["Apple"], "weight_pct": [10.0], "source": ["FMP"]}
        ),
    )
    assert not yahoo.exists()
    assert not underlying.exists()


def test_streamlit_readonly_skips_eia_cache_write(monkeypatch, tmp_path):
    monkeypatch.setenv("FMP_STREAMLIT_READONLY", "1")
    from data_sources import eia_wholesale

    monkeypatch.setattr(eia_wholesale, "EIA_CACHE_DIR", tmp_path / "eia")
    power = pd.DataFrame({"date": ["2020-01-02"], "hub": ["Mass Hub"], "price": [1.0]})
    gas = pd.DataFrame({"date": ["2020-01-02"], "hub": ["Henry Hub"], "price": [2.0]})
    eia_wholesale._write_cache(power, gas)
    assert not (tmp_path / "eia").exists()


def test_streamlit_readonly_skips_profile_bulk_cache_write(monkeypatch, tmp_path):
    monkeypatch.setenv("FMP_STREAMLIT_READONLY", "1")
    import tech_universe

    cache = tmp_path / "profile_bulk_all.pkl"
    monkeypatch.setattr(tech_universe.config, "PROFILE_BULK_CACHE_PATH", cache)
    monkeypatch.setattr(tech_universe, "_PROFILE_BULK_PART_SLEEP_S", 0)
    monkeypatch.setattr(tech_universe, "_PROFILE_BULK_MAX_EMPTY_STREAK", 1)
    monkeypatch.setattr(tech_universe.time, "sleep", lambda *_a, **_k: None)

    def _part(_session, _key, part):
        if part == 0:
            return [{"symbol": "AAPL", "sector": "Technology"}]
        return []

    monkeypatch.setattr(tech_universe, "_fetch_profile_bulk_part", _part)
    out = tech_universe.fetch_profile_bulk_all(None, "k")
    assert not out.empty
    assert not cache.exists()
