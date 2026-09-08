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
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from market_intelligence import readonly_db
from market_intelligence.analytics import build_analytics
from market_intelligence.export_policy import verify_export_hash
from market_intelligence.ingest_fred import ingest_fred_catalog
from market_intelligence.legacy_bridge import ingest_precomputed_root
from market_intelligence.locking import EXIT_LOCK_CONTENTION, writer_lock
from market_intelligence.morning_context import build_and_publish
from market_intelligence.nulls import canonical_sha256, strict_dumps, strict_loads
from market_intelligence.store import finish_run, start_run, upsert_source_registry
from tests.mi_fixtures import SYNTHETIC_MARKER, fake_fred_client, write_full_precomputed_root

ROOT = Path(__file__).resolve().parent.parent
PAGES = ROOT / "pages"
AS_OF = date(2024, 12, 31)
GENERATED_AT = datetime(2025, 1, 2, 11, 30, tzinfo=timezone.utc)
MI_PAGES = sorted(p for p in PAGES.glob("1[0-6]_*.py"))


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


@pytest.fixture(scope="module")
def populated(pg_engine, tmp_path_factory):
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
    client = fake_fred_client(AS_OF)
    fred = ingest_fred_catalog(pg_engine, client, mode="full", today=AS_OF, parent_run_id=parent)
    assert not fred.failed, [r.as_dict() for r in fred.failed]
    root = write_full_precomputed_root(tmp_path_factory.mktemp("precomputed"), AS_OF)
    legacy = ingest_precomputed_root(pg_engine, root, parent_run_id=parent, today=AS_OF)
    assert not legacy.failed_bundles, legacy.as_dict()
    with pg_engine.begin() as conn:
        rid = start_run(conn, source_id="ANALYTICS", dataset="metric_snapshots", parent_run_id=parent)
        analytics = build_analytics(conn, as_of=AS_OF, run_id=rid)
        finish_run(conn, rid, status="SUCCEEDED", details=analytics.as_dict())
    with pg_engine.begin() as conn:
        finish_run(conn, parent, status="SUCCEEDED")
    morning = build_and_publish(pg_engine, parent_run_id=parent, as_of=AS_OF, generated_at=GENERATED_AT)
    return {"fred": fred, "legacy": legacy, "analytics": analytics, "morning": morning, "root": root, "client": client, "parent": parent}


@pytest.fixture(scope="module")
def ro_engine(pg_engine, pg_database, pg_admin_url):
    """Provision the real read-only role from db/roles/*.sql (test-unique name) and return its engine."""
    role = "mi_readonly_test_{0}".format(uuid.uuid4().hex[:8])
    password = "ro_{0}".format(uuid.uuid4().hex)
    dbname = make_url(pg_database).database
    sql = (ROOT / "db" / "roles" / "market_intelligence_readonly.sql").read_text(encoding="utf-8")
    sql = sql.replace("mi_readonly", role).replace(":'ro_password'", "'{0}'".format(password)).replace(':"DBNAME"', '"{0}"'.format(dbname))
    statements = [s.strip() for s in "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--")).split(";") if s.strip()]
    with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for statement in statements:
            conn.execute(text(statement))
    url = make_url(pg_database).set(username=role, password=password)
    engine = create_engine(url, future=True, pool_pre_ping=True, connect_args={"options": "-c default_transaction_read_only=on -c statement_timeout={0}".format(readonly_db.DEFAULT_STATEMENT_TIMEOUT_MS)})
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

def test_morning_snapshot_is_hashed_immutable_and_deterministic(pg_engine, populated):
    morning = populated["morning"]
    assert morning.published_new and morning.completeness in {"COMPLETE", "PARTIAL"}
    with pg_engine.connect() as conn:
        row = conn.execute(text("SELECT snapshot_json, snapshot_sha256, generated_at, cutoff_at FROM mi_v_morning_context_latest")).mappings().one()
    body = row["snapshot_json"]
    assert body["artifact_sha256"] == row["snapshot_sha256"] == canonical_sha256(body)
    assert body["schema_version"] == "morning_context_v1"
    assert set(body["sections"]) >= {"data_health", "market", "macro", "rates", "credit", "sectors", "strategy_monitor_summary"}
    assert body["sections"]["market"]["data"]["overnight_quotes"]["status"] != "OK"
    strategies = body["sections"]["strategy_monitor_summary"]["data"]["strategies"]
    fixture = next(s for s in strategies if s["strategy_id"] == "FIXTURE_STRATEGY")
    assert fixture["research_status"] == "COMPLETE" and fixture["economic_gate"] == "NOT_DEFINED" and fixture["promotion_gate"] == "LOCKED"
    text_json = strict_dumps(body)
    assert "NaN" not in text_json and strict_loads(text_json) == body
    # Replay with identical inputs and explicit generation parameters -> identical hash, no new row.
    replay = build_and_publish(pg_engine, parent_run_id=populated["parent"], as_of=AS_OF, generated_at=GENERATED_AT)
    assert replay.snapshot_sha256 == morning.snapshot_sha256 and replay.published_new is False
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM mi_morning_context_snapshots")).scalar() == 1
        # Immutability: published rows cannot be rewritten by a second publish with a different body.
        different = build_and_publish(pg_engine, parent_run_id=populated["parent"], as_of=AS_OF, generated_at=GENERATED_AT.replace(hour=12))
        assert different.snapshot_id != morning.snapshot_id
        assert conn.execute(text("SELECT COUNT(*) FROM mi_morning_context_snapshots")).scalar() == 2
        first = conn.execute(text("SELECT snapshot_sha256 FROM mi_morning_context_snapshots WHERE snapshot_id=:s"), {"s": morning.snapshot_id}).scalar()
    assert first == morning.snapshot_sha256


# ---- read-only role: real denial -----------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO mi_source_registry (source_id, provider, dataset) VALUES ('X','x','x')",
        "UPDATE mi_macro_observations SET value = 0",
        "DELETE FROM mi_morning_context_snapshots",
        "CREATE TABLE mi_should_fail (id INT)",
        "SELECT COUNT(*) FROM mi_macro_observations",
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


ROUTES = ["/v1/ready", "/v1/context/morning/latest", "/v1/context/macro/latest", "/v1/context/rates/latest", "/v1/context/credit/latest", "/v1/context/sectors/latest", "/v1/context/strategies/latest", "/v1/context/data-health"]


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


def test_morning_route_is_export_filtered_and_hash_consistent(api, populated, pg_engine):
    with pg_engine.connect() as conn:
        latest = conn.execute(text("SELECT snapshot_id, snapshot_sha256, completeness FROM mi_v_morning_context_latest")).mappings().one()
    r = api.get("/v1/context/morning/latest", headers={"Authorization": "Bearer fixture-token"})
    payload = r.json()
    assert payload["available"] is True
    assert payload["source_snapshot_hash"] == latest["snapshot_sha256"]
    assert payload["export_filtered"] is True and payload["restricted_entries"] >= 9
    assert payload["export_sha256"] != payload["source_snapshot_hash"]
    assert verify_export_hash({k: v for k, v in payload.items() if k in {"export_schema_version", "source_snapshot_hash", "export_filtered", "restricted_entries", "body", "export_sha256"}})
    buckets = payload["body"]["sections"]["credit"]["data"]["buckets"]
    assert all(b["restricted"] and "oas_bps" not in b and "percentile" not in b for b in buckets)
    assert buckets[0]["series_id"] == "BAMLC0A0CM"  # identity kept, catalog order (not value-ranked)
    # Unrestricted official macro data still exported.
    curve = payload["body"]["sections"]["rates"]["data"]["curve"]
    assert any(c["yield_pct"] is not None for c in curve)
    assert payload["latest_snapshot"]["snapshot_id"] == latest["snapshot_id"]
    assert payload["degraded"] == (latest["completeness"] != "COMPLETE")


def test_credit_route_redacts_every_value_including_history_and_deltas(api, pg_engine):
    r = api.get("/v1/context/credit/latest", headers={"Authorization": "Bearer fixture-token"})
    payload = r.json()
    with pg_engine.connect() as conn:
        real = conn.execute(text("SELECT oas_bps FROM mi_v_credit_latest WHERE series_id='BAMLH0A0HYM2'")).scalar()
    assert payload["restricted_entries"] == 9
    assert "{0:.4f}".format(float(real)) [:6] not in json.dumps(payload["body"])
    for b in payload["body"]["data"]["buckets"]:
        assert set(b) & {"oas_bps", "change_1d_bps", "change_1w_bps", "change_1m_bps", "percentile", "zscore", "history"} == set()
        assert b["restriction_reason"]


def test_strategies_route_preserves_gate_status_not_success_claims(api):
    r = api.get("/v1/context/strategies/latest", headers={"Authorization": "Bearer fixture-token"})
    fixture = next(s for s in r.json()["body"]["data"]["strategies"] if s["strategy_id"] == "FIXTURE_STRATEGY")
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
    if page.stem not in {"14_Sector_Rotation_V2", "15_Data_Health"}:
        assert "not endorsed or certified by the Federal Reserve Bank of St. Louis" in text_out
    if page.stem == "10_Market_Pulse":
        assert "Overnight quotes unavailable" in text_out
    if page.stem == "16_Morning_Context":
        assert "SHA-256" in text_out
    if page.stem == "14_Sector_Rotation_V2":
        assert "CURRENT_UNIVERSE_CONTEXT_ONLY" in text_out and "never as zero" in text_out


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
    for banned in ("data_loader", "precomputed_loader", "nightly_refresh", "db.connection", "market_intelligence.writer_db", "market_intelligence.fred_client", "market_intelligence.legacy_bridge", "market_intelligence.store", "jobs.sync_quantconnect", "requests"):
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
    assert [s["step"] for s in out["plan"]["steps"]] == ["fred", "legacy_sector", "build_analytics", "build_morning"]
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
    with pg_engine.connect() as conn:
        access = conn.execute(text("SELECT access_status, enabled FROM mi_source_registry WHERE source_id='FRED'")).one()
    assert access.access_status == "CONFIGURATION_REQUIRED" and access.enabled is False
    # No-configured-provider success does not imply freshness: existing freshness rows untouched.
    assert "FRESH" not in json.dumps(out["results"]["fred"])
