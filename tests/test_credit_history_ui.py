"""Credit EM OAS, rating tiles, stored-history date ranges, and implied−realized chart."""

from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from market_intelligence.catalog import (
    CATALOG_BY_ID,
    CREDIT_BROAD_BUCKETS,
    CREDIT_BROAD_TILES,
    CREDIT_RATING_TILES,
    CREDIT_SERIES,
    EXPORT_RESTRICTED,
    ICE_ATTRIBUTION,
)
from market_intelligence.components.market_chart import build_market_chart_payload
from market_intelligence.freshness import SERIES_POLICIES
from market_intelligence.history_range import (
    align_range_selection,
    chart_series_from_histories,
    filter_history_rows,
    ordered_selection,
    pills_layout_kwargs,
    union_history_bounds,
)
from market_intelligence.ingest_fred import request_window
from market_intelligence.pages_ui import _render_yahoo_vol_core

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "pages" / "13_Credit_Overview.py"


def _point(day: str, value):
    return {"as_of": day, "value": value, "units": "bps", "status": "OK"}


def test_catalog_adds_emerging_markets_oas_without_the_usd_subset():
    spec = CATALOG_BY_ID["BAMLEMCBPIOAS"]
    assert spec.category == "credit"
    assert spec.subcategory == "em_broad"
    assert spec.expected_frequency == "D"
    assert spec.expected_units_contains == ("percent",)
    assert spec.expected_sa == "NSA"
    assert spec.value_kind == "percent"
    assert spec.transforms == ("oas_bps", "chg_bps", "pctile_available")
    assert spec.export_scope == EXPORT_RESTRICTED
    assert spec.attribution == ICE_ATTRIBUTION
    assert spec.label == "EM Corporate OAS"
    assert "ICE BofA Emerging Markets Corporate Plus Index Option-Adjusted Spread" in spec.notes
    assert "BAMLEMUBCRPIUSOAS" not in CREDIT_SERIES
    assert "BAMLEMUBCRPIUSOAS" not in CATALOG_BY_ID
    assert CREDIT_SERIES.index("BAMLEMCBPIOAS") == 2
    assert SERIES_POLICIES["BAMLEMCBPIOAS"].cadence == "D"
    assert [label for _sid, label in CREDIT_RATING_TILES] == ["AAA", "AA", "A", "BBB", "BB", "B", "CCC & lower"]
    assert [sid for sid, _label in CREDIT_RATING_TILES] == [
        "BAMLC0A1CAAA",
        "BAMLC0A2CAA",
        "BAMLC0A3CA",
        "BAMLC0A4CBBB",
        "BAMLH0A1HYBB",
        "BAMLH0A2HYB",
        "BAMLH0A3HYC",
    ]
    assert [label for _sid, label in CREDIT_BROAD_TILES] == ["IG", "HY", "EM"]
    for sid, _label in (*CREDIT_BROAD_TILES, *CREDIT_RATING_TILES):
        assert CATALOG_BY_ID[sid].category == "credit"
        assert "oas_bps" in CATALOG_BY_ID[sid].transforms


def test_em_is_broad_market_not_a_rating_bucket():
    buckets = [
        {"bucket": "ig_broad", "series_id": "BAMLC0A0CM"},
        {"bucket": "hy_broad", "series_id": "BAMLH0A0HYM2"},
        {"bucket": "em_broad", "series_id": "BAMLEMCBPIOAS"},
        {"bucket": "aaa", "series_id": "BAMLC0A1CAAA"},
    ]
    broad = [row for row in buckets if row["bucket"] in CREDIT_BROAD_BUCKETS]
    rating = [row for row in buckets if row["bucket"] not in CREDIT_BROAD_BUCKETS]
    assert [row["series_id"] for row in broad] == ["BAMLC0A0CM", "BAMLH0A0HYM2", "BAMLEMCBPIOAS"]
    assert [row["series_id"] for row in rating] == ["BAMLC0A1CAAA"]


def test_pills_wrap_is_omitted_when_the_runtime_rejects_it(monkeypatch):
    import market_intelligence.history_range as history_range

    def pills_without_wrap(label, options, **kwargs):
        return options

    def pills_with_wrap(label, options, *, wrap=None, **kwargs):
        return options

    monkeypatch.setattr(history_range.st, "pills", pills_without_wrap)
    assert pills_layout_kwargs() == {}
    monkeypatch.setattr(history_range.st, "pills", pills_with_wrap)
    assert pills_layout_kwargs() == {"wrap": True}
    assert "wrap" in inspect.signature(history_range.st.pills).parameters


def test_rating_selection_order_select_all_and_clear_all():
    options = [sid for sid, _label in CREDIT_RATING_TILES]
    assert ordered_selection(options, options) == options
    assert ordered_selection(options, ["BAMLH0A3HYC", "BAMLC0A1CAAA", "missing"]) == ["BAMLC0A1CAAA", "BAMLH0A3HYC"]
    assert ordered_selection(options, []) == []
    several = ordered_selection(options, ["BAMLC0A4CBBB", "BAMLC0A1CAAA", "BAMLC0A3CA"])
    assert several == ["BAMLC0A1CAAA", "BAMLC0A3CA", "BAMLC0A4CBBB"]


def test_date_range_defaults_to_full_union_and_keeps_gaps():
    aaa = [_point("2020-01-02", 40), _point("2020-01-03", None), _point("2024-01-02", 42)]
    bbb = [_point("2021-06-01", 90), _point("2024-01-02", 94)]
    original = list(aaa)
    earliest, latest = union_history_bounds([aaa, bbb])
    assert (earliest, latest) == (date(2020, 1, 2), date(2024, 1, 2))
    start, end = align_range_selection(
        earliest=earliest,
        latest=latest,
        previous_span=None,
        current_from=None,
        current_to=None,
    )
    assert (start, end) == (earliest, latest)
    filtered = filter_history_rows(aaa, start=date(2020, 1, 2), end=date(2020, 1, 2))
    assert [row["as_of"] for row in filtered] == ["2020-01-02"]
    assert aaa == original
    assert filter_history_rows(aaa, start=date(2024, 1, 3), end=date(2024, 1, 2)) == []
    same_day = filter_history_rows(bbb, start=date(2021, 6, 1), end=date(2021, 6, 1))
    assert [row["value"] for row in same_day] == [90]
    custom = align_range_selection(
        earliest=earliest,
        latest=latest,
        previous_span=(earliest, latest),
        current_from=date(2021, 1, 1),
        current_to=latest,
    )
    assert custom == (date(2021, 1, 1), latest)
    widened = align_range_selection(
        earliest=date(1998, 12, 31),
        latest=latest,
        previous_span=(earliest, latest),
        current_from=earliest,
        current_to=latest,
    )
    assert widened == (date(1998, 12, 31), latest)


def test_multi_series_window_uses_union_and_does_not_fill():
    histories = {
        "BAMLC0A1CAAA": [_point("2020-01-02", 40), _point("2020-01-06", 42)],
        "BAMLC0A4CBBB": [_point("2020-01-03", 90)],
        "BAMLH0A3HYC": [_point("2022-01-03", 700)],
    }
    selected = [("BAMLC0A1CAAA", "AAA"), ("BAMLC0A4CBBB", "BBB"), ("BAMLH0A3HYC", "CCC & lower")]
    earliest, latest = union_history_bounds([histories[sid] for sid, _label in selected])
    assert earliest == date(2020, 1, 2)
    assert latest == date(2022, 1, 3)
    series, missing = chart_series_from_histories(selected, histories, start=earliest, end=date(2020, 1, 6))
    assert missing == ["CCC & lower"]
    assert [item["label"] for item in series] == ["AAA", "BBB"]
    payload = build_market_chart_payload(series=series, ranges=True, value_format="bps")
    by_label = {item["label"]: item["points"] for item in payload["series"]}
    aaa_by_day = {point["time"]: point.get("value") for point in by_label["AAA"]}
    bbb_by_day = {point["time"]: point.get("value") for point in by_label["BBB"]}
    assert aaa_by_day["2020-01-02"] == 40
    assert "value" not in next(point for point in by_label["AAA"] if point["time"] == "2020-01-03")
    assert bbb_by_day["2020-01-03"] == 90
    assert "2020-01-06" in bbb_by_day and "value" not in next(point for point in by_label["BBB"] if point["time"] == "2020-01-06")
    assert "2022-01-03" not in aaa_by_day
    lookahead = filter_history_rows(histories["BAMLH0A3HYC"], start=earliest, end=date(2020, 1, 6))
    assert lookahead == []


def test_implied_minus_realized_chart_uses_the_stored_spread_only():
    source = inspect.getsource(_render_yahoo_vol_core)
    assert "Implied − Realized Vol" in source
    assert "Implied vs Realized Vol" not in source
    assert "VIX_MINUS_GSPC_RV21" in source
    assert "GSPC_REALIZED_VOL_21D" not in source
    assert "yahoo_vix_curve_date" in source
    assert 'value_format="vol_points"' in source
    assert "historical_date_range" in source
    chart = (ROOT / "market_intelligence" / "components" / "market_chart" / "frontend" / "chart.js").read_text(encoding="utf-8")
    assert "vol pts" in chart
    assert 'state.valueFormat === "bps"' in chart


def test_credit_max_window_does_not_widen_incremental_refresh():
    spec = CATALOG_BY_ID["BAMLC0A0CM"]
    today = date(2026, 9, 25)
    incremental, _end = request_window(spec, mode="incremental", today=today, latest_stored=date(2026, 9, 24))
    assert incremental > date(2020, 1, 1)
    maximum, end = request_window(spec, mode="max", today=today, latest_stored=date(2026, 9, 24), provider_start=date(1996, 12, 31))
    assert maximum == date(1996, 12, 31)
    assert end == today


def _credit_context():
    def bucket(series_id, name, oas, change):
        return {
            "series_id": series_id,
            "bucket": CATALOG_BY_ID[series_id].subcategory,
            "label": CATALOG_BY_ID[series_id].label,
            "as_of": "2024-06-03",
            "oas_bps": oas,
            "change_1d_bps": change,
            "change_1w_bps": change,
            "change_1m_bps": change,
            "percentile_window": "1Y",
            "percentile": 40,
            "zscore": 0.1,
            "window_observations": 200,
            "history_first_date": "2020-01-02",
            "history_status": "AVAILABLE",
        }

    rows = [
        bucket("BAMLC0A0CM", "IG", 89, -1),
        bucket("BAMLH0A0HYM2", "HY", 320, 3),
        bucket("BAMLEMCBPIOAS", "EM", 260, 1),
        bucket("BAMLC0A1CAAA", "AAA", 42, 0),
        bucket("BAMLC0A2CAA", "AA", 55, 1),
        bucket("BAMLC0A3CA", "A", 68, 1),
        bucket("BAMLC0A4CBBB", "BBB", 94, 2),
        bucket("BAMLH0A1HYBB", "BB", 180, 2),
        bucket("BAMLH0A2HYB", "B", 310, 4),
        bucket("BAMLH0A3HYC", "CCC", 780, 6),
    ]
    return {
        "buckets": rows,
        "attribution": ICE_ATTRIBUTION,
        "coverage_note": "Provider history is limited.",
    }


def _histories():
    return {
        "BAMLC0A0CM.oas_bps": [_point("2019-01-02", 80), _point("2024-06-03", 89)],
        "BAMLH0A0HYM2.oas_bps": [_point("2020-01-02", 300), _point("2024-06-03", 320)],
        "BAMLEMCBPIOAS.oas_bps": [_point("2021-03-01", 250), _point("2024-06-03", 260)],
        "BAMLC0A1CAAA.oas_bps": [_point("2020-01-02", 40), _point("2020-01-06", 42)],
        "BAMLC0A2CAA.oas_bps": [_point("2020-01-02", 55)],
        "BAMLC0A3CA.oas_bps": [_point("2020-01-03", 68)],
        "BAMLC0A4CBBB.oas_bps": [_point("2020-01-03", 90), _point("2024-01-02", 94)],
        "BAMLH0A1HYBB.oas_bps": [_point("2020-02-03", 180)],
        "BAMLH0A2HYB.oas_bps": [_point("2020-02-03", 310)],
        "BAMLH0A3HYC.oas_bps": [_point("2022-01-03", 700)],
    }


def _run_credit(monkeypatch):
    calls = []
    histories = _histories()

    def fake_cached(fn_name, *args, **kwargs):
        if fn_name == "credit_context":
            return _credit_context()
        if fn_name == "metric_history":
            calls.append((args[0], kwargs.get("limit")))
            return list(histories.get(args[0], []))
        raise RuntimeError(fn_name)

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_cached)
    app = AppTest.from_file(str(PAGE), default_timeout=30)
    app.run()
    return app, calls


def _text(app):
    return " ".join(
        str(getattr(el, "value", el))
        for kind in ("title", "subheader", "caption", "markdown", "info")
        for el in getattr(app, kind)
    )


def _click(app, label):
    button = next(item for item in app.button if item.label == label)
    button.click()
    app.run()


def _click_nth(app, label, index):
    matches = [item for item in app.button if item.label == label]
    matches[index].click()
    app.run()


def test_spread_percent_change_uses_the_prior_oas():
    from market_intelligence.pages_ui import spread_percent_change

    assert spread_percent_change(110, 10) == pytest.approx(10.0)
    assert spread_percent_change(90, -10) == pytest.approx(-10.0)
    assert spread_percent_change(89, -1) == pytest.approx(-1 / 90 * 100)
    assert spread_percent_change(10, 10) is None
    assert spread_percent_change(None, 1) is None
    assert spread_percent_change(100, None) is None


def test_credit_page_broad_market_includes_em_and_full_history(monkeypatch):
    app, calls = _run_credit(monkeypatch)
    assert not app.exception, [exc.value for exc in app.exception]
    text = _text(app)
    headings = [item.value for item in app.subheader]
    assert headings[:4] == ["Broad market", "Ratings", "Broad market chart", "Ratings chart"]
    assert "Credit view" not in text
    assert "Sectors & subsectors" not in text
    assert "Select at least one series." not in text
    assert [item for item in app.pills[0].value] == [sid for sid, _label in CREDIT_BROAD_TILES]
    assert [item for item in app.pills[1].value] == [sid for sid, _label in CREDIT_RATING_TILES]
    assert app.date_input[0].value == date(2019, 1, 2)
    assert app.date_input[1].value == date(2024, 6, 3)
    broad = app.dataframe[0].value
    assert list(broad["Series"]) == ["IG OAS", "HY OAS", "EM OAS"]
    assert list(broad.columns) == ["Series", "As of", "OAS (bps)", "1D (bps)", "1W (bps)", "1M (bps)"]
    assert broad.loc[0, "As of"] == "2024-06-03"
    assert broad.loc[0, "1D (bps)"] == -1
    requested = [metric_id for metric_id, _limit in calls]
    app.radio[0].set_value("% change").run()
    changed = app.dataframe[0].value
    assert "1D (%)" in list(changed.columns)
    assert changed.loc[0, "1D (%)"] == pytest.approx(-1 / 90 * 100)
    assert changed.loc[0, "OAS (bps)"] == 89
    assert requested == ["{0}.oas_bps".format(sid) for sid, _label in (*CREDIT_BROAD_TILES, *CREDIT_RATING_TILES)]
    assert all(limit == 20000 for _metric_id, limit in calls)
    page = (ROOT / "market_intelligence" / "pages_ui.py").read_text(encoding="utf-8")
    assert "fred_client" not in page
    assert "yfinance" not in page
    assert "import yfinance" not in page


def test_rating_tiles_select_all_clear_all_and_survive_rerun(monkeypatch):
    app, calls = _run_credit(monkeypatch)
    assert not app.exception, [exc.value for exc in app.exception]
    assert [item for item in app.pills[1].value] == [sid for sid, _label in CREDIT_RATING_TILES]
    _click_nth(app, "Clear all", 1)
    assert list(app.pills[1].value) == []
    assert list(app.pills[0].value) == [sid for sid, _label in CREDIT_BROAD_TILES]
    assert "Select at least one rating." in _text(app)
    app.run()
    assert list(app.pills[1].value) == []
    assert "Select at least one rating." in _text(app)
    calls.clear()
    _click_nth(app, "Select all", 1)
    assert [item for item in app.pills[1].value] == [sid for sid, _label in CREDIT_RATING_TILES]
    assert "Select at least one rating." not in _text(app)
    requested = [metric_id for metric_id, _limit in calls]
    assert requested == ["{0}.oas_bps".format(sid) for sid, _label in (*CREDIT_BROAD_TILES, *CREDIT_RATING_TILES)]
    app.pills[1].set_value(["BAMLC0A1CAAA", "BAMLC0A4CBBB"]).run()
    assert ordered_selection(
        [sid for sid, _label in CREDIT_RATING_TILES],
        list(app.pills[1].value),
    ) == ["BAMLC0A1CAAA", "BAMLC0A4CBBB"]
    app.run()
    assert ordered_selection(
        [sid for sid, _label in CREDIT_RATING_TILES],
        list(app.pills[1].value),
    ) == ["BAMLC0A1CAAA", "BAMLC0A4CBBB"]


def test_credit_from_after_to_does_not_crash(monkeypatch):
    app, _calls = _run_credit(monkeypatch)
    app.date_input[0].set_value(date(2024, 6, 3)).run()
    app.date_input[1].set_value(date(2019, 1, 2)).run()
    assert not app.exception, [exc.value for exc in app.exception]
    assert "From must be on or before To." in _text(app)
    assert app.date_input[0].value == date(2024, 6, 3)
    assert app.date_input[1].value == date(2019, 1, 2)
