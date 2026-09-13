"""FRED ingestion and legacy-bundle bridge behaviour on real PostgreSQL (SYNTHETIC fixtures)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import text

from market_intelligence.catalog import CATALOG_BY_ID
from market_intelligence.ingest_fred import (
    REVISION_LOOKBACK,
    ingest_fred_catalog,
    request_window,
)
from market_intelligence.legacy_bridge import (
    PRICE_METRIC_KEYS,
    BundleQuarantined,
    etf_metrics_from_prices,
    ingest_precomputed_root,
    read_bundle,
    verify_stored_snapshot,
)
from market_intelligence.store import upsert_source_registry
from tests.mi_fixtures import (
    FakeFredClient,
    business_days,
    daily_series,
    fake_fred_client,
    synthetic_fred_data,
    write_dispersion_bundle,
    write_full_precomputed_root,
    write_spy_bundle,
)

AS_OF = date(2024, 12, 31)


def _registry(engine):
    with engine.begin() as conn:
        upsert_source_registry(conn, enabled={"FRED": True, "FMP_LEGACY": True})


# ---- FRED ------------------------------------------------------------------------------------------

def test_failed_series_isolated_and_error_redacted(mi_db):
    _registry(mi_db)
    client = fake_fred_client(AS_OF, failures={"DGS2"})
    report = ingest_fred_catalog(mi_db, client, series_ids=["DGS2", "DGS10"], mode="full", today=AS_OF)
    by_id = {r.series_id: r for r in report.results}
    assert by_id["DGS10"].status == "SUCCEEDED" and by_id["DGS2"].status == "FAILED"
    assert "HTTP 500" in by_id["DGS2"].error and "api_key" not in by_id["DGS2"].error
    with mi_db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM mi_macro_observations WHERE series_id='DGS10'")).scalar() > 0
        assert conn.execute(text("SELECT COUNT(*) FROM mi_macro_observations WHERE series_id='DGS2'")).scalar() == 0
        runs = {r.dataset: r.status for r in conn.execute(text("SELECT dataset, status FROM mi_ingestion_runs"))}
        fresh = {r.dataset: (r.transport_status, r.freshness_status) for r in conn.execute(text("SELECT dataset, transport_status, freshness_status FROM mi_data_freshness"))}
    assert runs["series:DGS2"] == "FAILED" and runs["series:DGS10"] == "SUCCEEDED"
    assert fresh["series:DGS2"] == ("FAILED", "TRANSPORT_FAILURE") and fresh["series:DGS10"] == ("OK", "LATEST_AVAILABLE")


def test_metadata_mismatch_is_reported_not_silently_accepted(mi_db):
    _registry(mi_db)
    client = FakeFredClient(synthetic_fred_data(AS_OF), metadata={"UNRATE": {"units": "Thousands of Persons", "frequency_short": "W"}})
    report = ingest_fred_catalog(mi_db, client, series_ids=["UNRATE", "CPIAUCSL"], mode="full", today=AS_OF)
    by_id = {r.series_id: r for r in report.results}
    # Publication gate (review finding): a MISMATCH is not a success and promotes nothing.
    assert by_id["UNRATE"].status == "QUARANTINED" and by_id["UNRATE"].metadata_status == "MISMATCH"
    assert by_id["CPIAUCSL"].metadata_status == "VALIDATED" and by_id["CPIAUCSL"].status == "SUCCEEDED"
    assert report.transport_status == "PARTIAL" and [r.series_id for r in report.quarantined] == ["UNRATE"]
    with mi_db.connect() as conn:
        row = conn.execute(text("SELECT metadata_status, metadata_mismatch_json, units, publication_status, publication_reason FROM mi_macro_series WHERE series_id='UNRATE'")).mappings().one()
        current = conn.execute(text("SELECT COUNT(*) FROM mi_macro_observations WHERE series_id='UNRATE'")).scalar()
        quarantined = conn.execute(text("SELECT COUNT(*), MIN(reason), MIN(metadata_status) FROM mi_macro_observation_quarantine WHERE series_id='UNRATE'")).one()
        latest = conn.execute(text("SELECT COUNT(*) FROM mi_v_macro_latest WHERE series_id='UNRATE' AND observation_date IS NOT NULL")).scalar()
    fields = {m["field"] for m in row["metadata_mismatch_json"]}
    assert fields == {"units", "frequency_short"}
    # No valid metadata was ever published for UNRATE, so the mismatching provider units are not adopted.
    assert row["units"] is None and row["publication_status"] == "QUARANTINED_METADATA" and "Thousands of Persons" in row["publication_reason"]
    assert current == 0 and latest == 0
    assert quarantined == (40, "METADATA_MISMATCH", "MISMATCH")


def test_incremental_refresh_uses_bounded_revision_lookback_and_reconciles_revisions(mi_db):
    _registry(mi_db)
    data = synthetic_fred_data(AS_OF)
    client = FakeFredClient(data)
    first = ingest_fred_catalog(mi_db, client, series_ids=["CPIAUCSL"], mode="full", today=AS_OF)
    assert first.results[0].counts["inserted"] == 40
    # Provider revises the latest month and adds a new one.
    revised = list(data["CPIAUCSL"])
    last_date, last_value = revised[-1]
    revised[-1] = (last_date, "{0:.3f}".format(float(last_value) + 1.0))
    revised.append((date(2025, 1, 1), "999.000"))
    client2 = FakeFredClient({"CPIAUCSL": revised})
    second = ingest_fred_catalog(mi_db, client2, series_ids=["CPIAUCSL"], mode="incremental", today=date(2025, 2, 15))
    counts = second.results[0].counts
    assert counts["revised"] == 1 and counts["inserted"] == 1 and counts["unchanged"] >= 10
    start, end = client2.observation_calls[0][1], client2.observation_calls[0][2]
    assert end == date(2025, 2, 15)
    assert start == max(date(2025 - CATALOG_BY_ID["CPIAUCSL"].backfill_years, 2, 1), last_date - REVISION_LOOKBACK["M"])
    with mi_db.connect() as conn:
        rows = conn.execute(text("SELECT revision_seq, is_current, value FROM mi_macro_observations WHERE series_id='CPIAUCSL' AND observation_date=:d ORDER BY revision_seq"), {"d": last_date}).fetchall()
        vintage = conn.execute(text("SELECT vintage_kind, pit_safe FROM mi_macro_series WHERE series_id='CPIAUCSL'")).one()
    assert [(r.revision_seq, r.is_current) for r in rows] == [(1, False), (2, True)]
    assert vintage.vintage_kind == "LATEST_REVISED" and vintage.pit_safe is False


def test_request_window_full_backfill_bounded_by_catalog():
    spec = CATALOG_BY_ID["DGS10"]
    start, end = request_window(spec, mode="full", today=AS_OF, latest_stored=None)
    assert end == AS_OF and start == date(AS_OF.year - spec.backfill_years, 12, 1)
    inc_start, _ = request_window(spec, mode="incremental", today=AS_OF, latest_stored=AS_OF - timedelta(days=3))
    assert inc_start == AS_OF - timedelta(days=3) - REVISION_LOOKBACK["D"]


def test_weekend_month_end_ice_observation_is_not_rejected(mi_db):
    _registry(mi_db)
    rows = daily_series(AS_OF, 100, 0.8, 0.001)
    rows.append((date(2024, 11, 30), "0.85"))  # Saturday month-end (legitimate for ICE indices)
    client = FakeFredClient({"BAMLC0A0CM": sorted(rows)})
    report = ingest_fred_catalog(mi_db, client, series_ids=["BAMLC0A0CM"], mode="full", today=AS_OF)
    assert report.results[0].counts["rejected"] == 0
    with mi_db.connect() as conn:
        assert conn.execute(text("SELECT value FROM mi_macro_observations WHERE series_id='BAMLC0A0CM' AND observation_date='2024-11-30'")).scalar() is not None


# ---- legacy bridge -----------------------------------------------------------------------------------

def test_failed_or_partial_bundles_are_quarantined_and_never_overwrite_valid_snapshots(mi_db, tmp_path):
    _registry(mi_db)
    root = write_full_precomputed_root(tmp_path / "pre", AS_OF)
    first = ingest_precomputed_root(mi_db, root, today=AS_OF)
    assert not first.failed_bundles and not first.quarantined
    with mi_db.connect() as conn:
        before = conn.execute(text("SELECT COUNT(*), MAX(as_of) FROM mi_sector_snapshots WHERE dataset='CONSTITUENT_DISPERSION'")).one()
    # Nightly job now writes a failed dispersion bundle and a half-written rotation bundle.
    newer = business_days(AS_OF + timedelta(days=5), 1)[0]
    write_dispersion_bundle(root, "Technology", newer, ok=False)
    (root / "rotation" / "Healthcare" / "bundle_meta.json").write_text("{", encoding="utf-8")
    second = ingest_precomputed_root(mi_db, root, today=newer)
    reasons = {q["bundle"].split("/")[-1]: q["reason"] for q in second.quarantined}
    assert "ok=False" in reasons["Technology"]
    assert "unstable bundle read" in reasons["Healthcare"] or "Expecting" in reasons["Healthcare"]
    with mi_db.connect() as conn:
        after = conn.execute(text("SELECT COUNT(*), MAX(as_of) FROM mi_sector_snapshots WHERE dataset='CONSTITUENT_DISPERSION'")).one()
        fresh = conn.execute(text("SELECT transport_status, latest_observation_date, last_success_at, last_error_redacted FROM mi_data_freshness WHERE dataset='precomputed_sector_bundles'")).one()
        statuses = [r[0] for r in conn.execute(text("SELECT status FROM mi_ingestion_runs WHERE dataset LIKE 'bundle:%' ORDER BY started_at"))]
    assert after == before  # previous valid snapshot intact
    # Review finding: quarantined bundles must degrade health even though the other bundles loaded.
    assert second.transport_status == "PARTIAL" and fresh.transport_status == "PARTIAL"
    assert fresh.latest_observation_date == AS_OF  # nothing newer accepted
    assert "Technology" in fresh.last_error_redacted and "Healthcare" in fresh.last_error_redacted
    assert "SKIPPED" in statuses
    # last_success_at reflects the last *fully accepted* refresh, not this partial one.
    with mi_db.connect() as conn:
        latest_attempt = conn.execute(text("SELECT last_attempt_at > last_success_at FROM mi_data_freshness WHERE dataset='precomputed_sector_bundles'")).scalar()
    assert latest_attempt is True
    # Health context surfaces the degraded transport for FMP_LEGACY.
    from market_intelligence.read_models import data_health_context

    with mi_db.connect() as conn:
        health = data_health_context(conn, today=newer)
    assert any(s["source_id"] == "FMP_LEGACY" for s in health["failed_transport"])


def test_missing_as_of_is_derived_only_from_dated_data_never_today(tmp_path):
    bundle = write_dispersion_bundle(tmp_path, "Technology", AS_OF, meta_as_of=None)
    meta = json.loads((bundle / "bundle_meta.json").read_text())
    assert meta["as_of"] is None
    read = read_bundle(bundle)
    assert read.as_of == AS_OF and read.as_of_source == "derived_from_dated_data"
    assert read.as_of != date.today()
    # Metadata date inconsistent with the actual dated data -> quarantined, not trusted.
    (bundle / "bundle_meta.json").write_text(json.dumps({"ok": True, "error": None, "as_of": "2030-01-01"}), encoding="utf-8")
    with pytest.raises(BundleQuarantined, match="inconsistent"):
        read_bundle(bundle)


def test_bundle_without_any_dates_or_files_is_quarantined(tmp_path):
    empty = tmp_path / "spy"
    empty.mkdir()
    (empty / "bundle_meta.json").write_text(json.dumps({"ok": True, "as_of": None}), encoding="utf-8")
    with pytest.raises(BundleQuarantined, match="no as_of"):
        read_bundle(empty)
    with pytest.raises(BundleQuarantined, match="missing bundle_meta"):
        read_bundle(tmp_path / "nowhere")


def test_unknown_sector_labels_are_quarantined_rows_not_assigned(mi_db, tmp_path):
    _registry(mi_db)
    root = tmp_path / "pre"
    root.mkdir()
    write_spy_bundle(root, AS_OF, include_unknown_label=True)
    report = ingest_precomputed_root(mi_db, root, today=AS_OF)
    assert not report.failed_bundles
    outcome = report.outcomes[0].as_dict()
    assert outcome["counts"].get("quarantined", 0) == 1
    with mi_db.connect() as conn:
        keys = {r[0] for r in conn.execute(text("SELECT sector_key FROM mi_sector_snapshots WHERE dataset='ETF_RS_VS_SPY'"))}
        nan_nulls = conn.execute(text("SELECT COUNT(*) FROM mi_sector_snapshots WHERE dataset='ETF_RS_VS_SPY' AND (metrics_json->>'rs_chg_12m') IS NULL")).scalar()
    assert "Mystery Sector" not in keys and "Health Care" in keys and "THEME:AI" in keys
    assert nan_nulls >= 1  # NaN in the Parquet metrics -> NULL, not 0


def test_repeated_ingestion_is_idempotent(mi_db, tmp_path):
    _registry(mi_db)
    root = write_full_precomputed_root(tmp_path / "pre", AS_OF)
    ingest_precomputed_root(mi_db, root, today=AS_OF)
    with mi_db.connect() as conn:
        first = conn.execute(text("SELECT COUNT(*), MIN(artifact_sha256) FROM mi_sector_snapshots")).one()
        first_ind = conn.execute(text("SELECT COUNT(*) FROM mi_industry_snapshots")).scalar()
    second = ingest_precomputed_root(mi_db, root, today=AS_OF)
    with mi_db.connect() as conn:
        again = conn.execute(text("SELECT COUNT(*), MIN(artifact_sha256) FROM mi_sector_snapshots")).one()
        again_ind = conn.execute(text("SELECT COUNT(*) FROM mi_industry_snapshots")).scalar()
    assert again == first and again_ind == first_ind
    totals = {}
    for o in second.outcomes:
        for k, v in o.counts.items():
            totals[k] = totals.get(k, 0) + v
    assert totals.get("inserted", 0) == 0 and totals.get("unchanged", 0) > 0


def _wide(symbols: dict[str, list[float | None]], end: date) -> pd.DataFrame:
    n = max(len(v) for v in symbols.values())
    return pd.DataFrame(symbols, index=pd.DatetimeIndex(business_days(end, n)))


def test_windowed_metrics_require_full_window_on_the_session_calendar():
    # 30 sessions only: a 252-session drawdown must NOT be produced (old code needed just 20 points).
    metrics, cov = etf_metrics_from_prices(_wide({"XLK": [100 + i for i in range(30)]}, AS_OF), "XLK", as_of=AS_OF)
    assert metrics["max_drawdown_252d"] is None and cov["price_metrics"]["max_drawdown_252d"] == "INSUFFICIENT_HISTORY"
    assert metrics["ret_1w"] is not None and cov["price_metrics"]["ret_1w"] == "FULL"
    assert metrics["ret_1m"] is not None and metrics["ret_3m"] is None and cov["price_metrics"]["ret_3m"] == "INSUFFICIENT_HISTORY"
    assert metrics["pct_vs_50dma"] is None and metrics["vol_63d_ann"] is None
    assert cov["price_status"] == "OK" and cov["calendar_sessions"] == 30 and cov["valid_sessions"] == 30
    # 300 sessions: everything FULL.
    full, fcov = etf_metrics_from_prices(_wide({"XLK": [100 + 0.1 * i for i in range(300)]}, AS_OF), "XLK", as_of=AS_OF)
    assert all(v is not None for v in (full[k] for k in PRICE_METRIC_KEYS))
    assert set(fcov["price_metrics"].values()) == {"FULL"} and fcov["last_price_date"] == AS_OF.isoformat()


def test_null_sessions_do_not_compress_the_trading_calendar():
    prices = [100.0 + 0.1 * i for i in range(300)]
    # Symbol A misses 10 consecutive sessions inside the last month; symbol B defines the calendar.
    a = list(prices)
    for i in range(-16, -6):
        a[i] = None
    wide = _wide({"A": a, "B": prices}, AS_OF)
    metrics, cov = etf_metrics_from_prices(wide, "A", as_of=AS_OF)
    # Old code dropped NULLs and called the return over the remaining 21 valid prints "1M" (really ~1.5M).
    assert cov["price_metrics"]["ret_1m"] == "TOO_MANY_NULLS" and metrics["ret_1m"] is None  # 10/22 NULL
    assert cov["price_metrics"]["ret_1w"] == "FULL" and metrics["ret_1w"] is not None  # window [-6:] fully valid
    assert cov["price_metrics"]["ret_12m"] == "PARTIAL" and metrics["ret_12m"] is not None  # 10/253 < 5%
    assert cov["price_metrics"]["pct_vs_50dma"] == "TOO_MANY_NULLS" and metrics["pct_vs_50dma"] is None  # 20%
    assert cov["price_metrics"]["pct_vs_200dma"] == "PARTIAL" and metrics["pct_vs_200dma"] is not None  # exactly 5%
    assert cov["price_metrics"]["max_drawdown_252d"] == "PARTIAL"
    assert cov["null_sessions"] == 10 and cov["calendar_sessions"] == 300 and cov["price_status"] == "OK"
    # A single dropped print inside a long window is disclosed as PARTIAL, not silently absorbed.
    b = list(prices)
    b[-100] = None
    metrics_b, cov_b = etf_metrics_from_prices(_wide({"A": prices, "B": b}, AS_OF), "B", as_of=AS_OF)
    assert cov_b["price_metrics"]["max_drawdown_252d"] == "PARTIAL" and metrics_b["max_drawdown_252d"] is not None
    assert cov_b["price_metrics"]["ret_1m"] == "FULL"


def test_stale_instrument_is_not_relabelled_current_by_bundle_as_of():
    prices = [100.0 + 0.1 * i for i in range(300)]
    stale = list(prices)
    for i in range(-3, 0):
        stale[i] = None  # last three sessions missing for this ETF only
    wide = _wide({"XLE": stale, "SPY": prices}, AS_OF)
    metrics, cov = etf_metrics_from_prices(wide, "XLE", as_of=AS_OF)
    assert cov["price_status"] == "STALE" and cov["stale_sessions"] == 3
    assert cov["last_price_date"] == business_days(AS_OF, 4)[0].isoformat()
    # Endpoint is NULL -> no return is published "as of" the bundle date.
    assert metrics["ret_1m"] is None and cov["price_metrics"]["ret_1m"] == "ENDPOINT_NULL"
    assert metrics["pct_vs_200dma"] is None and cov["price_metrics"]["pct_vs_200dma"] == "ENDPOINT_NULL"
    # Missing symbol / non-positive prices are UNAVAILABLE, never zero.
    missing, mcov = etf_metrics_from_prices(wide, "ZZZ", as_of=AS_OF)
    assert all(v is None for v in missing.values()) and mcov["price_status"] == "UNAVAILABLE"
    bad, bcov = etf_metrics_from_prices(_wide({"BAD": [0.0] * 300}, AS_OF), "BAD", as_of=AS_OF)
    assert all(v is None for v in bad.values()) and bcov["price_status"] == "UNAVAILABLE"


def test_ingested_bundle_flags_stale_instruments_and_stores_coverage(mi_db, tmp_path):
    _registry(mi_db)
    root = tmp_path / "pre"
    root.mkdir()
    bundle = write_spy_bundle(root, AS_OF)
    prices = pd.read_parquet(bundle / "prices.parquet")
    last_dates = sorted(prices["date"].unique())[-2:]
    prices.loc[(prices["symbol"] == "XLE") & (prices["date"].isin(last_dates)), "adjClose"] = float("nan")
    prices.to_parquet(bundle / "prices.parquet", index=True)
    report = ingest_precomputed_root(mi_db, root, today=AS_OF)
    assert report.transport_status == "OK" and report.stale_instruments == 1
    with mi_db.connect() as conn:
        xle = conn.execute(text("SELECT metrics_json, coverage_json FROM mi_v_sector_latest WHERE dataset='ETF_RS_VS_SPY' AND instrument_id='XLE'")).mappings().one()
        xlk = conn.execute(text("SELECT metrics_json, coverage_json FROM mi_v_sector_latest WHERE dataset='ETF_RS_VS_SPY' AND instrument_id='XLK'")).mappings().one()
    assert xle["coverage_json"]["price_status"] == "STALE" and xle["coverage_json"]["stale_sessions"] == 2
    assert xle["metrics_json"]["ret_1m"] is None and xle["coverage_json"]["price_metrics"]["ret_1m"] == "ENDPOINT_NULL"
    assert xlk["coverage_json"]["price_status"] == "OK" and xlk["metrics_json"]["ret_1m"] is not None
    assert xlk["coverage_json"]["price_metrics"]["max_drawdown_252d"] == "FULL" and xlk["coverage_json"]["rs_metrics"] == "ENGINE_PRECOMPUTED_AT_BUNDLE_AS_OF"


def test_dispersion_coverage_reports_denominators_and_stale_constituents(mi_db, tmp_path):
    _registry(mi_db)
    root = tmp_path / "pre"
    root.mkdir()
    bundle = write_dispersion_bundle(root, "Technology", AS_OF)
    wide = pd.read_parquet(bundle / "wide_close.parquet")
    wide.iloc[-1, wide.columns.get_loc("BBB")] = float("nan")  # BBB has no print on as_of
    wide.to_parquet(bundle / "wide_close.parquet", index=True)
    ingest_precomputed_root(mi_db, root, today=AS_OF)
    with mi_db.connect() as conn:
        cov = conn.execute(text("SELECT coverage_json FROM mi_v_sector_latest WHERE dataset='CONSTITUENT_DISPERSION'")).scalar()
    assert cov["constituents_with_prices"] == 2 and cov["stale_constituents"] == 1
    assert cov["denominator_status"] == "PARTIAL" and cov["universe_size"] == 40 and cov["pit"] is False
    assert cov["count_valid_200dma"] == 40


def test_stored_artifact_hash_covers_complete_body_including_revision_provenance(mi_db, tmp_path):
    _registry(mi_db)
    root = write_full_precomputed_root(tmp_path / "pre", AS_OF)
    ingest_precomputed_root(mi_db, root, today=AS_OF)
    # Revise one bundle in place (same as_of): the stored row is superseded with revision provenance.
    spy = root / "spy"
    metrics = pd.read_parquet(spy / "metrics.parquet")
    metrics.loc[metrics["ETF"] == "XLK", "1W RS %"] = 0.4242
    metrics.to_parquet(spy / "metrics.parquet", index=True)
    second = ingest_precomputed_root(mi_db, root, today=AS_OF)
    spy_outcome = [o for o in second.outcomes if o.dataset == "ETF_RS_VS_SPY"][0]
    assert spy_outcome.counts.get("revised") == 1 and spy_outcome.counts.get("inserted", 0) == 0
    with mi_db.connect() as conn:
        sectors = conn.execute(text("SELECT * FROM mi_sector_snapshots")).mappings().all()
        industries = conn.execute(text("SELECT * FROM mi_industry_snapshots")).mappings().all()
    assert sectors and industries
    # Every stored row hashes to its own artifact_sha256 when reconstructed from the columns.
    assert all(verify_stored_snapshot(r, kind="sector") for r in sectors)
    assert all(verify_stored_snapshot(r, kind="industry") for r in industries)
    xlk = [r for r in sectors if r["instrument_id"] == "XLK" and r["dataset"] == "ETF_RS_VS_SPY"][0]
    prov = xlk["provenance_json"]
    assert prov["revision_seq"] == 2 and prov["previous_sha256"] and prov["content_sha256"]
    assert prov["previous_sha256"] != xlk["artifact_sha256"] and prov["content_sha256"] != xlk["artifact_sha256"]
    assert xlk["metrics_json"]["rs_chg_1w"] == 0.4242
    others = [r for r in sectors if r["dataset"] == "ETF_RS_VS_SPY" and r["instrument_id"] != "XLK"]
    assert all(r["provenance_json"]["revision_seq"] == 1 and "previous_sha256" not in r["provenance_json"] for r in others)
    # Tampering with a stored metric is detectable.
    tampered = dict(xlk)
    tampered["metrics_json"] = {**xlk["metrics_json"], "rs_chg_1w": 0.0}
    assert verify_stored_snapshot(tampered, kind="sector") is False
    # Re-saving identical content (new file mtimes/fingerprint) is a no-op: file provenance is not content.
    metrics.to_parquet(spy / "metrics.parquet", index=True)
    third = ingest_precomputed_root(mi_db, root, today=AS_OF)
    assert all(o.counts.get("inserted", 0) == 0 and o.counts.get("revised", 0) == 0 for o in third.outcomes)


def test_bridge_never_imports_provider_modules():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    code = "import sys, market_intelligence.legacy_bridge, jobs.ingest_legacy_sector_precomputed; print('\\n'.join(sorted(sys.modules)))"
    loaded = set(subprocess.run([sys.executable, "-c", code], cwd=str(root), capture_output=True, text=True, check=True).stdout.split())
    for banned in ("nightly_refresh", "precomputed_loader", "data_loader", "dispersion_engine", "requests", "fmp_sector_etfs"):
        assert banned not in loaded, banned
