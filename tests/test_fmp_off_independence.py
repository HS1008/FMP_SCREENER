"""FMP-off boot, no silent FMP fallback, fixture equity updates read models."""

from __future__ import annotations

from datetime import date

import pytest

from market_intelligence.fmp_mode import fmp_free_mode, legacy_fmp_enabled
from market_intelligence.page_registry import visible_page_specs


def test_default_is_fmp_free_and_hides_legacy_nav(monkeypatch):
    monkeypatch.delenv("MI_FMP_FREE", raising=False)
    monkeypatch.delenv("MI_ALLOW_LEGACY_FMP", raising=False)
    assert fmp_free_mode() is True
    assert legacy_fmp_enabled() is False
    assert all(spec.route_id != "legacy_fmp" for spec in visible_page_specs())


def test_dashboard_module_does_not_import_legacy_engines():
    src = open("dashboard.py", encoding="utf-8").read()
    assert "FMP_API_KEY" not in src
    assert "import tech_rotation_engine" not in src
    assert "import data_loader" not in src
    assert "from market_intelligence.fmp_mode import" in src


def test_refresh_skips_legacy_in_fmp_free(monkeypatch):
    monkeypatch.delenv("MI_ALLOW_LEGACY_FMP", raising=False)
    monkeypatch.setenv("MI_FMP_FREE", "1")
    from jobs.market_intelligence_refresh import plan
    from argparse import Namespace

    args = Namespace(
        fred=False,
        finra=False,
        legacy_sector=False,
        treasury=False,
        equity=False,
        build_analytics=False,
        build_morning=False,
        all_configured=True,
        dry_run=True,
        probe_config=False,
        mode="incremental",
        series=None,
        as_of=None,
        backfill_analytics_from=None,
        wait_lock=False,
        json=False,
    )
    planned = plan(args, {"MI_FMP_FREE": "1"})
    steps = {s["step"]: s for s in planned["steps"]}
    assert "legacy_sector" not in steps
    assert steps["treasury"]["action"] == "ingest"


def test_fixture_equity_writes_1d_rs(mi_db):
    from market_intelligence.equity_eod import EquityBar, FixtureAdapter, ingest_equity_eod
    from market_intelligence.read_models import sectors_context

    bars = []
    for day, spy, xlk in (
        (date(2026, 9, 8), 100.0, 50.0),
        (date(2026, 9, 9), 101.0, 51.0),
        (date(2026, 9, 10), 102.01, 52.02),
    ):
        bars.append(EquityBar("SPY", day, spy))
        bars.append(EquityBar("XLK", day, xlk))
        for etf in ("XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLU", "XLRE"):
            bars.append(EquityBar(etf, day, 20.0 + (day.day / 100.0)))
        bars.append(EquityBar("NVDA", day, 100.0 + day.day))
        bars.append(EquityBar("AMD", day, 10.0 + day.day / 10.0))
    report = ingest_equity_eod(
        mi_db,
        FixtureAdapter(bars),
        today=date(2026, 9, 10),
        lookback_days=10,
    )
    assert report.failed is False
    assert report.latest_observation == date(2026, 9, 10)
    with mi_db.connect() as conn:
        ctx = sectors_context(conn)
    rows = (ctx.get("datasets") or {}).get("ETF_RS_VS_SPY") or []
    tech = next((row for row in rows if row["sector_key"] == "Information Technology"), None)
    assert tech is not None
    assert tech["source_id"] == "EQUITY_EOD"
    assert tech["metrics"]["ret_1d"] is not None
    assert tech["metrics"]["rs_chg_1d"] is not None


def test_fmp_http_is_not_used_by_equity_adapter():
    from market_intelligence.equity_eod import UnavailableAdapter

    adapter = UnavailableAdapter()
    with pytest.raises(Exception):
        adapter.fetch(["SPY"], date(2026, 1, 1), date(2026, 1, 2))


def test_fmp_http_blocked_and_key_absent(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    import urllib.request

    def _blocked(req, *args, **kwargs):
        url = getattr(req, "full_url", None) or str(req)
        if "financialmodelingprep.com" in str(url).lower():
            raise AssertionError("FMP HTTP must stay blocked")
        raise RuntimeError("network blocked")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    from market_intelligence.equity_eod import adapter_from_env

    adapter = adapter_from_env({"MI_EQUITY_PROVIDER": "unavailable"})
    assert adapter.access_status != "CONFIGURED"
    with pytest.raises(Exception):
        adapter.fetch(["SPY"], date(2026, 9, 9), date(2026, 9, 10))


def test_fixture_treasury_updates_rates(mi_db):
    from market_intelligence.ingest_treasury import ingest_treasury
    from market_intelligence.read_models import rates_context
    from market_intelligence.treasury_xml import parse_feed_xml
    from tests.test_treasury_xml import SAMPLE

    class _Client:
        def fetch_recent(self, *, today, lookback_months=2):
            return parse_feed_xml(SAMPLE, curve="nominal").points

    report = ingest_treasury(mi_db, _Client(), today=date(2026, 9, 10), lookback_months=1)
    assert report.failed is False
    assert report.latest_observation == date(2026, 9, 10)
    with mi_db.connect() as conn:
        ctx = rates_context(conn)
    assert ctx["complete_curve_date"] == "2026-09-09"
    assert {row["tenor"] for row in ctx["partial_newer"]} >= {"2Y", "3M"}
    tenors = {row["tenor"]: row for row in ctx["curve"]}
    assert tenors["10Y"]["source_id"] == "TREASURY"
    assert tenors["10Y"]["observation_date"] == "2026-09-09"
