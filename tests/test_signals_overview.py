"""Structured overview signals and credit sector coverage honesty."""

from __future__ import annotations

from market_intelligence.signals import build_what_matters, credit_sector_coverage


def test_what_matters_is_nonduplicative_and_factual():
    items = build_what_matters(
        sectors={
            "datasets": {
                "ETF_RS_VS_SPY": [
                    {"sector_key": "Technology", "instrument_id": "XLK", "as_of": "2026-09-05", "metrics": {"ret_1d": 0.021}},
                    {"sector_key": "Energy", "instrument_id": "XLE", "as_of": "2026-09-05", "metrics": {"ret_1d": -0.014}},
                ]
            }
        },
        rates={"curve": [{"tenor": "10Y", "yield_pct": 4.12, "chg_prev_bps": -3, "observation_date": "2026-09-08", "source_id": "TREASURY"}], "slopes": {"2s10s": {"value": 45, "units": "bps", "chg_prev_bps": 2}}},
        credit={"buckets": [{"bucket": "ig_broad", "label": "IG", "oas_bps": 89, "change_1d_bps": -1, "as_of": "2026-09-08"}, {"bucket": "hy_broad", "label": "HY", "oas_bps": 320, "change_1d_bps": 3, "as_of": "2026-09-08"}]},
        macro={"categories": {"inflation": [{"label": "CPI", "series_id": "CPIAUCSL", "latest": {"value": 320.0, "observation_date": "2026-07-01"}, "transforms": {"yoy_pct": {"value": 2.7, "units": "pct"}}}]}},
        order_flow={"breadth": {"rows": [{"product_category": "all securities", "total_volume": 100, "total_trades": 10, "volume_change": 5, "trade_count_change": 1, "observation_date": "2026-09-08"}]}},
        horizon="1D",
        limit=5,
    )
    categories = [item.category for item in items]
    assert len(categories) == len(set(categories))
    assert 1 <= len(items) <= 5
    text = " ".join(item.text for item in items)
    assert "because" not in text.lower() and "should" not in text.lower()
    assert any(item.drilldown_route == "sectors" for item in items)
    assert all(item.observation_date for item in items if item.category in {"Sectors", "Rates", "Credit"})


def test_what_matters_omits_missing_and_rejects_nan_as_signal():
    assert build_what_matters() == []
    items = build_what_matters(
        sectors={"datasets": {"ETF_RS_VS_SPY": [{"sector_key": "Technology", "metrics": {"ret_1d": None}}]}},
        credit={"buckets": [{"bucket": "ig_broad", "oas_bps": None}]},
    )
    assert items == []
    import math

    nan_items = build_what_matters(
        rates={"curve": [{"tenor": "10Y", "yield_pct": math.nan, "chg_prev_bps": math.nan, "observation_date": "2026-09-08"}]},
    )
    assert nan_items == []


def test_credit_sector_coverage_is_unavailable_without_fabricating_values():
    coverage = credit_sector_coverage(
        {"buckets": [{"series_id": "BAMLC0A0CM", "bucket": "ig_broad"}, {"series_id": "BAMLH0A0HYM2", "bucket": "hy_broad"}]}
    )
    assert coverage["status"] == "UNAVAILABLE"
    assert coverage["sector_oas_available"] is False
    assert coverage["subsector_oas_available"] is False
    assert "BAMLC0A0CM" in coverage["available_series"]
    assert "sector" in coverage["note"].lower()
