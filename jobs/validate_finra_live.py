"""Bounded live FINRA Query API validation against disposable PostgreSQL.

    python -m jobs.validate_finra_live --json

Requires FINRA_CLIENT_ID + FINRA_CLIENT_SECRET (or FINRA_API_* aliases) and
FMP_TEST_DATABASE_URL. Creates ``fmp_finra_val_<id>``, probes official Query API
datasets, ingests a 45-day aggregate window, and records capability statuses.
Never prints credentials, tokens, or TRAQS/TRACE file-download hosts.
Does not probe individual TRACE tape products.

Exit codes: 0 ok (capabilities recorded; some datasets may be entitlement-required),
2 validation/auth failed, 3 configuration, 4 refused production-looking URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from market_intelligence.finra_catalog import QUERY_DATASETS, TRACE_INDIVIDUAL
from market_intelligence.finra_client import FinraClient, FinraError, credentials_from_env, redact
from market_intelligence.nulls import strict_dumps

logger = logging.getLogger("market_intelligence.finra_validate")

EXIT_OK = 0
EXIT_FAIL = 2
EXIT_CONFIG = 3
EXIT_REFUSED = 4
_PRODUCTION_HINTS = ("ondigitalocean", "digitalocean.com", "db.ondigitalocean.com", "aws.amazon", "rds.amazonaws")


def _refuse_production_url(url: str) -> str | None:
    lowered = url.lower()
    for hint in _PRODUCTION_HINTS:
        if hint in lowered:
            return hint
    if "fmp_mi_test_" in lowered or "fmp_finra_val_" in lowered or "fmp_fred_val_" in lowered or "127.0.0.1" in url or "localhost" in lowered:
        return None
    host = ""
    try:
        from sqlalchemy.engine import make_url

        host = (make_url(url).host or "").lower()
    except Exception:
        host = ""
    if host and host not in {"127.0.0.1", "localhost", "postgres"}:
        return host
    return None


def _create_child_database(admin_url: str):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    name = "fmp_finra_val_{0}".format(uuid.uuid4().hex[:10])
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text('CREATE DATABASE "{0}"'.format(name)))
    child = str(make_url(admin_url).set(database=name).render_as_string(hide_password=False))
    return admin, child


def _drop_child(admin, name: str) -> None:
    from sqlalchemy import text

    with admin.connect() as conn:
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n AND pid <> pg_backend_pid()"), {"n": name})
        conn.execute(text('DROP DATABASE IF EXISTS "{0}"'.format(name)))
    admin.dispose()


def _probe_row(probe) -> dict[str, Any]:
    return {
        "dataset": probe.dataset,
        "group": probe.group,
        "environment": probe.environment,
        "capability_status": probe.capability_status,
        "http_status": probe.http_status,
        "record_count": probe.record_count,
        "schema_fields": probe.schema_fields[:40],
        "latest_observation_date": probe.latest_observation_date.isoformat() if probe.latest_observation_date else None,
        "retrieved_at": probe.retrieved_at,
        "coverage_note": probe.coverage_note,
        "error_redacted": probe.error_redacted,
    }


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded live FINRA Query API validation on disposable PostgreSQL")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    client_id, client_secret = credentials_from_env()
    if not client_id or not client_secret:
        logger.error("FINRA client id/secret is not configured")
        return EXIT_CONFIG
    admin_url = (os.environ.get("FMP_TEST_DATABASE_URL") or "").strip()
    if not admin_url:
        logger.error("FMP_TEST_DATABASE_URL is required (disposable PostgreSQL only)")
        return EXIT_CONFIG
    refused = _refuse_production_url(admin_url)
    if refused:
        logger.error("refusing a non-disposable database URL (matched %s)", refused)
        return EXIT_REFUSED

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from jobs.apply_migrations import apply_migrations
    from market_intelligence.ingest_finra import ensure_finra_sources, ingest_finra, record_individual_trace_limitation, upsert_capability
    from market_intelligence.morning_context import build_and_publish

    strategies_sql = """
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
    child_name = None
    admin = None
    engine = None
    retrieved_at = datetime.now(timezone.utc).isoformat()
    report: dict[str, Any] = {
        "authenticated": False,
        "individual_trace": {
            "dataset": TRACE_INDIVIDUAL.dataset,
            "capability_status": "ENTITLEMENT_REQUIRED",
            "probed": False,
            "coverage_note": TRACE_INDIVIDUAL.coverage_note,
        },
        "datasets": [],
        "sample_datasets": [],
    }
    secrets = (client_id, client_secret)
    try:
        admin, child_url = _create_child_database(admin_url)
        child_name = make_url(child_url).database
        engine = create_engine(child_url, future=True)
        with engine.begin() as conn:
            conn.execute(text(strategies_sql))
        report["migrations"] = [n for n in apply_migrations(engine=engine) if not str(n).endswith("(skipped)")]
        client = FinraClient(client_id, client_secret)
        try:
            client.authenticate()
            report["authenticated"] = True
        except FinraError as exc:
            report["authenticated"] = False
            report["auth_status"] = exc.status
            report["auth_capability"] = exc.capability
            report["auth_error"] = redact(str(exc), *secrets)
            report["status"] = "AUTH_FAILED"
            if args.json:
                print(strict_dumps(report))
            return EXIT_FAIL
        probes = client.probe_catalog(retrieved_at=retrieved_at, environment="production")
        report["datasets"] = [_probe_row(p) for p in probes]
        from dataclasses import replace

        sample_rows = []
        for spec in QUERY_DATASETS:
            if not spec.mock_dataset:
                continue
            mock = replace(spec, dataset=spec.mock_dataset)
            sample_rows.append(_probe_row(client.probe_dataset(mock, retrieved_at=retrieved_at, environment="sample")))
        report["sample_datasets"] = sample_rows
        with engine.begin() as conn:
            ensure_finra_sources(conn, query_enabled=True, query_access="CONFIGURED")
            record_individual_trace_limitation(conn)
            for spec, probe in zip(QUERY_DATASETS, probes):
                upsert_capability(
                    conn,
                    spec=spec,
                    status=probe.capability_status,
                    http_status=probe.http_status,
                    record_count=probe.record_count,
                    schema_fields=probe.schema_fields,
                    latest=probe.latest_observation_date,
                    coverage_note=spec.coverage_note,
                    error_redacted=probe.error_redacted,
                    success=probe.capability_status == "AVAILABLE",
                )
        ingest = ingest_finra(engine, client, mode="full")
        report["ingest"] = ingest.as_dict()
        with engine.connect() as conn:
            aggregates = int(conn.execute(text("SELECT COUNT(*) FROM mi_finra_aggregate_observations WHERE is_current")).scalar() or 0)
            trades = int(conn.execute(text("SELECT COUNT(*) FROM mi_bond_trades WHERE source_id = 'FINRA_TRACE'")).scalar() or 0)
            interval = conn.execute(
                text("SELECT MIN(observation_date), MAX(observation_date) FROM mi_finra_aggregate_observations WHERE is_current")
            ).one()
        report["stored"] = {
            "current_aggregate_rows": aggregates,
            "individual_trace_rows": trades,
            "min_observation_date": interval[0].isoformat() if interval[0] else None,
            "max_observation_date": interval[1].isoformat() if interval[1] else None,
        }
        if trades:
            report["status"] = "FABRICATED_TRADES"
            if args.json:
                print(strict_dumps(report))
            return EXIT_FAIL
        morning = build_and_publish(engine, created_by="finra_live_validation")
        report["morning"] = {
            "snapshot_id": morning.snapshot_id,
            "completeness": morning.completeness,
            "order_flow_status": (morning.sections_status.get("order_flow") or {}).get("status"),
        }
        report["request_count"] = client.request_count
        report["status"] = "PASSED"
        if args.json:
            print(strict_dumps(report))
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001
        logger.error("validation error: %s", redact(exc.__class__.__name__, *secrets))
        report["status"] = "ERROR"
        report["error_class"] = exc.__class__.__name__
        if args.json:
            print(strict_dumps(report))
        return EXIT_FAIL
    finally:
        if engine is not None:
            engine.dispose()
        if admin is not None and child_name:
            _drop_child(admin, child_name)


def main() -> int:  # pragma: no cover
    return run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
