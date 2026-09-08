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
    new = [n for n in names if n.startswith(("008", "009", "010", "011", "012"))]
    assert new == [
        "008_market_intelligence_core.sql",
        "009_market_intelligence_analytics.sql",
        "010_research_ideas.sql",
        "011_bond_securities.sql",
        "012_market_intelligence_publication.sql",
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
    assert {"008_market_intelligence_core.sql", "009_market_intelligence_analytics.sql", "010_research_ideas.sql", "011_bond_securities.sql", "012_market_intelligence_publication.sql"} <= applied
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


def test_schema_objects_exist(pg_engine):
    expected_tables = {
        "mi_source_registry", "mi_ingestion_runs", "mi_data_freshness", "mi_macro_series", "mi_macro_observations",
        "mi_market_instruments", "mi_instrument_identifiers", "mi_market_bars", "mi_market_quotes",
        "mi_metric_snapshots", "mi_credit_index_snapshots", "mi_sector_snapshots", "mi_industry_snapshots",
        "mi_morning_context_snapshots", "mi_research_ideas", "mi_research_idea_versions", "mi_research_idea_transitions",
        "mi_research_idea_approvals", "mi_research_idea_tests", "mi_bond_securities", "mi_bond_quotes", "mi_bond_trades", "mi_bond_analytics",
    }
    with pg_engine.connect() as conn:
        tables = {r[0] for r in conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'mi\\_%' AND table_type='BASE TABLE'"))}
        views = {r[0] for r in conn.execute(text("SELECT table_name FROM information_schema.views WHERE table_name LIKE 'mi\\_v\\_%'"))}
        # Pre-existing research tables untouched.
        research = {r[0] for r in conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_name IN ('research_runs','research_experiments','strategies')"))}
    assert expected_tables <= tables
    assert {"mi_v_source_health", "mi_v_macro_latest", "mi_v_credit_latest", "mi_v_morning_context_latest", "mi_v_strategy_research_summary", "mi_v_research_ideas"} <= views
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
