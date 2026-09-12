"""IBKR equity EOD adapter: fixtures only. No live TWS, no orders."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import pytest

from ibkr_collector.historical import (
    ADJUSTMENT_BASIS_ADJUSTED_LAST,
    ADJUSTMENT_BASIS_TRADES,
    BACKFILL_DURATION,
    HistoricalBarRaw,
    HistoricalPacer,
    PHASE3_SYMBOLS,
    PRIMARY_EXCHANGE,
    QualifiedContract,
    STATUS_ADJUSTED_LAST_REJECTED,
    STATUS_AMBIGUOUS,
    STATUS_INVALID,
    STATUS_NO_DATA,
    STATUS_NO_ENTITLEMENT,
    STATUS_OK,
    STATUS_PACING,
    STATUS_TIMEOUT,
    SymbolFetchResult,
    WHAT_TO_SHOW_ADJUSTED,
    WHAT_TO_SHOW_TRADES,
    adjustment_basis_for,
    catalog_covers_universe,
    classify_historical_failure,
    connect_historical_session,
    coverage_summary,
    dashboard_universe,
    drop_incomplete_session,
    duration_for_lookback,
    eod_end_datetime,
    exit_code_for_coverage,
    fetch_symbol,
    parse_historical_bar,
    parse_ibkr_bar_date,
    select_unique_us_listing,
    spec_for,
)
from ibkr_collector.values import BLOCKED_ECLIENT_METHODS, classify_error
from market_intelligence.calendars import CAL_NYSE, is_session
from market_intelligence.equity_eod import (
    EQUITY_SOURCE_ID,
    AdapterUnavailable,
    CollectorStoreAdapter,
    FixtureAdapter,
    UnavailableAdapter,
    YahooAdapter,
    adapter_from_env,
    compute_metrics,
    ingest_equity_eod,
    load_adj_closes,
    pct_vs_dma,
    window_return,
)
from market_intelligence.ibkr_eod import (
    IBKRAdapter,
    equity_bars_from_result,
    results_to_ingest_payload,
    tws_socket_allowed,
)
from market_intelligence.source_resolve import resolve_observation
from market_intelligence.taxonomy import UNIVERSE_SYMBOLS


def _qualified(symbol: str = "SPY", con_id: int = 756733) -> QualifiedContract:
    return QualifiedContract(
        symbol=symbol,
        con_id=con_id,
        exchange="SMART",
        primary_exchange=PRIMARY_EXCHANGE[symbol],
        currency="USD",
        sec_type="STK",
    )


def _raw_bar(day: date, close: float, volume: float | None = 1_000_000.0) -> HistoricalBarRaw:
    return HistoricalBarRaw(
        bar_date=day,
        open=close - 1.0,
        high=close + 1.0,
        low=close - 2.0,
        close=close,
        volume=volume,
        what_to_show=WHAT_TO_SHOW_ADJUSTED,
    )


def _ok_result(symbol: str, bars: list[HistoricalBarRaw], con_id: int = 756733) -> SymbolFetchResult:
    return SymbolFetchResult(
        symbol=symbol,
        status=STATUS_OK,
        qualified=_qualified(symbol, con_id),
        bars=bars,
        what_to_show=WHAT_TO_SHOW_ADJUSTED,
    )


def _nyse_sessions_ending(end: date, n: int) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < n:
        if is_session(cursor, CAL_NYSE):
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


class FakeSession:
    def __init__(self, qualify_map=None, bars_map=None) -> None:
        self.qualify_map = qualify_map or {}
        self.bars_map = bars_map or {}

    def qualify(self, spec):
        return self.qualify_map.get(spec.symbol, (None, STATUS_INVALID, []))

    def daily_bars(self, qualified, *, duration, what_to_show):
        return self.bars_map.get(qualified.symbol, ([], STATUS_NO_DATA, []))


def test_adapter_selection_ibkr_yahoo_fixture_and_default(monkeypatch):
    monkeypatch.delenv("MI_EQUITY_PROVIDER", raising=False)
    assert isinstance(adapter_from_env({}), UnavailableAdapter)
    assert isinstance(adapter_from_env({"MI_EQUITY_PROVIDER": ""}), UnavailableAdapter)
    assert isinstance(adapter_from_env({"MI_EQUITY_PROVIDER": "yahoo"}), YahooAdapter)
    assert isinstance(adapter_from_env({"MI_EQUITY_PROVIDER": "fixture"}), FixtureAdapter)
    adapter = adapter_from_env({"MI_EQUITY_PROVIDER": "ibkr"})
    assert isinstance(adapter, CollectorStoreAdapter)
    assert adapter.source_id == "IBKR"
    assert adapter.rebuild_only is True
    collector = adapter_from_env({"MI_EQUITY_PROVIDER": "ibkr_collector"})
    assert isinstance(collector, CollectorStoreAdapter)


def test_ibkr_disabled_without_socket_flag_does_not_open_tws(monkeypatch):
    monkeypatch.delenv("IBKR_ALLOW_TWS_SOCKET", raising=False)
    adapter = IBKRAdapter.from_env({})
    assert tws_socket_allowed({}) is False
    with pytest.raises(AdapterUnavailable, match="Windows collector"):
        adapter.fetch(["SPY"], date(2026, 9, 1), date(2026, 9, 10))


def test_connect_historical_session_refuses_non_localhost():
    session, client, error = connect_historical_session(host="8.8.8.8", port=7496)
    assert session is None and client is None
    assert error == "refused_non_localhost_tws"


def test_connect_historical_session_times_out_when_api_version_hangs(monkeypatch):
    import threading
    import time

    class _HangClient:
        handshake = threading.Event()
        errors: list = []

        def connect(self, *args, **kwargs):
            time.sleep(30)

        def isConnected(self):
            return False

        def disconnect(self):
            return None

    monkeypatch.setattr("ibkr_collector.readonly_client.ReadOnlyTwsClient", lambda: _HangClient())
    monkeypatch.setattr("ibkr_collector.diagnostic.probe_socket", lambda *args, **kwargs: {"ok": True})
    started = time.monotonic()
    session, client, error = connect_historical_session(host="127.0.0.1", port=7496, timeout_sec=0.4)
    assert session is None and error == "api_version_handshake_timeout"
    assert time.monotonic() - started < 2.5


def test_catalog_covers_entire_dashboard_universe():
    assert catalog_covers_universe() == []
    assert set(PRIMARY_EXCHANGE) >= set(UNIVERSE_SYMBOLS)
    assert set(dashboard_universe()) == set(UNIVERSE_SYMBOLS)
    assert set(PHASE3_SYMBOLS) <= set(UNIVERSE_SYMBOLS)
    assert spec_for("spy").primary_exchange == "ARCA"
    assert spec_for("NVDA").primary_exchange == "NASDAQ"


def test_contract_resolution_unique_invalid_ambiguous():
    spec = spec_for("SPY")
    unique, status = select_unique_us_listing(
        [{"symbol": "SPY", "sec_type": "STK", "currency": "USD", "primary_exchange": "ARCA", "con_id": 756733, "exchange": "SMART"}],
        spec,
    )
    assert status is None and unique is not None and unique.con_id == 756733
    missing, status = select_unique_us_listing([], spec)
    assert missing is None and status == STATUS_INVALID
    foreign, status = select_unique_us_listing(
        [{"symbol": "SPY", "sec_type": "STK", "currency": "EUR", "primary_exchange": "IBIS", "con_id": 1}],
        spec,
    )
    assert foreign is None and status == STATUS_INVALID
    ambiguous, status = select_unique_us_listing(
        [
            {"symbol": "TSM", "sec_type": "STK", "currency": "USD", "primary_exchange": "NYSE", "con_id": 10},
            {"symbol": "TSM", "sec_type": "STK", "currency": "USD", "primary_exchange": "NYSE", "con_id": 11},
        ],
        spec_for("TSM"),
    )
    assert ambiguous is None and status == STATUS_AMBIGUOUS


def test_daily_bar_date_parsing_ignores_time_suffix():
    assert parse_ibkr_bar_date("20260910") == date(2026, 9, 10)
    assert parse_ibkr_bar_date("20260910 16:00:00") == date(2026, 9, 10)
    assert parse_ibkr_bar_date("2026-09-10") == date(2026, 9, 10)
    assert parse_ibkr_bar_date("") is None
    parsed = parse_historical_bar({"date": "20260910", "open": 1, "high": 3, "low": 0.5, "close": 2, "volume": 10}, what_to_show=WHAT_TO_SHOW_ADJUSTED)
    assert parsed is not None and parsed.close == 2.0 and parsed.bar_date == date(2026, 9, 10)
    assert parse_historical_bar({"date": "20260910", "close": None}, what_to_show=WHAT_TO_SHOW_ADJUSTED) is None


def test_adjustment_basis_never_labels_trades_as_adjusted_close():
    assert adjustment_basis_for(WHAT_TO_SHOW_ADJUSTED) == ADJUSTMENT_BASIS_ADJUSTED_LAST
    assert adjustment_basis_for(WHAT_TO_SHOW_TRADES) == ADJUSTMENT_BASIS_TRADES
    assert ADJUSTMENT_BASIS_TRADES != "adjusted_close"
    bars = equity_bars_from_result(
        _ok_result("SPY", [_raw_bar(date(2026, 9, 10), 650.0)]),
        start=date(2026, 9, 1),
        end=date(2026, 9, 10),
    )
    assert bars[0].adjustment_basis == "IBKR_ADJUSTED_LAST"
    assert bars[0].source_id == EQUITY_SOURCE_ID
    assert bars[0].con_id == 756733


def test_adjusted_last_rejection_does_not_fall_back_to_trades():
    rejected = SymbolFetchResult(symbol="SPY", status=STATUS_ADJUSTED_LAST_REJECTED, reason="end date")
    adapter = IBKRAdapter(session=object(), recorded=[])  # type: ignore[arg-type]
    adapter.recorded = []

    class _RejectSession:
        def qualify(self, spec):
            return _qualified(spec.symbol), None, []

        def daily_bars(self, qualified, *, duration, what_to_show):
            raise AssertionError("live session must not be used when testing recorded reject")

    # Drive the live-result path through a stubbed fetch_universe replacement.
    from market_intelligence import ibkr_eod as module

    def _fake_fetch(session, symbols, duration, what_to_show):
        assert what_to_show == WHAT_TO_SHOW_ADJUSTED
        return [rejected]

    adapter.session = _RejectSession()
    original = module.fetch_universe
    module.fetch_universe = _fake_fetch
    try:
        with pytest.raises(AdapterUnavailable, match="not falling back to TRADES"):
            adapter.fetch(["SPY"], date(2026, 9, 1), date(2026, 9, 10))
    finally:
        module.fetch_universe = original


def test_drop_incomplete_current_session():
    # Friday 2026-09-11 12:00 ET: regular close has not happened, so 2026-09-10 is last completed.
    now = datetime(2026, 9, 11, 16, 0, tzinfo=timezone.utc)
    bars = [
        _raw_bar(date(2026, 9, 10), 100.0),
        _raw_bar(date(2026, 9, 11), 101.0),
    ]
    kept = drop_incomplete_session(bars, now=now)
    assert [b.bar_date for b in kept] == [date(2026, 9, 10)]
    assert eod_end_datetime(now) == "20260910 16:00:00 US/Eastern"
    assert classify_error(2188) == "info"


def test_historical_failure_classification():
    assert classify_historical_failure([{"error_code": 420, "error_string": "pacing"}]) == STATUS_PACING
    assert classify_historical_failure([{"error_code": 354, "kind": "entitlement"}]) == STATUS_NO_ENTITLEMENT
    assert classify_historical_failure([{"error_code": 200}]) == STATUS_INVALID
    assert classify_historical_failure([{"error_code": 162, "error_string": "HMDS query returned no data"}]) == STATUS_NO_DATA
    assert classify_historical_failure([{"error_code": 321, "error_string": "ADJUSTED_LAST end date rejected"}]) == STATUS_ADJUSTED_LAST_REJECTED


def test_pacer_enforces_interval_identical_cooldown_and_window(monkeypatch):
    sleeps: list[float] = []
    clock = {"t": 100.0}

    def now() -> float:
        return clock["t"]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    pacer = HistoricalPacer(
        min_interval_sec=2.0,
        identical_cooldown_sec=16.0,
        max_per_window=2,
        window_sec=600.0,
        sleeper=sleep,
        clock=now,
    )
    pacer.wait("SPY|1 W|1 day|ADJUSTED_LAST")
    pacer.wait("XLK|1 W|1 day|ADJUSTED_LAST")
    assert sleeps and sleeps[0] == pytest.approx(2.0)
    pacer.wait("SPY|1 W|1 day|ADJUSTED_LAST")
    assert any(s >= 15.9 for s in sleeps)
    pacer.wait("XLE|1 W|1 day|ADJUSTED_LAST")
    assert any(s > 500 for s in sleeps)


def test_duration_and_incremental_windows():
    assert duration_for_lookback(start=date(2026, 9, 1), end=date(2026, 9, 10), incremental=True) == "1 W"
    assert duration_for_lookback(start=date(2025, 9, 1), end=date(2026, 9, 10), incremental=False) == "2 Y"
    assert BACKFILL_DURATION == "2 Y"


def test_fetch_symbol_timeout_and_pacing():
    timeout_session = FakeSession(
        qualify_map={"SPY": (_qualified(), None, [])},
        bars_map={"SPY": ([], STATUS_TIMEOUT, [])},
    )
    timed = fetch_symbol(timeout_session, "SPY", duration="1 W")
    assert timed.status == STATUS_TIMEOUT and timed.bars == []
    paced = FakeSession(
        qualify_map={"SPY": (_qualified(), None, [420])},
        bars_map={"SPY": ([], STATUS_PACING, [420])},
    )
    out = fetch_symbol(paced, "SPY", duration="1 W")
    assert out.status == STATUS_PACING


def test_reqhistoricaldata_is_unblocked_orders_remain_blocked():
    assert "reqHistoricalData" not in BLOCKED_ECLIENT_METHODS
    assert "placeOrder" in BLOCKED_ECLIENT_METHODS
    assert "reqPositions" in BLOCKED_ECLIENT_METHODS
    assert "reqExecutions" in BLOCKED_ECLIENT_METHODS
    assert classify_error(420) == "pacing"
    assert classify_error(354) == "entitlement"


def test_zero_return_is_not_null_and_holiday_gap_is_null():
    flat = {date(2026, 9, 9): 100.0, date(2026, 9, 10): 100.0}
    metrics, coverage = compute_metrics(flat, flat, date(2026, 9, 10), adjustment_basis="IBKR_ADJUSTED_LAST")
    assert metrics["ret_1d"] == 0.0 and metrics["rs_chg_1d"] == 0.0
    assert coverage["aligned_with_benchmark"] is True
    assert coverage["return_kind"] == "ibkr_adjusted_last_price_return"
    # 2026-09-07 is Labor Day. A 1D jump from Friday 9/4 to Tuesday 9/8 is valid.
    holiday_ok = {date(2026, 9, 4): 100.0, date(2026, 9, 8): 102.0}
    bench = {date(2026, 9, 4): 200.0, date(2026, 9, 8): 202.0}
    metrics_h, cov_h = compute_metrics(holiday_ok, bench, date(2026, 9, 8), adjustment_basis="IBKR_ADJUSTED_LAST")
    assert cov_h["aligned_with_benchmark"] is True
    assert metrics_h["ret_1d"] == pytest.approx(0.02)
    assert metrics_h["rs_chg_1d"] == pytest.approx((102.0 / 202.0) / (100.0 / 200.0) - 1.0)
    # Missing the Friday session is not a 1D return.
    gapped = {date(2026, 9, 3): 100.0, date(2026, 9, 8): 102.0}
    metrics_g, cov_g = compute_metrics(gapped, bench, date(2026, 9, 8))
    assert metrics_g["ret_1d"] is None
    assert cov_g["aligned_with_benchmark"] is False


def test_pct_vs_50_and_200_dma_requires_full_window():
    sessions = _nyse_sessions_ending(date(2026, 9, 10), 200)
    series = {day: 100.0 + i for i, day in enumerate(sessions)}
    assert pct_vs_dma(series, date(2026, 9, 10), 50) == pytest.approx(series[sessions[-1]] / (sum(series[d] for d in sessions[-50:]) / 50.0) - 1.0)
    assert pct_vs_dma(series, date(2026, 9, 10), 200) == pytest.approx(series[sessions[-1]] / (sum(series.values()) / 200.0) - 1.0)
    short = {day: 10.0 for day in sessions[-49:]}
    assert pct_vs_dma(short, date(2026, 9, 10), 50) is None
    metrics, _ = compute_metrics(series, series, date(2026, 9, 10), adjustment_basis="IBKR_ADJUSTED_LAST")
    assert metrics["pct_vs_50dma"] is not None
    assert metrics["pct_vs_200dma"] is not None


def test_fmp_vs_equity_eod_date_priority():
    older_ibkr = {"source_id": "EQUITY_EOD", "series_id": "XLK", "observation_date": date(2026, 9, 9), "value": 50.0}
    newer_fmp = {"source_id": "FMP_LEGACY", "series_id": "XLK", "observation_date": date(2026, 9, 10), "value": 51.0}
    picked = resolve_observation("XLK", [older_ibkr, newer_fmp])
    assert picked is not None
    assert picked.source_id == "FMP_LEGACY"
    assert picked.observation_date == date(2026, 9, 10)
    tied_ibkr = {"source_id": "EQUITY_EOD", "series_id": "XLK", "observation_date": date(2026, 9, 10), "value": 52.0}
    tied = resolve_observation("XLK", [newer_fmp, tied_ibkr])
    assert tied is not None and tied.source_id == "EQUITY_EOD"
    assert tied.value == 52.0


def test_remote_gateway_redacts_ibkr_equity_values():
    from ai_gateway.config import remote_value_sources
    from market_intelligence.export_policy import EXPORT_MODE_EXTERNAL, EXPORT_MODE_OWNER, filter_for_export

    assert remote_value_sources({}) == ()
    assert "IBKR" not in remote_value_sources({})
    assert "EQUITY_EOD" not in remote_value_sources({})
    entry = {
        "sector_key": "Technology",
        "source_id": "EQUITY_EOD",
        "provider": "IBKR",
        "export_scope": "INTERNAL_ONLY",
        "ret_1d": 0.012,
        "rs_chg_1d": 0.004,
        "pct_vs_50dma": 0.03,
        "adjustment_basis": "IBKR_ADJUSTED_LAST",
    }
    remote = filter_for_export(entry, export_mode=EXPORT_MODE_EXTERNAL)
    assert remote.get("restricted") is True
    dumped = str(remote)
    assert "0.012" not in dumped and "0.004" not in dumped
    assert remote.get("source_id") == "EQUITY_EOD"
    owner = filter_for_export(entry, export_mode=EXPORT_MODE_OWNER)
    assert owner.get("ret_1d") == 0.012
    still_redacted = filter_for_export(entry, export_mode=EXPORT_MODE_EXTERNAL, remote_value_sources=("IBKR",))
    # Allowlisting the provider name must not unlock EQUITY_EOD values.
    assert still_redacted.get("restricted") is True


def test_results_payload_keeps_conid_and_adjustment_basis():
    result = _ok_result("XLK", [_raw_bar(date(2026, 9, 10), 220.0)], con_id=123)
    payload = results_to_ingest_payload([result], collector_id="harin-laptop")
    assert payload["source_id"] == EQUITY_SOURCE_ID
    assert payload["provider"] == "IBKR"
    assert payload["bars"][0]["con_id"] == 123
    assert payload["bars"][0]["adjustment_basis"] == "IBKR_ADJUSTED_LAST"
    failed = results_to_ingest_payload(
        [SymbolFetchResult(symbol="NVDA", status=STATUS_NO_ENTITLEMENT)],
        collector_id="harin-laptop",
    )
    assert failed["bars"] == []
    assert failed["coverage"]["failed_symbols"]["NVDA"] == STATUS_NO_ENTITLEMENT


def test_coverage_summary_counts_statuses():
    rows = [
        _ok_result("SPY", [_raw_bar(date(2026, 9, 1), 1.0), _raw_bar(date(2026, 9, 10), 2.0)]),
        SymbolFetchResult(symbol="NVDA", status=STATUS_TIMEOUT),
        SymbolFetchResult(symbol="XLE", status=STATUS_NO_ENTITLEMENT),
    ]
    summary = coverage_summary(rows)
    assert summary["requested"] == 3
    assert summary["successful"] == 1
    assert summary["failed"] == 2
    assert summary["overall"] == "PARTIAL_SUCCESS"
    assert summary["failed_symbols"]["NVDA"] == STATUS_TIMEOUT
    assert exit_code_for_coverage(summary) == 1


def test_ibkr_ingest_upsert_incremental_partial_and_transport_skip(mi_db):
    from sqlalchemy import text

    days = [date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10)]
    recorded = []
    for symbol, con_id, base in (("SPY", 756733, 100.0), ("XLK", 111, 50.0), ("XLE", 222, 80.0)):
        recorded.append(
            _ok_result(
                symbol,
                [_raw_bar(day, base + i) for i, day in enumerate(days)],
                con_id=con_id,
            )
        )
    for etf, px in (
        ("XLC", 20.0),
        ("XLY", 21.0),
        ("XLP", 22.0),
        ("XLF", 23.0),
        ("XLV", 24.0),
        ("XLI", 25.0),
        ("XLB", 26.0),
        ("XLU", 27.0),
        ("XLRE", 28.0),
    ):
        recorded.append(_ok_result(etf, [_raw_bar(day, px) for day in days], con_id=300))
    adapter = IBKRAdapter(recorded=recorded)
    report = ingest_equity_eod(mi_db, adapter, today=date(2026, 9, 10), lookback_days=10, incremental=False)
    assert report.failed is False
    assert report.latest_observation == date(2026, 9, 10)
    assert report.bars_written > 0
    with mi_db.connect() as conn:
        spy = conn.execute(
            text("SELECT adj_close_price, close_price, adjustment_basis, con_id, source_id, provider_symbol, provider FROM mi_market_bars WHERE instrument_id='SPY' AND bar_date='2026-09-10'")
        ).mappings().one()
        assert float(spy["adj_close_price"]) == 102.0
        assert spy["adjustment_basis"] == "IBKR_ADJUSTED_LAST"
        assert int(spy["con_id"]) == 756733
        assert spy["source_id"] == EQUITY_SOURCE_ID
        assert spy["provider"] == "IBKR"
        provider = conn.execute(text("SELECT provider, usage_scope FROM mi_source_registry WHERE source_id='EQUITY_EOD'")).mappings().one()
        assert provider["provider"] == "IBKR"
        assert provider["usage_scope"] == "INTERNAL_ONLY"
        snap = conn.execute(
            text("SELECT metrics_json, coverage_json, provenance_json FROM mi_sector_snapshots WHERE sector_key='Technology' AND as_of='2026-09-10' AND source_id='EQUITY_EOD'")
        ).mappings().one()
        assert snap["metrics_json"]["ret_1d"] == pytest.approx(52.0 / 51.0 - 1.0)
        assert snap["metrics_json"]["rs_chg_1d"] == pytest.approx((52.0 / 102.0) / (51.0 / 101.0) - 1.0)
        assert snap["coverage_json"]["adjustment_basis"] == "IBKR_ADJUSTED_LAST"
        assert snap["provenance_json"]["provider"] == "IBKR"
        first_count = conn.execute(text("SELECT COUNT(*) FROM mi_market_bars WHERE source_id='EQUITY_EOD'")).scalar()
    again = ingest_equity_eod(mi_db, adapter, today=date(2026, 9, 10), lookback_days=10, incremental=True)
    assert again.failed is False
    with mi_db.connect() as conn:
        second_count = conn.execute(text("SELECT COUNT(*) FROM mi_market_bars WHERE source_id='EQUITY_EOD'")).scalar()
    assert second_count == first_count
    closed = IBKRAdapter.from_env({})
    skipped = ingest_equity_eod(mi_db, closed, today=date(2026, 9, 10), lookback_days=10)
    assert skipped.failed is False
    assert skipped.status == "SKIPPED"
    assert skipped.latest_observation == date(2026, 9, 10)
    with mi_db.connect() as conn:
        still = conn.execute(text("SELECT COUNT(*) FROM mi_market_bars WHERE source_id='EQUITY_EOD'")).scalar()
        assert still == first_count
        freshness = conn.execute(
            text("SELECT transport_status, latest_observation_date FROM mi_data_freshness WHERE source_id='EQUITY_EOD' AND dataset='equity_etf_daily_bars'")
        ).mappings().one()
        assert freshness["transport_status"] == "SKIPPED"
        assert freshness["latest_observation_date"] == date(2026, 9, 10)


def test_partial_symbol_failure_keeps_successful_bars():
    adapter = IBKRAdapter(
        recorded=[
            _ok_result("SPY", [_raw_bar(date(2026, 9, 10), 100.0)]),
            SymbolFetchResult(symbol="NVDA", status=STATUS_TIMEOUT, reason="timeout"),
        ]
    )
    bars = adapter.fetch(["SPY", "NVDA"], date(2026, 9, 1), date(2026, 9, 10))
    assert [b.instrument_id for b in bars] == ["SPY"]
    assert adapter.last_results[1].status == STATUS_TIMEOUT


def test_freshness_policy_uses_nyse_last_completed_session():
    from market_intelligence.freshness import assess_freshness, policy_for

    policy = policy_for(source_id="EQUITY_EOD", cadence="D")
    assert policy is not None and policy.calendar == "NYSE"
    assert policy.same_day_available is True
    now = datetime(2026, 9, 11, 20, 30, tzinfo=timezone.utc)  # Friday 16:30 ET, after the regular close
    stale = assess_freshness(date(2026, 9, 8), cadence="D", now=now, source_id="EQUITY_EOD")
    fresh = assess_freshness(date(2026, 9, 11), cadence="D", now=now, source_id="EQUITY_EOD")
    assert stale.status != fresh.status
    assert fresh.status == "LATEST_AVAILABLE"
    assert fresh.expected_latest == date(2026, 9, 11)


def test_eod_lock_is_separate_from_quote_lock(tmp_path):
    from ibkr_collector.config import CollectorConfig
    from ibkr_collector.eod_cli import acquire_eod_lock
    from ibkr_collector.lock import InstanceLock

    cfg = CollectorConfig(data_dir=tmp_path)
    assert cfg.eod_lock_path != cfg.lock_path
    first = acquire_eod_lock(cfg)
    second = acquire_eod_lock(cfg)
    quote = InstanceLock(cfg.lock_path)
    assert first is not None
    assert second is None
    assert quote.acquire() is True
    first.release()
    quote.release()


def test_full_partial_and_empty_universe_coverage_exit_codes():
    ok_rows = [_ok_result(symbol, [_raw_bar(date(2026, 9, 10), 10.0 + i)], con_id=1000 + i) for i, symbol in enumerate(UNIVERSE_SYMBOLS)]
    full = coverage_summary(ok_rows, requested=list(UNIVERSE_SYMBOLS))
    assert full["overall"] == "FULL_SUCCESS"
    assert full["successful"] == 46
    assert full["failed"] == 0
    assert exit_code_for_coverage(full) == 0
    partial_rows = list(ok_rows)
    partial_rows[-1] = SymbolFetchResult(symbol=UNIVERSE_SYMBOLS[-1], status=STATUS_TIMEOUT)
    partial = coverage_summary(partial_rows, requested=list(UNIVERSE_SYMBOLS))
    assert partial["overall"] == "PARTIAL_SUCCESS"
    assert partial["successful"] == 45
    assert partial["failed_symbols"][UNIVERSE_SYMBOLS[-1]] == STATUS_TIMEOUT
    assert exit_code_for_coverage(partial) == 1
    empty = coverage_summary(
        [SymbolFetchResult(symbol=s, status=STATUS_INVALID) for s in UNIVERSE_SYMBOLS],
        requested=list(UNIVERSE_SYMBOLS),
    )
    assert empty["overall"] == "FAILED"
    assert empty["successful"] == 0
    assert empty["failed"] == 46
    assert exit_code_for_coverage(empty) == 2


def test_fetch_symbol_entitlement_ambiguous_invalid_and_pacing_retry():
    entitled = FakeSession(qualify_map={"NVDA": (None, STATUS_NO_ENTITLEMENT, [354])})
    out = fetch_symbol(entitled, "NVDA", duration="1 W")
    assert out.status == STATUS_NO_ENTITLEMENT
    assert 354 in out.error_codes
    invalid = FakeSession(qualify_map={"SPY": (None, STATUS_INVALID, [200])})
    assert fetch_symbol(invalid, "SPY", duration="1 W").status == STATUS_INVALID
    # Ambiguous listings fail closed.
    status = select_unique_us_listing(
        [
            {"symbol": "SPY", "sec_type": "STK", "currency": "USD", "primary_exchange": "ARCA", "con_id": 1, "exchange": "SMART"},
            {"symbol": "SPY", "sec_type": "STK", "currency": "USD", "primary_exchange": "ARCA", "con_id": 2, "exchange": "SMART"},
        ],
        spec_for("SPY"),
    )[1]
    assert status == STATUS_AMBIGUOUS
    none, invalid_status = select_unique_us_listing([], spec_for("SPY"))
    assert none is None and invalid_status == STATUS_INVALID

    class RetrySession:
        def __init__(self) -> None:
            self.calls = 0

        def qualify(self, spec):
            return _qualified(spec.symbol), None, []

        def daily_bars(self, qualified, *, duration, what_to_show):
            self.calls += 1
            if self.calls == 1:
                return [], STATUS_PACING, [420]
            return [_raw_bar(date(2026, 9, 10), 100.0)], None, []

    retried = fetch_symbol(RetrySession(), "SPY", duration="1 W")
    assert retried.status == STATUS_OK
    assert len(retried.bars) == 1


def test_two_year_backfill_covers_200dma_and_252_session_metrics():
    assert BACKFILL_DURATION == "2 Y"
    sessions = _nyse_sessions_ending(date(2026, 9, 10), 520)
    assert len(sessions) >= 253
    series = {day: 100.0 + i * 0.1 for i, day in enumerate(sessions)}
    spy = {day: 200.0 + i * 0.05 for i, day in enumerate(sessions)}
    metrics, _ = compute_metrics(series, spy, sessions[-1], adjustment_basis="IBKR_ADJUSTED_LAST")
    assert metrics["pct_vs_200dma"] is not None
    assert metrics["ret_12m"] is not None
    assert metrics["rs_chg_12m"] is not None
    assert window_return(series, sessions[-1], 252) is not None
    one_year = {day: series[day] for day in sessions[-252:]}
    assert pct_vs_dma(one_year, sessions[-1], 200) is not None
    assert window_return(one_year, sessions[-1], 252) is None


def test_load_adj_closes_isolates_provider_histories(mi_db):
    from datetime import datetime as dt

    from market_intelligence.equity_eod import EquityBar, upsert_bars

    retrieved = dt(2026, 9, 10, tzinfo=timezone.utc)
    with mi_db.begin() as conn:
        upsert_bars(
            conn,
            [
                EquityBar("SPY", date(2026, 9, 8), 100.0, 100.0, source_id=EQUITY_SOURCE_ID, provider="IBKR"),
                EquityBar("SPY", date(2026, 9, 9), 101.0, 101.0, source_id=EQUITY_SOURCE_ID, provider="IBKR"),
            ],
            run_id="r1",
            retrieved_at=retrieved,
            provider="IBKR",
        )
        upsert_bars(
            conn,
            [EquityBar("SPY", date(2026, 9, 10), 50.0, 50.0, source_id="FMP_LEGACY", provider="FMP")],
            run_id="r2",
            retrieved_at=retrieved,
            provider="FMP",
        )
        upsert_bars(
            conn,
            [EquityBar("SPY", date(2026, 9, 10), 999.0, 999.0, source_id=EQUITY_SOURCE_ID, provider="YAHOO")],
            run_id="r3",
            retrieved_at=retrieved,
            provider="YAHOO",
        )
        ibkr = load_adj_closes(conn, ["SPY"], source_id=EQUITY_SOURCE_ID, provider="IBKR")
        yahoo = load_adj_closes(conn, ["SPY"], source_id=EQUITY_SOURCE_ID, provider="YAHOO")
        mixed_raised = False
        try:
            load_adj_closes(conn, ["SPY"], source_id=EQUITY_SOURCE_ID)
        except ValueError as exc:
            mixed_raised = "mixed providers" in str(exc)
    assert set(ibkr["SPY"]) == {date(2026, 9, 8), date(2026, 9, 9)}
    assert ibkr["SPY"][date(2026, 9, 9)] == 101.0
    assert set(yahoo["SPY"]) == {date(2026, 9, 10)}
    assert yahoo["SPY"][date(2026, 9, 10)] == 999.0
    assert mixed_raised is True


def test_collector_store_rebuild_does_not_clobber_transport_ok(mi_db):
    days = [date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10)]
    recorded = [_ok_result("SPY", [_raw_bar(day, 100.0 + i) for i, day in enumerate(days)])]
    for etf, px in (("XLK", 50.0), ("XLE", 80.0), ("XLC", 20.0), ("XLY", 21.0), ("XLP", 22.0), ("XLF", 23.0), ("XLV", 24.0), ("XLI", 25.0), ("XLB", 26.0), ("XLU", 27.0), ("XLRE", 28.0)):
        recorded.append(_ok_result(etf, [_raw_bar(day, px) for day in days], con_id=300))
    ingest_equity_eod(mi_db, IBKRAdapter(recorded=recorded), today=date(2026, 9, 10), lookback_days=10, incremental=False)
    rebuilt = ingest_equity_eod(mi_db, CollectorStoreAdapter(), today=date(2026, 9, 10), lookback_days=10)
    assert rebuilt.status == "SKIPPED"
    with mi_db.connect() as conn:
        from sqlalchemy import text

        row = conn.execute(
            text("SELECT transport_status, coverage_status FROM mi_data_freshness WHERE source_id='EQUITY_EOD' AND dataset='equity_etf_daily_bars'")
        ).mappings().one()
        assert row["transport_status"] == "OK"
