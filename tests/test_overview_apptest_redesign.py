"""AppTest smoke for redesigned Market Overview without requiring live Postgres."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


def _seed_contexts():
    rates = {
        "curve": [
            {"tenor": "2Y", "yield_pct": 3.8, "chg_prev_bps": -1, "observation_date": "2026-09-14", "source_id": "TREASURY"},
            {"tenor": "10Y", "yield_pct": 4.1, "chg_prev_bps": -3, "observation_date": "2026-09-14", "source_id": "TREASURY"},
            {"tenor": "30Y", "yield_pct": 4.4, "chg_prev_bps": -2, "observation_date": "2026-09-14", "source_id": "TREASURY"},
        ],
        "slopes": {"2s10s": {"value": 30, "units": "bps", "chg_prev_bps": 1}},
        "curve_dates_mixed": False,
        "complete_curve_date": "2026-09-14",
        "source_ids": ["TREASURY"],
    }
    credit = {
        "buckets": [
            {"series_id": "BAMLC0A0CM", "bucket": "ig_broad", "label": "IG OAS", "oas_bps": 89, "change_1d_bps": -1, "as_of": "2026-09-14", "percentile_window": None, "percentile": None, "zscore": None, "window_observations": None, "history_first_date": None, "history_status": "AVAILABLE"},
            {"series_id": "BAMLH0A0HYM2", "bucket": "hy_broad", "label": "HY OAS", "oas_bps": 310, "change_1d_bps": 2, "as_of": "2026-09-14", "percentile_window": None, "percentile": None, "zscore": None, "window_observations": None, "history_first_date": None, "history_status": "AVAILABLE"},
            {"series_id": "BAMLC0A4CBBB", "bucket": "bbb", "label": "BBB", "oas_bps": 110, "change_1d_bps": 0, "change_1w_bps": 1, "change_1m_bps": 3, "as_of": "2026-09-14", "percentile_window": None, "percentile": None, "zscore": None, "window_observations": None, "history_first_date": None, "history_status": "AVAILABLE"},
        ],
        "attribution": "ICE BofA",
        "coverage_note": "test",
    }
    sectors = {
        "datasets": {
            "ETF_RS_VS_SPY": [
                {"sector_key": "Technology", "instrument_id": "XLK", "benchmark": "SPY", "as_of": "2026-09-14", "source_id": "EOD", "entity_kind": "sector", "return_basis": "adj_close", "metrics": {"ret_1d": 0.012, "ret_1w": 0.02, "ret_1m": 0.04, "rs_chg_1d": 0.004, "rs_chg_1m": 0.01}},
                {"sector_key": "Energy", "instrument_id": "XLE", "benchmark": "SPY", "as_of": "2026-09-14", "source_id": "EOD", "entity_kind": "sector", "return_basis": "adj_close", "metrics": {"ret_1d": -0.008, "ret_1w": -0.01, "ret_1m": -0.02, "rs_chg_1d": -0.003, "rs_chg_1m": -0.015}},
            ]
        }
    }
    macro = {
        "categories": {
            "inflation": [{"label": "CPI", "series_id": "CPIAUCSL", "latest": {"value": 320.0, "observation_date": "2026-07-01", "units": "index"}, "transforms": {"yoy_pct": {"value": 2.7, "units": "pct"}}}],
            "commodities": [{"label": "WTI", "series_id": "DCOILWTICO", "catalog_frequency": "daily", "latest": {"value": 78.0, "observation_date": "2026-09-12"}, "transforms": {"chg_prev": {"value": 1.2, "units": ""}}}],
        }
    }
    order_flow = {
        "breadth": {
            "rows": [
                {"product_category": "all securities", "total_volume": 12_000_000_000, "total_trades": 45000, "volume_change": 500_000_000, "trade_count_change": 1200, "observation_date": "2026-09-12", "advances": 10, "declines": 8}
            ]
        },
        "capped_volume": {"headline_eligible": False, "identity_note": "capped withheld"},
    }
    return {
        "source_health": [],
        "rates_context": rates,
        "credit_context": credit,
        "sectors_context": sectors,
        "macro_context": macro,
        "order_flow_overview": order_flow,
        "options_volatility_context": {"reason": "No published OpenBB/Cboe snapshots"},
        "ibkr_collector_status": [],
        "ibkr_quotes_latest": [],
        "metric_history": [],
        "order_flow_context": {"breadth": order_flow["breadth"], "history": [], "coverage_explanation": "reported TRACE aggregates", "attribution": "FINRA"},
        "data_health_context": {},
        "pit_sector_context": {"available": False},
    }


def test_overview_apptest_signal_layout(monkeypatch):
    seed = _seed_contexts()

    def fake_cached(fn_name, *args, **kwargs):
        if fn_name in seed:
            return seed[fn_name]
        raise RuntimeError("unexpected optional read {0}".format(fn_name))

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_cached)
    at = AppTest.from_file(str(ROOT / "pages" / "10_Market_Pulse.py"), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    text = "\n".join(item.value for item in at.markdown) + "\n".join(item.value for item in at.text) + "\n".join(str(t.value) for t in at.title) + "\n".join(c.value for c in at.caption) + "\n".join(i.value for i in at.info)
    # subheaders
    heads = [h.value for h in at.subheader]
    assert "What matters" in heads
    assert "Category snapshot" in heads
    assert "Day to day" not in heads
    assert "What changed" not in heads
    assert "Market Overview" in [t.value for t in at.title]
    assert "Sector leadership" in heads


def test_optional_options_failure_does_not_stop_overview(monkeypatch):
    seed = _seed_contexts()

    def fake_cached(fn_name, *args, **kwargs):
        if fn_name == "options_volatility_context":
            raise RuntimeError("options view missing")
        if fn_name in seed:
            return seed[fn_name]
        return {}

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_cached)
    at = AppTest.from_file(str(ROOT / "pages" / "10_Market_Pulse.py"), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "What matters" in [h.value for h in at.subheader]


def test_credit_sector_view_explains_absence(monkeypatch):
    seed = _seed_contexts()

    def fake_cached(fn_name, *args, **kwargs):
        if fn_name in seed:
            return seed[fn_name]
        if fn_name == "metric_history":
            return []
        return {}

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_cached)
    at = AppTest.from_file(str(ROOT / "pages" / "13_Credit_Overview.py"), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    # Select sectors view
    radios = [w for w in at.radio if "Credit view" in str(getattr(w, "label", ""))]
    assert radios
    radios[0].set_value("Sectors & subsectors")
    at.run()
    text = " ".join(i.value for i in at.info)
    assert "sector" in text.lower() or "unavailable" in text.lower()


def test_options_page_empty_state(monkeypatch):
    def fake_cached(fn_name, *args, **kwargs):
        if fn_name == "options_volatility_context":
            return {"reason": "No published OpenBB/Cboe snapshots"}
        raise RuntimeError(fn_name)

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_cached)
    at = AppTest.from_file(str(ROOT / "pages" / "21_Options_Volatility.py"), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Options" in str(t.value) for t in at.title)
