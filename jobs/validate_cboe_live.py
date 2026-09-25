"""Bounded live Cboe LiveVol validation against a disposable PostgreSQL database.

    python -m jobs.validate_cboe_live

Requires ``CBOE_CLIENT_ID``, ``CBOE_CLIENT_SECRET``, and ``FMP_TEST_DATABASE_URL``.
Creates ``fmp_cboe_val_<id>``, applies migrations, proves OAuth, exercises one
documented underlying-quotes request, then runs one bounded ingest within the
trial point budget. Drops the child database on exit.

Never prints credentials, tokens, or Authorization headers. Ordinary PR CI does
not run this module.
Exit codes: 0 ok (partial entitlement OK), 2 hard failure, 3 configuration,
4 refused production-looking URL.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from typing import Any

logger = logging.getLogger("market_intelligence.cboe_validate")

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
    if "fmp_mi_test_" in lowered or "fmp_cboe_val_" in lowered or "127.0.0.1" in url or "localhost" in lowered:
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


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True, default=str))
    else:
        for key, value in sorted(payload.items()):
            print("{0}={1}".format(key, value))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--point-budget", type=int, default=40, help="Hard ceiling for this validation run")
    args = parser.parse_args(argv)

    client_id = (os.environ.get("CBOE_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("CBOE_CLIENT_SECRET") or "").strip()
    admin_url = (os.environ.get("FMP_TEST_DATABASE_URL") or "").strip()
    if not client_id or not client_secret:
        print("CBOE_CLIENT_ID and CBOE_CLIENT_SECRET are required", file=sys.stderr)
        return EXIT_CONFIG
    if not admin_url:
        print("FMP_TEST_DATABASE_URL is required", file=sys.stderr)
        return EXIT_CONFIG
    refused = _refuse_production_url(admin_url)
    if refused:
        print("refusing production-looking database URL ({0})".format(refused), file=sys.stderr)
        return EXIT_REFUSED

    from jobs.apply_migrations import apply_migrations
    from market_intelligence.cboe_client import CboeClient, CboeError, UNDERLYING_QUOTES, session_date
    from market_intelligence.ingest_cboe import ingest_cboe
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    child = "fmp_cboe_val_{0}".format(uuid.uuid4().hex[:10])
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
    child_url = make_url(admin_url).set(database=child).render_as_string(hide_password=False)
    report: dict[str, Any] = {
        "status": "STARTED",
        "auth": None,
        "endpoint": UNDERLYING_QUOTES,
        "endpoint_status": None,
        "entitlement": None,
        "ingest": None,
        "points_used": 0,
        "requests_made": 0,
        "metrics_ok": [],
        "metrics_incomplete": [],
        "views_ok": False,
        "child_database": child,
        "api_mode": (os.environ.get("CBOE_API_MODE") or "delayed").strip().lower() or "delayed",
        "credential_id_len": len(client_id),
        "credential_secret_len": len(client_secret),
    }
    engine = None
    try:
        with admin.connect() as conn:
            conn.execute(text('CREATE DATABASE "{0}"'.format(child)))
        engine = create_engine(child_url, future=True)
        # Migrations assume the pre-existing Stage-1 strategies table (same as FRED live validation).
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
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
                )
            )
        apply_migrations(engine=engine)
        with engine.connect() as conn:
            views = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.views "
                        "WHERE table_schema = 'public' AND table_name LIKE 'mi_v_cboe%'"
                    )
                )
            }
        report["views_ok"] = {"mi_v_cboe_vol_latest", "mi_v_cboe_vol_history"} <= views

        as_of = session_date()
        client = CboeClient(client_id, client_secret, point_budget=max(8, int(args.point_budget)))
        try:
            token = client.access_token()
            report["auth"] = "SUCCEEDED" if token else "FAILED"
        except CboeError as exc:
            report["auth"] = exc.capability
            report["entitlement"] = exc.capability
            report["auth_http_status"] = exc.http_status
            report["auth_error"] = str(exc)[:160]
            # WAF signature bans are UNAVAILABLE, not rejected credentials.
            report["status"] = exc.capability or "AUTH_FAILED"
            _emit(report, as_json=args.json)
            return EXIT_FAIL
        except Exception as exc:  # noqa: BLE001
            report["auth"] = "UNAVAILABLE"
            report["auth_error"] = type(exc).__name__
            report["status"] = "AUTH_FAILED"
            _emit(report, as_json=args.json)
            return EXIT_FAIL

        try:
            # Smallest live schema proof: VIX spot via documented underlying-quotes.
            payload = client.underlying_quotes(["VIX"], as_of, session_date=as_of)
            report["endpoint_status"] = "SUCCEEDED"
            report["endpoint_rows"] = len(payload) if isinstance(payload, list) else 1
            report["entitlement"] = "READY"
        except CboeError as exc:
            report["endpoint_status"] = exc.capability
            report["entitlement"] = exc.capability
            logger.info("endpoint probe capability=%s", exc.capability)

        os.environ["MI_CBOE_ENABLED"] = "1"
        ingest_report = ingest_cboe(engine, client, today=as_of)
        report["ingest"] = {
            "failed": bool(ingest_report.get("failed")),
            "sections": {
                name: section.get("status")
                for name, section in (ingest_report.get("sections") or {}).items()
            },
        }
        report["points_used"] = int(ingest_report.get("points_used") or client.points_used)
        report["requests_made"] = int(ingest_report.get("requests_made") or client.requests_made)

        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT metric_id, status, value IS NOT NULL AS has_value,
                           inputs_retrieved_max IS NOT NULL AS has_obs,
                           computed_at IS NOT NULL AS has_ingest
                    FROM mi_v_cboe_vol_latest
                    ORDER BY metric_id
                    """
                )
            ).mappings().all()
        for row in rows:
            if row["status"] == "OK" and row["has_value"]:
                report["metrics_ok"].append(row["metric_id"])
            else:
                report["metrics_incomplete"].append(
                    {"metric_id": row["metric_id"], "status": row["status"]}
                )
            if not row["has_obs"] or not row["has_ingest"]:
                report.setdefault("lineage_gaps", []).append(row["metric_id"])

        hard = report["auth"] not in {"SUCCEEDED", "READY"} or not report["views_ok"]
        if hard or not report["metrics_ok"]:
            report["status"] = "FAILED"
            _emit(report, as_json=args.json)
            return EXIT_FAIL
        report["status"] = "OK"
        if report.get("entitlement") not in {None, "READY", "SUCCEEDED"}:
            report["status"] = "PARTIAL"
        _emit(report, as_json=args.json)
        return EXIT_OK
    finally:
        if engine is not None:
            engine.dispose()
        try:
            with admin.connect() as conn:
                conn.execute(text('DROP DATABASE IF EXISTS "{0}" WITH (FORCE)'.format(child)))
        except Exception:
            pass
        admin.dispose()


def run(argv: list[str] | None = None) -> int:
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
