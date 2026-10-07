"""Published Overview snapshot + 1D overlay, batched history reads, cross-asset bar quality, calendar memoization."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from market_intelligence import calendars, macro_ui
from market_intelligence.cross_asset_universe import INSTRUMENT_BY_ID
from market_intelligence.ingest_yahoo_cross_asset import BAR_COMPLETE, BAR_PROVISIONAL, bar_quality_for
from market_intelligence.overview_live import EOD_LABEL, compose_overview
from market_intelligence.overview_publish import publish_overview_snapshot
from market_intelligence.overview_snapshot import (
    SECTION_COMMODITIES,
    SECTION_GLOBAL,
    SECTION_MARKET_RATIOS,
    SECTION_US_INDEXES,
    SECTION_VIX_TERM,
    SECTION_YIELD_CURVE,
    overview_snapshot,
)
from market_intelligence.pages_ui import _load_oas_histories
from market_intelligence.read_models import metric_histories, observation_histories
from market_intelligence.return_policy import BASIS_EOD_CLOSE, BASIS_SESSION_OPEN

ET = ZoneInfo("America/New_York")


def _et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


class _NoDb:
    def begin_nested(self):
        raise RuntimeError("no database")


def _quote(symbol: str, *, price: float, open_: float, close: float, ts: datetime, instrument_id: str | None = None) -> dict:
    return {
        "symbol": symbol,
        "display_name": symbol,
        "instrument_id": instrument_id or symbol,
        "last_price": price,
        "quote_ts": ts,
        "provenance": {
            "symbol": symbol,
            "current_price": price,
            "session_open": open_,
            "session_date": ts.date().isoformat(),
            "last_close": close,
            "last_close_date": "2026-10-06",
        },
    }


def _row_by_key(composed: dict, section_id: str, key: str) -> dict:
    for section in composed["sections"]:
        if section["section_id"] == section_id:
            for row in section["rows"]:
                if row["key"] == key:
                    return row
    raise AssertionError((section_id, key))


# ---- Overview: published EOD snapshot + stored-quote 1D overlay -----------------------------------


def test_compose_overview_overlays_only_1d_and_labels_rows_without_quotes_eod():
    snapshot = overview_snapshot(_NoDb(), today=date(2026, 10, 7))
    now = _et(2026, 10, 7, 15, 15)
    spy = _quote("SPY", price=505.0, open_=500.0, close=495.0, ts=_et(2026, 10, 7, 15, 14))
    qqq = _quote("QQQ", price=404.0, open_=400.0, close=399.0, ts=_et(2026, 10, 7, 15, 10))
    gold = _quote("GC=F", price=2020.0, open_=2000.0, close=1990.0, ts=_et(2026, 10, 7, 15, 12), instrument_id="GC")
    composed = compose_overview(snapshot, equity_quotes=[spy, qqq], cross_asset_quotes=[gold], now=now)

    spy_row = _row_by_key(composed, SECTION_US_INDEXES, "SPY")
    assert spy_row["changes"]["1D"] == pytest.approx(0.01)
    assert spy_row["one_day_basis"] == BASIS_SESSION_OPEN and spy_row["one_day_label"] == "Since session open"
    assert spy_row["level"] == 505.0 and spy_row["level_basis"] == "quote"
    assert datetime.fromisoformat(spy_row["observed_at"]) == _et(2026, 10, 7, 15, 14)
    assert "Last updated: Oct 7, 2026, 3:14 PM EDT" in spy_row["one_day_note"]
    # Longer horizons were never touched by the overlay (the EOD snapshot had none here).
    assert {k: v for k, v in spy_row["changes"].items() if k != "1D"} == {
        k: v for k, v in _row_by_key(snapshot, SECTION_US_INDEXES, "SPY")["changes"].items() if k != "1D"
    }

    # A tracked ETF without a stored quote is labeled EOD close-to-close, not pending.
    iwm_row = _row_by_key(composed, SECTION_US_INDEXES, "IWM")
    assert iwm_row["one_day_basis"] == BASIS_EOD_CLOSE and iwm_row["one_day_label"] == EOD_LABEL
    assert iwm_row["observed_at"] is None

    # Ratio: (QQQ/SPY) / (open_QQQ/open_SPY) - 1 on matching session-open bases; the tooltip names both legs.
    ratio_row = _row_by_key(composed, SECTION_MARKET_RATIOS, "QQQ_SPY")
    assert ratio_row["changes"]["1D"] == pytest.approx((404.0 / 505.0) / (400.0 / 500.0) - 1.0)
    assert "QQQ" in ratio_row["one_day_note"] and "SPY" in ratio_row["one_day_note"]
    assert "3:10 PM EDT" in ratio_row["one_day_note"]  # the older leg is exposed

    # Cross-asset rows key on instrument_id (GC), not the Yahoo symbol.
    gold_row = _row_by_key(composed, SECTION_COMMODITIES, "GC")
    assert gold_row["changes"]["1D"] == pytest.approx(0.01) and gold_row["one_day_basis"] == BASIS_SESSION_OPEN

    # EOD-only publishers are labeled as such even when quotes exist elsewhere.
    assert _row_by_key(composed, SECTION_YIELD_CURVE, "MOVE")["one_day_label"] == EOD_LABEL
    assert _row_by_key(composed, SECTION_VIX_TERM, "VIX_3M")["one_day_label"] == EOD_LABEL

    assert composed["eod_snapshot_id"] == snapshot["snapshot_id"]
    assert composed["snapshot_id"].startswith(snapshot["snapshot_id"] + "-")
    assert composed["live_rows"] == 5  # SPY (US indexes), QQQ, QQQ_SPY, SPY (global equity), GC
    assert _row_by_key(composed, SECTION_GLOBAL, "VEA")["one_day_label"] == EOD_LABEL


def test_composed_snapshot_id_changes_only_when_the_overlay_changes():
    snapshot = overview_snapshot(_NoDb(), today=date(2026, 10, 7))
    now = _et(2026, 10, 7, 15, 15)
    spy = _quote("SPY", price=505.0, open_=500.0, close=495.0, ts=_et(2026, 10, 7, 15, 14))
    first = compose_overview(snapshot, equity_quotes=[spy], cross_asset_quotes=[], now=now)
    same_quote_later = compose_overview(snapshot, equity_quotes=[spy], cross_asset_quotes=[], now=_et(2026, 10, 7, 15, 45))
    assert first["snapshot_id"] == same_quote_later["snapshot_id"]
    moved = compose_overview(
        snapshot,
        equity_quotes=[_quote("SPY", price=506.0, open_=500.0, close=495.0, ts=_et(2026, 10, 7, 15, 30))],
        cross_asset_quotes=[],
        now=now,
    )
    assert moved["snapshot_id"] != first["snapshot_id"]


class _RecordingConn:
    def __init__(self):
        self.statements: list[tuple[str, dict]] = []

    def begin_nested(self):
        raise RuntimeError("no database")

    def execute(self, statement, params=None):
        self.statements.append((str(getattr(statement, "text", statement)), dict(params or {})))
        return None


def test_publish_overview_snapshot_upserts_by_snapshot_id_and_prunes():
    conn = _RecordingConn()
    summary = publish_overview_snapshot(conn, now=datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc), keep=48)
    insert, prune = conn.statements
    assert "INSERT INTO mi_overview_snapshots" in insert[0]
    assert "ON CONFLICT (snapshot_id) DO UPDATE" in insert[0]
    assert "CAST(:payload AS jsonb)" in insert[0]
    assert insert[1]["snapshot_id"] == summary["snapshot_id"]
    assert insert[1]["published_at"].tzinfo is not None
    assert "DELETE FROM mi_overview_snapshots" in prune[0] and prune[1] == {"keep": 48}
    assert summary["payload_bytes"] > 100 and summary["rows_total"] > 0


# ---- Batched history reads ------------------------------------------------------------------------


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _BatchConn(_RecordingConn):
    def __init__(self, rows):
        super().__init__()
        self._rows = rows

    def execute(self, statement, params=None):
        super().execute(statement, params)
        return _Mappings(self._rows)


def test_observation_histories_issues_one_lateral_query_for_all_series():
    conn = _BatchConn(
        [
            {"series_id": "USREC", "observation_date": date(2020, 1, 1), "value": 0.0},
            {"series_id": "UNRATE", "observation_date": date(2020, 1, 1), "value": 3.5},
            {"series_id": "UNRATE", "observation_date": date(2020, 2, 1), "value": 3.6},
        ]
    )
    grouped = observation_histories(conn, ["UNRATE", "USREC", "UNRATE", ""], limit=500)
    assert len(conn.statements) == 1
    sql, params = conn.statements[0]
    assert "JOIN LATERAL" in sql and "mi_v_macro_observations_current" in sql and "LIMIT :limit" in sql
    assert params["limit"] == 500 and params["s0"] == "UNRATE" and params["s1"] == "USREC" and "s2" not in params
    assert [row["value"] for row in grouped["UNRATE"]] == [3.5, 3.6]
    assert grouped["USREC"][0]["observation_date"] == "2020-01-01"  # ISO strings, like observation_history
    assert observation_histories(conn, []) == {}


def test_metric_histories_groups_rows_and_keeps_requested_keys():
    conn = _BatchConn([{"metric_id": "BAMLC0A0CM.oas_bps", "as_of": date(2024, 6, 3), "value": 89.0, "units": "bps", "status": "OK"}])
    grouped = metric_histories(conn, ["BAMLC0A0CM.oas_bps", "BAMLH0A0HYM2.oas_bps"], limit=20000)
    assert len(conn.statements) == 1 and "mi_v_metric_history" in conn.statements[0][0]
    assert grouped["BAMLC0A0CM.oas_bps"][0]["value"] == 89.0
    assert grouped["BAMLH0A0HYM2.oas_bps"] == []


def test_macro_group_loads_in_two_reads_and_credit_in_one(monkeypatch):
    calls: list[tuple] = []

    def fake_load(fn_name, *args, **kwargs):
        calls.append((fn_name, tuple(args[0]) if args else ()))
        if fn_name == "observation_histories":
            return {sid: [{"observation_date": date(2024, 1, 1), "value": 1.0}] for sid in args[0]}
        if fn_name == "metric_histories":
            return {mid: [{"as_of": date(2024, 1, 1), "value": 2.0}] for mid in args[0]}
        raise AssertionError(fn_name)

    monkeypatch.setattr(macro_ui, "load_or_stop", fake_load)
    histories = macro_ui._load_sources(["UNRATE", "USREC", "M2SL.yoy_pct", "PAYEMS"])
    assert [name for name, _ids in calls] == ["observation_histories", "metric_histories"]
    assert calls[0][1] == ("UNRATE", "USREC", "PAYEMS") and calls[1][1] == ("M2SL.yoy_pct",)
    assert histories["UNRATE"] == [{"as_of": date(2024, 1, 1), "value": 1.0}]
    assert histories["M2SL.yoy_pct"] == [{"as_of": date(2024, 1, 1), "value": 2.0}]

    calls.clear()
    monkeypatch.setattr("market_intelligence.pages_ui.load_or_stop", fake_load)
    oas = _load_oas_histories(["BAMLC0A0CM", "BAMLH0A0HYM2"])
    assert [name for name, _ids in calls] == ["metric_histories"]
    assert calls[0][1] == ("BAMLC0A0CM.oas_bps", "BAMLH0A0HYM2.oas_bps")
    assert set(oas) == {"BAMLC0A0CM", "BAMLH0A0HYM2"} and oas["BAMLC0A0CM"][0]["value"] == 2.0
    assert _load_oas_histories([]) == {}


# ---- Cross-asset bar quality ----------------------------------------------------------------------


def test_cross_asset_bar_quality_follows_each_instruments_session():
    gold = INSTRUMENT_BY_ID["GC"]
    btc = INSTRUMENT_BY_ID["BTC"]
    eurusd = INSTRUMENT_BY_ID["EURUSD"]
    # CME Globex trade date 2026-10-07 closes 17:00 ET: a 15:00 ET collection is provisional.
    assert bar_quality_for(gold, date(2026, 10, 7), retrieved_at=_et(2026, 10, 7, 15, 0)) == BAR_PROVISIONAL
    assert bar_quality_for(gold, date(2026, 10, 7), retrieved_at=_et(2026, 10, 7, 17, 30)) == BAR_COMPLETE
    assert bar_quality_for(gold, date(2026, 10, 6), retrieved_at=_et(2026, 10, 7, 15, 0)) == BAR_COMPLETE
    # Crypto days are UTC: the 2026-10-07 bar is provisional until 00:00 UTC on the 8th.
    assert bar_quality_for(btc, date(2026, 10, 7), retrieved_at=datetime(2026, 10, 7, 23, 0, tzinfo=timezone.utc)) == BAR_PROVISIONAL
    assert bar_quality_for(btc, date(2026, 10, 7), retrieved_at=datetime(2026, 10, 8, 0, 5, tzinfo=timezone.utc)) == BAR_COMPLETE
    # FX rolls at 17:00 New York.
    assert bar_quality_for(eurusd, date(2026, 10, 7), retrieved_at=_et(2026, 10, 7, 16, 0)) == BAR_PROVISIONAL
    assert bar_quality_for(eurusd, date(2026, 10, 7), retrieved_at=_et(2026, 10, 7, 17, 5)) == BAR_COMPLETE


def test_cross_asset_history_read_excludes_provisional_bars_when_column_exists():
    from market_intelligence import cross_asset_read

    class _Conn(_RecordingConn):
        def execute(self, statement, params=None):
            super().execute(statement, params)
            text = str(getattr(statement, "text", statement))
            if "to_regclass" in text or "information_schema" in text:
                return _Scalar(1)
            return _Mappings([])

    class _Scalar:
        def __init__(self, value):
            self._value = value

        def scalar(self):
            return self._value

    conn = _Conn()
    cross_asset_read.yahoo_cross_asset_bars(conn, ["GC", "CL"], since=date(2026, 1, 1))
    history = [item for item in conn.statements if "mi_v_yahoo_cross_asset_history" in item[0] and "SELECT instrument_id" in item[0]]
    assert len(history) == 1
    sql, params = history[0]
    assert "bar_quality IS DISTINCT FROM 'PROVISIONAL'" in sql
    assert "instrument_id = ANY(:instrument_ids)" in sql and params["instrument_ids"] == ["GC", "CL"]
    assert params["since"] == date(2026, 1, 1)


# ---- Global Markets: regional ETFs are tracked quotes with the live 1D policy ---------------------


def test_global_markets_regional_etfs_use_the_stored_quote_1d_policy(monkeypatch):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from market_intelligence import markets_ui
    from market_intelligence.ibkr_live_universe import APPROVED_EQUITY_ETF_SYMBOLS

    assert all(symbol in APPROVED_EQUITY_ETF_SYMBOLS for symbol in ("VEA", "VWO", "EWJ", "VGK", "MCHI", "INDA", "EWZ"))
    symbols = ["SPY", "VEA", "VGK", "EWJ", "VWO", "MCHI", "INDA", "EWZ"]
    history = {
        "bars": {s: [{"date": "2026-10-06", "value": 100.0}] for s in symbols},
        "meta": {},
        "returns": {s: {"1D": 0.001, "1W": 0.02, "1M": 0.03, "3M": None, "6M": None, "1Y": None} for s in symbols},
        "latest_price": {s: 100.0 for s in symbols},
        "bounds": {"earliest": "2024-01-02", "latest": "2026-10-06"},
    }
    stamp = _et(2026, 10, 7, 15, 14)
    quotes = [_quote(s, price=102.0, open_=100.0, close=99.0, ts=stamp) for s in ("VEA", "VWO")]  # EWJ has no quote

    def fake_read(fn_name, *args, **kwargs):
        if fn_name == "global_markets_history":
            return history
        raise AssertionError(fn_name)

    def fake_quote_read(fn_name, *args, **kwargs):
        assert fn_name == "dashboard_quotes_latest"
        return quotes

    captured: dict = {}
    real = markets_ui.column_scaled_return_heatmap

    def spy(*args, **kwargs):
        if kwargs.get("key") == "global_heatmap":
            captured["labels"] = list(args[0])
            captured["columns"] = list(args[1])
            captured["values"] = [list(row) for row in args[2]]
            captured["notes"] = [list(row) for row in kwargs["notes"]]
        return real(*args, **kwargs)

    monkeypatch.setattr("market_intelligence.ui.cached_read", fake_read)
    monkeypatch.setattr("market_intelligence.ui.cached_quote_read", fake_quote_read)
    monkeypatch.setattr(markets_ui, "column_scaled_return_heatmap", spy)
    monkeypatch.setattr(markets_ui, "_now", lambda: _et(2026, 10, 7, 15, 15))
    root = Path(__file__).resolve().parents[1]
    at = AppTest.from_file(str(root / "pages" / "23_Global_Markets.py"), default_timeout=40)
    at.run()
    assert not at.exception, [item.value for item in at.exception]

    # Snapshot cards: VEA/VWO show the live since-open 1D with their own timestamp; EWJ stays labeled EOD.
    by_label = {m.label: m for m in at.metric}
    assert "+2.00% 1D (since session open)" in by_label["Developed ex-US"].delta
    assert "+2.00% 1D (since session open)" in by_label["Emerging Markets"].delta
    assert "+0.10% 1D (EOD)" in by_label["Japan"].delta
    captions = "\n".join(str(c.value) for c in at.caption)
    assert "Since session open: 2" in captions and "Oct 7, 2026, 3:14 PM EDT" in captions
    missing_caption = next(str(c.value) for c in at.caption if "No usable stored quote yet" in str(c.value))
    assert "EWJ" in missing_caption and "SPY" in missing_caption and "VEA" not in missing_caption

    # Heatmap: only the 1D column of quoted rows changes; 1W/1M stay the stored completed close-to-close values.
    one_d = captured["columns"].index("1D")
    rows = dict(zip(captured["labels"], captured["values"]))
    notes = dict(zip(captured["labels"], captured["notes"]))
    assert rows["Developed ex-US"][one_d] == pytest.approx(0.02)
    assert rows["Japan"][one_d] == pytest.approx(0.001)
    assert rows["Developed ex-US"][captured["columns"].index("1W")] == pytest.approx(0.02)
    assert "Since session open" in notes["Developed ex-US"][one_d] and "3:14 PM EDT" in notes["Developed ex-US"][one_d]
    assert notes["Japan"][one_d].startswith("EOD close-to-close")
    assert notes["Japan"][captured["columns"].index("1M")].startswith("Completed close-to-close")


def test_global_cards_render_from_stored_quotes_when_market_monitor_history_is_empty(monkeypatch):
    """A regional ETF with a stored quote but no market-monitor closes still gets a live 1D card."""
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from market_intelligence import markets_ui

    empty_history = {"bars": {}, "meta": {}, "returns": {}, "latest_price": {}, "bounds": {}}
    stamp = _et(2026, 10, 7, 17, 15)
    quote = _quote("VEA", price=71.07, open_=70.0, close=70.26, ts=stamp)
    quote["provenance"]["last_close_date"] = "2026-10-07"  # after the close the reference is today's completed close
    quotes = [quote]

    monkeypatch.setattr("market_intelligence.ui.cached_read", lambda fn_name, *a, **k: empty_history)
    monkeypatch.setattr("market_intelligence.ui.cached_quote_read", lambda fn_name, *a, **k: quotes)
    monkeypatch.setattr(markets_ui, "_now", lambda: _et(2026, 10, 7, 18, 20))
    root = Path(__file__).resolve().parents[1]
    at = AppTest.from_file(str(root / "pages" / "23_Global_Markets.py"), default_timeout=40)
    at.run()
    assert not at.exception, [item.value for item in at.exception]
    by_label = {m.label: m for m in at.metric}
    assert list(by_label) == ["Developed ex-US"]
    assert by_label["Developed ex-US"].value == "71.07"
    assert by_label["Developed ex-US"].delta.startswith("+1.15% 1D (since last session close)")


def test_overview_section_caption_counts_live_rows_without_eod_observations():
    from market_intelligence.overview_ui import _section_as_of

    live = {"as_of_min": None, "as_of_max": None, "rows": [{"observed_at": "2026-10-07T22:10:00+00:00"}, {"observed_at": None}]}
    assert _section_as_of(live) == "no stored EOD observations · 1 row from stored quotes"
    assert _section_as_of({"as_of_min": None, "rows": []}) == "no observations"
    mixed = {"as_of_min": "2026-10-06", "as_of_max": "2026-10-07", "rows": [{"observed_at": "x"}, {"observed_at": "y"}]}
    assert _section_as_of(mixed) == "as of 2026-10-06 … 2026-10-07 · 2 live 1D rows"
    assert _section_as_of({"as_of_min": "2026-10-07", "as_of_max": "2026-10-07", "rows": []}) == "as of 2026-10-07"


# ---- Calendar memoization -------------------------------------------------------------------------


def test_holiday_sets_are_memoized_and_callers_cannot_mutate_the_cache():
    first = calendars.holiday_set(calendars.CAL_NYSE, 2026)
    assert date(2026, 7, 3) in first  # Independence Day observed (July 4 is a Saturday)
    first.add(date(2026, 10, 7))
    assert date(2026, 10, 7) not in calendars.holiday_set(calendars.CAL_NYSE, 2026)
    assert calendars.is_session(date(2026, 10, 7), calendars.CAL_NYSE)
    assert not calendars.is_session(date(2026, 7, 3), calendars.CAL_NYSE)
    info = calendars._holiday_set_cached.cache_info()
    assert info.hits > 0
    early = calendars.nyse_early_close_dates(2026)
    assert date(2026, 11, 27) in early and date(2026, 12, 24) in early
    early.clear()
    assert date(2026, 11, 27) in calendars.nyse_early_close_dates(2026)
