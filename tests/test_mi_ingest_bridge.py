"""FRED ingestion and legacy-bundle bridge behaviour on real PostgreSQL (SYNTHETIC fixtures)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from market_intelligence.catalog import CATALOG_BY_ID
from market_intelligence.ingest_fred import REVISION_LOOKBACK, ingest_fred_catalog, request_window
from market_intelligence.legacy_bridge import BundleQuarantined, ingest_precomputed_root, read_bundle
from market_intelligence.store import upsert_source_registry
from tests.mi_fixtures import (
    FakeFredClient,
    business_days,
    daily_series,
    fake_fred_client,
    synthetic_fred_data,
    write_dispersion_bundle,
    write_full_precomputed_root,
    write_rotation_style_bundle,
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
    assert fresh["series:DGS2"] == ("FAILED", "UNKNOWN") and fresh["series:DGS10"] == ("OK", "FRESH")


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
        fresh = conn.execute(text("SELECT transport_status, latest_observation_date FROM mi_data_freshness WHERE dataset='precomputed_sector_bundles'")).one()
        statuses = [r[0] for r in conn.execute(text("SELECT status FROM mi_ingestion_runs WHERE dataset LIKE 'bundle:%' ORDER BY started_at"))]
    assert after == before  # previous valid snapshot intact
    assert fresh.transport_status == "OK" and fresh.latest_observation_date == AS_OF  # skipped bundles, nothing newer accepted
    assert "SKIPPED" in statuses


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


def test_bridge_never_imports_provider_modules():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    code = "import sys, market_intelligence.legacy_bridge, jobs.ingest_legacy_sector_precomputed; print('\\n'.join(sorted(sys.modules)))"
    loaded = set(subprocess.run([sys.executable, "-c", code], cwd=str(root), capture_output=True, text=True, check=True).stdout.split())
    for banned in ("nightly_refresh", "precomputed_loader", "data_loader", "dispersion_engine", "requests", "fmp_sector_etfs"):
        assert banned not in loaded, banned
