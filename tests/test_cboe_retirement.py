"""CBOE/OpenBB-CBOE stays retired: no scheduled fetch, no active Data Health outage."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from jobs.market_intelligence_refresh import _execute, build_parser, plan
from market_intelligence.catalog import RETIRED_CBOE_SOURCE_IDS
from market_intelligence.read_models import options_volatility_context, partition_source_health, source_health
from market_intelligence.store import retire_cboe_registry

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db" / "migrations" / "041_retire_cboe_openbb_sources.sql"
RIGHTS = {
    "MI_OPENBB_OPTIONS_ENABLED": "1",
    "MI_OPENBB_VIX_ENABLED": "1",
    "MI_OPENBB_OPTIONS_RIGHTS_ACK": "1",
    "MI_OPENBB_VIX_RIGHTS_ACK": "1",
    "MI_TREASURY_ENABLED": "0",
}


def test_retirement_migration_is_additive_and_keeps_history():
    sql = MIGRATION.read_text(encoding="utf-8")
    upper = sql.upper()
    for source_id in RETIRED_CBOE_SOURCE_IDS:
        assert source_id in sql
    assert "RETIRED_OPTIONAL" in sql
    assert "ENABLED = FALSE" in upper or "FALSE" in upper
    assert "ON CONFLICT (SOURCE_ID) DO UPDATE" in upper
    assert "PROVIDER = EXCLUDED.PROVIDER" not in upper
    assert "DATASET = EXCLUDED.DATASET" not in upper
    assert "DROP TABLE" not in upper
    assert "DROP VIEW" not in upper
    assert "DELETE FROM" not in upper
    assert "TRUNCATE" not in upper
    from jobs.apply_migrations import split_sql_statements

    for statement in split_sql_statements(sql):
        assert "MI_OPENBB_" not in statement.upper()
        assert "DROP" not in statement.upper()
        assert "DELETE" not in statement.upper()
    assert "historical mi_openbb_" in sql.lower()


def test_scheduled_and_explicit_plans_do_not_ingest_cboe():
    parser = build_parser()
    for argv in (
        ["--due-configured"],
        ["--all-configured"],
        ["--options"],
        ["--vix"],
        ["--options", "--vix"],
    ):
        the_plan = plan(parser.parse_args(argv), RIGHTS)
        steps = {step["step"]: step for step in the_plan["steps"]}
        for name in ("options", "vix"):
            if name in steps:
                assert steps[name]["action"] == "skip_retired"
                assert steps[name]["configured"] is False
                assert steps[name]["source_id"] in RETIRED_CBOE_SOURCE_IDS
        assert "ingest_openbb" not in (ROOT / "jobs" / "market_intelligence_refresh.py").read_text(encoding="utf-8")
    due = plan(parser.parse_args(["--due-configured"]), RIGHTS)
    yahoo = next(step for step in due["steps"] if step["step"] == "yahoo_vol")
    assert yahoo["action"] == "ingest"
    assert yahoo["source_id"] == "YAHOO_VOL"
    service = (ROOT / "deploy" / "market_intelligence" / "fmp-mi-refresh.service").read_text(encoding="utf-8")
    assert "--due-configured" in service
    assert "--options" not in service
    assert "--vix" not in service


def test_execute_skip_retired_does_not_call_openbb(monkeypatch):
    called = []

    def boom(*_args, **_kwargs):
        called.append("ingest_openbb")
        raise AssertionError("retired refresh called OpenBB/Cboe")

    monkeypatch.setattr("market_intelligence.openbb_provider.ingest.ingest_openbb", boom)
    monkeypatch.setattr("market_intelligence.ingest_finra.ensure_finra_sources", lambda *args, **kwargs: None)
    monkeypatch.setattr("market_intelligence.ingest_finra.record_individual_trace_limitation", lambda conn: None)
    monkeypatch.setattr("market_intelligence.store.start_run", lambda *args, **kwargs: "run_test")
    monkeypatch.setattr("market_intelligence.store.finish_run", lambda *args, **kwargs: None)
    monkeypatch.setattr("market_intelligence.store.upsert_source_registry", lambda *args, **kwargs: 0)
    monkeypatch.setattr("market_intelligence.store.retire_cboe_registry", lambda conn: 4)
    engine = MagicMock()
    args = build_parser().parse_args(["--options", "--vix"])
    the_plan = plan(args, RIGHTS)
    status = {"results": {}}
    code = _execute(args, the_plan, status, engine, None, RIGHTS)
    assert called == []
    assert status["results"]["options"]["outcome"] == "SKIPPED_RETIRED"
    assert status["results"]["vix"]["outcome"] == "SKIPPED_RETIRED"
    assert code in {0, 2}


def test_forced_options_action_still_does_not_call_cboe(monkeypatch):
    called = []
    monkeypatch.setattr(
        "market_intelligence.openbb_provider.ingest.ingest_openbb",
        lambda *_a, **_k: called.append("called"),
    )
    monkeypatch.setattr("market_intelligence.ingest_finra.ensure_finra_sources", lambda *args, **kwargs: None)
    monkeypatch.setattr("market_intelligence.ingest_finra.record_individual_trace_limitation", lambda conn: None)
    monkeypatch.setattr("market_intelligence.store.start_run", lambda *args, **kwargs: "run_test")
    monkeypatch.setattr("market_intelligence.store.finish_run", lambda *args, **kwargs: None)
    monkeypatch.setattr("market_intelligence.store.upsert_source_registry", lambda *args, **kwargs: 0)
    monkeypatch.setattr("market_intelligence.store.retire_cboe_registry", lambda conn: 4)
    engine = MagicMock()
    args = build_parser().parse_args(["--options"])
    the_plan = {
        "steps": [
            {
                "step": "options",
                "source_id": "OPENBB_CBOE_OPTIONS",
                "action": "ingest",
                "configured": True,
            }
        ]
    }
    status = {"results": {}}
    _execute(args, the_plan, status, engine, None, RIGHTS)
    assert called == []
    assert status["results"]["options"]["outcome"] == "SKIPPED_RETIRED"


def test_retired_cboe_cannot_enter_failed_transport_and_active_failures_still_do(monkeypatch):
    retired = {
        "source_id": "CBOE_ALL_ACCESS",
        "dataset": "iv_minus_rv",
        "freshness_dataset": "iv_minus_rv",
        "access_status": "RETIRED_OPTIONAL",
        "transport_status": "PARTIAL",
        "freshness_status": "MISSING",
        "latest_observation_date": None,
        "expected_cadence": "D",
        "coverage_status": "PARTIAL",
    }
    active = {
        "source_id": "FRED",
        "dataset": "series:DGS10",
        "freshness_dataset": "series:DGS10",
        "access_status": "CONFIGURED",
        "transport_status": "FAILED",
        "freshness_status": "FAILED",
        "latest_observation_date": "2026-09-01",
        "expected_cadence": "D",
    }
    monkeypatch.setattr("market_intelligence.read_models._rows", lambda conn, sql, params=None: [retired, active])
    health = source_health(object(), today=date(2026, 9, 26))
    buckets = partition_source_health(health)
    failed_ids = {row["source_id"] for row in buckets["failed_transport"]}
    assert "CBOE_ALL_ACCESS" not in failed_ids
    assert "FRED" in failed_ids
    cboe = next(row for row in health if row["source_id"] == "CBOE_ALL_ACCESS")
    assert cboe["retired_optional"] is True
    assert cboe["policy_status"] == "RETIRED"
    from market_intelligence.quote_status import exception_note

    note = exception_note(cboe)
    assert "outage" in note.lower()
    assert "retired" in note.lower()

    ready = {
        "source_id": "CBOE_ALL_ACCESS",
        "dataset": "iv_minus_rv",
        "freshness_dataset": "iv_minus_rv",
        "access_status": "READY",
        "transport_status": "PARTIAL",
        "freshness_status": "MISSING",
        "latest_observation_date": None,
        "expected_cadence": "D",
        "coverage_status": "PARTIAL",
    }
    monkeypatch.setattr("market_intelligence.read_models._rows", lambda conn, sql, params=None: [ready])
    still_active = source_health(object(), today=date(2026, 9, 26))
    assert {row["source_id"] for row in partition_source_health(still_active)["failed_transport"]} == {"CBOE_ALL_ACCESS"}


def test_options_context_is_yahoo_only_and_streamlit_stays_database_only(monkeypatch):
    monkeypatch.setattr(
        "market_intelligence.read_models.yahoo_vol_core",
        lambda conn: {"status": "OK", "vix": {"value": 18.5, "as_of": "2026-09-24"}},
    )
    ctx = options_volatility_context(object())
    assert ctx["status"] == "OK"
    assert ctx["source_id"] == "YAHOO_VOL"
    assert ctx["symbols"] == []
    assert ctx["vix"] is None
    assert ctx["yahoo_core"]["vix"]["value"] == 18.5
    page = (ROOT / "pages" / "21_Options_Volatility.py").read_text(encoding="utf-8")
    ui = (ROOT / "market_intelligence" / "pages_ui.py").read_text(encoding="utf-8")
    read_model = (ROOT / "market_intelligence" / "read_models.py").read_text(encoding="utf-8")
    for blob in (page, ui):
        assert "yfinance" not in blob
        assert "ingest_yahoo_vol" not in blob
        assert "cboe_client" not in blob
        assert "ingest_openbb" not in blob
        assert "cdn.cboe.com" not in blob
        assert "Options chains and VX futures" not in blob
    assert "mi_v_options_latest" not in read_model.split("def options_volatility_context", 1)[1].split("def options_chain_details", 1)[0]
    assert "mi_v_vix_curve_latest" not in read_model.split("def options_volatility_context", 1)[1].split("def options_chain_details", 1)[0]


def test_retire_registry_preserves_existing_labels_and_history(mi_db):
    from sqlalchemy import text

    with mi_db.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO mi_source_registry (
                    source_id, provider, dataset, enabled, access_status, usage_scope, updated_at
                ) VALUES (
                    'CBOE_ALL_ACCESS', 'Legacy Cboe Label', 'legacy_cboe_dataset', TRUE, 'READY', 'INTERNAL_ONLY', NOW()
                )
                ON CONFLICT (source_id) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    dataset = EXCLUDED.dataset,
                    enabled = TRUE,
                    access_status = 'READY',
                    updated_at = NOW()
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO mi_data_freshness (
                    source_id, dataset, transport_status, freshness_status, coverage_status, updated_at
                ) VALUES (
                    'CBOE_ALL_ACCESS', 'iv_minus_rv', 'PARTIAL', 'MISSING', 'PARTIAL', NOW()
                )
                ON CONFLICT (source_id, dataset) DO UPDATE SET
                    transport_status = 'PARTIAL',
                    freshness_status = 'MISSING',
                    coverage_status = 'PARTIAL',
                    updated_at = NOW()
                """
            )
        )
        before = source_health(conn, today=date(2026, 9, 26))
        retire_cboe_registry(conn)
        after = source_health(conn, today=date(2026, 9, 26))
        label = conn.execute(
            text("SELECT provider, dataset, enabled, access_status FROM mi_source_registry WHERE source_id = 'CBOE_ALL_ACCESS'")
        ).mappings().one()
        freshness = conn.execute(
            text("SELECT transport_status FROM mi_data_freshness WHERE source_id = 'CBOE_ALL_ACCESS' AND dataset = 'iv_minus_rv'")
        ).scalar()
        openbb = conn.execute(text("SELECT to_regclass('public.mi_openbb_snapshots')")).scalar()
    before_failed = {row["source_id"] for row in partition_source_health(before)["failed_transport"]}
    after_failed = {row["source_id"] for row in partition_source_health(after)["failed_transport"]}
    assert "CBOE_ALL_ACCESS" in before_failed
    assert "CBOE_ALL_ACCESS" not in after_failed
    assert label["provider"] == "Legacy Cboe Label"
    assert label["dataset"] == "legacy_cboe_dataset"
    assert label["enabled"] is False
    assert label["access_status"] == "RETIRED_OPTIONAL"
    assert freshness == "PARTIAL"
    assert openbb == "mi_openbb_snapshots"
