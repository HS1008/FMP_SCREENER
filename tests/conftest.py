import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Real PostgreSQL fixtures for Market Intelligence tests.
#
# Set FMP_TEST_DATABASE_URL to a *disposable* PostgreSQL database whose role may
# CREATE DATABASE (e.g. postgresql+psycopg2://fmp_test:fmp_test@127.0.0.1:5432/fmp_test).
# A fresh database is created per test session and dropped afterwards. Tests that
# need it are skipped (and reported as skipped, i.e. unverified) when unset.
# ---------------------------------------------------------------------------

STRATEGIES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id VARCHAR(100) PRIMARY KEY,
    name VARCHAR(255),
    environment VARCHAR(32),
    status VARCHAR(32),
    qc_project_id VARCHAR(100),
    qc_deployment_id VARCHAR(100),
    git_commit VARCHAR(80),
    rules_json JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
)
"""


def _admin_url():
    return (os.environ.get("FMP_TEST_DATABASE_URL") or "").strip() or None


@pytest.fixture(scope="session")
def pg_admin_url():
    url = _admin_url()
    if not url:
        if (os.environ.get("MI_REQUIRE_DB_TESTS") or "").strip() in {"1", "true", "yes"}:
            pytest.fail("MI_REQUIRE_DB_TESTS is set but FMP_TEST_DATABASE_URL is missing: DB checks must not silently skip in CI")
        pytest.skip("FMP_TEST_DATABASE_URL not set; real-PostgreSQL tests unverified")
    return url


@pytest.fixture(scope="session")
def pg_database(pg_admin_url):
    """Create a disposable database for this session; yields its URL."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    name = "fmp_mi_test_{0}".format(uuid.uuid4().hex[:10])
    admin = create_engine(pg_admin_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text('CREATE DATABASE "{0}"'.format(name)))
    url = make_url(pg_admin_url).set(database=name)
    yield str(url.render_as_string(hide_password=False))
    with admin.connect() as conn:
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n AND pid <> pg_backend_pid()"), {"n": name})
        conn.execute(text('DROP DATABASE IF EXISTS "{0}"'.format(name)))
    admin.dispose()


@pytest.fixture(scope="session")
def pg_engine(pg_database):
    """Engine on the disposable DB with the pre-existing ``strategies`` table and all migrations applied."""
    from sqlalchemy import create_engine, text

    from jobs.apply_migrations import apply_migrations

    engine = create_engine(pg_database, future=True, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(text(STRATEGIES_TABLE_SQL))
    apply_migrations(engine=engine)
    yield engine
    engine.dispose()


MI_TABLES_TRUNCATE = (
    "mi_pit_sector_internals",
    "mi_pit_sector_artifacts",
    "mi_macro_observation_quarantine",
    "mi_research_idea_tests",
    "mi_research_idea_approvals",
    "mi_research_idea_transitions",
    "mi_research_idea_versions",
    "mi_research_ideas",
    "mi_bond_analytics",
    "mi_bond_trades",
    "mi_bond_quotes",
    "mi_bond_securities",
    "mi_morning_context_snapshots",
    "mi_industry_snapshots",
    "mi_sector_snapshots",
    "mi_credit_index_snapshots",
    "mi_metric_snapshots",
    "mi_finra_aggregate_observations",
    "mi_finra_aggregate_quarantine",
    "mi_finra_ingest_checkpoint",
    "mi_finra_dataset_capability",
    "mi_collector_status",
    "mi_market_quotes",
    "mi_equity_eod_batch_chunks",
    "mi_equity_eod_batches",
    "mi_market_bars",
    "mi_instrument_identifiers",
    "mi_market_instruments",
    "mi_macro_observations",
    "mi_provider_observations",
    "mi_macro_series",
    "mi_data_freshness",
    "mi_ingestion_runs",
    "mi_source_registry",
)


@pytest.fixture
def mi_db(pg_engine):
    """Function-scoped clean slate for mi_* tables (research tables untouched)."""
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE {0} CASCADE".format(", ".join(MI_TABLES_TRUNCATE))))
    return pg_engine
