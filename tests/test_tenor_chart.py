"""ECharts tenor curve: explicit order, honest gaps, and a categorical option."""

from __future__ import annotations

import inspect
import json
import math
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from market_intelligence.components.tenor_chart import (
    ECHARTS_VERSION,
    build_tenor_curve,
    calendar_day,
    echarts_option,
)
from market_intelligence.components.tenor_chart import __file__ as tenor_module_file
from market_intelligence.yahoo_vol import TENOR_AXIS, TERM_TENORS

ROOT = Path(tenor_module_file).resolve().parent
FRONTEND = ROOT / "frontend"
PAGES_UI = Path(__file__).resolve().parents[1] / "market_intelligence" / "pages_ui.py"

VIX_ROWS = [
    {"tenor": "1Y", "yahoo_ticker": "^VIX1Y", "value": Decimal("20.5"), "as_of": "2026-09-25"},
    {"tenor": "6M", "yahoo_ticker": "^VIX6M", "value": 19.25, "as_of": "2026-09-25"},
    {"tenor": "3M", "yahoo_ticker": "^VIX3M", "value": 17.89, "as_of": "2026-09-25"},
    {"tenor": "1M", "yahoo_ticker": "^VIX", "value": 16.1, "as_of": "2026-09-25"},
    {"tenor": "1D", "yahoo_ticker": "^VIX1D", "value": 11.95, "as_of": "2026-09-25"},
    {"tenor": "9D", "yahoo_ticker": "^VIX9D", "value": 14.4, "as_of": "2026-09-25"},
    {"tenor": "2M", "yahoo_ticker": "^NOT_A_TENOR", "value": 99.0, "as_of": "2026-09-25"},
]


def test_payload_keeps_explicit_tenor_order_and_tickers():
    payload = build_tenor_curve(VIX_ROWS, axis=TENOR_AXIS, curve_date=date(2026, 9, 25), y_title="Vol points")
    assert payload["axis"] == ["1M", "3M", "6M", "1Y"]
    assert payload["axis"] != sorted(payload["axis"])
    assert [point["tenor"] for point in payload["points"]] == payload["axis"]
    assert [(ticker, tenor) for ticker, tenor, _metric in TERM_TENORS] == [
        ("^VIX", "1M"),
        ("^VIX3M", "3M"),
        ("^VIX6M", "6M"),
        ("^VIX1Y", "1Y"),
    ]
    by_tenor = {point["tenor"]: point for point in payload["points"]}
    assert by_tenor["1M"]["ticker"] == "^VIX"
    assert by_tenor["1M"]["name"] == "VIX"
    assert by_tenor["3M"]["name"] == "VIX3M"
    assert by_tenor["3M"]["ticker"] == "^VIX3M"
    assert by_tenor["3M"]["value"] == 17.89
    assert by_tenor["3M"]["curve_date_label"] == "Sep 25, 2026"
    assert "1D" not in payload["axis"]
    assert "9D" not in payload["axis"]
    assert all(math.isfinite(point["value"]) for point in payload["points"])
    assert payload["missing_tenors"] == []
    assert payload["curve_date"] == "2026-09-25"
    assert payload["curve_date_label"] == "Sep 25, 2026"
    assert "2M" not in payload["axis"]
    encoded = json.dumps(payload, allow_nan=False)
    assert "NaN" not in encoded
    assert "Infinity" not in encoded


def test_missing_values_stay_missing_and_duplicates_keep_the_first_row():
    rows = [
        {"tenor": "1M", "yahoo_ticker": "^VIX", "value": None, "as_of": "2026-09-25"},
        {"tenor": "1M", "yahoo_ticker": "^VIX", "value": 0, "as_of": "2026-09-25"},
        {"tenor": "3M", "yahoo_ticker": "^VIX3M", "value": float("nan")},
        {"tenor": "6M", "yahoo_ticker": "^VIX6M", "value": 18.0},
        {"tenor": "6M", "yahoo_ticker": "^VIX6M", "value": 99.0},
        {"tenor": "1Y", "yahoo_ticker": "^VIX1Y", "value": "nope"},
    ]
    payload = build_tenor_curve(rows, axis=TENOR_AXIS, curve_date="2026-09-25", y_title="Vol points")
    by_tenor = {point["tenor"]: point for point in payload["points"]}
    assert by_tenor["1M"]["value"] is None
    assert by_tenor["3M"]["value"] is None
    assert by_tenor["6M"]["value"] == 18.0
    assert by_tenor["1Y"]["value"] is None
    assert payload["missing_tenors"] == ["1M", "3M", "1Y"]
    assert 0 not in {point["value"] for point in payload["points"]}
    option = echarts_option(payload)
    series = option["series"][0]["data"]
    assert series[0]["value"] is None
    assert series[1]["value"] is None
    assert series[2]["value"] == 18.0
    assert series[3]["value"] is None
    assert option["series"][0]["connectNulls"] is False


def test_curve_date_is_not_shifted_or_invented():
    eastern = timezone(timedelta(hours=-4))
    stamp = datetime(2026, 9, 18, 21, 0, tzinfo=eastern)
    assert calendar_day(stamp) == date(2026, 9, 18)
    assert calendar_day("2026-09-18T23:30:00-04:00") == date(2026, 9, 18)
    payload = build_tenor_curve(
        [{"tenor": "1M", "ticker": "^VIX", "value": 16.0, "as_of": "1999-01-01"}],
        axis=("1M",),
        curve_date=stamp,
    )
    assert payload["curve_date"] == "2026-09-18"
    assert payload["points"][0]["curve_date"] == "2026-09-18"
    mixed = build_tenor_curve(
        [
            {"tenor": "1D", "ticker": "^VIX1D", "value": 12.0, "as_of": "2026-09-24"},
            {"tenor": "9D", "ticker": "^VIX9D", "value": 13.0, "as_of": "2026-09-25"},
        ],
        axis=("1D", "9D"),
    )
    assert mixed["curve_date"] is None
    assert mixed["curve_date_label"] == ""
    assert "1999-01-01" not in json.dumps(payload)


def test_echarts_option_is_a_categorical_curve_without_zoom():
    payload = build_tenor_curve(VIX_ROWS, axis=TENOR_AXIS, curve_date=date(2026, 9, 25), y_title="Vol points")
    option = echarts_option(payload)
    assert option["xAxis"]["type"] == "category"
    assert option["xAxis"]["data"] == ["1M", "3M", "6M", "1Y"]
    assert option["yAxis"]["type"] == "value"
    assert option["yAxis"]["name"] == "Vol points"
    assert "time" not in json.dumps(option["xAxis"])
    series = option["series"][0]
    assert series["type"] == "line"
    assert series["showSymbol"] is True
    assert series["symbol"] == "circle"
    assert series["label"]["show"] is True
    assert series["label"]["position"] == "top"
    assert series["labelLayout"]["hideOverlap"] is False
    assert series["connectNulls"] is False
    assert option["dataZoom"] == []
    assert option["legend"]["show"] is False
    assert option["toolbox"]["show"] is False
    assert option["tooltip"]["trigger"] == "axis"
    assert option["tooltip"]["triggerOn"] == "mousemove|click"
    assert option["tooltip"]["axisPointer"]["type"] == "line"
    three_month = series["data"][1]
    assert three_month["tenor"] == "3M"
    assert three_month["name"] == "VIX3M"
    assert three_month["ticker"] == "^VIX3M"
    assert three_month["value"] == 17.89
    assert three_month["curve_date_label"] == "Sep 25, 2026"
    tooltip = option["tooltip"]
    assert tooltip["backgroundColor"] == "rgba(22, 24, 28, 0.96)"
    assert tooltip["borderColor"] == "rgba(255, 255, 255, 0.14)"
    assert tooltip["borderWidth"] == 1
    assert tooltip["textStyle"]["color"] == "#f4f6f8"
    json.dumps(option, allow_nan=False)


def test_frontend_bundles_echarts_and_does_not_fetch():
    bundle = (FRONTEND / "echarts.common.min.js").read_text(encoding="utf-8")
    script = (FRONTEND / "chart.js").read_text(encoding="utf-8")
    style = (FRONTEND / "chart.css").read_text(encoding="utf-8")
    markup = (FRONTEND / "chart.html").read_text(encoding="utf-8")
    assert ECHARTS_VERSION == "6.1.0"
    assert 'version:"6.1.0"' in bundle or 'version: "6.1.0"' in bundle
    assert "Apache License" in (FRONTEND / "echarts.LICENSE").read_text(encoding="utf-8")
    assert "export default function" in script
    assert "echarts.init" in script
    assert "setOption" in script
    assert "new ResizeObserver" in script
    assert "dataZoom = []" in script
    assert "formatTooltip" in script
    assert "toFixed(2)" in script
    assert "rgba(22, 24, 28, 0.96)" in script
    assert "borderWidth" in script
    assert 'color: "#f4f6f8"' in script
    assert " · " in script
    assert "point.value == null" in script
    assert "unavailable on this date" in script
    assert "touch-action: pan-y" in style
    assert "320px" not in style
    assert "390px" not in style
    assert "var MOBILE_HEIGHT = 320" in script
    assert "var DESKTOP_HEIGHT = 390" in script
    assert 'id="chart"' in markup
    for blob in (script, style, markup):
        assert "fetch(" not in blob
        assert "XMLHttpRequest" not in blob
        assert "yfinance" not in blob
        assert "query1.finance.yahoo" not in blob
    module = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "urllib.request" not in module
    assert "import yfinance" not in module
    assert "import requests" not in module
    page = PAGES_UI.read_text(encoding="utf-8")
    renderer = inspect.getsource(__import__("market_intelligence.pages_ui", fromlist=["_render_yahoo_vol_core"])._render_yahoo_vol_core)
    assert "tenor_curve_chart" in renderer
    assert "build_tenor_curve" in renderer
    assert "TENOR_AXIS" in renderer
    assert "st.plotly_chart" not in renderer
    assert "lightweight_market_chart" in renderer
    assert "import yfinance" not in page
    node = shutil.which("node")
    assert node, "node is required to syntax-check the chart module"
    combined = Path("/tmp/tenor-chart-module.mjs")
    combined.write_text(bundle + "\n" + script, encoding="utf-8")
    subprocess.run([node, "--check", str(combined)], check=True)
    probe = Path("/tmp/tenor-chart-probe.js")
    probe.write_text(
        "const fs=require('fs'); const vm=require('vm'); const context={console:console};"
        "context.globalThis=context; vm.createContext(context);"
        "vm.runInContext(fs.readFileSync(process.argv[2],'utf8'), context);"
        "if(!context.echarts || context.echarts.version!=='6.1.0' || typeof context.echarts.init!=='function'){"
        "throw new Error('echarts bundle did not expose init');}",
        encoding="utf-8",
    )
    subprocess.run([node, str(probe), str(FRONTEND / "echarts.common.min.js")], check=True)
