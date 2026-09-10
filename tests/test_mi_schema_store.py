"""Real-PostgreSQL tests: additive migrations 008-011, idempotent observations with revisions,
NULL vs zero, per-source isolation, freshness vs transport, advisory writer lock.

Skipped (reported as unverified) unless FMP_TEST_DATABASE_URL points at a disposable DB.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from jobs.apply_migrations import apply_migrations, pending_migration_files, split_sql_statements
from market_intelligence import store
from market_intelligence.locking import LockContention, writer_lock

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "db" / "migrations"

pytestmark = pytest.mark.usefixtures("pg_engine")


# ---- migrations ---------------------------------------------------------------------------------

def test_new_migrations_are_additive_and_numbered_after_007():
    names = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    new = [n for n in names if n.startswith(("008", "009", "010", "011", "012", "013", "014", "015", "016", "017", "018", "019", "020", "021", "022", "023", "024", "025", "026"))]
    assert new == [
        "008_market_intelligence_core.sql",
        "009_market_intelligence_analytics.sql",
        "010_research_ideas.sql",
        "011_bond_securities.sql",
        "012_market_intelligence_publication.sql",
        "013_research_idea_completeness.sql",
        "014_pit_sector_internals.sql",
        "015_metric_latest_skips_withdrawn.sql",
        "016_ibkr_collector.sql",
        "017_finra_order_flow.sql",
        "018_ibkr_callback_freshness.sql",
        "019_finra_identity_quarantine.sql",
        "020_ops_status_and_migration_checksum.sql",
        "021_ops_status_extended.sql",
        "022_deploy_host_identity.sql",
        "023_streamlit_readonly_identity.sql",
        "024_research_live_identity.sql",
        "025_stage1_live_identity.sql",
        "026_treasury_equity_eod.sql",
    ]
    for name in new:
        sql = (MIGRATIONS / name).read_text(encoding="utf-8").upper()
        assert "DROP TABLE" not in sql and "ALTER TABLE" not in sql.replace("ALTER TABLE MI_", "")
        assert "DO $$" not in sql and "CREATE FUNCTION" not in sql and "CREATE TRIGGER" not in sql
        for statement in split_sql_statements((MIGRATIONS / name).read_text(encoding="utf-8")):
            assert statement.upper().startswith(("CREATE TABLE IF NOT EXISTS MI_", "CREATE INDEX IF NOT EXISTS", "CREATE UNIQUE INDEX IF NOT EXISTS", "CREATE OR REPLACE VIEW MI_V_", "COMMENT ON", "ALTER TABLE MI_")), statement[:80]


def test_migrations_applied_once_and_second_application_is_noop(pg_engine):
    with pg_engine.connect() as conn:
        applied = {r[0] for r in conn.execute(text("SELECT filename FROM schema_migrations"))}
    assert {"008_market_intelligence_core.sql", "009_market_intelligence_analytics.sql", "010_research_ideas.sql", "011_bond_securities.sql", "012_market_intelligence_publication.sql", "013_research_idea_completeness.sql", "014_pit_sector_internals.sql", "015_metric_latest_skips_withdrawn.sql", "016_ibkr_collector.sql", "017_finra_order_flow.sql", "018_ibkr_callback_freshness.sql", "019_finra_identity_quarantine.sql", "020_ops_status_and_migration_checksum.sql", "021_ops_status_extended.sql", "022_deploy_host_identity.sql", "023_streamlit_readonly_identity.sql", "024_research_live_identity.sql", "025_stage1_live_identity.sql", "026_treasury_equity_eod.sql"} <= applied
    files = sorted(MIGRATIONS.glob("*.sql"))
    assert pending_migration_files(files, applied) == []
    # Second application must be a no-op (idempotent) and leave research tables intact.
    with pg_engine.connect() as conn:
        before = conn.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar()
        research_before = conn.execute(text("SELECT COUNT(*) FROM research_runs")).scalar()
    apply_migrations(engine=pg_engine)
    with pg_engine.connect() as conn:
        after = conn.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar()
        assert conn.execute(text("SELECT COUNT(*) FROM research_runs")).scalar() == research_before
    assert after == before


def test_upgrade_from_011_schema_preserves_rows_and_second_apply_is_noop(pg_admin_url, tmp_path):
    """Upgrade path: a DB already at 011 (with data written by the reviewed code) upgrades to 012 additively."""
    import shutil
    import uuid

    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from tests.conftest import STRATEGIES_TABLE_SQL

    staged = tmp_path / "migrations_011"
    staged.mkdir()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name < "012":
            shutil.copy(path, staged / path.name)
    name = "fmp_mi_upgrade_{0}".format(uuid.uuid4().hex[:8])
    admin = create_engine(pg_admin_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text('CREATE DATABASE "{0}"'.format(name)))
    engine = create_engine(make_url(pg_admin_url).set(database=name), future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text(STRATEGIES_TABLE_SQL))
        apply_migrations(staged, engine=engine)
        with engine.begin() as conn:
            applied = {r[0] for r in conn.execute(text("SELECT filename FROM schema_migrations"))}
            assert "011_bond_securities.sql" in applied and "012_market_intelligence_publication.sql" not in applied
            cols = {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='mi_macro_series'"))}
            assert "publication_status" not in cols
            # Rows in the pre-012 shape (what the reviewed head would have written).
            conn.execute(text("INSERT INTO mi_source_registry (source_id, provider, dataset, enabled) VALUES ('FRED', 'FRED', 'series', TRUE)"))
            conn.execute(
                text(
                    """
                    INSERT INTO mi_macro_series (series_id, source_id, provider_series_id, category, subcategory, catalog_version,
                        source_url, export_scope, frequency_short, units, metadata_status, vintage_kind, pit_safe)
                    VALUES ('WTREGEN', 'FRED', 'WTREGEN', 'liquidity', 'tga', 'fred_catalog_v1', 'https://fred.stlouisfed.org/series/WTREGEN',
                        'ATTRIBUTION_REQUIRED', 'W', 'Billions of U.S. Dollars', 'OK', 'LATEST_REVISED', FALSE)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO mi_macro_observations (series_id, observation_date, value, raw_value, revision_seq, is_current, retrieved_at)
                    VALUES ('WTREGEN', '2024-12-25', 722000.0, '722000.0', 1, TRUE, NOW())
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO mi_data_freshness (source_id, dataset, last_attempt_at, latest_observation_date, expected_cadence,
                        tolerance_days, transport_status, freshness_status, updated_at)
                    VALUES ('FRED', 'series:WTREGEN', NOW(), '2024-12-25', 'W', 10, 'OK', 'FRESH', NOW())
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO mi_morning_context_snapshots (snapshot_id, schema_version, generated_at, cutoff_at, as_of_date,
                        generation_params, input_refs, sections_status, snapshot_json, snapshot_sha256, completeness, publication_state)
                    VALUES ('legacy-snap', 'morning_context_v1', NOW(), NOW(), '2024-12-31', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
                        '{"schema_version":"morning_context_v1"}'::jsonb, repeat('a', 64), 'COMPLETE', 'PUBLISHED')
                    """
                )
            )
            # A v1 idea version written before 013 (no completeness columns yet).
            conn.execute(text("INSERT INTO mi_research_ideas (idea_id, lineage_id, title, research_type, current_state, current_version, created_by) VALUES ('idea_legacy', 'LINEAGE_LEGACY', 'legacy', 'SECTOR_ROTATION_DIAGNOSTIC', 'SPEC_FROZEN', 1, 'alice')"))
            conn.execute(
                text(
                    """
                    INSERT INTO mi_research_idea_versions (idea_id, version, spec_json, spec_hash, research_type, conception_at, execution_support, created_by)
                    VALUES ('idea_legacy', 1, '{"schema_version":"idea_spec_v1"}'::jsonb, repeat('b', 64), 'SECTOR_ROTATION_DIAGNOSTIC', NOW(), 'SUPPORTED_DRY_RUN_CONTRACT', 'alice')
                    """
                )
            )
        # Upgrade with the full directory: exactly the post-011 migrations are pending, in order.
        applied_now = apply_migrations(MIGRATIONS, engine=engine)
        expected_pending = sorted(p.name for p in MIGRATIONS.glob("*.sql") if p.name >= "012")
        assert [a for a in applied_now if not a.endswith("(skipped)")] == expected_pending
        with engine.connect() as conn:
            legacy_idea = conn.execute(text("SELECT spec_completeness, missing_fields, effective_holdout_start, economic_gate FROM mi_v_research_ideas WHERE idea_id='idea_legacy'")).mappings().one()
            series = conn.execute(text("SELECT units, metadata_status, publication_status, publication_reason, catalog_units FROM mi_macro_series WHERE series_id='WTREGEN'")).mappings().one()
            obs = conn.execute(text("SELECT value, is_current FROM mi_macro_observations WHERE series_id='WTREGEN'")).one()
            fresh = conn.execute(text("SELECT freshness_status, freshness_policy_version, metadata_status FROM mi_data_freshness WHERE dataset='series:WTREGEN'")).one()
            snap = conn.execute(text("SELECT quality_status, content_sha256, superseded_by, snapshot_sha256 FROM mi_morning_context_snapshots WHERE snapshot_id='legacy-snap'")).one()
            latest_view = conn.execute(text("SELECT series_id, publication_status, value, units FROM mi_v_macro_latest WHERE series_id='WTREGEN'")).mappings().one()
            health_view = conn.execute(text("SELECT source_id, freshness_dataset, freshness_policy_version FROM mi_v_source_health WHERE freshness_dataset='series:WTREGEN'")).mappings().one()
            quarantine = conn.execute(text("SELECT COUNT(*) FROM mi_macro_observation_quarantine")).scalar()
            snap_view = conn.execute(text("SELECT snapshot_id, quality_status, content_sha256 FROM mi_v_morning_context_latest")).mappings().one()
        # Existing data preserved; the pre-012 series is honestly UNVALIDATED (not silently PUBLISHED) until
        # the next refresh re-validates it against fred_catalog_v2; its raw values are untouched.
        assert (series["units"], series["metadata_status"]) == ("Billions of U.S. Dollars", "OK")
        assert series["publication_status"] == "UNVALIDATED" and series["publication_reason"] is None and series["catalog_units"] is None
        assert float(obs.value) == 722000.0 and obs.is_current is True
        assert fresh == ("FRESH", None, None)
        assert snap == ("OK", None, None, "a" * 64)  # immutable snapshot body/hash untouched
        assert latest_view["publication_status"] == "UNVALIDATED" and float(latest_view["value"]) == 722000.0
        assert health_view["freshness_policy_version"] is None and quarantine == 0
        assert snap_view["snapshot_id"] == "legacy-snap" and snap_view["quality_status"] == "OK" and snap_view["content_sha256"] is None
        # Pre-013 idea versions are INCOMPLETE by default (never approvable without a human revision).
        assert legacy_idea["spec_completeness"] == "INCOMPLETE" and legacy_idea["missing_fields"] == [] and legacy_idea["economic_gate"] == "NOT_DEFINED"
        assert str(legacy_idea["effective_holdout_start"]) == "2025-01-01"
        # Second application is a no-op.
        again = apply_migrations(MIGRATIONS, engine=engine)
        assert all(a.endswith("(skipped)") for a in again)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar() == len(list(MIGRATIONS.glob("*.sql")))
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n AND pid <> pg_backend_pid()"), {"n": name})
            conn.execute(text('DROP DATABASE IF EXISTS "{0}"'.format(name)))
        admin.dispose()


def test_schema_objects_exist(pg_engine):
    expected_tables = {
        "mi_source_registry", "mi_ingestion_runs", "mi_data_freshness", "mi_macro_series", "mi_macro_observations",
        "mi_market_instruments", "mi_instrument_identifiers", "mi_market_bars", "mi_market_quotes",
        "mi_metric_snapshots", "mi_credit_index_snapshots", "mi_sector_snapshots", "mi_industry_snapshots",
        "mi_morning_context_snapshots", "mi_research_ideas", "mi_research_idea_versions", "mi_research_idea_transitions",
        "mi_research_idea_approvals", "mi_research_idea_tests", "mi_bond_securities", "mi_bond_quotes", "mi_bond_trades", "mi_bond_analytics",
        "mi_collector_status",
        "mi_finra_dataset_capability",
        "mi_finra_ingest_checkpoint",
        "mi_finra_aggregate_observations",
        "mi_deploy_host_identity",
    }
    with pg_engine.connect() as conn:
        tables = {r[0] for r in conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'mi\\_%' AND table_type='BASE TABLE'"))}
        views = {r[0] for r in conn.execute(text("SELECT table_name FROM information_schema.views WHERE table_name LIKE 'mi\\_v\\_%'"))}
        # Pre-existing research tables untouched.
        research = {r[0] for r in conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_name IN ('research_runs','research_experiments','strategies')"))}
    assert expected_tables <= tables
    assert {"mi_v_source_health", "mi_v_macro_latest", "mi_v_credit_latest", "mi_v_morning_context_latest", "mi_v_strategy_research_summary", "mi_v_research_ideas", "mi_v_pit_sector_artifacts", "mi_v_pit_sector_internals_current", "mi_v_pit_sector_internals_latest", "mi_v_ibkr_collector_status", "mi_v_ibkr_quotes_latest", "mi_v_order_flow_coverage", "mi_v_finra_aggregate_current"} <= views
    assert {"research_runs", "strategies"} <= research


# ---- observations ---------------------------------------------------------------------------------

def _series(conn, sid="TESTSER", units="Percent"):
    store.upsert_source_registry(conn)
    store.upsert_macro_series(
        conn,
        series_id=sid,
        source_id="FRED",
        provider_series_id=sid,
        spec_fields={"category": "rates", "subcategory": "test", "catalog_version": "fred_catalog_v1", "source_url": "https://fred.stlouisfed.org/series/{0}".format(sid), "notes": "", "export_scope": "ATTRIBUTION_REQUIRED", "expected_frequency": "D"},
        meta={"title": "Test", "units": units, "frequency_short": "D", "seasonal_adjustment_short": "NSA"},
        metadata_status="VALIDATED",
        mismatches=None,
    )


def _rows(conn, sid="TESTSER"):
    return conn.execute(text("SELECT observation_date, value, raw_value, revision_seq, is_current, superseded_at FROM mi_macro_observations WHERE series_id=:s ORDER BY observation_date, revision_seq"), {"s": sid}).fetchall()


def test_identical_input_does_not_duplicate_and_revision_is_auditable(mi_db):
    t0 = datetime(2024, 12, 31, 12, tzinfo=timezone.utc)
    rows = [store.ObservationInput(date(2024, 12, 30), "4.25"), store.ObservationInput(date(2024, 12, 31), "4.35"), store.ObservationInput(date(2024, 12, 27), ".")]
    with mi_db.begin() as conn:
        _series(conn)
        c1 = store.upsert_observations(conn, series_id="TESTSER", rows=rows, retrieved_at=t0, run_id=None)
        c2 = store.upsert_observations(conn, series_id="TESTSER", rows=rows, retrieved_at=t0 + timedelta(hours=1), run_id=None)
        revised = store.upsert_observations(conn, series_id="TESTSER", rows=[store.ObservationInput(date(2024, 12, 31), "4.40")], retrieved_at=t0 + timedelta(days=1), run_id=None)
        stored = _rows(conn)
        current = store.current_observations(conn, "TESTSER")
    assert (c1.inserted, c1.unchanged, c1.rejected) == (3, 0, 0)
    assert (c2.inserted, c2.unchanged, c2.revised) == (0, 3, 0)
    assert (revised.revised, revised.inserted) == (1, 0)
    # 3 economic dates, 4 physical rows (one superseded revision).
    assert len(stored) == 4
    dec31 = [r for r in stored if r.observation_date == date(2024, 12, 31)]
    assert [(r.value, r.revision_seq, r.is_current) for r in dec31] == [(Decimal("4.35"), 1, False), (Decimal("4.40"), 2, True)]
    assert dec31[0].superseded_at is not None
    assert current[date(2024, 12, 31)] == Decimal("4.40")
    assert current[date(2024, 12, 27)] is None  # '.' -> NULL, raw token retained
    assert [r.raw_value for r in stored if r.observation_date == date(2024, 12, 27)] == ["."]


def test_null_is_not_zero_and_malformed_is_rejected(mi_db):
    t0 = datetime(2024, 12, 31, tzinfo=timezone.utc)
    with mi_db.begin() as conn:
        _series(conn)
        counts = store.upsert_observations(
            conn,
            series_id="TESTSER",
            rows=[store.ObservationInput(date(2024, 12, 30), "0"), store.ObservationInput(date(2024, 12, 31), "."), store.ObservationInput(date(2024, 12, 29), "n/a"), store.ObservationInput(date(2024, 12, 28), "garbage")],
            retrieved_at=t0,
            run_id=None,
        )
        stored = {r.observation_date: (r.value, r.raw_value) for r in _rows(conn)}
    assert counts.rejected == 1 and counts.inserted == 3
    assert stored[date(2024, 12, 30)] == (Decimal("0"), "0")
    assert stored[date(2024, 12, 31)][0] is None
    assert date(2024, 12, 28) not in stored
    assert counts.rejected_samples and "garbage" in str(counts.rejected_samples[0])


def test_failed_source_transaction_does_not_erase_other_source(mi_db):
    t0 = datetime(2024, 12, 31, tzinfo=timezone.utc)
    with mi_db.begin() as conn:
        _series(conn, "GOOD")
        store.upsert_observations(conn, series_id="GOOD", rows=[store.ObservationInput(date(2024, 12, 31), "1.0")], retrieved_at=t0, run_id=None)
    with pytest.raises(Exception):
        with mi_db.begin() as conn:
            _series(conn, "BAD")
            store.upsert_observations(conn, series_id="BAD", rows=[store.ObservationInput(date(2024, 12, 31), "2.0")], retrieved_at=t0, run_id=None)
            raise RuntimeError("provider failed mid-source")
    with mi_db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM mi_macro_observations WHERE series_id='GOOD'")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM mi_macro_observations WHERE series_id='BAD'")).scalar() == 0


def test_freshness_separates_transport_from_staleness(mi_db):
    today = date(2024, 12, 31)
    with mi_db.begin() as conn:
        store.upsert_source_registry(conn)
        run_id = store.start_run(conn, source_id="FRED", dataset="series:X")
        status = store.record_freshness(conn, source_id="FRED", dataset="series:X", cadence="D", transport_status="OK", latest_observation=date(2024, 12, 2), success=True, error_redacted=None, run_id=run_id, today=today)
        assert status == "STALE"  # successful retrieval of old data is not fresh
        row = conn.execute(text("SELECT transport_status, freshness_status, last_success_at, latest_observation_date FROM mi_data_freshness WHERE source_id='FRED' AND dataset='series:X'")).one()
        assert row.transport_status == "OK" and row.freshness_status == "STALE" and row.last_success_at is not None
        # Failed retrieval must not erase last valid observation / last success.
        store.record_freshness(conn, source_id="FRED", dataset="series:X", cadence="D", transport_status="FAILED", latest_observation=None, success=False, error_redacted="HTTP 500", run_id=run_id, today=today)
        row2 = conn.execute(text("SELECT transport_status, freshness_status, last_success_at, latest_observation_date, last_error_redacted FROM mi_data_freshness WHERE source_id='FRED' AND dataset='series:X'")).one()
    assert row2.transport_status == "FAILED"
    assert row2.latest_observation_date == date(2024, 12, 2)
    assert row2.last_success_at == row.last_success_at
    assert row2.last_error_redacted == "HTTP 500"


def test_ingestion_run_lifecycle_and_redacted_error(mi_db):
    with mi_db.begin() as conn:
        store.upsert_source_registry(conn)
        run_id = store.start_run(conn, source_id="FRED", dataset="series:DGS10", request_window=(date(2024, 1, 1), date(2024, 12, 31)))
        store.finish_run(conn, run_id, status="FAILED", counts={"received": 5, "rejected": 1}, error_redacted="FRED HTTP 429 api_key=[REDACTED]", retry_count=3)
        row = conn.execute(text("SELECT status, rows_received, rows_rejected, error_redacted, retry_count, finished_at, request_window_start, request_window_end FROM mi_ingestion_runs WHERE run_id=:r"), {"r": run_id}).one()
    assert row.status == "FAILED" and row.rows_received == 5 and row.rows_rejected == 1 and row.retry_count == 3
    assert row.finished_at is not None and row.request_window_start == date(2024, 1, 1)


# ---- lock -----------------------------------------------------------------------------------------

def test_writer_lock_contention_is_distinct_and_released_on_exit(pg_engine):
    with writer_lock(pg_engine):
        with pytest.raises(LockContention):
            with writer_lock(pg_engine):
                pass
    # Released after the block (a crashed process releases with its session).
    with writer_lock(pg_engine):
        pass


def test_readonly_role_file_grants_exactly_the_curated_views_that_migrations_create():
    """Every mi_v_* view shipped by a migration needs an explicit GRANT (no default-privilege shortcut) and nothing else."""
    import re

    views: set[str] = set()
    for path in MIGRATIONS.glob("*.sql"):
        views |= set(re.findall(r"CREATE OR REPLACE VIEW (mi_v_\w+)", path.read_text(encoding="utf-8")))
    role_sql = (ROOT / "db" / "roles" / "market_intelligence_readonly.sql").read_text(encoding="utf-8")
    granted = set(re.findall(r"GRANT SELECT ON (mi_v_\w+) TO mi_readonly", role_sql))
    assert granted == views, {"missing_grant": sorted(views - granted), "grant_without_view": sorted(granted - views)}
    statements = [line.strip() for line in role_sql.splitlines() if line.strip() and not line.strip().startswith("--")]
    assert not [s for s in statements if s.upper().startswith("ALTER DEFAULT PRIVILEGES")]
    for statement in statements:
        if statement.upper().startswith("GRANT SELECT ON"):
            assert re.match(r"GRANT SELECT ON mi_v_\w+ TO mi_readonly;$", statement), statement  # views only, never raw tables / ALL TABLES
