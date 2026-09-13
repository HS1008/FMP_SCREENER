"""Bounded read-only live TWS validation for IBKR equity EOD.

Never places orders. Never changes TWS settings. Never posts to production.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from ibkr_collector.diagnostic import probe_socket
from ibkr_collector.historical import (
    ADJUSTMENT_BASIS_ADJUSTED_LAST,
    BACKFILL_DURATION,
    DEFAULT_EOD_CLIENT_ID,
    PHASE3_SYMBOLS,
    STATUS_OK,
    WHAT_TO_SHOW_ADJUSTED,
    connect_historical_session,
    coverage_summary,
    fetch_universe,
)
from market_intelligence.calendars import CAL_NYSE, last_completed_session
from market_intelligence.taxonomy import UNIVERSE_SYMBOLS
from ibkr_collector.values import utcnow

LOCAL_TWS_PORTS = (7496, 7497, 4001, 4002)


def detect_local_tws_port() -> dict[str, Any]:
    probes = [probe_socket("127.0.0.1", port, timeout=0.5) for port in LOCAL_TWS_PORTS]
    open_ports = [row for row in probes if row.get("ok")]
    return {"probes": probes, "port": open_ports[0]["port"] if open_ports else None}


def _row_report(result) -> dict[str, Any]:
    bars = list(result.bars or [])
    dates = [b.bar_date for b in bars]
    last_close = bars[-1].close if bars else None
    qualified = result.qualified
    return {
        "symbol": result.symbol,
        "conId": qualified.con_id if qualified else None,
        "primary_exchange": qualified.primary_exchange if qualified else None,
        "status": result.status,
        "bars_returned": len(bars),
        "first_date": dates[0].isoformat() if dates else None,
        "last_date": dates[-1].isoformat() if dates else None,
        "latest_close": last_close,
        "whatToShow": result.what_to_show,
        "adjustment_basis": ADJUSTMENT_BASIS_ADJUSTED_LAST if result.what_to_show == WHAT_TO_SHOW_ADJUSTED else None,
        "error_codes": list(result.error_codes or []),
        "currency": qualified.currency if qualified else None,
    }


def run_live_fetch(symbols: list[str], *, duration: str, port: int, client_id: int = DEFAULT_EOD_CLIENT_ID) -> dict[str, Any]:
    session, client, error = connect_historical_session(host="127.0.0.1", port=port, client_id=client_id)
    if session is None:
        return {"ok": False, "handshake": False, "error": error, "port": port, "client_id": client_id}
    print(
        "handshake ok port={0} client_id={1} nextValidId={2}".format(port, client_id, getattr(client, "next_valid_id", None)),
        flush=True,
    )
    try:
        results = []
        for symbol in symbols:
            print("fetch {0} duration={1}".format(symbol, duration), flush=True)
            row = fetch_universe(session, [symbol], duration=duration, what_to_show=WHAT_TO_SHOW_ADJUSTED)[0]
            print(
                "done {0} status={1} bars={2} errors={3} last_errors={4}".format(
                    symbol,
                    row.status,
                    len(row.bars or []),
                    list(row.error_codes or []),
                    list(getattr(session.client, "errors", []) or [])[-4:],
                ),
                flush=True,
            )
            results.append(row)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    completed = last_completed_session(utcnow(), CAL_NYSE)
    rows = [_row_report(row) for row in results]
    adjusted_rejected = [row for row in results if "adjusted_last_rejected" in (row.status, row.reason or "")]
    future = [
        row.symbol
        for row in results
        for bar in row.bars
        if bar.bar_date > completed
    ]
    trades_fallback = [row.symbol for row in results if row.what_to_show != WHAT_TO_SHOW_ADJUSTED]
    summary = coverage_summary(results, requested=symbols)
    enough_200 = all(len(row.bars) >= 200 for row in results if row.status == STATUS_OK)
    enough_252 = all(len(row.bars) >= 253 for row in results if row.status == STATUS_OK)
    return {
        "ok": summary["overall"] == "FULL_SUCCESS" and not adjusted_rejected and not future and not trades_fallback,
        "handshake": True,
        "port": port,
        "client_id": client_id,
        "duration": duration,
        "completed_session": completed.isoformat(),
        "summary": summary,
        "rows": rows,
        "adjusted_last_rejected": [row.symbol for row in adjusted_rejected],
        "future_bars": future,
        "trades_fallback": trades_fallback,
        "enough_200dma": enough_200,
        "enough_252_session": enough_252,
        "orders_placed": 0,
    }


def smoke(port: int) -> dict[str, Any]:
    return run_live_fetch(list(PHASE3_SYMBOLS), duration=BACKFILL_DURATION, port=port)


def full_universe(port: int) -> dict[str, Any]:
    return run_live_fetch(list(UNIVERSE_SYMBOLS), duration=BACKFILL_DURATION, port=port)


def ingest_live_to_local_engine(port: int, engine) -> dict[str, Any]:
    """Non-production: live TWS fetch -> private ingest TestClient -> disposable PostgreSQL."""
    import uuid

    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from ibkr_ingest.app import app, set_engine_for_tests
    from market_intelligence.ibkr_eod import results_to_ingest_payload
    from market_intelligence.taxonomy import UNIVERSE_SYMBOLS as universe

    session, client, error = connect_historical_session(host="127.0.0.1", port=port, client_id=DEFAULT_EOD_CLIENT_ID)
    if session is None:
        return {"ok": False, "handshake": False, "error": error}
    try:
        results = fetch_universe(session, list(universe), duration=BACKFILL_DURATION, what_to_show=WHAT_TO_SHOW_ADJUSTED)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    summary = coverage_summary(results, requested=list(universe))
    payload = results_to_ingest_payload(results, collector_id="local-e2e", requested=list(universe), request_mode="backfill")
    batch_id = str(uuid.uuid4())
    set_engine_for_tests(engine)
    try:
        import os

        os.environ["IBKR_INGEST_TOKEN"] = "local-e2e-token"
        http = TestClient(app)
        headers = {"Authorization": "Bearer local-e2e-token"}
        bars = payload["bars"]
        chunks = [bars[i : i + 400] for i in range(0, len(bars), 400)] or [[]]
        posted = 0
        for index, chunk in enumerate(chunks, start=1):
            if not chunk:
                continue
            body = dict(payload)
            body["bars"] = chunk
            body["batch_id"] = batch_id
            body["chunk_index"] = index
            body["chunk_count"] = len(chunks)
            body["finalize"] = False
            response = http.post("/v1/equity_bars", headers=headers, json=body)
            if response.status_code != 200:
                return {"ok": False, "chunk_error": response.text, "chunk_index": index, "summary": summary}
            posted += len(chunk)
        fin = http.post(
            "/v1/equity_bars/finalize",
            headers=headers,
            json={
                "collector_id": "local-e2e",
                "batch_id": batch_id,
                "coverage": payload["coverage"],
                "request_mode": "backfill",
                "provider": "IBKR",
                "source_id": "EQUITY_EOD",
                "what_to_show": WHAT_TO_SHOW_ADJUSTED,
                "adjustment_basis": ADJUSTMENT_BASIS_ADJUSTED_LAST,
            },
        )
        if fin.status_code != 200:
            return {"ok": False, "finalize_error": fin.text, "summary": summary, "posted_bars": posted}
        with engine.connect() as conn:
            sector = conn.execute(
                text(
                    """
                    SELECT sector_key, instrument_id, as_of, source_id, research_eligible,
                           metrics_json, provenance_json, coverage_json
                    FROM mi_sector_snapshots
                    WHERE source_id='EQUITY_EOD' AND dataset='ETF_RS_VS_SPY'
                    """
                )
            ).mappings().all()
            industry = conn.execute(text("SELECT COUNT(*) FROM mi_industry_snapshots WHERE source_id='EQUITY_EOD' AND dataset='INDUSTRY_RS_VS_SECTOR_ETF'")).scalar()
            baskets = conn.execute(text("SELECT COUNT(*) FROM mi_industry_snapshots WHERE source_id='EQUITY_EOD' AND dataset='THEME_RS'")).scalar()
            provenance = conn.execute(text("SELECT provider, adjustment_basis, source_id FROM mi_market_bars WHERE source_id='EQUITY_EOD' LIMIT 1")).mappings().first()
            registry = conn.execute(text("SELECT provider, usage_scope FROM mi_source_registry WHERE source_id='EQUITY_EOD'")).mappings().first()
            freshness = conn.execute(text("SELECT transport_status, coverage_status, metadata_status FROM mi_data_freshness WHERE source_id='EQUITY_EOD' AND dataset='equity_etf_daily_bars'")).mappings().first()
            tech = next((dict(row) for row in sector if row["sector_key"] == "Technology"), None)
        metrics = (tech or {}).get("metrics_json") or (tech or {}).get("metrics") or {}
        required = [
            "ret_1d", "rs_chg_1d",
            "ret_1w", "rs_chg_1w",
            "ret_1m", "rs_chg_1m",
            "ret_3m", "rs_chg_3m",
            "ret_6m", "rs_chg_6m",
            "ret_12m", "rs_chg_12m",
            "pct_vs_50dma", "pct_vs_200dma",
        ]
        missing = [key for key in required if metrics.get(key) is None]
        return {
            "ok": not missing and summary["overall"] == "FULL_SUCCESS" and provenance and provenance["provider"] == "IBKR",
            "handshake": True,
            "posted_bars": posted,
            "chunks": len(chunks),
            "finalize": fin.json(),
            "summary": summary,
            "sector_rows": len(sector),
            "industry_rows": int(industry or 0),
            "basket_rows": int(baskets or 0),
            "technology_metrics": {key: metrics.get(key) for key in required},
            "missing_metrics": missing,
            "research_eligible": tech.get("research_eligible") if tech else None,
            "provenance": dict(provenance) if provenance else None,
            "registry": dict(registry) if registry else None,
            "freshness": dict(freshness) if freshness else None,
            "fmp_called": False,
            "orders_placed": 0,
        }
    finally:
        set_engine_for_tests(None)


def print_table(report: dict[str, Any]) -> str:
    lines = [
        "symbol conId exchange status bars first last close whatToShow basis errors",
        "-" * 100,
    ]
    for row in report.get("rows") or []:
        lines.append(
            "{symbol} {conId} {primary_exchange} {status} {bars_returned} {first_date} {last_date} {latest_close} {whatToShow} {adjustment_basis} {error_codes}".format(**row)
        )
    return "\n".join(lines)


def run_local_ingest_e2e() -> dict[str, Any]:
    """Create a disposable local database, ingest live TWS bars, then drop the database."""
    import os
    import uuid

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from jobs.apply_migrations import apply_migrations

    admin_url = (os.environ.get("FMP_TEST_DATABASE_URL") or "").strip()
    if not admin_url:
        raise SystemExit("FMP_TEST_DATABASE_URL is required for --ingest-local")
    detected = detect_local_tws_port()
    if not detected.get("port"):
        return {"ok": False, "error": "no local TWS port", "detected": detected}
    name = "fmp_ibkr_e2e_" + uuid.uuid4().hex[:8]
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        leftovers = conn.execute(text("SELECT datname FROM pg_database WHERE datname LIKE 'fmp_ibkr_e2e_%'")).scalars().all()
        for leftover in leftovers:
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n AND pid <> pg_backend_pid()"), {"n": leftover})
            conn.execute(text('DROP DATABASE IF EXISTS "{0}"'.format(leftover)))
        conn.execute(text('CREATE DATABASE "{0}"'.format(name)))
    engine = create_engine(str(make_url(admin_url).set(database=name).render_as_string(hide_password=False)), future=True, pool_pre_ping=True)
    try:
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
        report = ingest_live_to_local_engine(int(detected["port"]), engine)
        report["database"] = name
        report["detected"] = detected
        return report
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n AND pid <> pg_backend_pid()"), {"n": name})
            conn.execute(text('DROP DATABASE IF EXISTS "{0}"'.format(name)))
        admin.dispose()


if __name__ == "__main__":
    import sys

    detected = detect_local_tws_port()
    print(json.dumps({"detected": detected}, default=str))
    if not detected["port"]:
        raise SystemExit(2)
    if "--ingest-local" in sys.argv:
        report = run_local_ingest_e2e()
        print(json.dumps(report, default=str, indent=2, sort_keys=True))
        raise SystemExit(0 if report.get("ok") else 1)
    report = smoke(detected["port"])
    print(print_table(report))
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, default=str, indent=2, sort_keys=True))
    raise SystemExit(0 if report.get("ok") else 1)
