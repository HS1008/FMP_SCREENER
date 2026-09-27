"""Macro dashboard catalog, transforms, ranges, recession bands, and page charts."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

from streamlit.testing.v1 import AppTest

from market_intelligence.analytics import series_metrics
from market_intelligence.catalog import CATALOG_BY_ID, MACRO_MAX_BACKFILL_SERIES, validate_metadata
from market_intelligence.components.market_chart import build_market_chart_payload, observation_day
from market_intelligence.components.tenor_chart import build_policy_rate_option
from market_intelligence.freshness import SERIES_POLICIES
from market_intelligence.macro_dashboard import (
    CHART_TITLES,
    CHARTS,
    DISPLAY_SCALE,
    GROUP_LABELS,
    GROUP_ORDER,
    chart_unit,
    history_window_bounds,
    policy_rate_frame,
    prepare_line_points,
    recession_intervals,
    rows_in_range,
    scale_level,
    selected_lines,
)
from market_intelligence.read_models import MACRO_HISTORY_LIMIT, MAX_HISTORY_ROWS
from market_intelligence.transforms import ann3m_pct, ann6m_pct, period_difference, qoq_annualized_pct, yoy_pct
from tests.mi_fixtures import official_metadata

ROOT = Path(__file__).resolve().parents[1]
NEW_SERIES = (
    "NFCI",
    "CUSR0000SASL2RS",
    "PERMIT",
    "NEWORDER",
    "AWHMAN",
    "CMRMT",
    "PCEC96",
    "W875RX1",
    "UEMPMED",
    "ISRATIO",
    "BUSLOANS",
    "DRBLACBS",
    "ULCNFB",
    "USREC",
)


def _month(year: int, month: int, value: float) -> tuple[date, float]:
    return date(year, month, 1), value


def _monthly_levels() -> dict[date, float]:
    levels: dict[date, float] = {}
    value = 100.0
    year, month = 2022, 1
    for _ in range(30):
        levels[date(year, month, 1)] = value
        value *= 1.01
        month += 1
        if month == 13:
            month = 1
            year += 1
    return levels


def test_new_macro_series_match_official_metadata():
    for series_id in NEW_SERIES + ("DFEDTARL", "DFEDTARU"):
        spec = CATALOG_BY_ID[series_id]
        status, mismatches = validate_metadata(spec, official_metadata(series_id))
        assert status == "VALIDATED", (series_id, mismatches)
        assert series_id in SERIES_POLICIES
    assert CATALOG_BY_ID["ICSA"].category == "labor"
    assert CATALOG_BY_ID["UNRATE"].category == "labor"
    assert CATALOG_BY_ID["USREC"].category == "recession"
    assert "NFCI" in MACRO_MAX_BACKFILL_SERIES
    assert "USREC" in MACRO_MAX_BACKFILL_SERIES


def test_canonical_growth_formulas_use_calendar_lags():
    levels = _monthly_levels()
    at = date(2024, 6, 1)
    lag = date(2023, 6, 1)
    yoy = yoy_pct(levels, at)
    assert yoy.value == 100.0 * (levels[at] / levels[lag] - 1.0)
    three = ann3m_pct(levels, at)
    assert three.value == 100.0 * ((levels[at] / levels[date(2024, 3, 1)]) ** 4 - 1.0)
    six = ann6m_pct(levels, at)
    assert six.value == 100.0 * ((levels[at] / levels[date(2023, 12, 1)]) ** 2 - 1.0)
    missing = dict(levels)
    del missing[date(2023, 6, 1)]
    assert yoy_pct(missing, at).value is None
    quarterly = {date(2023, 1, 1): 100.0, date(2023, 4, 1): 101.0, date(2023, 7, 1): 102.0, date(2024, 1, 1): 104.0, date(2024, 4, 1): 105.0}
    qoq = qoq_annualized_pct(quarterly, date(2024, 4, 1))
    assert qoq.value == 100.0 * ((105.0 / 104.0) ** 4 - 1.0)
    assert qoq_annualized_pct(quarterly, date(2024, 1, 1)).value is None


def test_payroll_change_and_claims_average_do_not_invent_gaps():
    payroll = {date(2024, 1, 1): 155000.0, date(2024, 2, 1): 155180.0}
    change = period_difference(payroll, date(2024, 2, 1), 1, units="thousands")
    assert change.value == 180.0
    assert change.units == "thousands"
    rows = series_metrics(CATALOG_BY_ID["PAYEMS"], payroll, at=date(2024, 2, 1))
    mom = next(row for row in rows if row.metric_id == "PAYEMS.mom_change")
    assert mom.result.value == 180.0
    assert mom.result.units == "thousands"
    start = date(2024, 1, 6)
    claims = {start + timedelta(days=7 * i): 200000.0 + i * 1000 for i in range(6)}
    missing_week = dict(claims)
    del missing_week[start + timedelta(days=21)]
    del missing_week[start + timedelta(days=28)]
    at = start + timedelta(days=35)
    averaged = series_metrics(CATALOG_BY_ID["ICSA"], missing_week, at=at)
    avg = next(row for row in averaged if row.metric_id == "ICSA.avg_4w")
    assert avg.result.status == "GAP_IN_WINDOW"
    trailing = [missing_week[day] for day in sorted(missing_week) if day <= at][-4:]
    assert avg.result.value == sum(trailing) / 4
    assert len(missing_week) == 4


def test_loan_yoy_uses_the_monthly_calendar_lag():
    loans = {date(2024, month, 1): 1000.0 + month for month in range(1, 13)}
    loans[date(2025, 1, 1)] = 1120.0
    stored = series_metrics(CATALOG_BY_ID["BUSLOANS"], loans, at=date(2025, 1, 1))
    yoy = next(row for row in stored if row.metric_id == "BUSLOANS.yoy_pct")
    assert yoy.result.value == 100.0 * (1120.0 / 1001.0 - 1.0)
    assert CATALOG_BY_ID["BUSLOANS"].expected_frequency == "M"
    assert CATALOG_BY_ID["BUSLOANS"].expected_sa == "SA"


def test_liquidity_display_scale_is_one_conversion():
    assert scale_level("WALCL", 8_500_000) == 8.5
    assert DISPLAY_SCALE["WALCL"]["divisor"] == 1_000_000
    assert scale_level("WALCL", 8_500_000) != 8.5 / 1000
    assert scale_level("WRESBAL", 3_300_000) == 3300
    assert scale_level("WTREGEN", 700_000) == 700
    assert scale_level("RRPONTSYD", 450) == 450
    assert scale_level("M2SL", 21_000) == 21
    assert scale_level("M2SL", None) is None
    balance = next(chart for chart in CHARTS["fed"] if chart["title"] == "Federal Reserve Balance Sheet")
    liquidity = next(chart for chart in CHARTS["fed"] if chart["title"] == "System Liquidity Components")
    assert chart_unit(balance) == "USD tn"
    assert chart_unit(liquidity) == "USD bn"
    assert [item[0] for item in selected_lines(balance)] == ["WALCL"]
    assert [item[0] for item in selected_lines(liquidity)] == ["WRESBAL", "WTREGEN", "RRPONTSYD"]
    m2 = next(chart for chart in CHARTS["fed"] if chart["title"] == "M2 Money Supply")
    assert chart_unit(m2, mode="Level") == "USD tn"
    assert chart_unit(m2, mode="YoY %") == "Percent"
    assert selected_lines(m2, mode="Level")[0][0] == "M2SL"
    assert selected_lines(m2, mode="YoY %")[0][0] == "M2SL.yoy_pct"


def test_recession_intervals_and_chart_payload():
    rows = [
        {"as_of": "2019-12-01", "value": 0},
        {"as_of": "2020-02-01", "value": 1},
        {"as_of": "2020-03-01", "value": 1},
        {"as_of": "2020-04-01", "value": 0},
        {"as_of": "2020-05-01", "value": 1},
        {"as_of": "2020-06-01", "value": None},
    ]
    intervals = recession_intervals(rows)
    assert intervals == [
        {"start": "2020-02-01", "end": "2020-03-01"},
        {"start": "2020-05-01", "end": "2020-05-01"},
    ]
    assert intervals[0]["start"] != "2020-01-01"
    points = [{"as_of": "2020-03-01", "value": 4.2}]
    payload = build_market_chart_payload(points, series_label="Unemployment rate", recession_bands=intervals, value_format="percent")
    assert payload["series"][0]["points"][0]["value"] == 4.2
    assert payload["recession_bands"] == intervals
    plain = build_market_chart_payload(points, series_label="Unemployment rate")
    assert "recession_bands" not in plain
    encoded = json.dumps(payload)
    assert "NaN" not in encoded and "Infinity" not in encoded
    assert observation_day("2020-03-01") == date(2020, 3, 1)


def test_subsection_date_window_is_an_inclusive_union():
    early = [{"as_of": "2000-01-01", "value": 1.0}, {"as_of": "2024-01-01", "value": 2.0}]
    later = [{"as_of": "2010-04-01", "value": 3.0}, {"as_of": "2024-06-01", "value": None}]
    start, end = history_window_bounds([early, later])
    assert start == date(2000, 1, 1)
    assert end == date(2024, 1, 1)
    kept = rows_in_range(early + later, start=date(2010, 4, 1), end=date(2010, 4, 1))
    assert [row["as_of"] for row in kept] == ["2010-04-01"]
    assert rows_in_range(early, start=date(2024, 2, 1), end=date(2024, 1, 1)) == []
    quarterly = [{"as_of": "2024-01-01", "value": 1.0}, {"as_of": "2024-04-01", "value": 1.1}]
    window = prepare_line_points(quarterly, start=date(2024, 1, 1), end=date(2024, 6, 1))
    assert [point["as_of"] for point in window] == ["2024-01-01", "2024-04-01"]
    assert all(math.isfinite(point["value"]) for point in window)


def test_policy_frame_does_not_paint_the_range_backward():
    lower = [{"as_of": "2008-12-16", "value": 0.0}, {"as_of": "2008-12-17", "value": 0.0}]
    upper = [{"as_of": "2008-12-16", "value": 0.25}, {"as_of": "2008-12-17", "value": 0.25}]
    effective = [{"as_of": "2008-12-01", "value": 0.5}, {"as_of": "2008-12-16", "value": 0.12}]
    frame = policy_rate_frame(lower, upper, effective, [], [], start=date(2008, 12, 1), end=date(2008, 12, 17))
    by_day = {row[0]: row[1] for row in frame["lower"]}
    assert by_day["2008-12-01"] is None
    assert by_day["2008-12-16"] == 0.0
    option = build_policy_rate_option(frame)
    assert option["chartKind"] == "policy_rates"
    assert option["yAxis"]["name"] == "Percent"
    encoded = json.dumps(option)
    assert "NaN" not in encoded


def test_macro_history_cap_covers_daily_fed_funds():
    assert MACRO_HISTORY_LIMIT >= 50000
    assert MACRO_HISTORY_LIMIT > MAX_HISTORY_ROWS


def test_macro_backfill_is_not_part_of_scheduled_refresh():
    from jobs.market_intelligence_refresh import build_parser, plan

    scheduled = plan(build_parser().parse_args(["--all-configured"]), {})
    names = {step["step"] for step in scheduled["steps"]}
    assert "fred_macro_backfill" not in names
    assert "fred_macro_coverage" not in names
    explicit = plan(build_parser().parse_args(["--fred-macro-backfill"]), {})
    step = explicit["steps"][0]
    assert step["mode"] == "max"
    assert step["series"] == list(MACRO_MAX_BACKFILL_SERIES)


def _texts(at: AppTest) -> str:
    chunks = []
    for widget in (*at.title, *at.subheader, *at.caption, *at.markdown, *at.info):
        chunks.append(str(widget.value))
    return "\n".join(chunks)


def _fake_read(fn_name, *args, **kwargs):
    if fn_name == "observation_history":
        series_id = args[0]
        value = 1.0 if series_id != "USREC" else 1.0
        return [
            {"observation_date": date(2020, 1, 1), "value": 0.0 if series_id == "USREC" else value},
            {"observation_date": date(2020, 3, 1), "value": value},
            {"observation_date": date(2024, 6, 1), "value": value + 1},
        ]
    if fn_name == "metric_history":
        return [
            {"as_of": date(2020, 3, 1), "value": 2.5},
            {"as_of": date(2024, 6, 1), "value": 2.8},
        ]
    raise AssertionError(fn_name)


def _macro_page(monkeypatch, section: str) -> AppTest:
    monkeypatch.setattr("market_intelligence.ui.cached_read", _fake_read)
    at = AppTest.from_file(str(ROOT / "pages" / "11_Macro_Overview.py"), default_timeout=40)
    at.session_state["macro_section"] = section
    at.run()
    assert not at.exception, [item.value for item in at.exception]
    return at


def test_macro_page_renders_each_subsection(monkeypatch):
    for group in GROUP_ORDER:
        at = _macro_page(monkeypatch, GROUP_LABELS[group])
        assert at.title[0].value == "Macro & Liquidity"
        assert len(at.dataframe) == 0
        landed = _texts(at)
        assert "latest stored FRED vintage" in landed
        for title in CHART_TITLES[group]:
            assert title in landed, group
    leading = _macro_page(monkeypatch, "Leading indicators")
    assert "Conference Board" in _texts(leading)
    coincident = _macro_page(monkeypatch, "Coincident indicators")
    assert "proprietary coincident index" in _texts(coincident)


def test_chart_frontend_does_not_request_market_data():
    chart_js = (ROOT / "market_intelligence" / "components" / "market_chart" / "frontend" / "chart.js").read_text(encoding="utf-8")
    tenor_js = (ROOT / "market_intelligence" / "components" / "tenor_chart" / "frontend" / "chart.js").read_text(encoding="utf-8")
    macro_ui = (ROOT / "market_intelligence" / "macro_ui.py").read_text(encoding="utf-8")
    for text in (chart_js, tenor_js, macro_ui):
        assert "fetch(" not in text
        assert "XMLHttpRequest" not in text
        assert "fred.stlouisfed.org" not in text
    assert "recession_bands" in chart_js or "recession" in chart_js
    assert "No network calls" in chart_js
