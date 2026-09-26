"""Multi-series payloads for the shared Lightweight Charts component."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from market_intelligence.components.market_chart import (
    build_market_chart_payload,
    time_series_points,
)


def test_multi_series_payload_aligns_dates_with_whitespace():
    payload = build_market_chart_payload(
        [{"time": "1999-01-01", "value": 1}],
        series_label="Ignored",
        series=[
            {
                "label": "VIX",
                "points": [
                    {"as_of": "2026-09-19", "value": None},
                    {"as_of": "2026-09-18", "value": 10},
                    {"as_of": "2026-09-18", "value": 14.92},
                ],
            },
            {
                "label": "GSPC RV21",
                "points": [
                    {"time": "2026-09-19", "value": 11.34},
                    {"as_of": "2026-09-16", "value": 9},
                    {"as_of": "2026-09-16", "value": None},
                    {"as_of": "2026-09-16", "value": 10.5},
                ],
            },
        ],
        ranges=True,
        value_format="number",
    )
    assert [item["label"] for item in payload["series"]] == ["VIX", "GSPC RV21"]
    assert payload["series"][0]["points"] == [
        {"time": "2026-09-16"},
        {"time": "2026-09-18", "value": 14.92},
        {"time": "2026-09-19"},
    ]
    assert payload["series"][1]["points"] == [
        {"time": "2026-09-16", "value": 10.5},
        {"time": "2026-09-18"},
        {"time": "2026-09-19", "value": 11.34},
    ]
    assert "1999-01-01" not in json.dumps(payload)
    assert payload["ranges"] is True
    assert payload["value_format"] == "number"
    for item in payload["series"]:
        for point in item["points"]:
            if "value" not in point:
                assert set(point) == {"time"}


def test_dates_do_not_shift_across_timezones():
    eastern = timezone(timedelta(hours=-4))
    payload = build_market_chart_payload(
        series=[
            {
                "label": "VIX",
                "points": [
                    {"as_of": datetime(2026, 9, 18, 23, 30, tzinfo=eastern), "value": 14.92},
                    {"as_of": "2026-09-18T23:30:00-04:00", "value": 14.5},
                ],
            },
            {
                "label": "GSPC RV21",
                "points": [{"as_of": "2026-09-19T03:30:00Z", "value": 11.34}],
            },
        ]
    )
    assert payload["series"][0]["points"] == [
        {"time": "2026-09-18", "value": 14.5},
        {"time": "2026-09-19"},
    ]
    assert payload["series"][1]["points"][0] == {"time": "2026-09-18"}
    assert payload["series"][1]["points"][1] == {"time": "2026-09-19", "value": 11.34}
    encoded = json.dumps(payload)
    assert "2026-09-18" in encoded
    assert "T" not in encoded
    for item in payload["series"]:
        for point in item["points"]:
            assert isinstance(point["time"], str)
            assert point["time"].count("-") == 2


def test_missing_values_are_not_stored_as_zero():
    payload = build_market_chart_payload(
        series=[
            {
                "label": "VIX",
                "points": [
                    {"as_of": "2026-09-18", "value": 0},
                    {"as_of": "2026-09-19", "value": None},
                    {"as_of": "2026-09-20", "value": float("nan")},
                ],
            },
            {
                "label": "GSPC RV21",
                "points": [
                    {"as_of": "2026-09-19", "value": 11.34},
                    {"as_of": "2026-09-20", "value": None},
                ],
            },
        ]
    )
    vix = payload["series"][0]["points"]
    realized = payload["series"][1]["points"]
    assert vix == [
        {"time": "2026-09-18", "value": 0.0},
        {"time": "2026-09-19"},
    ]
    assert realized == [
        {"time": "2026-09-18"},
        {"time": "2026-09-19", "value": 11.34},
    ]
    assert all(point.get("value") != 0 or point["time"] == "2026-09-18" for point in vix)
    encoded = json.dumps(payload)
    assert "null" not in encoded
    assert "NaN" not in encoded
    whitespace = [point for item in payload["series"] for point in item["points"] if "value" not in point]
    assert whitespace
    assert all(set(point) == {"time"} for point in whitespace)


def test_points_stay_in_ascending_order():
    payload = build_market_chart_payload(
        series=[
            {
                "label": "VIX",
                "points": [
                    {"as_of": "2026-09-19", "value": 15.1},
                    {"as_of": "2026-09-16", "value": 13.25},
                    {"as_of": "2026-09-18", "value": 14.92},
                ],
            },
            {
                "label": "GSPC RV21",
                "points": [
                    {"as_of": "2026-09-17", "value": 11.0},
                    {"as_of": "2026-09-19", "value": 12.0},
                ],
            },
        ]
    )
    times = [point["time"] for point in payload["series"][0]["points"]]
    assert times == ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19"]
    assert times == sorted(times)
    for item in payload["series"]:
        assert [point["time"] for point in item["points"]] == times


def test_single_series_path_returns_one_series():
    rows = [
        {"as_of": "2026-09-18", "value": 14.92},
        {"as_of": "2026-09-17", "value": None},
        {"time": "2026-09-16", "value": 13.25},
    ]
    payload = build_market_chart_payload(rows, series_label="VIX")
    assert set(payload) == {"series", "ranges", "value_format"}
    assert len(payload["series"]) == 1
    assert payload["series"][0]["label"] == "VIX"
    assert payload["series"][0]["points"] == time_series_points(rows)
    assert payload["ranges"] is False
    assert payload["value_format"] == "number"
    assert all("value" in point for point in payload["series"][0]["points"])


def test_percent_format_is_passed_through_for_the_readout():
    payload = build_market_chart_payload(
        [{"time": "2026-09-18", "value": 1.25}],
        series_label="Move",
        ranges=False,
        value_format="percent",
    )
    assert payload["value_format"] == "percent"
    assert payload["ranges"] is False
    assert payload["series"] == [
        {"label": "Move", "points": [{"time": "2026-09-18", "value": 1.25}]}
    ]
