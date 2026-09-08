"""Bounded live FRED validation against a disposable PostgreSQL database.

    python -m jobs.validate_fred_live

Requires ``FRED_API_KEY`` and ``FMP_TEST_DATABASE_URL``. The URL must be a throw-away
admin database (CREATE DATABASE privilege). The job creates ``fmp_fred_val_<id>``,
applies migrations, ingests a small catalog slice, builds analytics and a morning
snapshot, then hits the context API export. The child database is dropped on exit.

Never prints the API key, request URLs (they contain the key), raw restricted credit
values, or database credentials. Ordinary PR CI does not run this module.
Exit codes: 0 ok, 2 validation failed, 3 configuration, 4 refused production-looking URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from datetime import date, timedelta
from typing import Any

from market_intelligence.fred_client import FredClient, api_key_from_env, redact
from market_intelligence.nulls import strict_dumps

logger = logging.getLogger("market_intelligence.fred_validate")

EXIT_OK = 0
EXIT_FAIL = 2
EXIT_CONFIG = 3
EXIT_REFUSED = 4

# One or two series per required domain. Not the full catalog.
VALIDATION_SERIES = (
    "CPIAUCSL",
    "PCEPI",
    "DGS10",
    "DFF",
    "WALCL",
    "WTREGEN",
    "BAMLC0A0CM",
)
RESTRICTED_SERIES = frozenset({"BAMLC0A0CM"})
_PRODUCTION_HINTS = ("ondigitalocean", "digitalocean.com", "db.ondigitalocean.com", "aws.amazon", "rds.amazonaws")


def _refuse_production_url(url: str) -> str | None:
    lowered = url.lower()
    for hint in _PRODUCTION_HINTS:
        if hint in lowered:
            return hint
    if "fmp_mi_test_" in lowered or "fmp_fred_val_" in lowered or "127.0.0.1" in url or "localhost" in lowered:
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


def _safe_log(message: str, api_key: str | None) -> None:
    logger.info(redact(message, api_key))


def _validate_stored_quality(engine) -> dict[str, Any]:
    """Metadata, units, and missing-value contracts. Never includes observation values."""
    from sqlalchemy import text

    from market_intelligence.catalog import CATALOG_BY_ID, META_VALIDATED

    with engine.connect() as conn:
        series_rows = conn.execute(
            text(
                """
                SELECT series_id, metadata_status, publication_status, units, frequency_short
                FROM mi_macro_series
                WHERE series_id = ANY(:ids)
                """
            ),
            {"ids": list(VALIDATION_SERIES)},
        ).mappings().all()
        missing_as_zero = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM mi_macro_observations
                WHERE series_id = ANY(:ids) AND is_current
                  AND raw_value IN ('.', '') AND value IS NOT NULL
                """
            ),
            {"ids": list(VALIDATION_SERIES)},
        ).scalar()
        current_nulls = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM mi_macro_observations
                WHERE series_id = ANY(:ids) AND is_current AND value IS NULL
                """
            ),
            {"ids": list(VALIDATION_SERIES)},
        ).scalar()
        current_nonzero = conn.execute(
            text(
                """
                SELECT series_id, COUNT(*) AS n FROM mi_macro_observations
                WHERE series_id = ANY(:ids) AND is_current AND value IS NOT NULL
                GROUP BY series_id
                """
            ),
            {"ids": list(VALIDATION_SERIES)},
        ).mappings().all()

    by_id = {row["series_id"]: row for row in series_rows}
    units_ok: dict[str, bool] = {}
    metadata_ok: dict[str, bool] = {}
    for series_id in VALIDATION_SERIES:
        row = by_id.get(series_id)
        spec = CATALOG_BY_ID[series_id]
        if row is None:
            metadata_ok[series_id] = False
            units_ok[series_id] = False
            continue
        metadata_ok[series_id] = row["metadata_status"] == META_VALIDATED and row["publication_status"] == "PUBLISHED"
        units_text = str(row["units"] or "").lower()
        units_ok[series_id] = bool(spec.expected_units_contains) and any(tok in units_text for tok in spec.expected_units_contains)
        if spec.expected_frequency and row["frequency_short"] and str(row["frequency_short"]).upper() != spec.expected_frequency:
            units_ok[series_id] = False
    counts = {row["series_id"]: int(row["n"]) for row in current_nonzero}
    ok = (
        all(metadata_ok.values())
        and all(units_ok.values())
        and int(missing_as_zero or 0) == 0
        and all(counts.get(sid, 0) > 0 for sid in VALIDATION_SERIES)
    )
    return {
        "ok": ok,
        "metadata_validated": metadata_ok,
        "units_and_frequency_match": units_ok,
        "missing_token_stored_as_zero": int(missing_as_zero or 0),
        "current_null_observations": int(current_nulls or 0),
        "current_valued_observations": counts,
    }


def _create_child_database(admin_url: str) -> tuple[Any, str]:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    name = "fmp_fred_val_{0}".format(uuid.uuid4().hex[:10])
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


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded live FRED validation on a disposable database")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    api_key = api_key_from_env()
    if not api_key:
        logger.error("FRED_API_KEY is not configured")
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
    from market_intelligence import readonly_db
    from market_intelligence.analytics import build_analytics
    from market_intelligence.export_policy import verify_export_hash
    from market_intelligence.ingest_fred import ingest_fred_catalog
    from market_intelligence.morning_context import build_and_publish
    from market_intelligence.store import finish_run, start_run, upsert_source_registry
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
    report: dict[str, Any] = {"series": list(VALIDATION_SERIES), "restricted_checked": sorted(RESTRICTED_SERIES)}
    try:
        admin, child_url = _create_child_database(admin_url)
        child_name = make_url(child_url).database
        _safe_log("created disposable validation database", api_key)
        engine = create_engine(child_url, future=True)
        with engine.begin() as conn:
            conn.execute(text(strategies_sql))
        applied = apply_migrations(engine=engine)
        report["migrations"] = [n for n in applied if not str(n).endswith("(skipped)")]
        client = FredClient(api_key)
        with engine.begin() as conn:
            upsert_source_registry(conn, enabled={"FRED": True}, access={"FRED": "CONFIGURED"})
            parent = start_run(conn, source_id="ORCHESTRATOR", dataset="fred_live_validation")
        ingest = ingest_fred_catalog(engine, client, series_ids=VALIDATION_SERIES, mode="full", parent_run_id=parent)
        report["ingest"] = {
            "succeeded": [r.series_id for r in ingest.succeeded],
            "failed": [r.series_id for r in ingest.failed],
            "quarantined": [r.series_id for r in ingest.quarantined],
            "metadata_status": {r.series_id: r.metadata_status for r in ingest.succeeded},
            "request_count": client.request_count,
        }
        if ingest.failed or set(VALIDATION_SERIES) - {r.series_id for r in ingest.succeeded}:
            report["status"] = "INGEST_FAILED"
            if args.json:
                print(strict_dumps({k: v for k, v in report.items()}))
            return EXIT_FAIL
        quality = _validate_stored_quality(engine)
        report["quality"] = quality
        if not quality.get("ok"):
            report["status"] = "QUALITY_FAILED"
            if args.json:
                print(strict_dumps(report))
            return EXIT_FAIL
        today = date.today()
        with engine.begin() as conn:
            rid = start_run(conn, source_id="ANALYTICS", dataset="metric_snapshots", parent_run_id=parent)
            analytics = build_analytics(conn, as_of=today, run_id=rid, history_start=today - timedelta(days=400), series_ids=VALIDATION_SERIES)
            finish_run(conn, rid, status="SUCCEEDED", details=analytics.as_dict())
            finish_run(conn, parent, status="SUCCEEDED")
        report["analytics"] = {"metrics_written": analytics.metrics_written, "credit_written": analytics.credit_written}
        morning = build_and_publish(engine, parent_run_id=parent, created_by="fred_live_validation")
        report["morning"] = {
            "snapshot_id": morning.snapshot_id,
            "completeness": morning.completeness,
            "published_new": morning.published_new,
            "macro_status": (morning.sections_status.get("macro") or {}).get("status"),
            "rates_status": (morning.sections_status.get("rates") or {}).get("status"),
            "liquidity_status": (morning.sections_status.get("liquidity") or {}).get("status"),
            "credit_status": (morning.sections_status.get("credit") or {}).get("status"),
        }
        token = "fred-validation-token"
        os.environ["AI_CONTEXT_API_TOKEN"] = token
        readonly_db.set_engine_for_tests(engine)
        from fastapi.testclient import TestClient

        import ai_context_api

        client_http = TestClient(ai_context_api.app, raise_server_exceptions=False)
        headers = {"Authorization": "Bearer {0}".format(token)}
        macro = client_http.get("/v1/context/macro/latest", headers=headers).json()
        credit = client_http.get("/v1/context/credit/latest", headers=headers).json()
        if not verify_export_hash(macro) or not verify_export_hash(credit):
            report["status"] = "EXPORT_HASH_FAILED"
            if args.json:
                print(strict_dumps(report))
            return EXIT_FAIL
        inflation = ((macro.get("body") or {}).get("categories") or {}).get("inflation") or []
        cpi = next((b for b in inflation if b.get("series_id") == "CPIAUCSL"), None)
        latest_value = None if cpi is None else (cpi.get("latest") or {}).get("value")
        yoy = None if cpi is None else ((cpi.get("transforms") or {}).get("yoy_pct") or {}).get("value")
        credit_restricted = all(b.get("restricted") is True and "oas_bps" not in b for b in ((credit.get("body") or {}).get("buckets") or []))
        report["export"] = {
            "macro_available": macro.get("available"),
            "cpi_latest_present": latest_value is not None,
            "cpi_yoy_present": yoy is not None,
            "credit_restricted": credit_restricted,
            "restricted_entries": credit.get("restricted_entries"),
        }
        if latest_value is None or not credit_restricted:
            report["status"] = "EXPORT_CONTRACT_FAILED"
            if args.json:
                print(strict_dumps(report))
            return EXIT_FAIL
        report["status"] = "PASSED"
        _safe_log("FRED live validation passed ({0} requests)".format(client.request_count), api_key)
        if args.json:
            print(strict_dumps(report))
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - never leak URLs or the key
        logger.error("validation error: %s", redact(exc.__class__.__name__, api_key))
        report["status"] = "ERROR"
        report["error_class"] = exc.__class__.__name__
        if args.json:
            print(strict_dumps(report))
        return EXIT_FAIL
    finally:
        readonly_db.set_engine_for_tests(None)
        if engine is not None:
            engine.dispose()
        if admin is not None and child_name:
            _drop_child(admin, child_name)


def main() -> int:  # pragma: no cover
    return run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
