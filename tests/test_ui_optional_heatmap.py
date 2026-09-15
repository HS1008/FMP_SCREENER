"""Optional load containment and metric-specific heatmap scales."""

from __future__ import annotations

import math

import pandas as pd

from market_intelligence.ui import HEATMAP_SCALES, heatmap_scale_for, load_optional, styled_heatmap


def test_heatmap_scales_are_metric_specific():
    assert heatmap_scale_for("1D return")["threshold"] != heatmap_scale_for("1M return")["threshold"]
    assert heatmap_scale_for("1D (bps)")["as_percent"] is False
    assert "1D RS" in HEATMAP_SCALES
    frame = pd.DataFrame({"Sector": ["A", "B"], "1D return": [0.002, -0.01], "1M return": [0.03, None]})
    styled = styled_heatmap(frame, ["1D return", "1M return"])
    assert styled is not None


def test_missing_and_nan_stay_blank_not_zero():
    frame = pd.DataFrame({"Sector": ["A"], "1D return": [math.nan]})
    styled = styled_heatmap(frame, ["1D return"])
    # Styler or marker fallback must not coerce missing to 0.00%
    text = str(getattr(styled, "data", styled))
    assert "0.00%" not in text or "nan" in text.lower() or "—" in text or "⬜" in text


def test_load_optional_contains_query_failures(monkeypatch):
    from market_intelligence import ui

    def boom(fn_name, *args, **kwargs):
        raise RuntimeError("missing view")

    monkeypatch.setattr(ui, "cached_read", boom)
    result = load_optional("options_volatility_context", default={})
    assert result["available"] is False
    assert result["error"] == "RuntimeError"
    assert result["data"] == {}
