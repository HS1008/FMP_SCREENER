"""Rates & Curve page wires custom historical comparison through the read model."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]

CURRENT = "2026-09-23"
PRIOR = "2026-09-18"
WEEKEND_REQUEST = "2026-09-20"


def _curve(day: str, yield_pct: float) -> list[dict]:
    tenors = ("3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y")
    return [
        {
            "tenor": tenor,
            "yield_pct": yield_pct + index / 100,
            "observation_date": day,
            "source_id": "TREASURY",
            "chg_prev_bps": -1,
            "chg_1w_bps": 2,
            "chg_1m_bps": 3,
        }
        for index, tenor in enumerate(tenors)
    ]


def _rates() -> dict:
    return {
        "curve": _curve(CURRENT, 3.5),
        "slopes": {"2s10s": {"value": 30, "units": "bps"}},
        "curve_dates_mixed": False,
        "complete_curve_date": CURRENT,
        "curve_observation_dates": [CURRENT],
        "source_ids": ["TREASURY"],
        "fallback": False,
        "partial_newer": [],
        "real_yields": [],
        "inflation_compensation": [],
        "policy": [],
        "units_note": "Yields in percent.",
    }


def _lookup(target: str, not_after: str) -> dict:
    if target > not_after:
        return {"requested_date": target, "effective_date": None, "fallback": False, "found": False, "reason": "requested_after_current_curve", "curve": []}
    effective = PRIOR if target < CURRENT else CURRENT
    if target == "2026-06-15":
        effective = "2026-06-15"
    fallback = effective != target
    return {
        "requested_date": target,
        "effective_date": effective,
        "fallback": fallback,
        "found": True,
        "reason": None,
        "curve": _curve(effective, 3.2),
        "source_ids": ["TREASURY"],
    }


def _run(monkeypatch, *, compare: str | None = None, custom: date | None = None) -> AppTest:
    calls: list[tuple] = []

    def fake_cached(fn_name, *args, **kwargs):
        calls.append((fn_name, args))
        if fn_name == "rates_context":
            return _rates()
        if fn_name == "treasury_complete_curve_bounds":
            return {"earliest_complete_date": "2026-06-15", "latest_complete_date": CURRENT}
        if fn_name == "complete_treasury_curve_on_or_before":
            return _lookup(args[0], args[1])
        return {}

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_cached)
    at = AppTest.from_file(str(ROOT / "pages" / "12_Rates_Curve.py"), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    if compare is not None:
        at.radio[0].set_value(compare)
        at.run()
        assert not at.exception, [e.value for e in at.exception]
    if custom is not None:
        at.date_input[0].set_value(custom)
        at.run()
        assert not at.exception, [e.value for e in at.exception]
    at.session_state["_calls"] = calls
    return at


def _text(at: AppTest) -> str:
    chunks = []
    for widget in (*at.markdown, *at.caption, *at.info, *at.warning):
        chunks.append(str(widget.value))
    return "\n".join(chunks)


def test_current_curve_date_is_prominent_without_comparison(monkeypatch):
    at = _run(monkeypatch)
    text = _text(at)
    assert "Current Treasury Curve" in text
    assert "Sep 23, 2026" in text
    assert "Source: U.S. Treasury" in text
    assert "Complete curve: Yes" in text
    assert at.radio[0].value == "None"
    assert "Custom date" in at.radio[0].options
    assert not any(call[0] == "complete_treasury_curve_on_or_before" for call in at.session_state["_calls"])


def test_custom_date_option_uses_exact_trading_date(monkeypatch):
    at = _run(monkeypatch, compare="Custom date", custom=date(2026, 6, 15))
    text = _text(at)
    assert "Sep 23, 2026 — Current · Jun 15, 2026 — Comparison" in text
    assert "Using nearest prior complete curve" not in text
    assert at.date_input[0].value == date(2026, 6, 15)
    lookup_calls = [call for call in at.session_state["_calls"] if call[0] == "complete_treasury_curve_on_or_before"]
    assert ("2026-06-15", "2026-09-23") in [call[1] for call in lookup_calls]


def test_custom_weekend_shows_requested_and_effective_dates(monkeypatch):
    at = _run(monkeypatch, compare="Custom date", custom=date(2026, 9, 20))
    text = _text(at)
    assert "Requested date: Sep 20, 2026" in text
    assert "Using nearest prior complete curve: Sep 18, 2026" in text
    assert WEEKEND_REQUEST
    lookup_calls = [call for call in at.session_state["_calls"] if call[0] == "complete_treasury_curve_on_or_before"]
    assert any(call[1][0] == WEEKEND_REQUEST for call in lookup_calls)


def test_prior_session_requests_complete_curve_lookup(monkeypatch):
    at = _run(monkeypatch, compare="Prior session")
    text = _text(at)
    assert "Sep 23, 2026 — Current · Sep 18, 2026 — Comparison" in text
    lookup_calls = [call for call in at.session_state["_calls"] if call[0] == "complete_treasury_curve_on_or_before"]
    assert lookup_calls
    assert lookup_calls[-1][1][1] == CURRENT
