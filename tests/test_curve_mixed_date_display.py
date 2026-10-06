"""Rates & Curve must not draw a connected mixed-date Treasury curve.

The Market Overview no longer draws a curve chart (it is a template table), so
the mixed-date guard is exercised on the Rates & Curve page that owns it.
"""

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
        "metric_history": [],
        "observation_history": [],
        "fed_funds_target_on_or_before": {},
        "move_index_context": {"status": "UNAVAILABLE", "value": None, "source_note": "MOVE unavailable in test"},
    }


def _run(monkeypatch, *, mixed: bool) -> AppTest:
    seed = _seed(mixed=mixed)

    def fake_cached(fn_name, *args, **kwargs):
        if fn_name in seed:
            return seed[fn_name]
        return {}

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_cached)
    at = AppTest.from_file(str(ROOT / "pages" / "12_Rates_Curve.py"), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_rates_withholds_connected_curve_when_dates_mixed(monkeypatch):
    at = _run(monkeypatch, mixed=True)
    text = "\n".join([*(str(w.value) for w in at.warning), *(str(c.value) for c in at.caption), *(str(i.value) for i in at.info)])
    assert "Connected curve withheld" in text
    assert "Complete curve: No" in text


def test_rates_draws_coherent_curve_when_same_date(monkeypatch):
    at = _run(monkeypatch, mixed=False)
    captions = "\n".join(str(c.value) for c in at.caption)
    assert "Complete curve: Yes" in captions
    assert not any("Connected curve withheld" in str(w.value) for w in at.warning)
    assert "MOVE index" in [h.value for h in at.subheader]
