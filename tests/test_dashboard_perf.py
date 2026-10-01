"""Performance guards: chart payload, quote cadence, and view-check caching."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from market_intelligence.components.market_chart import _component as market_component
from market_intelligence.components.tenor_chart import _component as tenor_component
from market_intelligence.markets_ui import YAHOO_HEATMAP_REFRESH_SECONDS, _remember_legs
from market_intelligence.read_models import _view_exists
from market_intelligence.schema_cache import VIEW_EXISTS_TTL_SECONDS, clear_view_exists_cache

ROOT = Path(__file__).resolve().parents[1]


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Conn:
    def __init__(self):
        self.info: dict = {}
        self.calls = 0

    def execute(self, *_args, **_kwargs):
        self.calls += 1
        return _Result((1,))


def _install_runtime(manager):
    from streamlit.runtime.runtime import Runtime

    previous = Runtime._instance
    runtime = MagicMock()
    runtime.bidi_component_registry = manager
    Runtime._instance = runtime
    return previous


def _definition(manager, name: str):
    from market_intelligence.components import chart_component

    chart_component._MOUNTS.clear()
    if name == "tenor_chart":
        tenor_component()
    else:
        market_component()
    return manager.get(name)


def test_chart_javascript_is_not_copied_into_each_mount():
    from streamlit.components.v2.component_manager import BidiComponentManager
    from streamlit.runtime.runtime import Runtime

    previous = _install_runtime(BidiComponentManager())
    try:
        manager = Runtime.instance().bidi_component_registry
        tenor = _definition(manager, "tenor_chart")
        market = _definition(manager, "market_chart")
        assert tenor is not None and market is not None
        assert tenor.js_content is None
        assert market.js_content is None
        assert tenor.js_url == "bundle.js"
        assert market.js_url == "bundle.js"
        tenor_bundle = Path(tenor.js).read_text(encoding="utf-8")
        market_bundle = Path(market.js).read_text(encoding="utf-8")
        assert "export default function" in tenor_bundle
        assert 't.version="6.1.0"' in tenor_bundle
        assert "export default function" in market_bundle
        assert "window.LightweightCharts" in market_bundle
        assert len(tenor_bundle) > 1_000_000
        first = tenor_component()
        assert tenor_component() is first
    finally:
        Runtime._instance = previous


def test_chart_registration_follows_the_active_registry():
    from streamlit.components.v2.component_manager import BidiComponentManager
    from streamlit.runtime.runtime import Runtime

    previous = _install_runtime(BidiComponentManager())
    try:
        first_manager = Runtime.instance().bidi_component_registry
        first = tenor_component()
        second_manager = BidiComponentManager()
        Runtime.instance().bidi_component_registry = second_manager
        second = tenor_component()
        assert second is not first
        assert first_manager.get("tenor_chart").js_content is None
        assert second_manager.get("tenor_chart").js_content is None
    finally:
        Runtime._instance = previous


def test_us_heatmap_refresh_matches_the_yahoo_quote_cron():
    source = (ROOT / "market_intelligence" / "markets_ui.py").read_text(encoding="utf-8")
    assert YAHOO_HEATMAP_REFRESH_SECONDS == 15 * 60
    assert "run_every=YAHOO_HEATMAP_REFRESH_SECONDS" in source
    assert "run_every=15)" not in source
    monitor = (ROOT / "pages" / "strategy_monitor.py").read_text(encoding="utf-8")
    assert 'LIVE_MONITOR_REFRESH = "30s"' in monitor


def test_repeated_view_checks_use_the_connection_cache():
    conn = _Conn()
    assert _view_exists(conn, "mi_v_live_quotes_by_source") is True
    assert _view_exists(conn, "mi_v_live_quotes_by_source") is True
    assert conn.calls == 1
    clear_view_exists_cache(conn)
    assert _view_exists(conn, "mi_v_live_quotes_by_source") is True
    assert conn.calls == 2
    assert VIEW_EXISTS_TTL_SECONDS == 60


def test_shared_ticker_horizons_are_prepared_once(monkeypatch):
    calls = {"n": 0}

    def _legs(_bars, _price, _anchor):
        calls["n"] += 1
        return {"1W": {"label": "1W", "value": 0.1, "reason": "close 2026-09-23", "reference": None, "target": None}}

    monkeypatch.setattr("market_intelligence.markets_ui.price_horizons", _legs)
    cache: dict = {}
    bars = [{"bar_date": "2026-09-23", "close": 80.0, "basis": "SPLIT_ADJUSTED_PRICE", "quality": "FINAL"}]
    first = _remember_legs(cache, "NVDA", bars, 100.0, "2026-09-30T14:00:00+00:00")
    second = _remember_legs(cache, "NVDA", bars, 100.0, "2026-09-30T14:00:00+00:00")
    assert first == second
    assert calls["n"] == 1
    assert first["1W"]["value"] == 0.1
