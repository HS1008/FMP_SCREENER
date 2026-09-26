"""Yahoo volatility analytics, ingest parsing, and Options page boundary. No live Yahoo."""

from __future__ import annotations

import inspect
import json
import math
import statistics
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from market_intelligence import yahoo_vol as vol
from market_intelligence.ingest_yahoo_vol import HISTORY_LOOKBACK_DAYS, TERM_HISTORY_PERIOD, fetch_yahoo_closes, ingest_yahoo_vol

ROOT = Path(__file__).resolve().parents[1]


def _gspc_series(end: date, n: int = 22, start_px: float = 5000.0) -> list[tuple[date, float]]:
    rows = []
    for i in range(n):
        day = end - timedelta(days=(n - 1 - i))
        rows.append((day, start_px + i))
    return rows


def _same_day_tenors(day: date, levels: dict[str, float | None]) -> dict[str, list[tuple[date, float]]]:
    out: dict[str, list[tuple[date, float]]] = {}
    for ticker, level in levels.items():
        if level is None:
            out[ticker] = []
        else:
            out[ticker] = [(day, float(level))]
    return out


def _complete_tenor_levels(**overrides: float | None) -> dict[str, float | None]:
    levels: dict[str, float | None] = {
        "^VIX": 15.0,
        "^VIX3M": 16.0,
        "^VIX6M": 17.0,
        "^VIX1Y": 18.0,
    }
    levels.update(overrides)
    return levels


def test_realized_vol_21_uses_21_log_returns_and_22_closes():
    closes = [100.0]
    for _ in range(20):
        closes.append(closes[-1] * math.exp(0.01))
    closes.append(closes[-1] * math.exp(0.02))
    assert len(closes) == 22
    out = vol.realized_vol_21(closes)
    assert out["status"] == "OK"
    assert out["window"] == 21
    assert out["returns"] == 21
    assert out["closes_required"] == 22
    assert out["ddof"] == 1
    assert out["underlying"] == "GSPC"
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    assert len(returns) == 21
    expected = statistics.stdev(returns) * math.sqrt(252) * 100.0
    assert out["value"] == pytest.approx(expected, rel=1e-9)
    assert out["value"] != 0


def test_realized_vol_21_from_series_uses_last_window_close_date():
    end = date(2026, 9, 23)
    rows = _gspc_series(end, n=22)
    out = vol.realized_vol_21_from_series(rows)
    assert out["status"] == "OK"
    assert out["observation_date"] == end
    assert out["window_start_date"] == rows[0][0]
    assert out["window_start_date"] == rows[-22][0]


def test_realized_vol_21_insufficient_history_stays_missing():
    twenty_one_closes = [100.0 + i for i in range(21)]
    short = vol.realized_vol_21(twenty_one_closes)
    assert short["status"] == "INCOMPLETE"
    assert short["value"] is None
    assert short["reason"] == "fewer_than_22_closes"
    assert vol.realized_vol_21([100.0] * 10)["value"] is None
    assert vol.realized_vol_21([])["value"] is None
    assert vol.historical_realized_vol(_gspc_series(date(2026, 9, 23), n=21)) == []


def test_historical_realized_vol_rolls_across_dates():
    end = date(2026, 9, 23)
    rows = _gspc_series(end, n=30)
    points = vol.historical_realized_vol(rows)
    assert len(points) == 9
    assert points[0]["observation_date"] == rows[21][0]
    assert points[-1]["observation_date"] == end
    assert all(point["status"] == "OK" and point["value"] not in (None, 0) for point in points)
    assert [point["observation_date"] for point in points] == [row[0] for row in rows[21:]]


def test_historical_vix_and_rv21_align_by_trading_date():
    end = date(2026, 9, 30)
    gspc = _gspc_series(end, n=30)
    rv_points = vol.historical_realized_vol(gspc)
    skipped = rv_points[2]["observation_date"]
    vix = [(point["observation_date"], 18.0) for point in rv_points if point["observation_date"] != skipped]
    spreads = vol.historical_vix_minus_rv(vix, rv_points)
    assert len(spreads) == len(rv_points) - 1
    assert skipped not in {spread["observation_date"] for spread in spreads}
    for spread in spreads:
        assert spread["observation_date"] == spread["vix_observation_date"] == spread["rv_observation_date"]
        assert spread["value"] is not None
        assert spread["value"] != 0


def test_vix_minus_rv_requires_matching_observation_dates():
    rv_ok = {"status": "OK", "value": 12.0, "reason": None, "observation_date": date(2026, 9, 23)}
    assert vol.vix_minus_rv(None, rv_ok, vix_observation_date=date(2026, 9, 23))["value"] is None
    assert vol.vix_minus_rv(20.0, {"status": "INCOMPLETE", "value": None, "reason": "x"})["value"] is None
    mismatch = vol.vix_minus_rv(
        20.0, rv_ok, vix_observation_date=date(2026, 9, 24), rv_observation_date=date(2026, 9, 23)
    )
    assert mismatch["status"] == "INCOMPLETE"
    assert mismatch["reason"] == "date_mismatch"
    assert mismatch["value"] is None
    spread = vol.vix_minus_rv(
        20.0, rv_ok, vix_observation_date=date(2026, 9, 23), rv_observation_date=date(2026, 9, 23)
    )
    assert spread["status"] == "OK"
    assert spread["value"] == pytest.approx(8.0)
    assert spread["observation_date"] == date(2026, 9, 23)


def test_align_vix_minus_rv_common_date_fallback():
    end = date(2026, 9, 23)
    gspc = _gspc_series(end, n=25)
    gspc_lagged = [(d, c) for d, c in gspc if d < end]
    aligned = vol.align_vix_minus_rv([(end - timedelta(days=1), 18.0)], gspc_lagged + [(end, 5100.0)])
    assert aligned["status"] == "OK"
    assert aligned["observation_date"] == end - timedelta(days=1)
    assert aligned["vix"] == pytest.approx(18.0)


def test_align_vix_minus_rv_fail_closed_on_no_overlap():
    end = date(2026, 9, 23)
    gspc = _gspc_series(end, n=22)
    vix = [(end + timedelta(days=5), 20.0)]
    out = vol.align_vix_minus_rv(vix, gspc)
    assert out["status"] == "INCOMPLETE"
    assert out["reason"] == "date_mismatch"
    assert out["value"] is None


def test_tenor_order_is_explicit_not_alphabetical():
    assert vol.TENOR_AXIS == ("1M", "3M", "6M", "1Y")
    assert [ticker for ticker, _tenor, _metric in vol.TERM_TENORS] == ["^VIX", "^VIX3M", "^VIX6M", "^VIX1Y"]
    assert [tenor for _ticker, tenor, _metric in vol.TERM_TENORS] == list(vol.TENOR_AXIS)
    assert list(vol.TENOR_AXIS) != sorted(vol.TENOR_AXIS)
    assert vol.TENOR_RANK["1M"] == 0
    assert vol.TENOR_RANK["1Y"] == 3
    assert "1D" not in vol.TENOR_AXIS
    assert "9D" not in vol.TENOR_AXIS
    day = date(2026, 9, 23)
    curve = vol.term_structure(_same_day_tenors(day, _complete_tenor_levels()))
    assert [point["tenor"] for point in curve["points"]] == ["1M", "3M", "6M", "1Y"]


def test_term_structure_all_tenors_same_date():
    day = date(2026, 9, 23)
    series = _same_day_tenors(day, _complete_tenor_levels())
    curve = vol.term_structure(series)
    assert curve["observation_date"] == day
    assert curve["front_tenor"] == "1M"
    assert curve["back_tenor"] == "1Y"
    assert curve["front_to_back_slope"] == pytest.approx(3.0)
    assert curve["curve_state"] == "upward_sloping"
    assert curve["slope_status"] == "OK"
    assert curve["unavailable_tenors"] == []
    assert all(p["observation_date"] == day for p in curve["points"])
    assert curve["points"][0]["ticker"] == "^VIX"


def test_term_structure_1m_missing_slope_incomplete():
    day = date(2026, 9, 23)
    series = _same_day_tenors(day, _complete_tenor_levels(**{"^VIX": None}))
    curve = vol.term_structure(series)
    assert curve["observation_date"] == day
    assert curve["front_to_back_slope"] is None
    assert curve["slope_status"] == "INCOMPLETE"
    assert "1M" in curve["unavailable_tenors"]


def test_term_structure_1y_missing_slope_incomplete():
    day = date(2026, 9, 23)
    series = _same_day_tenors(day, _complete_tenor_levels(**{"^VIX1Y": None}))
    curve = vol.term_structure(series)
    assert curve["front_to_back_slope"] is None
    assert curve["slope_status"] == "INCOMPLETE"
    assert "1Y" in curve["unavailable_tenors"]


def test_term_structure_intermediate_missing_still_publishes_slope():
    day = date(2026, 9, 23)
    series = _same_day_tenors(day, _complete_tenor_levels(**{"^VIX3M": None}))
    curve = vol.term_structure(series)
    assert curve["front_to_back_slope"] == pytest.approx(3.0)
    assert curve["slope_status"] == "OK"
    assert "3M" in curve["unavailable_tenors"]
    assert next(p for p in curve["points"] if p["tenor"] == "3M")["level"] is None


def test_term_structure_rejects_mixed_observation_dates():
    d1 = date(2026, 9, 22)
    d2 = date(2026, 9, 23)
    series = {
        "^VIX": [(d1, 99.0), (d2, 15.0)],
        "^VIX3M": [(d1, 50.0)],
        "^VIX6M": [(d2, 17.0)],
        "^VIX1Y": [(d2, 18.0)],
    }
    curve = vol.term_structure(series)
    assert curve["observation_date"] == d2
    assert curve["front_to_back_slope"] == pytest.approx(3.0)
    by_tenor = {p["tenor"]: p for p in curve["points"]}
    assert by_tenor["1M"]["level"] == pytest.approx(15.0)
    assert by_tenor["3M"]["level"] is None
    assert "3M" in curve["unavailable_tenors"]
    series2 = {
        "^VIX": [(d2, 15.0)],
        "^VIX3M": [(d1, 16.0)],
        "^VIX6M": [(d2, 17.0)],
        "^VIX1Y": [(d1, 18.0)],
    }
    curve2 = vol.term_structure(series2)
    assert curve2["observation_date"] == d2
    assert curve2["front_to_back_slope"] is None
    assert curve2["slope_status"] == "INCOMPLETE"
    by_tenor2 = {p["tenor"]: p for p in curve2["points"]}
    assert by_tenor2["3M"]["level"] is None
    assert by_tenor2["1Y"]["level"] is None
    assert by_tenor2["1M"]["level"] == pytest.approx(15.0)


def test_curve_shape_follows_1m_to_1y_not_alphabetical_ends():
    day = date(2026, 9, 23)
    # Sorted labels are 1M, 1Y, 3M, 6M, so a first-to-last alphabetical slope
    # would use 6M − 1M and look upward. The published slope is 1Y minus 1M.
    levels = _complete_tenor_levels(**{"^VIX": 20.0, "^VIX3M": 18.0, "^VIX6M": 22.0, "^VIX1Y": 12.0})
    curve = vol.term_structure(_same_day_tenors(day, levels))
    assert curve["front_tenor"] == "1M"
    assert curve["back_tenor"] == "1Y"
    assert curve["front_to_back_slope"] == pytest.approx(-8.0)
    assert curve["curve_state"] == "downward_sloping"
    assert [point["tenor"] for point in curve["points"]] == list(vol.TENOR_AXIS)


def test_term_structure_slope_labels_not_contango():
    day = date(2026, 9, 23)
    curve = vol.term_structure(_same_day_tenors(day, _complete_tenor_levels()))
    assert curve["curve_state"] == "upward_sloping"
    assert curve["curve_state"] not in {"contango", "backwardation"}
    flat = vol.term_structure(
        _same_day_tenors(day, _complete_tenor_levels(**{"^VIX": 15.02, "^VIX3M": None, "^VIX6M": None, "^VIX1Y": 15.01}))
    )
    assert flat["curve_state"] == "flat"
    down = vol.term_structure(
        _same_day_tenors(day, _complete_tenor_levels(**{"^VIX": 18.0, "^VIX3M": 16.0, "^VIX6M": 15.0, "^VIX1Y": 14.0}))
    )
    assert down["curve_state"] == "downward_sloping"
    assert "Never labeled contango/backwardation" in curve["note"]


def test_positive_number_rejects_zero_nan():
    assert vol.positive_number(0) is None
    assert vol.positive_number(-1) is None
    assert vol.positive_number(float("nan")) is None
    assert vol.positive_number("12.5") == pytest.approx(12.5)


def test_fetch_yahoo_closes_parses_history_frame():
    idx = pd.to_datetime(["2026-09-22", "2026-09-23"])
    frame = pd.DataFrame({"Close": [15.2, 16.1]}, index=idx)

    class _Ticker:
        def history(self, **_kwargs):
            return frame

    with patch("yfinance.Ticker", return_value=_Ticker()):
        rows = fetch_yahoo_closes("^VIX", start=date(2026, 9, 1), end=date(2026, 9, 23))
    assert rows == [(date(2026, 9, 22), 15.2), (date(2026, 9, 23), 16.1)]


def test_fetch_yahoo_closes_empty_on_blank():
    class _Ticker:
        def history(self, **_kwargs):
            return pd.DataFrame()

    with patch("yfinance.Ticker", return_value=_Ticker()):
        assert fetch_yahoo_closes("^SKEW", start=date(2026, 9, 1), end=date(2026, 9, 23)) == []


def test_ingest_observation_dates_not_run_day():
    """Refresh on day T with Yahoo closes through T-1 must date metrics/freshness as T-1."""
    run_day = date(2026, 9, 24)
    obs = date(2026, 9, 23)
    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value = MagicMock()
    engine.begin.return_value.__exit__.return_value = False
    written: list[dict] = []
    freshness: list[dict] = []

    def fake_fetch(ticker, *, start, end, period=None):
        assert end == run_day
        assert period is None
        if ticker == "^GSPC":
            return _gspc_series(obs, n=22)
        if ticker == "^VIX":
            return [(obs - timedelta(days=1), 18.0), (obs, 18.5)]
        if ticker == "^SKEW":
            return [(obs, 135.0)]
        return [(obs, 17.0 + (hash(ticker) % 5))]

    def capture_write(_engine, rows, _run_id):
        written.extend(rows)
        return len(rows)

    def capture_freshness(_conn, **kwargs):
        freshness.append(kwargs)

    with (
        patch("market_intelligence.ingest_yahoo_vol.fetch_yahoo_closes", side_effect=fake_fetch),
        patch("market_intelligence.ingest_yahoo_vol.start_run", return_value="run-1"),
        patch("market_intelligence.ingest_yahoo_vol.finish_run"),
        patch("market_intelligence.ingest_yahoo_vol.record_freshness", side_effect=capture_freshness),
        patch("market_intelligence.ingest_yahoo_vol._ensure_source"),
        patch("market_intelligence.ingest_yahoo_vol._write_rows", side_effect=capture_write),
    ):
        report = ingest_yahoo_vol(engine, today=run_day)

    assert report["run_day"] == run_day.isoformat()
    assert report["sections"]["vix"]["observation_date"] == obs.isoformat()
    assert report["sections"]["iv_minus_rv"]["observation_date"] == obs.isoformat()
    assert report["sections"]["iv_minus_rv"]["rv_observation_date"] == obs.isoformat()
    assert report["sections"]["vix_term_structure"]["observation_date"] == obs.isoformat()

    by_id = {
        row["metric_id"]: row
        for row in written
        if row["metric_id"]
        in {"GSPC_REALIZED_VOL_21D", "VIX_MINUS_GSPC_RV21", "VIX_INDEX_FRONT_TO_BACK", "VIX_1M", "VIX_3M", "VIX_1Y"}
    }
    assert by_id["GSPC_REALIZED_VOL_21D"]["as_of"] == obs
    assert by_id["VIX_MINUS_GSPC_RV21"]["as_of"] == obs
    assert by_id["VIX_MINUS_GSPC_RV21"]["status"] == "OK"
    assert by_id["VIX_INDEX_FRONT_TO_BACK"]["as_of"] == obs
    assert json.loads(by_id["VIX_INDEX_FRONT_TO_BACK"]["detail"])["front_tenor"] == "1M"
    assert by_id["VIX_1M"]["as_of"] == obs
    assert by_id["VIX_3M"]["as_of"] == obs
    assert by_id["VIX_1Y"]["as_of"] == obs
    assert not any(row["metric_id"] in {"VIX_1D", "VIX_9D"} for row in written)
    assert all(row["as_of"] != run_day for row in written)

    for mark in freshness:
        assert mark["latest_observation"] != run_day
        if mark["dataset"] in {"vix", "skew", "iv_minus_rv", "vix_term_structure"} and mark["success"]:
            assert mark["latest_observation"] == obs


def test_ingest_spread_incomplete_when_vix_gspc_dates_diverge_without_common_window():
    run_day = date(2026, 9, 24)
    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value = MagicMock()
    engine.begin.return_value.__exit__.return_value = False
    written: list[dict] = []

    def fake_fetch(ticker, *, start, end, period=None):
        assert period is None
        if ticker == "^GSPC":
            return _gspc_series(date(2026, 9, 20), n=22)
        if ticker == "^VIX":
            return [(date(2026, 9, 23), 18.5)]
        if ticker == "^SKEW":
            return [(date(2026, 9, 20), 135.0)]
        return [(date(2026, 9, 20), 16.0)]

    with (
        patch("market_intelligence.ingest_yahoo_vol.fetch_yahoo_closes", side_effect=fake_fetch),
        patch("market_intelligence.ingest_yahoo_vol.start_run", return_value="run-1"),
        patch("market_intelligence.ingest_yahoo_vol.finish_run"),
        patch("market_intelligence.ingest_yahoo_vol.record_freshness"),
        patch("market_intelligence.ingest_yahoo_vol._ensure_source"),
        patch(
            "market_intelligence.ingest_yahoo_vol._write_rows",
            side_effect=lambda _e, rows, _r: written.extend(rows) or len(rows),
        ),
    ):
        report = ingest_yahoo_vol(engine, today=run_day)

    assert report["sections"]["iv_minus_rv"]["status"] == "INCOMPLETE"
    assert report["sections"]["iv_minus_rv"]["reason"] == "date_mismatch"
    spread_rows = [r for r in written if r["metric_id"] == "VIX_MINUS_GSPC_RV21"]
    assert spread_rows
    assert spread_rows[0]["status"] == "INCOMPLETE"
    assert spread_rows[0]["value"] is None
    assert spread_rows[0]["as_of"] == date(2026, 9, 20)


def test_resolve_curve_date_exact_weekend_and_no_lookahead():
    common = [date(2026, 9, 24), date(2026, 9, 25)]
    assert vol.resolve_curve_date(date(2026, 9, 25), common) == date(2026, 9, 25)
    assert vol.resolve_curve_date(date(2026, 9, 26), common) == date(2026, 9, 25)
    assert vol.resolve_curve_date(date(2026, 9, 27), common) == date(2026, 9, 25)
    holiday_gap = [date(2026, 9, 21), date(2026, 9, 24)]
    assert vol.resolve_curve_date(date(2026, 9, 23), holiday_gap) == date(2026, 9, 21)
    assert vol.resolve_curve_date(date(2026, 9, 20), common) is None
    assert vol.resolve_curve_date(None, common) == date(2026, 9, 25)


def test_partial_history_is_selectable_and_not_filled():
    assert vol.TERM_TENORS[0] == ("^VIX", "1M", "VIX_1M")
    later = date(2026, 9, 24)
    earlier = date(2010, 1, 4)
    prior = date(2010, 1, 3)
    history: dict[str, list[dict]] = {
        "VIX_1M": [
            {"as_of": earlier, "value": 15.0},
            {"as_of": later, "value": 16.0},
        ],
        "VIX_3M": [{"as_of": prior, "value": 50.0}, {"as_of": later, "value": 17.0}],
        "VIX_6M": [{"as_of": earlier, "value": 18.0}, {"as_of": later, "value": 19.0}],
        "VIX_1Y": [{"as_of": later, "value": 20.0}],
        "VIX_1D": [{"as_of": earlier, "value": 11.0}],
    }
    available = vol.available_curve_dates_from_history(history)
    assert earlier in available
    assert prior in available
    assert later in available
    earlier_levels = vol.curve_levels_on_date(history, earlier)
    by_tenor = {point["tenor"]: point for point in earlier_levels}
    assert [point["tenor"] for point in earlier_levels] == ["1M", "3M", "6M", "1Y"]
    assert by_tenor["1M"]["value"] == pytest.approx(15.0)
    assert by_tenor["1M"]["as_of"] == "2010-01-04"
    assert by_tenor["3M"]["value"] is None
    assert by_tenor["3M"]["as_of"] is None
    assert by_tenor["6M"]["value"] == pytest.approx(18.0)
    assert by_tenor["1Y"]["value"] is None
    assert vol.resolve_curve_date(date(2010, 1, 4), available) == earlier
    assert vol.resolve_curve_date(date(2010, 1, 2), available) is None


def test_ingest_writes_historical_rv21_series():
    run_day = date(2026, 9, 24)
    obs = date(2026, 9, 23)
    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value = MagicMock()
    engine.begin.return_value.__exit__.return_value = False
    written: list[dict] = []
    gspc = _gspc_series(obs, n=30)

    def fake_fetch(ticker, *, start, end, period=None):
        assert end == run_day
        assert period is None
        assert start == run_day - timedelta(days=HISTORY_LOOKBACK_DAYS)
        if ticker == "^GSPC":
            return gspc
        if ticker == "^VIX":
            return [(day, 18.0) for day, _close in gspc]
        if ticker == "^SKEW":
            return [(obs, 135.0)]
        return [(obs, 16.0), (obs - timedelta(days=1), 15.5)]

    with (
        patch("market_intelligence.ingest_yahoo_vol.fetch_yahoo_closes", side_effect=fake_fetch),
        patch("market_intelligence.ingest_yahoo_vol.start_run", return_value="run-1"),
        patch("market_intelligence.ingest_yahoo_vol.finish_run"),
        patch("market_intelligence.ingest_yahoo_vol.record_freshness"),
        patch("market_intelligence.ingest_yahoo_vol._ensure_source"),
        patch(
            "market_intelligence.ingest_yahoo_vol._write_rows",
            side_effect=lambda _e, rows, _r: written.extend(rows) or len(rows),
        ),
    ):
        ingest_yahoo_vol(engine, today=run_day)

    rv_rows = [row for row in written if row["metric_id"] == "GSPC_REALIZED_VOL_21D"]
    spread_rows = [row for row in written if row["metric_id"] == "VIX_MINUS_GSPC_RV21" and row["status"] == "OK"]
    assert len(rv_rows) == 9
    assert len(spread_rows) == 9
    assert rv_rows[0]["as_of"] != rv_rows[-1]["as_of"]
    assert all(row["value"] not in (None, 0) for row in rv_rows)
    assert [row["as_of"] for row in rv_rows] == [row["as_of"] for row in spread_rows]
    vix1m = [row for row in written if row["metric_id"] == "VIX_1M"]
    assert {row["as_of"] for row in vix1m} == {day for day, _close in gspc}
    assert not any(row["metric_id"] == "VIX_1D" for row in written)


def test_options_page_shows_yahoo_core_without_provider_imports(monkeypatch):
    history = {
        "VIX_SPOT": [
            {"as_of": "2026-09-22", "value": 18.0},
            {"as_of": "2026-09-24", "value": 18.5},
        ],
        "SKEW_INDEX": [
            {"as_of": "2026-09-22", "value": 134.0},
            {"as_of": "2026-09-24", "value": 135.0},
        ],
        "GSPC_REALIZED_VOL_21D": [
            {"as_of": "2026-09-22", "value": 14.2},
            {"as_of": "2026-09-24", "value": 14.5},
        ],
        "VIX_MINUS_GSPC_RV21": [
            {"as_of": "2026-09-22", "value": 3.8},
            {"as_of": "2026-09-24", "value": 4.0},
        ],
    }
    for metric_id, level in (
        ("VIX_1M", 18.5),
        ("VIX_3M", 19.0),
        ("VIX_6M", 19.5),
        ("VIX_1Y", 20.0),
    ):
        history[metric_id] = [
            {"as_of": "2026-09-22", "value": level - 0.4},
            {"as_of": "2026-09-24", "value": level},
        ]
    yahoo = {
        "status": "OK",
        "vix": {"metric_id": "VIX_SPOT", "value": 18.5, "status": "OK", "as_of": "2026-09-24"},
        "skew": {"metric_id": "SKEW_INDEX", "value": 135.0, "status": "OK", "as_of": "2026-09-24"},
        "spread": {"metric_id": "VIX_MINUS_GSPC_RV21", "value": 4.0, "status": "OK", "as_of": "2026-09-24"},
        "slope": {"metric_id": "VIX_INDEX_FRONT_TO_BACK", "value": 1.2, "status": "OK", "as_of": "2026-09-24"},
        "rv21": {"metric_id": "GSPC_REALIZED_VOL_21D", "value": 14.5, "status": "OK", "as_of": "2026-09-24"},
        "curve_state": "upward_sloping",
        "curve_observation_date": "2026-09-24",
        "curve_dates": ["2026-09-22", "2026-09-24"],
        "unavailable_tenors": [],
        "history": history,
    }

    def fake_cached(fn_name, *args, **kwargs):
        if fn_name == "options_volatility_context":
            return {
                "status": "OK",
                "symbols": [],
                "vix": None,
                "yahoo_core": yahoo,
                "reason": "No published OpenBB/Cboe snapshots",
            }
        if fn_name == "options_chain_details":
            return []
        raise RuntimeError(fn_name)

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_cached)
    page = (ROOT / "pages" / "21_Options_Volatility.py").read_text(encoding="utf-8")
    ui = (ROOT / "market_intelligence" / "pages_ui.py").read_text(encoding="utf-8")
    for blob in (page, ui):
        assert "yfinance" not in blob
        assert "ingest_yahoo_vol" not in blob
        assert "cboe_client" not in blob
    at = AppTest.from_file(str(ROOT / "pages" / "21_Options_Volatility.py"), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    text = " ".join(
        str(getattr(el, "value", el))
        for kind in ("title", "subheader", "caption", "markdown", "info")
        for el in getattr(at, kind)
    )
    assert "Cboe SKEW Index" in text
    assert "Core volatility" in text
    assert "Implied vs Realized Vol" in text
    assert "VIX − GSPC RV21" in text
    assert "Curve as of September 24, 2026" in text
    assert "RV20" not in text
    assert "GSPC RV20" not in text
    labels = [metric.label for metric in at.metric]
    assert "Implied − Realized Vol" in labels
    assert "VIX index curve" in labels
    assert at.date_input
    assert at.date_input[0].value == date(2026, 9, 24)
    assert not any("Tenor" in str(frame) for frame in at.dataframe)
    from market_intelligence import pages_ui

    source = inspect.getsource(pages_ui._render_yahoo_vol_core)
    assert "st.columns(2)" not in source
    assert "st.dataframe" not in source
    assert "RV20" not in source
    assert "tenor_curve_chart" in source
    assert "st.plotly_chart" not in source
    assert "go.Figure" not in source
    at.date_input[0].set_value(date(2026, 9, 23)).run()
    rerun_text = " ".join(
        str(getattr(el, "value", el))
        for kind in ("title", "subheader", "caption", "markdown", "info")
        for el in getattr(at, kind)
    )
    assert "Curve as of September 22, 2026" in rerun_text
    assert "Curve as of September 24, 2026" not in rerun_text


def test_options_page_nulls_stay_unavailable(monkeypatch):
    def fake_cached(fn_name, *args, **kwargs):
        if fn_name == "options_volatility_context":
            return {
                "status": "UNAVAILABLE",
                "symbols": [],
                "vix": None,
                "yahoo_core": {"status": "UNAVAILABLE", "reason": "No Yahoo volatility metrics published yet."},
                "reason": "empty",
            }
        raise RuntimeError(fn_name)

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_cached)
    at = AppTest.from_file(str(ROOT / "pages" / "21_Options_Volatility.py"), default_timeout=30)
    at.run()
    assert not at.exception
    text = " ".join(str(el.value) for el in at.info)
    assert "unavailable" in text.lower() or "No Yahoo" in text


def test_full_backfill_uses_max_history_only_for_active_term_tickers():
    run_day = date(2026, 9, 24)
    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value = MagicMock()
    engine.begin.return_value.__exit__.return_value = False
    calls: list[tuple] = []

    def fake_fetch(ticker, *, start, end, period=None):
        calls.append((ticker, start, period))
        assert end == run_day
        return [(run_day - timedelta(days=1), 16.0)]

    with (
        patch("market_intelligence.ingest_yahoo_vol.fetch_yahoo_closes", side_effect=fake_fetch),
        patch("market_intelligence.ingest_yahoo_vol.start_run", return_value="run-1"),
        patch("market_intelligence.ingest_yahoo_vol.finish_run"),
        patch("market_intelligence.ingest_yahoo_vol.record_freshness"),
        patch("market_intelligence.ingest_yahoo_vol._ensure_source"),
        patch("market_intelligence.ingest_yahoo_vol._write_rows", side_effect=lambda _e, rows, _r: len(rows)),
    ):
        report = ingest_yahoo_vol(engine, today=run_day, mode="full")

    assert report["mode"] == "full"
    assert TERM_HISTORY_PERIOD == "max"
    by_period = {}
    for ticker, start, period in calls:
        by_period.setdefault(ticker, []).append((start, period))
    assert "^VIX1D" not in by_period
    assert "^VIX9D" not in by_period
    assert by_period["^SKEW"] == [(run_day - timedelta(days=HISTORY_LOOKBACK_DAYS), None)]
    assert by_period["^GSPC"] == [(run_day - timedelta(days=HISTORY_LOOKBACK_DAYS), None)]
    assert (run_day - timedelta(days=HISTORY_LOOKBACK_DAYS), None) in by_period["^VIX"]
    assert any(period == "max" for _start, period in by_period["^VIX"])
    for ticker in ("^VIX3M", "^VIX6M", "^VIX1Y"):
        assert by_period[ticker] == [(run_day - timedelta(days=HISTORY_LOOKBACK_DAYS), "max")]
    assert report["rows_by_ticker"]["^VIX1Y"] == 1
    assert report["sections"]["vix_term_structure"]["rows_by_ticker"]["^VIX"] == 1
