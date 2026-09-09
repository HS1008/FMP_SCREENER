"""Fixture-fed end-to-end path on real PostgreSQL (SYNTHETIC data; not research evidence):

synthetic FRED + legacy bundles -> canonical DB -> analytics -> morning snapshot ->
read-only role -> AI context API + Streamlit pages, with every HTTP client patched to raise.

Also proves: DB denies DML/DDL and raw-table reads under the real read-only role; auth
missing/wrong/correct; no mutating routes; export redaction; immutable snapshots;
deterministic replay; refresh CLI partial failure + lock contention.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from market_intelligence import morning_context, readonly_db
from market_intelligence.analytics import build_analytics
from market_intelligence.export_policy import verify_export_hash
from market_intelligence.ingest_fred import ingest_fred_catalog
from market_intelligence.legacy_bridge import ingest_precomputed_root
from market_intelligence.locking import EXIT_LOCK_CONTENTION, writer_lock
from market_intelligence.morning_context import HistoricalReconstructionUnsupported, build_and_publish
from market_intelligence.nulls import canonical_sha256, strict_dumps, strict_loads
from market_intelligence.pit_sector import ingest_artifact
from market_intelligence.store import finish_run, start_run, upsert_source_registry
from tests.mi_fixtures import SYNTHETIC_MARKER, fake_fred_client, write_full_precomputed_root

ROOT = Path(__file__).resolve().parent.parent
PAGES = ROOT / "pages"
AS_OF = date(2024, 12, 31)
HISTORY_START = date(2024, 7, 1)
GENERATED_AT = datetime(2025, 1, 2, 11, 30, tzinfo=timezone.utc)
# The builder's capture clock is the DB transaction timestamp. The synthetic data ends 2024-12-31,
# so the module pins that clock to GENERATED_AT (a test seam on a private function, not a
# production knob); clock-advancement tests move it forward explicitly.
CAPTURE_CLOCK = {"now": GENERATED_AT}
MI_PAGES = sorted(p for p in PAGES.glob("1[0-8]_*.py"))


def _boom(*_a, **_k):
    raise AssertionError("network call attempted during DB-only consumer read")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every HTTP client path raises; PostgreSQL (psycopg2) is unaffected."""
    monkeypatch.setattr(http.client.HTTPConnection, "connect", _boom)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", _boom)
    import requests

    monkeypatch.setattr(requests.Session, "request", _boom)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _boom)
    yield


@pytest.fixture(scope="module", autouse=True)
def pinned_capture_clock():
    mp = pytest.MonkeyPatch()
    mp.setattr(morning_context, "_db_now", lambda conn: CAPTURE_CLOCK["now"])
    yield
    mp.undo()
    CAPTURE_CLOCK["now"] = GENERATED_AT


@pytest.fixture(scope="module")
def populated(pg_engine, tmp_path_factory, pinned_capture_clock):
    """Populate the disposable DB once for this module (module-scoped; tests only read)."""
    from tests.conftest import MI_TABLES_TRUNCATE

    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE {0} CASCADE".format(", ".join(MI_TABLES_TRUNCATE))))
        conn.execute(text("DELETE FROM research_runs WHERE strategy_id = 'FIXTURE_STRATEGY'"))
        conn.execute(
            text(
                """
                INSERT INTO research_runs (research_run_id, strategy_id, run_status, git_commit, first_seen_at, last_seen_at,
                    research_kind, economic_gate, promotion_gate, holdout_status, delivery_status, expected_experiment_count, synced_experiment_count, completed_count, failed_count, skipped_count)
                VALUES ('FIXTURE_RUN', 'FIXTURE_STRATEGY', 'COMPLETE', 'deadbeef', NOW(), NOW(), 'FIXTURE', 'NOT_DEFINED', 'LOCKED', 'EXCLUDED', 'LAST_KNOWN_GOOD', 3, 3, 3, 0, 0)
                """
            )
        )
        upsert_source_registry(conn, enabled={"FRED": True, "FMP_LEGACY": True}, access={"FRED": "CONFIGURED", "FMP_LEGACY": "CONFIGURED"})
        parent = start_run(conn, source_id="ORCHESTRATOR", dataset="fixture_pipeline")
        from market_intelligence.ingest_finra import ingest_finra, record_individual_trace_limitation

        record_individual_trace_limitation(conn)
    class _FixtureFinra:
        def query_all(self, spec, **_kwargs):
            if spec.dataset == "corporateMarketBreadth":
                return [
                    {
                        "tradeReportDate": AS_OF.isoformat(),
                        "productCategory": "all securities",
                        "totalVolume": 1000.0,
                        "totalTrades": 100,
                        "advances": 40,
                        "declines": 30,
                        "unchanged": 5,
                        "fiftyTwoWeekHigh": 2,
                        "fiftyTwoWeekLow": 1,
                    }
                ]
            if spec.dataset == "corporateMarketSentiment":
                return [
                    {"tradeReportDate": AS_OF.isoformat(), "tradeType": "all securities", "productCategory": "customer buy", "totalVolume": 400.0, "totalTrades": 40, "totalTransactions": 40},
                    {"tradeReportDate": AS_OF.isoformat(), "tradeType": "all securities", "productCategory": "customer sell", "totalVolume": 250.0, "totalTrades": 30, "totalTransactions": 30},
                ]
            if spec.dataset == "corporatesAndAgenciesCappedVolume":
                return [
                    {"tradeReportDate": AS_OF.isoformat(), "gradeCode": "IG", "144AFlag": "N", "totalTradeCount": 80, "totalVolumeQuantity": 500.0, "customerBuyParLessThan5YearsQuantity": 100.0, "customerSellParLessThan5YearsQuantity": 50.0}
                ]
            return []

    ingest_finra(pg_engine, _FixtureFinra(), parent_run_id=parent, today=AS_OF, mode="full")
    client = fake_fred_client(AS_OF)
    fred = ingest_fred_catalog(pg_engine, client, mode="full", today=AS_OF, parent_run_id=parent)
    assert not fred.failed, [r.as_dict() for r in fred.failed]
    root = write_full_precomputed_root(tmp_path_factory.mktemp("precomputed"), AS_OF)
    legacy = ingest_precomputed_root(pg_engine, root, parent_run_id=parent, today=AS_OF)
    assert not legacy.failed_bundles, legacy.as_dict()
    with pg_engine.begin() as conn:
        rid = start_run(conn, source_id="ANALYTICS", dataset="metric_snapshots", parent_run_id=parent)
        # First run: bounded historical backfill so page charts have real stored history immediately.
        analytics = build_analytics(conn, as_of=AS_OF, run_id=rid, history_start=HISTORY_START)
        finish_run(conn, rid, status="SUCCEEDED", details=analytics.as_dict())
    with pg_engine.begin() as conn:
        # Synthetic PIT sector internals artifact from the isolated quant-strategies producer (research-ineligible).
        pit = ingest_artifact(conn, json.loads((ROOT / "tests" / "fixtures" / "sector_internals_v1_synthetic.json").read_text(encoding="utf-8")), source_ref="sector_internals_v1_synthetic.json", parent_run_id=parent)
        assert pit.status == "INGESTED" and not pit.research_eligible
        finish_run(conn, parent, status="SUCCEEDED")
    morning = build_and_publish(pg_engine, parent_run_id=parent, generated_at=GENERATED_AT)
    return {"fred": fred, "legacy": legacy, "analytics": analytics, "morning": morning, "root": root, "client": client, "parent": parent}


def provision_role_with_psql(admin_url: str, role: str, password: str | None, tmp_dir: Path) -> subprocess.CompletedProcess:
    """Run the real db/roles file through psql (it uses psql meta-commands), renamed to a test role.

    The password is passed as a ``\\set`` inside a temp file, never in argv.
    """
    psql = shutil.which("psql")
    if psql is None:
        pytest.fail("psql client is required for read-only role tests (install postgresql-client)")
    sql = (ROOT / "db" / "roles" / "market_intelligence_readonly.sql").read_text(encoding="utf-8").replace("mi_readonly", role)
    if password is not None:
        sql = "\\set ro_password '{0}'\n".format(password) + sql
    path = tmp_dir / "{0}.sql".format(role)
    path.write_text(sql, encoding="utf-8")
    return subprocess.run([psql, admin_url, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(path)], capture_output=True, text=True, check=False, timeout=120)


@pytest.fixture(scope="module")
def ro_engine(pg_engine, pg_database, pg_admin_url, tmp_path_factory):
    """Provision the real read-only role from db/roles/*.sql (test-unique name) and return its engine."""
    role = "mi_readonly_test_{0}".format(uuid.uuid4().hex[:8])
    password = "ro_{0}".format(uuid.uuid4().hex)
    tmp_dir = tmp_path_factory.mktemp("roles")
    admin_on_test_db = make_url(pg_admin_url).set(database=make_url(pg_database).database, drivername="postgresql").render_as_string(hide_password=False)
    first = provision_role_with_psql(admin_on_test_db, role, password, tmp_dir)
    assert first.returncode == 0, first.stderr
    # Repeatable: a second run without a password refreshes grants and does not touch the password.
    second = provision_role_with_psql(admin_on_test_db, role, None, tmp_dir)
    assert second.returncode == 0, second.stderr
    assert "refreshing grants only" in second.stdout
    url = make_url(pg_database).set(username=role, password=password)
    engine = create_engine(url, future=True, pool_pre_ping=True, connect_args={"options": "-c default_transaction_read_only=on -c statement_timeout={0}".format(readonly_db.DEFAULT_STATEMENT_TIMEOUT_MS)})
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1  # original password still valid after the refresh run
    yield engine
    engine.dispose()
    with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = :u"), {"u": role})
        conn.execute(text('DROP OWNED BY "{0}"'.format(role)))
    admin = create_engine(pg_admin_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text('DROP ROLE IF EXISTS "{0}"'.format(role)))
    admin.dispose()


@pytest.fixture
def consumer(ro_engine, populated, monkeypatch):
    """Consumers read only through the injected read-only engine; no writer URL, no token leak."""
    readonly_db.set_engine_for_tests(ro_engine)
    monkeypatch.delenv("DATABASE_READONLY_URL", raising=False)
    monkeypatch.setenv("AI_CONTEXT_API_TOKEN", "fixture-token")
    from market_intelligence.ui import clear_read_cache

    clear_read_cache()
    yield ro_engine
    readonly_db.set_engine_for_tests(None)
    clear_read_cache()


# ---- ingestion / analytics facts -------------------------------------------------------------------

def test_fixture_marker_present_and_ingest_counts(populated):
    assert (populated["root"] / SYNTHETIC_MARKER).exists()
    fred = populated["fred"]
    assert fred.as_dict()["series_failed"] == 0 and fred.as_dict()["series_total"] >= 44
    by_id = {r.series_id: r for r in fred.results}
    assert by_id["DGS10"].freshness_status == "FRESH" and by_id["CPIAUCSL"].freshness_status == "FRESH"
    assert by_id["DGS10"].counts["rejected"] == 0 and by_id["DGS10"].counts["inserted"] > 700
    legacy = populated["legacy"].as_dict()
    assert legacy["bundles_processed"] >= 5 and legacy["bundles_failed"] == 0


def test_ice_history_truncation_yields_insufficient_history_not_failure(pg_engine, populated):
    with pg_engine.connect() as conn:
        rows = conn.execute(text("SELECT series_id, oas_bps, percentile, percentile_window, window_observations, history_status FROM mi_v_credit_latest ORDER BY series_id")).mappings().all()
    assert len(rows) == 9
    ig = next(r for r in rows if r["series_id"] == "BAMLC0A0CM")
    assert ig["oas_bps"] is not None and float(ig["oas_bps"]) > 0
    # ~300 sessions of history: 1Y window (>=200 obs) is adequate; 3Y is not -> labeled 1Y, never relabeled.
    assert ig["percentile_window"] == "1Y" and ig["percentile"] is not None
    assert ig["history_status"] == "LIMITED_TO_1Y" and ig["window_observations"] >= 200


def test_curve_slopes_and_bps_changes_are_stored_with_units(pg_engine, populated):
    with pg_engine.connect() as conn:
        metrics = {r["metric_id"]: r for r in conn.execute(text("SELECT metric_id, value, units, as_of, detail_json FROM mi_v_metric_latest")).mappings().all()}
    slope = metrics["curve.slope_10Y2Y_bps"]
    assert slope["units"] == "bps" and slope["detail_json"]["long"] == "DGS10" and slope["detail_json"]["short"] == "DGS2"
    assert {"curve.slope_30Y2Y_bps", "curve.slope_30Y5Y_bps", "curve.slope_10Y3M_bps"} <= set(metrics)
    dgs10_chg = next(v for k, v in metrics.items() if k.startswith("DGS10.chg_prev_bps"))
    assert dgs10_chg["units"] == "bps"
    assert dgs10_chg["detail_json"]["comparison_date"] < dgs10_chg["as_of"].isoformat()
    cpi_yoy = next(v for k, v in metrics.items() if k.startswith("CPIAUCSL.yoy_pct"))
    assert cpi_yoy["units"] == "pct" and cpi_yoy["detail_json"]["lag_date"] == "2023-12-01"
    # Provider-native units are stored; display conversion is a separate versioned metric.
    assert metrics["WTREGEN.level"]["units"] == "millions_usd" and float(metrics["WTREGEN.level"]["value"]) > 100000
    assert metrics["WTREGEN.level_display"]["units"] == "billions_usd"
    assert float(metrics["WTREGEN.level_display"]["value"]) == pytest.approx(float(metrics["WTREGEN.level"]["value"]) / 1000)
    assert metrics["WTREGEN.level_display"]["detail_json"]["conversion_version"] == "display_scale_v1"


def test_first_run_backfill_populates_metric_history_bounded_and_idempotent(pg_engine, populated):
    """Review finding: analytics wrote only the latest date, so the Credit chart had one point."""
    report = populated["analytics"].as_dict()
    assert report["history_dates_per_series"]["BAMLC0A0CM"] > 100 and report["truncated_series"] == []
    with pg_engine.connect() as conn:
        hist = conn.execute(text("SELECT as_of, value FROM mi_v_metric_history WHERE metric_id='BAMLC0A0CM.oas_bps' ORDER BY as_of")).all()
        credit_hist = conn.execute(text("SELECT COUNT(*) FROM mi_credit_index_snapshots WHERE series_id='BAMLH0A0HYM2'")).scalar()
        slope_hist = conn.execute(text("SELECT COUNT(*) FROM mi_metric_snapshots WHERE metric_id='curve.slope_10Y2Y_bps'")).scalar()
        lineage = conn.execute(text("SELECT inputs_retrieved_max FROM mi_metric_snapshots WHERE metric_id='BAMLC0A0CM.oas_bps' ORDER BY as_of DESC LIMIT 1")).scalar()
        before = conn.execute(text("SELECT COUNT(*), MAX(computed_at) FROM mi_metric_snapshots")).one()
    assert len(hist) > 100 and hist[0][0] >= HISTORY_START and hist[-1][0] == AS_OF
    assert credit_hist > 100 and slope_hist > 100 and lineage is not None
    # Idempotent: the same backfill again changes no row counts and no values.
    with pg_engine.begin() as conn:
        again = build_analytics(conn, as_of=AS_OF, run_id=None, history_start=HISTORY_START)
        after = conn.execute(text("SELECT COUNT(*), MAX(computed_at) FROM mi_metric_snapshots")).one()
        hist2 = conn.execute(text("SELECT as_of, value FROM mi_v_metric_history WHERE metric_id='BAMLC0A0CM.oas_bps' ORDER BY as_of")).all()
    assert again.metrics_written > 0 and after[0] == before[0] and hist2 == hist


def test_revision_aware_incremental_recompute_covers_dates_after_the_revised_input(pg_engine, populated):
    """A revised input on date D must recompute rolling/lagged values for every later date."""
    from decimal import Decimal

    from market_intelligence.analytics import last_analytics_run_at, revised_since
    from market_intelligence.store import ObservationInput, upsert_observations

    revised_day = AS_OF - timedelta(days=45)
    with pg_engine.connect() as conn:
        d, old = conn.execute(text("SELECT observation_date, value FROM mi_macro_observations WHERE series_id='BAMLC0A0CM' AND is_current AND observation_date <= :d ORDER BY observation_date DESC LIMIT 1"), {"d": revised_day}).one()
        before = {r[0]: r[1] for r in conn.execute(text("SELECT as_of, value FROM mi_metric_snapshots WHERE metric_id='BAMLC0A0CM.chg_1m_bps' ORDER BY as_of")).all()}
        since = last_analytics_run_at(conn)
    assert since is not None
    retrieved_at = datetime.now(timezone.utc)
    with pg_engine.begin() as conn:
        counts = upsert_observations(conn, series_id="BAMLC0A0CM", rows=[ObservationInput(d, str(Decimal(old) + Decimal("0.50")), date(2024, 1, 1), date(9999, 12, 31))], retrieved_at=retrieved_at, run_id=None, today=AS_OF)
        assert counts.revised == 1
        assert revised_since(conn, since) == {"BAMLC0A0CM": d}
        rid = start_run(conn, source_id="ANALYTICS", dataset="metric_snapshots")
        report = build_analytics(conn, run_id=rid, since=since)
        finish_run(conn, rid, status="SUCCEEDED", details=report.as_dict())
        after = {r[0]: r[1] for r in conn.execute(text("SELECT as_of, value FROM mi_metric_snapshots WHERE metric_id='BAMLC0A0CM.chg_1m_bps' ORDER BY as_of")).all()}
        # The revision is retained as history, not overwritten.
        revs = conn.execute(text("SELECT COUNT(*) FROM mi_macro_observations WHERE series_id='BAMLC0A0CM' AND observation_date=:d"), {"d": d}).scalar()
    assert revs == 2 and report.history_dates.get("BAMLC0A0CM", 0) > 1
    changed = [a for a in before if a >= d and before[a] != after.get(a)]
    unchanged = [a for a in before if a < d and before[a] != after.get(a)]
    assert changed and not unchanged, (len(changed), len(unchanged))
    # Restore the original value (another revision) so later tests see the fixture state.
    with pg_engine.begin() as conn:
        upsert_observations(conn, series_id="BAMLC0A0CM", rows=[ObservationInput(d, str(old), date(2024, 1, 1), date(9999, 12, 31))], retrieved_at=datetime.now(timezone.utc), run_id=None, today=AS_OF)
        build_analytics(conn, run_id=None, history_start=HISTORY_START, series_ids=["BAMLC0A0CM"])


def test_metadata_mismatch_quarantines_series_without_touching_others(pg_engine, populated):
    """Review finding: MISMATCH used to write observations and mark success."""
    bad = fake_fred_client(AS_OF, metadata={"WTREGEN": {"units": "Billions of U.S. Dollars", "units_short": "Bil. of U.S. $"}, "DGS2": {"id": "DGS1"}, "M2SL": {"units": None}})
    with pg_engine.begin() as conn:
        parent = start_run(conn, source_id="ORCHESTRATOR", dataset="quarantine_test")
        wtregen_before = conn.execute(text("SELECT COUNT(*) FROM mi_macro_observations WHERE series_id='WTREGEN' AND is_current")).scalar()
        units_before = conn.execute(text("SELECT units, publication_status FROM mi_macro_series WHERE series_id='WTREGEN'")).one()
    report = ingest_fred_catalog(pg_engine, bad, mode="incremental", today=AS_OF, parent_run_id=parent, series_ids=("WTREGEN", "DGS2", "M2SL", "DGS10"))
    by_id = {r.series_id: r for r in report.results}
    assert by_id["DGS10"].status == "SUCCEEDED"
    assert {r.series_id for r in report.quarantined} == {"WTREGEN", "DGS2", "M2SL"}
    assert by_id["WTREGEN"].metadata_status == "MISMATCH" and by_id["DGS2"].metadata_status == "IDENTITY_MISMATCH" and by_id["M2SL"].metadata_status == "UNAVAILABLE"
    assert report.transport_status == "PARTIAL"
    with pg_engine.connect() as conn:
        # Prior valid data and its validated metadata survive; nothing new was promoted.
        assert conn.execute(text("SELECT COUNT(*) FROM mi_macro_observations WHERE series_id='WTREGEN' AND is_current")).scalar() == wtregen_before
        units_after = conn.execute(text("SELECT units, publication_status, publication_reason, metadata_status FROM mi_macro_series WHERE series_id='WTREGEN'")).one()
        assert units_after.units == units_before.units and units_after.publication_status == "QUARANTINED_METADATA" and units_after.metadata_status == "MISMATCH"
        assert "Billions" in units_after.publication_reason
        q = conn.execute(text("SELECT series_id, COUNT(*), MIN(reason) FROM mi_macro_observation_quarantine GROUP BY series_id ORDER BY series_id")).all()
        assert {r[0] for r in q} == {"WTREGEN", "DGS2", "M2SL"} and all(r[1] > 0 for r in q)
        fresh = conn.execute(text("SELECT transport_status, metadata_status, latest_observation_date FROM mi_data_freshness WHERE dataset='series:WTREGEN'")).one()
        assert fresh.transport_status == "METADATA_REJECTED" and fresh.metadata_status == "MISMATCH" and fresh.latest_observation_date == max(d for d, _ in bad.data["WTREGEN"])
        summary = conn.execute(text("SELECT * FROM mi_v_macro_quarantine_summary WHERE series_id='WTREGEN'")).mappings().one()
        assert summary["quarantined_rows"] > 0
        runs = conn.execute(text("SELECT status FROM mi_ingestion_runs WHERE dataset='series:WTREGEN' ORDER BY started_at DESC LIMIT 1")).scalar()
    assert runs == "QUARANTINED"
    # A later validated retrieval republishes the series and clears the gate.
    good = fake_fred_client(AS_OF)
    report2 = ingest_fred_catalog(pg_engine, good, mode="incremental", today=AS_OF, parent_run_id=parent, series_ids=("WTREGEN", "DGS2", "M2SL"))
    assert not report2.failed and not report2.quarantined
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT publication_status FROM mi_macro_series WHERE series_id='WTREGEN'")).scalar() == "PUBLISHED"


def test_future_dated_observations_are_quarantined_not_promoted(pg_engine, populated):
    from market_intelligence.store import ObservationInput, upsert_observations

    future = AS_OF + timedelta(days=30)
    with pg_engine.begin() as conn:
        counts = upsert_observations(conn, series_id="DGS10", rows=[ObservationInput(future, "4.5", date(2024, 1, 1), date(9999, 12, 31))], retrieved_at=datetime.now(timezone.utc), run_id=None, today=AS_OF)
        assert counts.inserted == 0
        assert conn.execute(text("SELECT COUNT(*) FROM mi_macro_observations WHERE series_id='DGS10' AND observation_date=:d"), {"d": future}).scalar() == 0
        reason = conn.execute(text("SELECT reason FROM mi_macro_observation_quarantine WHERE series_id='DGS10' AND observation_date=:d"), {"d": future}).scalar()
    assert reason == "FUTURE_OBSERVATION_DATE"


def test_sector_snapshots_keep_provider_labels_units_and_per_group_dates(pg_engine, populated):
    with pg_engine.connect() as conn:
        rows = conn.execute(text("SELECT dataset, sector_key, entity_kind, provider_label, benchmark, return_basis, as_of, metrics_json, coverage_json, universe_method, research_eligible FROM mi_v_sector_latest")).mappings().all()
    rs = [r for r in rows if r["dataset"] == "ETF_RS_VS_SPY"]
    disp = [r for r in rows if r["dataset"] == "CONSTITUENT_DISPERSION"]
    assert {r["sector_key"] for r in rs} >= {"Consumer Discretionary", "Financials", "Health Care", "Materials", "Consumer Staples", "Technology"}
    hc = next(r for r in rs if r["sector_key"] == "Health Care")
    assert hc["provider_label"] == "Healthcare" and hc["benchmark"] == "SPY" and hc["entity_kind"] == "SECTOR"
    assert "ratio" in (hc["return_basis"] or "").lower() or "price" in (hc["return_basis"] or "").lower()
    ai = next(r for r in rs if r["provider_label"] == "AI")
    assert ai["entity_kind"] == "THEME"
    assert disp and disp[0]["universe_method"] == "CURRENT_UNIVERSE_CONTEXT_ONLY"
    # Dispersion lags rotation by one session; per-group as_of preserved, not merged to the newest.
    assert disp[0]["as_of"] < hc["as_of"] == AS_OF
    assert disp[0]["metrics_json"].get("top5_return_contribution") is None and disp[0]["research_eligible"] is False  # NaN JSON -> NULL


# ---- morning snapshot ----------------------------------------------------------------------------------

def test_morning_snapshot_is_hashed_current_only_with_db_capture_time_and_lineage(pg_engine, populated):
    morning = populated["morning"]
    assert morning.published_new and morning.completeness == "COMPLETE", morning.sections_status
    with pg_engine.connect() as conn:
        row = conn.execute(text("SELECT snapshot_json, snapshot_sha256, content_sha256, generated_at, cutoff_at, quality_status, publication_state FROM mi_v_morning_context_latest")).mappings().one()
    body = row["snapshot_json"]
    assert body["artifact_sha256"] == row["snapshot_sha256"] == canonical_sha256(body)
    assert body["content_sha256"] == row["content_sha256"] == morning_context.content_hash(body)
    assert body["schema_version"] == "morning_context_v2" and row["publication_state"] == "PUBLISHED"
    # cutoff_at is the DB capture time (pinned here), not the caller-supplied generated_at.
    assert body["cutoff_at"] == GENERATED_AT.isoformat() and body["generation_params"]["builder"] == "current_only"
    assert "as_of" not in body["generation_params"]
    assert set(body["sections"]) >= {"data_health", "market", "macro", "rates", "credit", "sectors", "liquidity", "strategy_monitor_summary"}
    assert body["sections"]["market"]["data"]["overnight_quotes"]["status"] != "OK"
    assert body["sections"]["market"]["data"]["leadership_order"].startswith("sector_key")
    strategies = body["sections"]["strategy_monitor_summary"]["data"]["strategies"]
    fixture = next(s for s in strategies if s["strategy_id"] == "FIXTURE_STRATEGY")
    assert fixture["research_status"] == "COMPLETE" and fixture["economic_gate"] == "NOT_DEFINED" and fixture["promotion_gate"] == "LOCKED"
    text_json = strict_dumps(body)
    assert "NaN" not in text_json and strict_loads(text_json) == body
    # Exact input lineage: series revisions, metric rows, credit rows, artifact hashes, run ids, versions.
    refs = body["input_refs"]
    assert {r["series_id"] for r in refs["series_revisions"]} >= {"DGS10", "CPIAUCSL", "WTREGEN"}
    dgs10 = next(r for r in refs["series_revisions"] if r["series_id"] == "DGS10")
    assert dgs10["revision_seq"] is not None and dgs10["ingestion_run_id"] and dgs10["observation_date"] == AS_OF.isoformat()
    assert dgs10["publication_status"] == "PUBLISHED"
    assert any(m["metric_id"] == "curve.slope_10Y2Y_bps" for m in refs["metric_rows"])
    assert len(refs["credit_rows"]) == 9 and len(refs["sector_artifact_hashes"]) >= 5
    assert refs["contributing_ingestion_run_ids"] and refs["versions"]["catalog_version"] == "fred_catalog_v2"
    assert refs["versions"]["freshness_policy_version"]
    # Captured health is frozen inside the body and every section carries its own freshness.
    assert body["captured_health"]["evaluated_at"] == body["cutoff_at"] and body["captured_health"]["quarantined_series"] == []
    assert body["sections_status"]["credit"]["captured_freshness"]["status"] == "FRESH"
    assert body["sections_status"]["macro"]["captured_freshness"]["cadence"] == "M"
    assert body["semantics"]["vintage"].startswith("FRED values are LATEST_REVISED")


def test_morning_snapshot_replay_dedupes_by_content_and_supersedes_explicitly(pg_engine, populated):
    # Earlier tests revised inputs (new revision_seq / run ids), so the current content may differ
    # from the fixture snapshot; that is a real content change and publishes a new row honestly.
    morning = build_and_publish(pg_engine, parent_run_id=populated["parent"], generated_at=GENERATED_AT)
    assert morning.published_new == (morning.snapshot_id != populated["morning"].snapshot_id)
    with pg_engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM mi_morning_context_snapshots")).scalar()
    # Replay with identical frozen inputs -> same content hash, nothing new published.
    replay = build_and_publish(pg_engine, parent_run_id=populated["parent"], generated_at=GENERATED_AT)
    assert replay.snapshot_sha256 == morning.snapshot_sha256 and replay.content_sha256 == morning.content_sha256 and replay.published_new is False
    # A different runtime timestamp / parent run alone is the same content: still not republished.
    later = build_and_publish(pg_engine, parent_run_id="another-run", generated_at=GENERATED_AT.replace(hour=12))
    assert later.published_new is False and later.content_sha256 == morning.content_sha256 and later.snapshot_id == morning.snapshot_id
    # Dedup must name the stored snapshot (id/hash/timestamps/body), not the just-computed one.
    assert later.generated_at == morning.generated_at and later.cutoff_at == morning.cutoff_at
    assert later.body["artifact_sha256"] == morning.snapshot_sha256
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM mi_morning_context_snapshots")).scalar() == count
    # Explicit supersession publishes a successor and marks the old row without editing its body.
    corrected = build_and_publish(pg_engine, parent_run_id=populated["parent"], generated_at=GENERATED_AT.replace(hour=13), supersede_reason="test: republish after metadata correction")
    assert corrected.published_new and corrected.snapshot_id != morning.snapshot_id and corrected.superseded_snapshot_id == morning.snapshot_id
    with pg_engine.connect() as conn:
        old = conn.execute(text("SELECT snapshot_sha256, publication_state, superseded_by, quality_status, snapshot_json FROM mi_morning_context_snapshots WHERE snapshot_id=:s"), {"s": morning.snapshot_id}).mappings().one()
        latest = conn.execute(text("SELECT snapshot_id FROM mi_v_morning_context_latest")).scalar()
        index = conn.execute(text("SELECT snapshot_id, publication_state, superseded_by FROM mi_v_morning_context_index ORDER BY generated_at")).all()
    assert old["snapshot_sha256"] == morning.snapshot_sha256 == old["snapshot_json"]["artifact_sha256"]
    assert old["publication_state"] == "SUPERSEDED" and old["superseded_by"] == corrected.snapshot_id and old["quality_status"] == "SUPERSEDED"
    assert latest == corrected.snapshot_id and len(index) == count + 1 and {"SUPERSEDED", "PUBLISHED"} <= {r[1] for r in index}
    # Quality flags never touch the body either.
    with pg_engine.begin() as conn:
        assert morning_context.mark_quality(conn, corrected.snapshot_id, quality_status="QUESTIONABLE", note="fixture") == 1
        q = conn.execute(text("SELECT quality_status, snapshot_sha256 FROM mi_morning_context_snapshots WHERE snapshot_id=:s"), {"s": corrected.snapshot_id}).one()
        assert q == ("QUESTIONABLE", corrected.snapshot_sha256)
        conn.execute(text("UPDATE mi_morning_context_snapshots SET quality_status = 'OK', quality_note = NULL WHERE snapshot_id=:s"), {"s": corrected.snapshot_id})


def test_morning_snapshot_refuses_historical_reconstruction(pg_engine, populated):
    with pytest.raises(HistoricalReconstructionUnsupported) as excinfo:
        build_and_publish(pg_engine, requested_cutoff=GENERATED_AT - timedelta(days=3))
    assert "historical snapshots cannot be rebuilt" in str(excinfo.value)
    # A cutoff request at/after the capture time is accepted (it is not a reconstruction).
    ok = build_and_publish(pg_engine, requested_cutoff=GENERATED_AT + timedelta(minutes=1))
    assert ok.published_new is False  # same content as the latest published snapshot


def test_morning_snapshot_health_advances_with_the_clock_without_ingestion(pg_engine, populated):
    """Freshness must decay when nobody ingests; a new capture on a later day is new content."""
    CAPTURE_CLOCK["now"] = GENERATED_AT + timedelta(days=30)
    try:
        aged = build_and_publish(pg_engine, parent_run_id=populated["parent"], generated_at=GENERATED_AT + timedelta(days=30))
        assert aged.published_new and aged.completeness == "PARTIAL"
        st = aged.sections_status
        assert st["credit"]["status"] == "STALE" and st["rates"]["status"] == "STALE"
        assert "CPIAUCSL" not in st["macro"]["stale_required"]
        assert set(st["macro"]["stale_required"]) >= {"ICSA", "CCSA", "DFF", "SOFR"}
        assert "required series stale at capture" in (st["credit"]["reason"] or "")
        assert aged.body["captured_health"]["stale_sources"], "daily FRED datasets must be listed stale at capture"
        # Live read model agrees: stored status was FRESH at ingest, re-evaluated STALE now.
        from market_intelligence.read_models import source_health

        with pg_engine.connect() as conn:
            rows = {r["freshness_dataset"]: r for r in source_health(conn, today=(GENERATED_AT + timedelta(days=30)).date())}
        dgs10 = rows["series:DGS10"]
        assert dgs10["stored_freshness_status"] == "FRESH" and dgs10["freshness_status"] == "STALE" and dgs10["dataset_cadence"] == "D"
        assert rows["series:CPIAUCSL"]["freshness_status"] == "FRESH" and rows["series:CPIAUCSL"]["dataset_cadence"] == "M"
        assert "expected_next_release" not in dgs10 and dgs10["stale_after_estimate"] is not None
    finally:
        CAPTURE_CLOCK["now"] = GENERATED_AT
        with pg_engine.begin() as conn:
            conn.execute(text("DELETE FROM mi_morning_context_snapshots WHERE snapshot_id=:s"), {"s": aged.snapshot_id})
            conn.execute(text("UPDATE mi_morning_context_snapshots SET publication_state='PUBLISHED', superseded_by=NULL, superseded_at=NULL, quality_status='OK', quality_note=NULL WHERE publication_state='SUPERSEDED' AND superseded_by=:s"), {"s": aged.snapshot_id})


def test_morning_snapshot_empty_db_is_explicit(pg_engine, populated):
    """Catalog-only / empty DB: sections UNAVAILABLE with reasons, completeness EMPTY, nothing fabricated."""
    from market_intelligence.read_models import macro_context, rates_context

    with pg_engine.connect() as conn:
        conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with conn.begin() as tx:
            conn.execute(text("SET LOCAL statement_timeout = '60s'"))
            conn.execute(text("TRUNCATE TABLE mi_morning_context_snapshots, mi_credit_index_snapshots, mi_metric_snapshots, mi_industry_snapshots, mi_sector_snapshots, mi_macro_observations, mi_macro_observation_quarantine, mi_macro_series, mi_data_freshness, mi_ingestion_runs, mi_source_registry CASCADE"))
            body = morning_context.build_snapshot_body(conn, generated_at=GENERATED_AT, cutoff_at=GENERATED_AT, generation_params={})
            assert not any(c["yield_pct"] is not None for c in rates_context(conn)["curve"])
            assert macro_context(conn)["categories"] == {} or all(not v for v in macro_context(conn)["categories"].values())
            tx.rollback()
    # Only the research-run summary (research_runs table, untouched) is available -> PARTIAL, not COMPLETE.
    assert body["completeness"] == "PARTIAL" and body["sections"]["strategy_monitor_summary"]["status"] == "OK"
    for name in ("macro", "rates", "credit", "sectors", "liquidity", "market", "data_health"):
        assert body["sections"][name]["status"] == "UNAVAILABLE" and body["sections"][name]["data"] is None and body["sections"][name]["reason"]
    assert body["input_refs"]["series_revisions"] == [] and body["input_refs"]["credit_rows"] == []


# ---- read-only role: real denial -----------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO mi_source_registry (source_id, provider, dataset) VALUES ('X','x','x')",
        "UPDATE mi_macro_observations SET value = 0",
        "DELETE FROM mi_morning_context_snapshots",
        "CREATE TABLE mi_should_fail (id INT)",
        "SELECT COUNT(*) FROM mi_macro_observations",
        "SELECT COUNT(*) FROM mi_pit_sector_internals",
        "UPDATE mi_pit_sector_internals SET is_current = FALSE",
        "SELECT COUNT(*) FROM research_runs",
        "INSERT INTO research_runs (research_run_id, strategy_id) VALUES ('x','y')",
    ],
)
def test_readonly_role_denies_writes_and_raw_tables(ro_engine, populated, sql):
    with pytest.raises(Exception) as excinfo:
        with ro_engine.begin() as conn:
            conn.execute(text(sql))
    message = str(excinfo.value).lower()
    assert "permission denied" in message or "read-only transaction" in message


def test_readonly_role_can_select_curated_views(ro_engine, populated):
    with ro_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM mi_v_macro_latest")).scalar() > 40
        assert conn.execute(text("SELECT COUNT(*) FROM mi_v_strategy_research_summary WHERE strategy_id='FIXTURE_STRATEGY'")).scalar() == 1
        for view in readonly_db.REQUIRED_VIEWS + ("mi_v_macro_quarantine_summary", "mi_v_metric_history", "mi_v_industry_latest", "mi_v_research_ideas", "mi_v_pit_sector_artifacts", "mi_v_pit_sector_internals_current", "mi_v_pit_sector_internals_latest"):
            conn.execute(text("SELECT * FROM {0} LIMIT 1".format(view)))


def test_readonly_role_privileges_hold_even_when_session_defaults_are_overridden(ro_engine, populated):
    """Privileges, not the default_transaction_read_only setting, enforce read-only."""
    for sql in ("INSERT INTO mi_source_registry (source_id, provider, dataset) VALUES ('X','x','x')", "CREATE TABLE mi_should_fail_rw (id INT)", "SELECT COUNT(*) FROM mi_macro_observations"):
        with pytest.raises(Exception) as excinfo:
            with ro_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text("SET default_transaction_read_only = off"))
                assert conn.execute(text("SHOW transaction_read_only")).scalar() == "off"
                conn.execute(text(sql))
        assert "permission denied" in str(excinfo.value).lower(), sql


def test_public_grants_are_not_cancelled_by_role_revoke_and_remediation_works(ro_engine, pg_engine, populated):
    """PostgreSQL < 15 grants CREATE ON SCHEMA public TO PUBLIC; REVOKE ... FROM <role> does not undo it."""
    role = make_url(ro_engine.url).username
    with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
        admin.execute(text("GRANT CREATE ON SCHEMA public TO PUBLIC"))
        try:
            admin.execute(text('REVOKE CREATE ON SCHEMA public FROM "{0}"'.format(role)))
            assert admin.execute(text("SELECT has_schema_privilege(:r, 'public', 'CREATE')"), {"r": role}).scalar() is True
            # Documented administrator remediation.
            admin.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
            assert admin.execute(text("SELECT has_schema_privilege(:r, 'public', 'CREATE')"), {"r": role}).scalar() is False
            assert admin.execute(text("SELECT has_schema_privilege(:r, 'public', 'USAGE')"), {"r": role}).scalar() is True
        finally:
            admin.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
    # No PUBLIC table grants exist on any mi_* / research table (the role must not inherit reads through PUBLIC).
    with pg_engine.connect() as conn:
        leaks = conn.execute(text("SELECT table_name FROM information_schema.role_table_grants WHERE grantee = 'PUBLIC' AND table_schema = 'public' AND table_name NOT LIKE 'mi\\_v\\_%'")).all()
    assert leaks == []


def test_probe_readonly_requires_every_view_readable(ro_engine, pg_engine, populated):
    readonly_db.set_engine_for_tests(ro_engine)
    try:
        probe = readonly_db.probe_readonly()
        assert probe["status"] == "OK" and probe["failing_views"] == [] and probe["transaction_read_only"] == "on"
        probe_missing = readonly_db.probe_readonly(required_views=readonly_db.REQUIRED_VIEWS + ("mi_v_does_not_exist",))
        assert probe_missing["status"] == "VIEWS_UNAVAILABLE" and probe_missing["failing_views"] == ["mi_v_does_not_exist:ProgrammingError"]
        role = make_url(ro_engine.url).username
        with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
            admin.execute(text('REVOKE SELECT ON mi_v_credit_latest FROM "{0}"'.format(role)))
            try:
                denied = readonly_db.probe_readonly()
                assert denied["status"] == "VIEWS_UNAVAILABLE" and denied["failing_views"] == ["mi_v_credit_latest:ProgrammingError"]
                assert "postgres" not in json.dumps(denied).lower()
            finally:
                admin.execute(text('GRANT SELECT ON mi_v_credit_latest TO "{0}"'.format(role)))
        assert readonly_db.probe_readonly()["status"] == "OK"
    finally:
        readonly_db.set_engine_for_tests(None)


def test_readonly_config_fails_closed_without_writer_fallback(monkeypatch):
    readonly_db.set_engine_for_tests(None)
    readonly_db._cached_engine = None
    monkeypatch.delenv("DATABASE_READONLY_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://writer:secret@127.0.0.1/should_not_be_used")
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    with pytest.raises(readonly_db.ReadOnlyUnavailable) as excinfo:
        readonly_db.readonly_engine()
    assert excinfo.value.reason == "CONFIGURATION_REQUIRED"
    assert "secret" not in str(excinfo.value)
    assert readonly_db.probe_readonly()["status"] == "CONFIGURATION_REQUIRED"


# ---- AI context API -----------------------------------------------------------------------------------

@pytest.fixture
def api(consumer):
    from fastapi.testclient import TestClient

    import ai_context_api

    return TestClient(ai_context_api.app, raise_server_exceptions=False)


SECTION_PATHS = ["/v1/context/{0}/latest".format(p) for p in ("morning", "macro", "rates", "credit", "sectors", "liquidity", "market", "strategies", "data-health", "order-flow")]
ROUTES = ["/v1/ready", *SECTION_PATHS, "/v1/context/data-health/live", "/v1/context/data-health"]


def test_health_is_minimal_and_unauthenticated(api):
    r = api.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize("route", ROUTES)
def test_all_v1_routes_require_correct_bearer_token(api, route):
    assert api.get(route).status_code == 401
    assert api.get(route, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.get(route, headers={"Authorization": "Basic Zm9vOmJhcg=="}).status_code == 401
    ok = api.get(route, headers={"Authorization": "Bearer fixture-token"})
    assert ok.status_code == 200, ok.text
    assert ok.headers["cache-control"] == "no-store"
    assert "fixture-token" not in ok.text


def test_missing_server_token_fails_closed(api, monkeypatch):
    monkeypatch.delenv("AI_CONTEXT_API_TOKEN")
    r = api.get("/v1/context/macro/latest", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503 and "token" in r.text and "fixture" not in r.text
    assert api.get("/health").status_code == 200


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_no_mutating_methods(api, method):
    r = getattr(api, method)("/v1/context/morning/latest", headers={"Authorization": "Bearer fixture-token"})
    assert r.status_code == 405


def test_docs_and_schema_routes_disabled(api):
    for route in ("/docs", "/redoc", "/openapi.json"):
        assert api.get(route).status_code == 404


def test_ready_reflects_view_readability(api, pg_engine, ro_engine):
    r = api.get("/v1/ready", headers={"Authorization": "Bearer fixture-token"})
    assert r.status_code == 200 and r.json()["ready"] is True and r.json()["database"]["failing_views"] == []
    role = make_url(ro_engine.url).username
    with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
        admin.execute(text('REVOKE SELECT ON mi_v_morning_context_latest FROM "{0}"'.format(role)))
        try:
            r = api.get("/v1/ready", headers={"Authorization": "Bearer fixture-token"})
            assert r.status_code == 503 and r.json()["ready"] is False and r.json()["database"]["status"] == "VIEWS_UNAVAILABLE"
            assert r.json()["database"]["failing_views"] == ["mi_v_morning_context_latest:ProgrammingError"]
        finally:
            admin.execute(text('GRANT SELECT ON mi_v_morning_context_latest TO "{0}"'.format(role)))


def test_morning_route_is_export_filtered_and_hash_covers_the_delivered_json(api, populated, pg_engine):
    with pg_engine.connect() as conn:
        latest = conn.execute(text("SELECT snapshot_id, snapshot_sha256, content_sha256, completeness FROM mi_v_morning_context_latest")).mappings().one()
    r = api.get("/v1/context/morning/latest", headers={"Authorization": "Bearer fixture-token"})
    payload = r.json()
    assert payload["available"] is True and payload["section"] is None
    assert payload["provenance"]["kind"] == "FROZEN_SNAPSHOT" and payload["provenance"]["snapshot_id"] == latest["snapshot_id"]
    assert payload["source_snapshot_hash"] == latest["snapshot_sha256"] == payload["provenance"]["source_snapshot_hash"]
    assert payload["export_schema_version"] == "export_safe_v2"
    assert payload["export_filtered"] is True and payload["restricted_entries"] >= 9
    assert payload["export_sha256"] != payload["source_snapshot_hash"]
    # Review finding: the hash must verify on the HTTP JSON exactly as delivered (no fields removed).
    assert verify_export_hash(payload)
    assert verify_export_hash(json.loads(r.text))
    assert not verify_export_hash(dict(payload, degraded=not payload["degraded"]))
    assert not verify_export_hash({**payload, "latest_snapshot": None})
    buckets = payload["body"]["sections"]["credit"]["data"]["buckets"]
    assert all(b["restricted"] and "oas_bps" not in b and "percentile" not in b for b in buckets)
    assert [b["series_id"] for b in buckets] == sorted(b["series_id"] for b in buckets)  # identity order after redaction
    # Unrestricted official macro data still exported — including nested latest/transforms (Finding A).
    curve = payload["body"]["sections"]["rates"]["data"]["curve"]
    assert any(c["yield_pct"] is not None for c in curve)
    macro_payload = api.get("/v1/context/macro/latest", headers={"Authorization": "Bearer fixture-token"}).json()
    assert verify_export_hash(macro_payload)
    inflation = (macro_payload["body"]["categories"] or {}).get("inflation") or []
    cpi = next(b for b in inflation if b["series_id"] == "CPIAUCSL")
    assert cpi["latest"]["value"] is not None and isinstance(cpi["latest"]["value"], (int, float))
    assert (cpi.get("transforms") or {}).get("yoy_pct", {}).get("value") is not None
    assert "oas_bps" not in json.dumps(macro_payload["body"])
    assert payload["latest_snapshot"]["snapshot_id"] == latest["snapshot_id"] and payload["latest_snapshot"]["content_sha256"] == latest["content_sha256"]
    # Delivery health is evaluated now (the fixture snapshot is old), inside the hashed envelope.
    assert payload["delivery_health"]["snapshot_age_status"] == "STALE" and payload["degraded"] is True
    assert payload["body"]["captured_health"]["evaluated_at"] == payload["body"]["cutoff_at"]  # immutable captured health preserved


@pytest.mark.parametrize("path", SECTION_PATHS)
def test_every_section_route_serves_the_same_frozen_snapshot_with_verifiable_hash(api, path, pg_engine):
    with pg_engine.connect() as conn:
        latest = conn.execute(text("SELECT snapshot_id, snapshot_sha256 FROM mi_v_morning_context_latest")).mappings().one()
    payload = api.get(path, headers={"Authorization": "Bearer fixture-token"}).json()
    assert verify_export_hash(payload)
    assert payload["provenance"] == {**payload["provenance"], "kind": "FROZEN_SNAPSHOT", "snapshot_id": latest["snapshot_id"], "source_snapshot_hash": latest["snapshot_sha256"]}
    assert payload["available"] is True and payload["body"] is not None
    if payload["section"]:
        assert payload["section_status"]["status"] in {"OK", "PARTIAL", "STALE"}


def test_live_data_health_route_declares_live_provenance_not_snapshot_hash(api, pg_engine):
    for path in ("/v1/context/data-health/live", "/v1/context/data-health"):
        payload = api.get(path, headers={"Authorization": "Bearer fixture-token"}).json()
        assert verify_export_hash(payload)
        assert payload["provenance"]["kind"] == "LIVE_VIEW" and payload["source_snapshot_hash"] is None and payload["provenance"]["captured_at"]
        assert payload["section"] == "data_health_live" and payload["available"] is True
        sources = {s["freshness_dataset"]: s for s in payload["body"]["sources"]}
        # Health is re-evaluated against the live clock: the 2024 fixture data is stale now, per-dataset cadence shown.
        assert sources["series:DGS10"]["freshness_status"] == "STALE" and sources["series:DGS10"]["dataset_cadence"] == "D"
        assert sources["series:DGS10"]["stored_freshness_status"] == "FRESH"
        assert "expected_next_release" not in sources["series:DGS10"]
        assert payload["latest_snapshot"]["snapshot_id"]  # the snapshot is referenced as context, not as the source
    frozen = api.get("/v1/context/data-health/latest", headers={"Authorization": "Bearer fixture-token"}).json()
    assert frozen["provenance"]["kind"] == "FROZEN_SNAPSHOT" and frozen["body"]["sources"]


def test_credit_route_redacts_every_value_including_history_and_deltas(api, pg_engine):
    r = api.get("/v1/context/credit/latest", headers={"Authorization": "Bearer fixture-token"})
    payload = r.json()
    with pg_engine.connect() as conn:
        real = conn.execute(text("SELECT oas_bps FROM mi_v_credit_latest WHERE series_id='BAMLH0A0HYM2'")).scalar()
    assert payload["restricted_entries"] == 9
    assert "{0:.4f}".format(float(real)) [:6] not in json.dumps(payload["body"])
    for b in payload["body"]["buckets"]:
        assert set(b) & {"oas_bps", "change_1d_bps", "change_1w_bps", "change_1m_bps", "percentile", "zscore", "history"} == set()
        assert b["restriction_reason"]


def test_sectors_route_redacts_internal_only_legacy_values(api):
    payload = api.get("/v1/context/sectors/latest", headers={"Authorization": "Bearer fixture-token"}).json()
    assert verify_export_hash(payload)
    rows = payload["body"]["datasets"]["ETF_RS_VS_SPY"]
    assert rows and all(r["restricted"] is True and "metrics" not in r for r in rows)
    assert [r["sector_key"] for r in rows] == sorted(r["sector_key"] for r in rows)
    assert all("Internal-only" in r["restriction_reason"] for r in rows)
    market = api.get("/v1/context/market/latest", headers={"Authorization": "Bearer fixture-token"}).json()
    leaders = market["body"]["sector_leadership_rs_vs_spy"]
    assert leaders and all(l["restricted"] and "rs_chg_1m" not in l for l in leaders)
    assert market["body"]["us_10y"]["yield_pct"] is not None  # official FRED data exported with attribution scope


def test_missing_snapshot_response_is_explicit_and_hashed(consumer, pg_engine):
    from fastapi.testclient import TestClient

    import ai_context_api

    client = TestClient(ai_context_api.app, raise_server_exceptions=False)
    with pg_engine.begin() as conn:
        conn.execute(text("UPDATE mi_morning_context_snapshots SET publication_state = 'WITHDRAWN_TEST' WHERE publication_state = 'PUBLISHED'"))
    try:
        for path in ("/v1/context/morning/latest", "/v1/context/credit/latest"):
            r = client.get(path, headers={"Authorization": "Bearer fixture-token"})
            payload = r.json()
            assert r.status_code == 200 and payload["available"] is False and payload["body"] is None
            assert payload["provenance"]["kind"] == "UNPUBLISHED" and payload["source_snapshot_hash"] is None
            assert payload["unavailable_reason"] == "no published morning_context snapshot" and payload["degraded"] is True
            assert payload["delivery_health"]["snapshot_age_status"] == "NONE"
            assert verify_export_hash(payload)
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text("UPDATE mi_morning_context_snapshots SET publication_state = 'PUBLISHED' WHERE publication_state = 'WITHDRAWN_TEST'"))


def test_strategies_route_preserves_gate_status_not_success_claims(api):
    r = api.get("/v1/context/strategies/latest", headers={"Authorization": "Bearer fixture-token"})
    fixture = next(s for s in r.json()["body"]["strategies"] if s["strategy_id"] == "FIXTURE_STRATEGY")
    assert fixture["research_status"] == "COMPLETE"
    assert fixture["economic_gate"] == "NOT_DEFINED" and fixture["promotion_gate"] == "LOCKED" and fixture["delivery_status"] == "LAST_KNOWN_GOOD"


def test_api_unavailable_states_are_explicit(consumer, monkeypatch):
    from fastapi.testclient import TestClient

    import ai_context_api

    readonly_db.set_engine_for_tests(None)
    readonly_db._cached_engine = None
    client = TestClient(ai_context_api.app, raise_server_exceptions=False)
    r = client.get("/v1/context/macro/latest", headers={"Authorization": "Bearer fixture-token"})
    assert r.status_code == 503 and r.json()["detail"]["status"] == "CONFIGURATION_REQUIRED"
    assert "postgres" not in r.text.lower()


# ---- Streamlit pages ---------------------------------------------------------------------------------------

def _run_page(path: Path):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(path), default_timeout=60)
    at.run()
    return at


def _texts(at) -> str:
    parts = []
    for kind in ("title", "caption", "info", "warning", "error", "markdown", "subheader"):
        for el in getattr(at, kind):
            parts.append(str(el.value))
    return "\n".join(parts)


@pytest.mark.parametrize("page", MI_PAGES, ids=[p.stem for p in MI_PAGES])
def test_pages_render_populated_state_db_only(consumer, page):
    at = _run_page(page)
    assert not at.exception, [e.value for e in at.exception]
    text_out = _texts(at)
    assert at.title[0].value
    assert len(at.dataframe) >= 1, "each page shows at least one table when data exists"
    if page.stem not in {"14_Sector_Rotation_V2", "15_Data_Health", "17_PIT_Sector_Internals", "18_Order_Flow"}:
        assert "not endorsed or certified by the Federal Reserve Bank of St. Louis" in text_out
    if page.stem == "10_Market_Pulse":
        assert "not labeled as overnight" in text_out.lower() or "quotes" in text_out.lower()
        assert "What changed" in text_out
        assert "Day to day" in text_out
    if page.stem == "16_Morning_Context":
        assert at.expander
    if page.stem == "14_Sector_Rotation_V2":
        assert "CURRENT_UNIVERSE_CONTEXT_ONLY" in text_out and "never as zero" in text_out
    if page.stem == "17_PIT_Sector_Internals":
        assert "research_eligible = FALSE" in text_out and "SYNTHETIC_TEST_ONLY" in text_out and "2020-01-01" in text_out
    if page.stem == "18_Order_Flow":
        assert "Corporate Bond Trading Activity" in text_out
        assert "not a live order book" in text_out.lower()


def test_dashboard_entry_point_overview_links_use_registry(consumer):
    from market_intelligence.page_registry import PAGE_BY_ROUTE

    at = _run_page(ROOT / "dashboard.py")
    assert not at.exception, [e.value for e in at.exception]
    text_out = _texts(at)
    assert "Overview" in text_out or (at.title and "Overview" in str(at.title[0].value))
    assert "Day to day" in text_out
    assert "What changed" in text_out
    journeys = (
        ("sectors", "Sectors"),
        ("rates", "Rates"),
        ("credit", "Credit"),
        ("macro", "Macro"),
        ("order_flow", "Order Flow"),
    )
    for route_id, title in journeys:
        spec = PAGE_BY_ROUTE[route_id]
        at.switch_page(spec.file_path or spec.legacy_path)
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        landed = _texts(at)
        titles = [str(item.value) for item in at.title]
        assert title in titles or title in landed


@pytest.mark.parametrize("page", MI_PAGES, ids=[p.stem for p in MI_PAGES])
def test_pages_render_unconfigured_state_without_writer_fallback(page, monkeypatch):
    readonly_db.set_engine_for_tests(None)
    readonly_db._cached_engine = None
    monkeypatch.delenv("DATABASE_READONLY_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://writer:secret@127.0.0.1/never")
    from market_intelligence.ui import clear_read_cache

    clear_read_cache()
    at = _run_page(page)
    assert not at.exception
    text_out = _texts(at)
    assert "CONFIGURATION_REQUIRED" in text_out and "DATABASE_READONLY_URL" in text_out
    assert "secret" not in text_out
    assert len(at.dataframe) == 0


@pytest.mark.parametrize("page", MI_PAGES, ids=[p.stem for p in MI_PAGES])
def test_pages_render_empty_state(page, ro_engine, pg_engine, monkeypatch):
    """Empty views -> informative empty state, never fabricated numbers (views return no rows)."""
    from market_intelligence.ui import clear_read_cache

    readonly_db.set_engine_for_tests(ro_engine)
    clear_read_cache()
    monkeypatch.setattr("market_intelligence.read_models._rows", lambda conn, sql, params=None: [])
    at = _run_page(page)
    readonly_db.set_engine_for_tests(None)
    clear_read_cache()
    assert not at.exception, [e.value for e in at.exception]
    text_out = _texts(at)
    assert any(word in text_out for word in ("No ", "not been published", "No sources"))
    assert len(at.metric) == 0


def test_pages_import_no_provider_modules():
    import importlib
    import sys

    for page in MI_PAGES:
        src = page.read_text(encoding="utf-8")
        assert "from market_intelligence.pages_ui import" in src
        for banned in ("data_loader", "precomputed_loader", "nightly_refresh", "requests", "db.connection", "dispersion_engine", "rotation_engine", "sync_quantconnect", "writer_db", "fred_client"):
            assert banned not in src
    import subprocess

    # Fresh interpreter: importing the page renderers must not pull provider/writer modules as side effects.
    code = "import sys, market_intelligence.pages_ui; print('\\n'.join(sorted(sys.modules)))"
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, check=True, env={**os.environ, "DATABASE_READONLY_URL": ""}).stdout.split()
    loaded = set(out)
    for banned in ("data_loader", "precomputed_loader", "nightly_refresh", "db.connection", "market_intelligence.writer_db", "market_intelligence.fred_client", "market_intelligence.finra_client", "market_intelligence.legacy_bridge", "market_intelligence.store", "market_intelligence.ibkr_store", "ibkr_collector", "ibkr_ingest", "jobs.sync_quantconnect", "requests"):
        assert banned not in loaded, banned
    assert "market_intelligence.readonly_db" in loaded
    _ = importlib


# ---- refresh CLI ---------------------------------------------------------------------------------------------

def test_refresh_dry_run_makes_no_calls_and_no_writes(pg_engine, populated, capsys):
    from jobs.market_intelligence_refresh import run

    with pg_engine.connect() as conn:
        before = conn.execute(text("SELECT COUNT(*) FROM mi_ingestion_runs")).scalar()
    code = run(["--all-configured", "--dry-run", "--json"], engine=pg_engine, fred_client_factory=lambda: _boom(), env={"FRED_API_KEY": "not-used", "DATABASE_URL": "x"})
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["status"] == "DRY_RUN_VALIDATED"
    assert [s["step"] for s in out["plan"]["steps"]] == ["fred", "finra", "legacy_sector", "build_analytics", "build_morning"]
    assert "not-used" not in json.dumps(out)
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM mi_ingestion_runs")).scalar() == before


def test_refresh_partial_failure_keeps_successful_work_and_exits_nonzero(pg_engine, populated, tmp_path, capsys):
    from jobs.market_intelligence_refresh import EXIT_PARTIAL, run

    failing = fake_fred_client(AS_OF, failures={"DGS10", "CPIAUCSL"})
    root = write_full_precomputed_root(tmp_path / "pre", AS_OF)
    code = run(
        ["--fred", "--legacy-sector", "--series", "DGS10", "DGS2", "CPIAUCSL", "--json", "--as-of", AS_OF.isoformat()],
        engine=pg_engine,
        fred_client_factory=lambda: failing,
        env={"FRED_API_KEY": "k", "MARKET_INTELLIGENCE_PRECOMPUTED_ROOT": str(root), "DATABASE_URL": "x"},
    )
    out = json.loads(capsys.readouterr().out)
    assert code == EXIT_PARTIAL and out["status"] == "PARTIAL" and out["failures"] == 1
    fred = out["results"]["fred"]
    assert fred["series_failed"] == 2 and fred["series_succeeded"] == 1
    assert out["results"]["legacy_sector"]["bundles_failed"] == 0
    failed = next(r for r in fred["results"] if r["series_id"] == "DGS10")
    assert "HTTP 500" in failed["error"] and "api_key" not in failed["error"]
    with pg_engine.connect() as conn:
        # Prior valid DGS10 observations survive the failed retrieval; transport failed, data retained.
        assert conn.execute(text("SELECT COUNT(*) FROM mi_macro_observations WHERE series_id='DGS10' AND is_current")).scalar() > 700
        fresh = conn.execute(text("SELECT transport_status, latest_observation_date FROM mi_data_freshness WHERE dataset='series:DGS10'")).one()
        assert fresh.transport_status == "FAILED" and fresh.latest_observation_date == AS_OF
        parent = conn.execute(text("SELECT status FROM mi_ingestion_runs WHERE run_id=:r"), {"r": out["parent_run_id"]}).scalar()
    assert parent == "PARTIAL"


def test_refresh_lock_contention_exit_code(pg_engine, populated, capsys):
    from jobs.market_intelligence_refresh import run

    with writer_lock(pg_engine):
        code = run(["--build-analytics", "--json"], engine=pg_engine, env={"DATABASE_URL": "x"})
    out = json.loads(capsys.readouterr().out)
    assert code == EXIT_LOCK_CONTENTION and out["status"] == "LOCK_CONTENTION"


def test_refresh_unconfigured_source_is_explicit_skip_not_crash(pg_engine, populated, capsys):
    from jobs.market_intelligence_refresh import run

    code = run(["--all-configured", "--json", "--as-of", AS_OF.isoformat()], engine=pg_engine, env={"MARKET_INTELLIGENCE_PRECOMPUTED_ROOT": "/nonexistent", "DATABASE_URL": "x"})
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["results"]["fred"]["status"] == "SKIPPED" and out["results"]["legacy_sector"]["status"] == "SKIPPED"
    assert out["results"]["finra"]["status"] == "SKIPPED"
    with pg_engine.connect() as conn:
        access = conn.execute(text("SELECT access_status, enabled FROM mi_source_registry WHERE source_id='FRED'")).one()
    assert access.access_status == "CONFIGURATION_REQUIRED" and access.enabled is False
    # No-configured-provider success does not imply freshness: existing freshness rows untouched.
    assert "FRESH" not in json.dumps(out["results"]["fred"])
