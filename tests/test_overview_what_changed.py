"""Deterministic Overview change lines — factual stored analytics only."""

from __future__ import annotations

from market_intelligence.overview import build_what_changed


def test_what_changed_is_factual_and_uses_frequency_aware_periods():
    items = build_what_changed(
        sectors={
            "datasets": {
                "ETF_RS_VS_SPY": [
                    {"sector_key": "Technology", "instrument_id": "XLK", "benchmark": "SPY", "as_of": "2026-09-05", "metrics": {"rs_chg_1m": 0.021}},
                    {"sector_key": "Energy", "instrument_id": "XLE", "benchmark": "SPY", "as_of": "2026-09-05", "metrics": {"rs_chg_1m": -0.014}},
                ]
            }
        },
        rates={"curve": [{"tenor": "10Y", "yield_pct": 4.12, "chg_prev_bps": -3, "observation_date": "2026-09-08"}]},
        credit={"buckets": [{"bucket": "ig_broad", "label": "IG", "oas_bps": 89, "change_1d_bps": -1, "as_of": "2026-09-08"}]},
        macro={"categories": {"inflation": [{"label": "CPI", "series_id": "CPIAUCSL", "latest": {"value": 320.0, "observation_date": "2026-07-01"}, "transforms": {"yoy_pct": {"value": 2.7, "units": "pct"}}}]}},
        order_flow={"breadth": {"rows": [{"product_category": "all securities", "total_volume": 100, "total_trades": 10, "volume_change": 5, "trade_count_change": 1, "observation_date": "2026-09-08"}]}},
    )
    text = " ".join(item["text"] for item in items)
    areas = [item["area"] for item in items]
    assert "Technology" in text and "Energy" in text
    assert "because" not in text.lower() and "should" not in text.lower()
    assert "Sectors" in areas and "Rates" in areas and "Credit" in areas and "Inflation" in areas and "Order Flow" in areas
    assert any(item["period"] == "prior release / YoY" for item in items)
    assert all(item["period"] != "1D" for item in items if item["area"] == "Inflation")


def test_what_changed_omits_missing_sections():
    assert build_what_changed() == []


def test_dashboard_navigation_groups_research_workspace():
    source = (__import__("pathlib").Path(__file__).resolve().parents[1] / "dashboard.py").read_text(encoding="utf-8")
    assert "st.navigation" in source
    for label in ("Overview", "Markets", "Economy", "Research", "System", "Legacy FMP comparison", "Morning Brief", "Order Flow"):
        assert label in source
    assert 'default=True' in source
