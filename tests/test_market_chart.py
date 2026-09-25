"""Lightweight Charts pilot: VIX history payload, and the tenor chart stays categorical."""

from __future__ import annotations

import inspect
import json
import math
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from market_intelligence.components.market_chart import (
    LIGHTWEIGHT_CHARTS_VERSION,
    observation_day,
    time_series_points,
)
from market_intelligence.components.market_chart import __file__ as chart_module_file

ROOT = Path(chart_module_file).resolve().parent
FRONTEND = ROOT / "frontend"
PAGES_UI = Path(__file__).resolve().parents[1] / "market_intelligence" / "pages_ui.py"


def test_vix_history_points_keep_real_dates_and_numbers():
    eastern = timezone(timedelta(hours=-4))
    rows = [
        {"as_of": "2026-09-19", "value": Decimal("15.10")},
        {"as_of": datetime(2026, 9, 18, 21, 0, tzinfo=eastern), "value": 14.92},
        {"as_of": "2026-09-18T23:30:00-04:00", "value": 14.5},
        {"as_of": "2026-09-17", "value": None},
        {"as_of": "2026-09-17", "value": float("nan")},
        {"as_of": "2026-09-17", "value": float("inf")},
        {"as_of": "2026-09-17", "value": True},
        {"as_of": "not-a-date", "value": 12},
        {"as_of": date(2026, 9, 16), "value": 13.25},
        {"time": "2026-09-15", "as_of": "1999-01-01", "value": 11},
    ]
    points = time_series_points(rows)
    assert points == [
        {"time": "2026-09-15", "value": 11.0},
        {"time": "2026-09-16", "value": 13.25},
        {"time": "2026-09-18", "value": 14.5},
        {"time": "2026-09-19", "value": 15.1},
    ]
    assert observation_day("2026-09-18T23:30:00-04:00") == date(2026, 9, 18)
    payload = json.dumps(points, allow_nan=False)
    assert "NaN" not in payload
    assert "Infinity" not in payload
    parsed = json.loads(payload)
    assert [point["time"] for point in parsed] == [point["time"] for point in points]
    assert all(isinstance(point["value"], float) and math.isfinite(point["value"]) for point in parsed)


def test_duplicate_dates_keep_the_last_finite_value():
    points = time_series_points(
        [
            {"as_of": "2026-09-18", "value": 10},
            {"as_of": "2026-09-18", "value": None},
            {"as_of": "2026-09-18", "value": 14.92},
        ]
    )
    assert points == [{"time": "2026-09-18", "value": 14.92}]


def test_chart_bundle_is_vendored_and_does_not_fetch():
    bundle = (FRONTEND / "lightweight-charts.standalone.production.js").read_text(encoding="utf-8")
    script = (FRONTEND / "chart.js").read_text(encoding="utf-8")
    markup = (FRONTEND / "chart.html").read_text(encoding="utf-8")
    style = (FRONTEND / "chart.css").read_text(encoding="utf-8")
    assert "TradingView Lightweight Charts™ v{0}".format(LIGHTWEIGHT_CHARTS_VERSION) in bundle
    assert "Apache License 2.0" in bundle
    for blob in (bundle, script, markup):
        assert "fetch(" not in blob
        assert "XMLHttpRequest" not in blob
        assert "yahoo" not in blob.lower()
        assert "unpkg.com" not in blob
        assert "jsdelivr" not in blob
    assert "export default function" in script
    assert "autoSize: true" in script
    assert "new ResizeObserver" in script
    assert "chart.remove()" in script
    assert "vertTouchDrag: false" in script
    assert "horzTouchDrag: false" in script
    assert "mouseWheel: false" in script
    assert "pinch: true" in script
    assert "touch-action: pan-y" in style
    assert 'id="readout-date"' in markup
    assert "Full range" in markup
    node = shutil.which("node")
    assert node, "node is required to syntax-check the chart module"
    combined = Path("/tmp/market-chart-module.mjs")
    combined.write_text(bundle + "\n" + script, encoding="utf-8")
    subprocess.run([node, "--check", str(combined)], check=True)


def test_vix_history_uses_lightweight_charts_and_tenor_curve_stays_categorical():
    from market_intelligence import pages_ui

    source = inspect.getsource(pages_ui._render_yahoo_vol_core)
    assert source.count("time_series_points") == 1
    assert 'time_series_points(history.get("VIX_SPOT")' in source
    assert "lightweight_market_chart" in source
    assert "st.line_chart(vix_frame)" not in source
    assert "st.line_chart(implied_frame)" in source
    assert "st.line_chart(skew_frame)" in source
    assert "tenor_curve_chart" in source
    assert source.count("lightweight_market_chart") == 1
    assert "st.plotly_chart" not in source
    assert "go.Figure" not in source
    chart_module = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "yield-curve scale" in chart_module
    page = PAGES_UI.read_text(encoding="utf-8")
    assert "import yfinance" not in page
    assert "ingest_yahoo_vol" not in page
    assert "cboe_client" not in page
    component = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "urllib.request" not in component
    assert "import yfinance" not in component
    assert "import requests" not in component
