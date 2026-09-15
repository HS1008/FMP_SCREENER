"""Overview and Rates must not draw a connected mixed-date Treasury curve."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


def _seed(*, mixed: bool):
    rates = {
        "curve": [
            {"tenor": "2Y", "yield_pct": 3.8, "chg_prev_bps": -1, "observation_date": "2026-09-14" if mixed else "2026-09-15", "source_id": "TREASURY"},
            {"tenor": "10Y", "yield_pct": 4.1, "chg_prev_bps": -3, "observation_date": "2026-09-15", "source_id": "TREASURY"},
            {"tenor": "30Y", "yield_pct": 4.4, "chg_prev_bps": -2, "observation_date": "2026-09-15", "source_id": "TREASURY"},
        ],
        "slopes": {"2s10s": {"value": 30, "units": "bps"}},
        "curve_dates_mixed": mixed,
        "complete_curve_date": None if mixed else "2026-09-15",
        "curve_observation_dates": ["2026-09-14", "2026-09-15"] if mixed else ["2026-09-15"],
        "source_ids": ["TREASURY"],
        "fallback": False,
    }
    return {
        "source_health": [],
        "rates_context": rates,
        "credit_context": {"buckets": [], "attribution": "", "coverage_note": ""},
        "sectors_context": {"datasets": {"ETF_RS_VS_SPY": []}},
        "macro_context": {"categories": {}},
        "order_flow_overview": {"breadth": {"rows": []}},
        "options_volatility_context": {"reason": "none"},
        "ibkr_collector_status": [],
        "ibkr_quotes_latest": [],
    }


def test_overview_withholds_connected_curve_when_dates_mixed(monkeypatch):
    seed = _seed(mixed=True)

    def fake_cached(fn_name, *args, **kwargs):
        if fn_name in seed:
            return seed[fn_name]
        return {}

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_cached)
    at = AppTest.from_file(str(ROOT / "pages" / "10_Market_Pulse.py"), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    text = "\n".join([*(str(w.value) for w in at.warning), *(str(c.value) for c in at.caption), *(str(i.value) for i in at.info)])
    assert "Connected curve is withheld" in text or "dates differ" in text.lower() or "span observation dates" in text.lower()
    assert "Latest coherent curve" not in "\n".join(str(p) for p in at)


def test_overview_draws_coherent_curve_when_same_date(monkeypatch):
    seed = _seed(mixed=False)

    def fake_cached(fn_name, *args, **kwargs):
        if fn_name in seed:
            return seed[fn_name]
        return {}

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_cached)
    at = AppTest.from_file(str(ROOT / "pages" / "10_Market_Pulse.py"), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    captions = "\n".join(str(c.value) for c in at.caption)
    assert "Same-date complete curve" in captions or at.plotly_chart
