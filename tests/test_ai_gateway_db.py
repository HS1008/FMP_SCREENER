"""Read-only AI gateway on a disposable PostgreSQL 16 with the post-FMP architecture.

The fixture ingests FRED (synthetic), the official Treasury XML sample (same-date curve
2026-09-09 plus partial newer tenors on 2026-09-10), fixture equity EOD bars (ret_1d / rs_1d
on aligned sessions, curated semiconductor basket), and an adversarial set of research rows
(holdout runs, NULL windows, 2025+ OOS, model binaries with innocuous names, unknown lineage).

Everything the gateway returns is read through the real ``db/roles/market_intelligence_readonly.sql``
role applied via psql, exactly like production.
"""

from __future__ import annotations

import http.client
import json
import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from market_intelligence import readonly_db
from market_intelligence.analytics import build_analytics
from market_intelligence.equity_eod import EquityBar, FixtureAdapter, ingest_equity_eod
from market_intelligence.export_policy import verify_export_hash
from market_intelligence.ingest_fred import ingest_fred_catalog
from market_intelligence.ingest_treasury import ingest_treasury
from market_intelligence.store import finish_run, start_run, upsert_source_registry
from market_intelligence.treasury_xml import parse_feed_xml
from tests.mi_fixtures import fake_fred_client
from tests.test_mi_pipeline import provision_role_with_psql
from tests.test_treasury_xml import SAMPLE

AS_OF = date(2026, 9, 9)  # FRED synthetic end == Treasury complete-curve date (tie -> TREASURY)
EQUITY_AS_OF = date(2026, 9, 10)
TOKEN = "gateway-fixture-token"
STRATEGY = "GW_STRAT"
LOCAL = ("127.0.0.1", 40000)
REMOTE = ("203.0.113.9", 40000)


def _boom(*_a, **_k):
    raise AssertionError("network access is not allowed in gateway tests")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(http.client.HTTPConnection, "connect", _boom)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", _boom)
    import requests

    monkeypatch.setattr(requests.Session, "request", _boom)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _boom)
    yield


class _TreasuryClient:
    def fetch_recent(self, *, today, lookback_months=2):
        return parse_feed_xml(SAMPLE, curve="nominal").points


def _equity_bars() -> list[EquityBar]:
    bars: list[EquityBar] = []
    sessions = [
        (date(2026, 9, 3), 100.0, 50.0),
        (date(2026, 9, 4), 101.0, 50.5),
        # Labor Day 2026-09-07 has no bar on purpose (aligned previous session = 09-04).
        (date(2026, 9, 8), 100.0, 51.0),
        (date(2026, 9, 9), 101.0, 51.0),
        (date(2026, 9, 10), 102.01, 52.02),
    ]
    for day, spy, xlk in sessions:
        bars.append(EquityBar("SPY", day, spy))
        bars.append(EquityBar("XLK", day, xlk))
        for etf in ("XLC", "XLY", "XLE", "XLF", "XLV", "XLI", "XLB", "XLU", "XLRE"):
            bars.append(EquityBar(etf, day, 20.0 + day.day / 100.0))
        # XLP is deliberately flat: a zero return is valid and must stay distinct from null.
        bars.append(EquityBar("XLP", day, 20.0))
        for name, base in (("NVDA", 100.0), ("AMD", 10.0), ("SMH", 200.0), ("XSD", 150.0), ("KRE", 50.0)):
            bars.append(EquityBar(name, day, base + day.day / 10.0))
    # XLU misses the last session on purpose -> ret_1d must be null, not zero, and not stale-filled.
    return [b for b in bars if not (b.instrument_id == "XLU" and b.bar_date == date(2026, 9, 10))]


RESEARCH_SQL = """
DELETE FROM research_artifacts WHERE research_run_id LIKE 'GW_RUN_%';
DELETE FROM research_oos_windows WHERE research_run_id LIKE 'GW_RUN_%';
DELETE FROM backtests WHERE strategy_id = 'GW_STRAT';
DELETE FROM research_runs WHERE strategy_id = 'GW_STRAT';

INSERT INTO research_runs (research_run_id, strategy_id, run_status, git_commit, research_kind, economic_gate, promotion_gate, holdout_status, holdout_exposure_status, holdout_accessed, holdout_access_count, expected_experiment_count, synced_experiment_count, completed_count, failed_count, skipped_count)
VALUES
  ('GW_RUN_OK',       'GW_STRAT', 'COMPLETE', 'aaaa1111', 'ML', 'NOT_DEFINED', 'LOCKED', 'LOCKED',   'PRISTINE',      FALSE, 0, 2, 2, 2, 0, 0),
  ('GW_RUN_HOLDOUT',  'GW_STRAT', 'COMPLETE', 'bbbb2222', 'ML', 'NOT_DEFINED', 'LOCKED', 'ACCESSED', 'ACCESSED_ONCE', TRUE,  1, 1, 1, 1, 0, 0),
  ('GW_RUN_MIXED',    'GW_STRAT', 'COMPLETE', 'cccc3333', 'ML', 'NOT_DEFINED', 'LOCKED', 'LOCKED',   'PRISTINE',      FALSE, 0, 2, 2, 2, 0, 0),
  ('GW_RUN_2025',     'GW_STRAT', 'COMPLETE', 'dddd4444', 'ML', 'NOT_DEFINED', 'LOCKED', 'LOCKED',   'PRISTINE',      FALSE, 0, 1, 1, 1, 0, 0),
  ('GW_RUN_UNKNOWN',  'GW_STRAT', 'COMPLETE', 'eeee5555', 'ML', 'NOT_DEFINED', 'LOCKED', 'LOCKED',   'PRISTINE',      FALSE, 0, 0, 0, 0, 0, 0),
  ('GW_RUN_NULLFLAG', 'GW_STRAT', 'COMPLETE', 'ffff6666', 'ML', 'NOT_DEFINED', 'LOCKED', 'LOCKED',   'PRISTINE',      FALSE, 0, 1, 1, 1, 0, 0),
  ('GW_RUN_NULLSTAT', 'GW_STRAT', 'COMPLETE', 'gggg7777', 'ML', 'NOT_DEFINED', 'LOCKED', NULL,      'PRISTINE',      FALSE, 0, 1, 1, 1, 0, 0),
  ('GW_RUN_NULLACC',  'GW_STRAT', 'COMPLETE', 'hhhh8888', 'ML', 'NOT_DEFINED', 'LOCKED', 'LOCKED',   'PRISTINE',      NULL,  0, 1, 1, 1, 0, 0),
  ('GW_RUN_NULLEXP',  'GW_STRAT', 'COMPLETE', 'iiii9999', 'ML', 'NOT_DEFINED', 'LOCKED', 'LOCKED',   NULL,            FALSE, 0, 1, 1, 1, 0, 0);

INSERT INTO backtests (backtest_id, strategy_id, name, status, research_run_id, research_experiment_id, research_test_type, research_phase, research_window_id, research_is_holdout, test_start, test_end, sharpe_ratio)
VALUES
  ('gw-ok-2016',    'GW_STRAT', 'S2__OOS_2016',        'Completed', 'GW_RUN_OK',       'OOS_2016',               'ML_OOS_TEST',      'OOS',     '2016', FALSE, DATE '2016-01-01', DATE '2016-12-31', 0.9),
  ('gw-ok-2017',    'GW_STRAT', 'S2__OOS_2017',        'Completed', 'GW_RUN_OK',       'OOS_2017',               'ML_OOS_TEST',      'OOS',     '2017', FALSE, DATE '2017-01-01', DATE '2017-12-31', 0.7),
  ('gw-hold-2025',  'GW_STRAT', 'S2__FINAL_HOLDOUT',   'Completed', 'GW_RUN_HOLDOUT',  'ML_FINAL_HOLDOUT_2025',  'ML_FINAL_HOLDOUT', 'HOLDOUT', '2025', TRUE,  DATE '2025-01-01', DATE '2025-12-31', 9.9),
  ('gw-mixed-2018', 'GW_STRAT', 'S2__OOS_2018',        'Completed', 'GW_RUN_MIXED',    'OOS_2018',               'ML_OOS_TEST',      'OOS',     '2018', FALSE, DATE '2018-01-01', DATE '2018-12-31', 0.5),
  ('gw-mixed-null', 'GW_STRAT', 'S2__diagnostics',     'Completed', 'GW_RUN_MIXED',    'DIAG_NULL_DATES',        'ML_OOS_TEST',      'HOLDOUT', NULL,   FALSE, NULL,              NULL,              8.8),
  ('gw-2025-oos',   'GW_STRAT', 'S2__OOS_2025Q1',      'Completed', 'GW_RUN_2025',     'OOS_2025Q1',             'ML_OOS_TEST',      'OOS',     '2025', FALSE, DATE '2025-01-01', DATE '2025-03-31', 7.7),
  ('gw-nullflag',   'GW_STRAT', 'S2__OOS_2020',        'Completed', 'GW_RUN_NULLFLAG', 'OOS_2020',               'ML_OOS_TEST',      'OOS',     '2020', NULL,  DATE '2020-01-01', DATE '2020-12-31', 6.6),
  ('gw-nullstat',   'GW_STRAT', 'S2__OOS_2015',        'Completed', 'GW_RUN_NULLSTAT', 'OOS_2015',               'ML_OOS_TEST',      'OOS',     '2015', FALSE, DATE '2015-01-01', DATE '2015-12-31', 5.5),
  ('gw-nullacc',    'GW_STRAT', 'S2__OOS_2014',        'Completed', 'GW_RUN_NULLACC',  'OOS_2014',               'ML_OOS_TEST',      'OOS',     '2014', FALSE, DATE '2014-01-01', DATE '2014-12-31', 4.4),
  ('gw-nullexp',    'GW_STRAT', 'S2__OOS_2013',        'Completed', 'GW_RUN_NULLEXP',  'OOS_2013',               'ML_OOS_TEST',      'OOS',     '2013', FALSE, DATE '2013-01-01', DATE '2013-12-31', 3.3);

INSERT INTO research_oos_windows (research_run_id, outer_window_id, oos_start, oos_end, metrics_json)
VALUES
  ('GW_RUN_OK',      '2016',  DATE '2016-01-01', DATE '2016-12-31', '{"rank_ic": 0.10}'::jsonb),
  ('GW_RUN_OK',      '2025H', DATE '2025-01-01', DATE '2025-12-31', '{"rank_ic": 0.99}'::jsonb),
  ('GW_RUN_OK',      'X',     DATE '2018-01-01', NULL,              '{"rank_ic": 0.55}'::jsonb),
  ('GW_RUN_HOLDOUT', '2016',  DATE '2016-01-01', DATE '2016-12-31', '{"rank_ic": 0.42}'::jsonb);

INSERT INTO research_artifacts (artifact_key, research_run_id, research_experiment_id, artifact_type, sha256, payload_json, created_at, transport, logical_path)
VALUES
  ('GW_RUN_OK/run_summary.json',            'GW_RUN_OK',      NULL,                    'run_summary',      repeat('a', 64), '{"secret_metric": 1}'::jsonb, NOW(), 'results_branch', 'GW_RUN_OK/run_summary.json'),
  ('GW_RUN_OK/oos_diagnostics_2016.json',   'GW_RUN_OK',      'OOS_2016',              'oos_diagnostics',  repeat('b', 64), '{"rank_ic": 0.1}'::jsonb,     NOW(), 'results_branch', 'GW_RUN_OK/oos_diagnostics_2016.json'),
  ('GW_RUN_OK/diagnostics.json',            'GW_RUN_OK',      'OOS_2016',              'diagnostics',      repeat('c', 64), '{}'::jsonb,                    NOW(), 'object_store',   'GW_RUN_OK/model.pkl'),
  ('GW_RUN_OK/model_metadata.json',         'GW_RUN_OK',      NULL,                    'model_metadata',   repeat('d', 64), '{"object_store_key": "k"}'::jsonb, NOW(), 'results_branch', 'GW_RUN_OK/model_metadata.json'),
  ('GW_RUN_OK/weights.pkl',                 'GW_RUN_OK',      'OOS_2017',              'training_summary', repeat('e', 64), '{}'::jsonb,                    NOW(), 'object_store',   'GW_RUN_OK/summary.json'),
  ('GW_RUN_OK/final_train_prep.json',       'GW_RUN_OK',      'OOS_2017',              'training_summary', repeat('f', 64), '{}'::jsonb,                    NOW(), 'results_branch', 'GW_RUN_OK/final_train_prep.json'),
  ('GW_RUN_HOLDOUT/diagnostics.json',       'GW_RUN_HOLDOUT', 'ML_FINAL_HOLDOUT_2025', 'diagnostics',      repeat('1', 64), '{"sharpe": 9.9}'::jsonb,       NOW(), 'results_branch', 'GW_RUN_HOLDOUT/diagnostics.json'),
  ('GW_RUN_HOLDOUT/run_summary.json',       'GW_RUN_HOLDOUT', NULL,                    'run_summary',      repeat('2', 64), '{}'::jsonb,                    NOW(), 'results_branch', 'GW_RUN_HOLDOUT/run_summary.json'),
  ('GW_RUN_MIXED/run_summary.json',         'GW_RUN_MIXED',   NULL,                    'run_summary',      repeat('3', 64), '{}'::jsonb,                    NOW(), 'results_branch', 'GW_RUN_MIXED/run_summary.json'),
  ('GW_RUN_MIXED/oos_diagnostics_2018.json','GW_RUN_MIXED',   'OOS_2018',              'oos_diagnostics',  repeat('4', 64), '{}'::jsonb,                    NOW(), 'results_branch', 'GW_RUN_MIXED/oos_diagnostics_2018.json'),
  ('GW_RUN_MIXED/diag_null.json',           'GW_RUN_MIXED',   'DIAG_NULL_DATES',       'diagnostics',      repeat('5', 64), '{}'::jsonb,                    NOW(), 'results_branch', 'GW_RUN_MIXED/diag_null.json'),
  ('GW_RUN_2025/oos_diagnostics.json',      'GW_RUN_2025',    'OOS_2025Q1',            'oos_diagnostics',  repeat('6', 64), '{}'::jsonb,                    NOW(), 'results_branch', 'GW_RUN_2025/oos_diagnostics.json'),
  ('GW_RUN_UNKNOWN/run_summary.json',       'GW_RUN_UNKNOWN', NULL,                    'run_summary',      repeat('7', 64), '{}'::jsonb,                    NOW(), 'results_branch', 'GW_RUN_UNKNOWN/run_summary.json'),
  ('GW_RUN_NULLFLAG/oos_diagnostics.json',  'GW_RUN_NULLFLAG','OOS_2020',              'oos_diagnostics',  repeat('8', 64), '{}'::jsonb,                    NOW(), 'results_branch', 'GW_RUN_NULLFLAG/oos_diagnostics.json');
"""

# Exactly these artifacts may escape: non-holdout, pre-2025, non-model, bound to proven lineage.
EXPECTED_VISIBLE_ARTIFACT_KEYS = {
    ("GW_RUN_OK", None, "run_summary"),
    ("GW_RUN_OK", "OOS_2016", "oos_diagnostics"),
    ("GW_RUN_OK", "OOS_2017", "training_summary"),  # final_train_prep.json (the weights.pkl one is hidden)
    ("GW_RUN_MIXED", "OOS_2018", "oos_diagnostics"),
}


@pytest.fixture(scope="module")
def gateway_db(pg_engine):
    from tests.conftest import MI_TABLES_TRUNCATE

    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE {0} CASCADE".format(", ".join(MI_TABLES_TRUNCATE))))
        for statement in [s for s in RESEARCH_SQL.split(";\n") if s.strip()]:
            conn.execute(text(statement))
        upsert_source_registry(conn, enabled={"FRED": True, "FMP_LEGACY": False}, access={"FRED": "CONFIGURED", "FMP_LEGACY": "RETIRED_OPTIONAL"})
        parent = start_run(conn, source_id="ORCHESTRATOR", dataset="gateway_fixture")
    fred = ingest_fred_catalog(pg_engine, fake_fred_client(AS_OF), mode="full", today=EQUITY_AS_OF, parent_run_id=parent)
    assert not fred.failed, [r.as_dict() for r in fred.failed]
    treasury = ingest_treasury(pg_engine, _TreasuryClient(), today=EQUITY_AS_OF, lookback_months=1, parent_run_id=parent)
    assert treasury.failed is False
    equity = ingest_equity_eod(pg_engine, FixtureAdapter(_equity_bars()), today=EQUITY_AS_OF, lookback_days=30, parent_run_id=parent)
    assert equity.failed is False and equity.latest_observation == EQUITY_AS_OF
    with pg_engine.begin() as conn:
        rid = start_run(conn, source_id="ANALYTICS", dataset="metric_snapshots", parent_run_id=parent)
        report = build_analytics(conn, as_of=AS_OF, run_id=rid)
        finish_run(conn, rid, status="SUCCEEDED", details=report.as_dict())
        finish_run(conn, parent, status="SUCCEEDED")
    return pg_engine


@pytest.fixture(scope="module")
def gateway_ro_engine(gateway_db, pg_database, pg_admin_url, tmp_path_factory):
    role = "gw_ro_{0}".format(uuid.uuid4().hex[:8])
    password = "gw-ro-{0}".format(uuid.uuid4().hex[:12])
    admin_on_test_db = make_url(pg_admin_url).set(database=make_url(pg_database).database, drivername="postgresql").render_as_string(hide_password=False)
    tmp_dir = tmp_path_factory.mktemp("gw_role")
    result = provision_role_with_psql(admin_on_test_db, role, password, tmp_dir)
    assert result.returncode == 0, result.stderr
    ro_url = make_url(pg_database).set(username=role, password=password)
    engine = create_engine(ro_url, future=True, pool_pre_ping=True, connect_args={"options": "-c default_transaction_read_only=on -c statement_timeout=15000"})
    yield engine
    engine.dispose()
    with gateway_db.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = :r AND pid <> pg_backend_pid()"), {"r": role})
        conn.execute(text('DROP OWNED BY "{0}"'.format(role)))
    admin = create_engine(pg_admin_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text('DROP ROLE IF EXISTS "{0}"'.format(role)))
    admin.dispose()


@pytest.fixture
def consumer(gateway_ro_engine, monkeypatch):
    from ai_gateway.cache import CACHE

    readonly_db.set_engine_for_tests(gateway_ro_engine)
    monkeypatch.delenv("DATABASE_READONLY_URL", raising=False)
    monkeypatch.delenv("AI_GATEWAY_EXPORT_MODE", raising=False)
    monkeypatch.delenv("AI_GATEWAY_REMOTE_VALUE_SOURCES", raising=False)
    monkeypatch.setenv("AI_CONTEXT_API_TOKEN", TOKEN)
    monkeypatch.setenv("AI_CONTEXT_API_HOST", "127.0.0.1")
    CACHE.clear()
    yield gateway_ro_engine
    readonly_db.set_engine_for_tests(None)
    CACHE.clear()


def _client(client_addr):
    from fastapi.testclient import TestClient

    import ai_context_api

    return TestClient(ai_context_api.app, raise_server_exceptions=False, client=client_addr)


@pytest.fixture
def remote(consumer):
    return _client(REMOTE)


@pytest.fixture
def local(consumer):
    return _client(LOCAL)


HEADERS = {"Authorization": "Bearer " + TOKEN}


# ---- SQL views: adversarial holdout rows ------------------------------------------------------------


def test_views_exist_and_readonly_role_can_select_them(gateway_ro_engine):
    with gateway_ro_engine.connect() as conn:
        for view in ("mi_v_strategy_nonholdout_runs", "mi_v_strategy_experiments", "mi_v_strategy_oos_windows", "mi_v_strategy_artifact_status"):
            conn.execute(text("SELECT * FROM {0} LIMIT 1".format(view)))


def test_nonholdout_runs_view_excludes_runs_that_accessed_the_holdout(gateway_db):
    with gateway_db.connect() as conn:
        runs = {r[0] for r in conn.execute(text("SELECT research_run_id FROM mi_v_strategy_nonholdout_runs WHERE strategy_id = 'GW_STRAT'"))}
    assert "GW_RUN_HOLDOUT" not in runs
    assert "GW_RUN_NULLSTAT" not in runs
    assert "GW_RUN_NULLACC" not in runs
    assert "GW_RUN_NULLEXP" not in runs
    assert {"GW_RUN_OK", "GW_RUN_MIXED", "GW_RUN_2025", "GW_RUN_UNKNOWN", "GW_RUN_NULLFLAG"} <= runs


def test_experiments_view_only_returns_explicit_pre2025_nonholdout_rows(gateway_db):
    with gateway_db.connect() as conn:
        rows = [dict(r) for r in conn.execute(text("SELECT * FROM mi_v_strategy_experiments WHERE strategy_id = 'GW_STRAT' ORDER BY backtest_id")).mappings().all()]
    ids = {r["backtest_id"] for r in rows}
    assert ids == {"gw-ok-2016", "gw-ok-2017", "gw-mixed-2018"}
    assert all(r["research_is_holdout"] is False for r in rows)
    assert all(r["test_end"] < date(2025, 1, 1) for r in rows)
    dumped = json.dumps(rows, default=str)
    assert "9.9" not in dumped and "8.8" not in dumped and "7.7" not in dumped and "6.6" not in dumped
    assert "5.5" not in dumped and "4.4" not in dumped and "3.3" not in dumped


def test_oos_windows_view_excludes_2025_null_end_and_holdout_runs(gateway_db):
    with gateway_db.connect() as conn:
        rows = [dict(r) for r in conn.execute(text("SELECT * FROM mi_v_strategy_oos_windows WHERE strategy_id = 'GW_STRAT'")).mappings().all()]
    assert [(r["research_run_id"], r["outer_window_id"]) for r in rows] == [("GW_RUN_OK", "2016")]
    assert "0.99" not in json.dumps(rows, default=str) and "0.42" not in json.dumps(rows, default=str)


def test_artifact_view_binds_artifacts_to_proven_nonholdout_lineage(gateway_db):
    with gateway_db.connect() as conn:
        rows = [dict(r) for r in conn.execute(text("SELECT * FROM mi_v_strategy_artifact_status WHERE strategy_id = 'GW_STRAT'")).mappings().all()]
    visible = {(r["research_run_id"], r["research_experiment_id"], r["artifact_type"]) for r in rows}
    assert visible == EXPECTED_VISIBLE_ARTIFACT_KEYS
    columns = set(rows[0]) if rows else set()
    assert "payload_json" not in columns and "logical_path" not in columns and "artifact_key" not in columns
    assert all(r["lineage_status"] in {"NONHOLDOUT_EXPERIMENT_BOUND", "NONHOLDOUT_RUN_ALL_EXPERIMENTS_PROVEN"} for r in rows)
    run_scoped = [r for r in rows if r["research_experiment_id"] is None]
    assert run_scoped and all(r["research_run_id"] == "GW_RUN_OK" for r in run_scoped)
    assert all(r["run_experiment_count"] == r["run_visible_experiment_count"] == 2 for r in run_scoped)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO backtests (backtest_id, strategy_id) VALUES ('gw-mutate', 'GW_STRAT')",
        "UPDATE research_runs SET holdout_accessed = FALSE WHERE research_run_id = 'GW_RUN_HOLDOUT'",
        "DELETE FROM research_artifacts WHERE research_run_id = 'GW_RUN_OK'",
        "SELECT payload_json FROM research_artifacts LIMIT 1",
        "SELECT * FROM backtests WHERE research_test_type = 'ML_FINAL_HOLDOUT'",
        "SELECT * FROM research_oos_windows WHERE oos_end >= DATE '2025-01-01'",
        "CREATE TABLE gw_escalate (x INT)",
        "CREATE OR REPLACE VIEW mi_v_strategy_experiments AS SELECT * FROM backtests",
    ],
)
def test_readonly_role_cannot_mutate_or_read_raw_research_tables(gateway_ro_engine, sql):
    with gateway_ro_engine.connect() as conn:
        with pytest.raises(Exception) as exc:
            conn.execute(text(sql))
            conn.commit()
    sqlstate = getattr(getattr(exc.value, "orig", None), "pgcode", None)
    assert sqlstate in {"42501", "25006"}, (sql, sqlstate, str(exc.value)[:200])


# ---- REST routes on the read-only role ------------------------------------------------------------


GATEWAY_PATHS = (
    "/api/v1/context/morning",
    "/api/v1/markets/pulse",
    "/api/v1/rates",
    "/api/v1/macro",
    "/api/v1/macro/DGS10",
    "/api/v1/credit",
    "/api/v1/sectors",
    "/api/v1/sectors/Technology",
    "/api/v1/industries",
    "/api/v1/subindustries",
    "/api/v1/order-flow",
    "/api/v1/strategies",
    "/api/v1/strategies/GW_STRAT/oos",
    "/api/v1/strategies/GW_STRAT/experiments",
    "/api/v1/data-health",
    "/api/v1/changes",
)


@pytest.mark.parametrize("path", GATEWAY_PATHS)
def test_routes_require_bearer_and_remote_default_is_external(remote, path):
    assert remote.get(path).status_code == 401
    assert remote.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = remote.get(path, headers=HEADERS)
    assert response.status_code == 200, response.text[:400]
    payload = response.json()
    assert payload["schema_version"] == "ai_gateway_v1"
    assert payload["export_mode"] == "external"
    assert payload["owner_session"] is False
    assert payload["interpretation"] == "NONE"
    assert verify_export_hash(payload)
    dumped = json.dumps(payload)
    assert TOKEN not in dumped and "postgresql" not in dumped.lower()


def test_owner_env_does_not_change_remote_sessions(remote, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_EXPORT_MODE", "owner")
    payload = remote.get("/api/v1/sectors", headers=HEADERS).json()
    assert payload["export_mode"] == "external" and payload["owner_session"] is False
    proxied = _client(LOCAL).get("/api/v1/sectors", headers={**HEADERS, "X-Forwarded-For": "203.0.113.9"}).json()
    assert proxied["export_mode"] == "external"
    forwarded = _client(LOCAL).get("/api/v1/sectors", headers={**HEADERS, "Forwarded": 'for="203.0.113.9";proto=https'}).json()
    assert forwarded["export_mode"] == "external"
    public_host = _client(LOCAL).get("/api/v1/sectors", headers={**HEADERS, "Host": "mcp.example.com"}).json()
    assert public_host["export_mode"] == "external"
    loopback_spoof = _client(LOCAL).get("/api/v1/sectors", headers={**HEADERS, "X-Forwarded-For": "127.0.0.1"}).json()
    assert loopback_spoof["export_mode"] == "external"
    monkeypatch.setenv("AI_GATEWAY_TRUST_PROXY", "0")
    still_remote = _client(LOCAL).get("/api/v1/sectors", headers={**HEADERS, "X-Forwarded-For": "198.51.100.7"}).json()
    assert still_remote["export_mode"] == "external"


def test_remote_sector_rotation_keeps_identity_but_not_entitlement_unverified_values(remote):
    payload = remote.get("/api/v1/sectors", headers=HEADERS).json()
    body = payload["body"]
    assert payload["available"] is True
    assert body["primary_source"] == "EQUITY_EOD"
    assert body["equity_provider"]["export_scope"] == "INTERNAL_ONLY"
    rows = body["rows"]
    assert rows and all(row.get("restricted") is True for row in rows)
    assert all("ret_1d" not in row and "rs_1d" not in row for row in rows)
    assert {row["sector_key"] for row in rows} >= {"Technology", "Energy"}
    assert all(row["source_id"] == "EQUITY_EOD" for row in rows)
    assert payload["restricted_entries"] >= len(rows)


def test_local_owner_session_exposes_ret_1d_and_rs_1d_on_aligned_sessions(local, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_EXPORT_MODE", "owner")
    payload = local.get("/api/v1/sectors", headers=HEADERS).json()
    assert payload["export_mode"] == "owner" and payload["owner_session"] is True
    rows = {row["sector_key"]: row for row in payload["body"]["rows"]}
    tech = rows["Technology"]
    assert tech["as_of"] == "2026-09-10" and tech["prev_session"] == "2026-09-09"
    assert tech["aligned_with_benchmark"] is True
    assert tech["ret_1d"] == pytest.approx(52.02 / 51.0 - 1.0)
    assert tech["rs_1d"] == pytest.approx((52.02 / 102.01) / (51.0 / 101.0) - 1.0)
    assert tech["adjustment_basis"] == "SPLIT_ADJUSTED_UNKNOWN_DIVIDEND"
    assert tech["legacy_fmp"] is False and tech["source_kind"] == "independent_equity_eod"
    # XLU has no 2026-09-10 bar: missing is null, never zero and never forward-filled.
    utilities = rows["Utilities"]
    assert utilities["ret_1d"] is None and utilities["rs_1d"] is None
    assert utilities["aligned_with_benchmark"] is False
    assert utilities["value_kind"]["ret_1d_kind"] == "unavailable"
    # A flat ETF is a valid zero return, distinct from null.
    flat = rows["Consumer Staples"]
    assert flat["ret_1d"] == pytest.approx(0.0, abs=1e-12) and flat["ret_1d"] is not None
    assert flat["aligned_with_benchmark"] is True and flat["value_kind"]["ret_1d_kind"] == "observed"
    assert flat["rs_1d"] == pytest.approx((20.0 / 102.01) / (20.0 / 101.0) - 1.0)


def test_rates_curve_prefers_treasury_by_observation_date_with_per_leg_provenance(remote):
    payload = remote.get("/api/v1/rates", headers=HEADERS).json()
    assert payload["available"] is True
    body = payload["body"]
    assert body["complete_curve_date"] == "2026-09-09"
    curve = {leg["tenor"]: leg for leg in body["curve"]}
    assert curve["10Y"]["source_id"] == "TREASURY" and curve["10Y"]["yield_pct"] == 4.9
    assert curve["10Y"]["observation_date"] == "2026-09-09"
    assert curve["10Y"]["selection_reason"].startswith("tie_preference_TREASURY")
    assert all(leg["observation_date"] == "2026-09-09" for leg in body["curve"] if leg.get("yield_pct") is not None)
    assert body["curve_dates_mixed"] is False
    partial = {leg["tenor"]: leg for leg in body["partial_newer"]}
    assert {"3M", "2Y"} <= set(partial) and all(leg["observation_date"] == "2026-09-10" for leg in partial.values())
    assert "TREASURY" in body["source_ids"]
    assert body["source_priority"]["preferred"].startswith("TREASURY")
    assert "Treasury" in body["attribution"]
    for item in body["derived_nominal_minus_real"] or []:
        assert item["label"] == "derived_nominal_minus_real" and "T5YIE" in item["notes"]
        assert item.get("restricted") is not True and item["observation_date"] == "2026-09-09"
    # Treasury values are ATTRIBUTION_REQUIRED and therefore visible to a remote client.
    assert curve["10Y"].get("restricted") is not True


def test_macro_series_reports_treasury_source_for_ust_series(remote):
    payload = remote.get("/api/v1/macro/UST_NOM_10Y?limit=5", headers=HEADERS).json()
    assert payload["available"] is True
    assert payload["body"]["source"]["source_id"] == "TREASURY"
    assert payload["body"]["latest"]["observation_date"] == "2026-09-09"
    fred = remote.get("/api/v1/macro/DGS10?limit=5", headers=HEADERS).json()
    assert fred["body"]["source"]["provider"] == "FRED"
    unknown = remote.get("/api/v1/macro/NOT_A_SERIES", headers=HEADERS)
    assert unknown.status_code == 400 and unknown.json()["error"] == "UNKNOWN_SERIES"


def test_credit_values_are_redacted_remotely_but_identity_and_dates_remain(remote):
    payload = remote.get("/api/v1/credit", headers=HEADERS).json()
    buckets = payload["body"]["buckets"]
    assert buckets and all(bucket.get("restricted") is True for bucket in buckets)
    assert all("oas_bps" not in bucket for bucket in buckets)
    assert all(bucket.get("bucket") for bucket in buckets)


def test_hierarchy_exposes_semiconductor_baskets_and_explicit_unavailable_sectors(remote, local, monkeypatch):
    payload = remote.get("/api/v1/subindustries?sector=Technology", headers=HEADERS).json()
    body = payload["body"]
    assert body["official_gics_subindustry"] is False
    labels = {row["industry"] for row in body["rows"]}
    assert {"AI Compute / GPUs", "Semiconductor Equipment", "Memory", "Networking / Connectivity", "Analog / Industrial", "Foundry / Manufacturing", "Mobile / Consumer Chips"} <= labels
    assert body["subgroup_status_by_sector"]["Technology"] == "AVAILABLE"
    unavailable = remote.get("/api/v1/subindustries?sector=Utilities", headers=HEADERS).json()
    assert unavailable["available"] is False
    assert "not guessed" in unavailable["unavailable_reason"]
    assert unavailable["body"]["subgroup_status_by_sector"]["Utilities"] == "UNAVAILABLE"
    assert "Utilities" in unavailable["body"]["unavailable_sectors"]
    industries = remote.get("/api/v1/industries?sector=Technology", headers=HEADERS).json()
    kinds = {row["group_kind"] for row in industries["body"]["rows"]}
    assert kinds == {"ETF_COMPARISON"}
    assert {row["industry"] for row in industries["body"]["rows"]} >= {"SMH (mega-cap semi ETF comparison)", "XSD (equal-weight semi ETF comparison)"}
    assert industries["body"]["legacy_fmp_rows"] == 0 and industries["body"]["independent_rows"] >= 2
    # Values on these INTERNAL_ONLY rows are redacted remotely but visible to a local owner.
    assert all(row.get("restricted") is True for row in industries["body"]["rows"])
    monkeypatch.setenv("AI_GATEWAY_EXPORT_MODE", "owner")
    owner = local.get("/api/v1/subindustries?sector=Technology", headers=HEADERS).json()
    gpus = next(row for row in owner["body"]["rows"] if row["industry"] == "AI Compute / GPUs")
    assert gpus["members"] == ["NVDA", "AMD"] and gpus["ret_1d"] is not None and gpus["benchmark"] == "XLK"
    assert gpus["weighting"] == "equal_dollar_daily_rebalance_v1"


def test_sector_detail_includes_hierarchy(remote):
    payload = remote.get("/api/v1/sectors/Technology", headers=HEADERS).json()
    body = payload["body"]
    assert body["sector"] == "Technology"
    assert body["hierarchy"]["sector_etf"] == "XLK"
    assert body["subgroup_status"] == "AVAILABLE"
    assert any(item["instrument_id"] == "SMH" for item in body["hierarchy"]["industry_etf_comparisons"])
    assert body["sector_row"]["sector_key"] == "Technology"


def test_data_health_uses_freshness_v2_vocabulary_and_separates_observation_from_ingestion(remote):
    payload = remote.get("/api/v1/data-health", headers=HEADERS).json()
    body = payload["body"]
    assert body["freshness_policy_version"] == "freshness_policy_v2"
    statuses = {row["freshness_status"] for row in body["sources"]}
    assert statuses <= {"LATEST_AVAILABLE", "AWAITING_RELEASE", "INGESTION_OVERDUE", "STALE", "MISSING", "INVALID_FUTURE", "TRANSPORT_FAILURE", "UNKNOWN", None}
    treasury = next(row for row in body["sources"] if row["source_id"] == "TREASURY")
    assert treasury["latest_observation"] == "2026-09-10"
    assert treasury["latest_successful_ingestion"] is not None
    assert treasury["transport_status"] == "OK"
    assert treasury["usage_scope"] == "ATTRIBUTION_REQUIRED"
    equity = next(row for row in body["sources"] if row["source_id"] == "EQUITY_EOD")
    assert equity["usage_scope"] == "INTERNAL_ONLY"


# ---- research tools: only the allowed class escapes -------------------------------------------------


def test_strategy_experiments_tool_double_filters_holdout_and_artifacts(remote):
    payload = remote.get("/api/v1/strategies/GW_STRAT/experiments", headers=HEADERS).json()
    body = payload["body"]
    assert payload["available"] is True and body["holdout_excluded"] is True
    assert body["holdout_filter"]["sql_view"] is True and body["holdout_filter"]["gateway"] is True
    ids = {row["backtest_id"] for row in body["experiments"]}
    assert ids == {"gw-ok-2016", "gw-ok-2017", "gw-mixed-2018"}
    visible = {(a["research_run_id"], a["research_experiment_id"], a["artifact_type"]) for a in body["artifacts"]}
    assert visible == EXPECTED_VISIBLE_ARTIFACT_KEYS
    dumped = json.dumps(payload)
    for leak in ("ML_FINAL_HOLDOUT", "GW_RUN_HOLDOUT", "model.pkl", "weights.pkl", "model_metadata", "object_store_key", "secret_metric", "2025-12-31", "9.9", "8.8", "7.7", "6.6", "GW_RUN_UNKNOWN", "GW_RUN_2025", "GW_RUN_NULLFLAG", "payload_json"):
        assert leak not in dumped, leak
    assert body["model_binaries"] == "never exported"


def test_strategy_oos_windows_tool_only_returns_pre2025_windows_from_nonholdout_runs(remote):
    payload = remote.get("/api/v1/strategies/GW_STRAT/oos", headers=HEADERS).json()
    body = payload["body"]
    assert body["holdout_excluded"] is True
    assert [(w["research_run_id"], w["outer_window_id"]) for w in body["windows"]] == [("GW_RUN_OK", "2016")]
    dumped = json.dumps(payload)
    assert "2025H" not in dumped and "0.99" not in dumped and "0.42" not in dumped


def test_holdout_identifiers_are_not_queryable(remote):
    response = remote.get("/api/v1/strategies/ML_FINAL_HOLDOUT/oos", headers=HEADERS)
    assert response.status_code == 400 and response.json()["error"] == "INVALID_INPUT"


def test_strategy_summary_hides_holdout_metrics(remote):
    payload = remote.get("/api/v1/strategies?strategy=GW_STRAT", headers=HEADERS).json()
    rows = payload["body"]["strategies"]
    assert rows and all(row["holdout_metrics"] == "NOT_EXPOSED" for row in rows)
    assert "9.9" not in json.dumps(payload)


def test_holdout_excluded_is_false_when_the_view_is_missing(consumer, monkeypatch):
    from ai_gateway import services

    monkeypatch.setattr(services, "_view_exists", lambda _conn, _name: False)
    from ai_gateway.cache import CACHE

    CACHE.clear()
    payload = _client(REMOTE).get("/api/v1/strategies/GW_STRAT/experiments", headers=HEADERS).json()
    assert payload["available"] is False
    assert payload["body"]["holdout_excluded"] is False
    assert payload["body"]["experiments"] == [] and payload["body"]["artifacts"] == []


# ---- MCP + OAuth ------------------------------------------------------------------------------------


def test_mcp_flow_is_read_only_and_remote_is_external(remote):
    headers = {**HEADERS, "Content-Type": "application/json"}
    assert remote.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).status_code == 401
    init = remote.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init.status_code == 200 and init.json()["result"]["serverInfo"]["name"] == "FMP Market Intelligence"
    listed = remote.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = [tool["name"] for tool in listed.json()["result"]["tools"]]
    assert "get_rates_curve" in names and "execute_sql" not in names
    called = remote.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_rates_curve", "arguments": {}}})
    body = json.loads(called.json()["result"]["content"][0]["text"])
    assert body["tool"] == "get_rates_curve" and body["export_mode"] == "external"
    assert body["body"]["complete_curve_date"] == "2026-09-09"
    for name in ("place_order", "execute_sql", "launch_backtest", "download_model", "open_holdout"):
        forbidden = remote.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": name, "arguments": {}}})
        assert forbidden.json()["error"]["message"] == "TOOL_FORBIDDEN"
    assert remote.put("/mcp", headers=headers, json={}).status_code == 405
    assert remote.post("/v1/context/morning/latest", headers=headers, json={}).status_code == 405


def test_oauth_pkce_flow_binds_the_token_to_this_resource(remote):
    import base64
    import hashlib

    metadata = remote.get("/.well-known/oauth-authorization-server").json()
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    registered = remote.post("/oauth/register", json={"redirect_uris": ["https://chatgpt.example/cb"], "client_name": "chatgpt"}).json()
    client_id = registered["client_id"]
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    denied = remote.post(
        "/oauth/authorize",
        data={"token": "wrong", "response_type": "code", "client_id": client_id, "redirect_uri": "https://chatgpt.example/cb", "state": "s", "code_challenge": challenge, "code_challenge_method": "S256"},
    )
    assert denied.status_code == 200 and "not accepted" in denied.text
    granted = remote.post(
        "/oauth/authorize",
        data={"token": TOKEN, "response_type": "code", "client_id": client_id, "redirect_uri": "https://chatgpt.example/cb", "state": "s", "code_challenge": challenge, "code_challenge_method": "S256"},
        follow_redirects=False,
    )
    assert granted.status_code == 302
    location = granted.headers["location"]
    code = location.split("code=", 1)[1].split("&", 1)[0]
    # A foreign resource is refused instead of minted.
    foreign = remote.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": "https://chatgpt.example/cb", "client_id": client_id, "code_verifier": verifier, "resource": "https://evil.example/mcp"})
    assert foreign.status_code == 400 and foreign.json()["error"] == "invalid_target"
    # The code was consumed by the refused attempt; re-authorize to obtain a fresh one.
    granted = remote.post(
        "/oauth/authorize",
        data={"token": TOKEN, "response_type": "code", "client_id": client_id, "redirect_uri": "https://chatgpt.example/cb", "state": "s", "code_challenge": challenge, "code_challenge_method": "S256"},
        follow_redirects=False,
    )
    code = granted.headers["location"].split("code=", 1)[1].split("&", 1)[0]
    bad_verifier = remote.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": "https://chatgpt.example/cb", "client_id": client_id, "code_verifier": "nope"})
    assert bad_verifier.status_code == 400
    granted = remote.post(
        "/oauth/authorize",
        data={"token": TOKEN, "response_type": "code", "client_id": client_id, "redirect_uri": "https://chatgpt.example/cb", "state": "s", "code_challenge": challenge, "code_challenge_method": "S256"},
        follow_redirects=False,
    )
    code = granted.headers["location"].split("code=", 1)[1].split("&", 1)[0]
    minted = remote.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": "https://chatgpt.example/cb", "client_id": client_id, "code_verifier": verifier})
    assert minted.status_code == 200
    access = minted.json()["access_token"]
    with_jwt = remote.get("/api/v1/data-health", headers={"Authorization": "Bearer " + access})
    assert with_jwt.status_code == 200 and with_jwt.json()["export_mode"] == "external"
    assert TOKEN not in json.dumps(minted.json())


def test_oauth_register_and_authorize_require_exact_https_redirect(remote):
    import base64
    import hashlib

    assert remote.post("/oauth/register", json={"redirect_uris": ["https://evil.example/*"], "client_name": "wild"}).status_code == 400
    assert remote.post("/oauth/register", json={"redirect_uris": ["http://evil.example/cb"], "client_name": "http"}).status_code == 400
    assert remote.post("/oauth/register", json={"redirect_uris": ["javascript:alert(1)"], "client_name": "js"}).status_code == 400
    assert remote.post("/oauth/register", json={"redirect_uris": [], "client_name": "empty"}).status_code == 400
    registered = remote.post("/oauth/register", json={"redirect_uris": ["https://chatgpt.example/cb"], "client_name": "chatgpt"}).json()
    client_id = registered["client_id"]
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    swapped = remote.post(
        "/oauth/authorize",
        data={"token": TOKEN, "response_type": "code", "client_id": client_id, "redirect_uri": "https://attacker.example/steal", "state": "s", "code_challenge": challenge, "code_challenge_method": "S256"},
        follow_redirects=False,
    )
    assert swapped.status_code == 200 and "not registered" in swapped.text
    granted = remote.post(
        "/oauth/authorize",
        data={"token": TOKEN, "response_type": "code", "client_id": client_id, "redirect_uri": "https://chatgpt.example/cb", "state": "s", "code_challenge": challenge, "code_challenge_method": "S256"},
        follow_redirects=False,
    )
    assert granted.status_code == 302
    code = granted.headers["location"].split("code=", 1)[1].split("&", 1)[0]
    substituted = remote.post(
        "/oauth/token",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": "https://attacker.example/steal", "client_id": client_id, "code_verifier": verifier},
    )
    assert substituted.status_code == 400 and substituted.json()["error"] == "invalid_grant"


def test_ready_reports_export_policy(remote):
    assert remote.get("/ready").status_code == 401
    ready = remote.get("/ready", headers=HEADERS)
    assert ready.status_code == 200 and ready.json()["ready"] is True
    assert ready.json()["export_policy"]["export_mode"] == "external"
