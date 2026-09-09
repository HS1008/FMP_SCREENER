"""Private authenticated IBKR market-data ingest API.

Binds to a private/Tailscale address. Writes only approved IBKR quote and heartbeat
records through the mi_ibkr_ingest role. Not the AI Context API; no arbitrary SQL.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request

from ibkr_ingest.validate import PayloadError, validate_heartbeat, validate_quote_batch

logger = logging.getLogger("ibkr_ingest")

TOKEN_ENV = "IBKR_INGEST_TOKEN"
DB_URL_ENV = "IBKR_INGEST_DATABASE_URL"

app = FastAPI(title="IBKR private ingest", docs_url=None, redoc_url=None, openapi_url=None)

_engine = None


def _token() -> str | None:
    token = (os.environ.get(TOKEN_ENV) or "").strip()
    return token or None


def require_token(request: Request) -> None:
    server = _token()
    if server is None:
        raise HTTPException(status_code=503, detail="server token not configured")
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(status_code=401, detail="unauthorized")
    if not hmac.compare_digest(presented.strip().encode("utf-8"), server.encode("utf-8")):
        raise HTTPException(status_code=401, detail="unauthorized")


def ingest_engine():
    global _engine
    if _engine is not None:
        return _engine
    url = (os.environ.get(DB_URL_ENV) or "").strip()
    if not url:
        raise HTTPException(status_code=503, detail="ingest database not configured")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg2" not in url:
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    from sqlalchemy import create_engine

    _engine = create_engine(url, pool_pre_ping=True, future=True, pool_size=2, max_overflow=2)
    return _engine


def set_engine_for_tests(engine) -> None:
    global _engine
    _engine = engine


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/heartbeat", dependencies=[Depends(require_token)])
def heartbeat(body: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = validate_heartbeat(body)
    except PayloadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    from market_intelligence.ibkr_store import upsert_heartbeat

    with ingest_engine().begin() as conn:
        upsert_heartbeat(conn, payload)
    return {"ok": True, "collector_id": payload["collector_id"]}


@app.post("/v1/quotes", dependencies=[Depends(require_token)])
def quotes(body: dict[str, Any]) -> dict[str, Any]:
    try:
        collector_id, records, pre_rejected = validate_quote_batch(body)
    except PayloadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    from market_intelligence.ibkr_store import ingest_quotes

    counts = {"received": len(records) + len(pre_rejected), "inserted": 0, "unchanged": 0, "rejected": len(pre_rejected), "results": list(pre_rejected), "run_id": None}
    if records:
        with ingest_engine().begin() as conn:
            stored = ingest_quotes(conn, records, collector_id=collector_id)
        counts["inserted"] = stored.get("inserted", 0)
        counts["unchanged"] = stored.get("unchanged", 0)
        counts["rejected"] = stored.get("rejected", 0) + len(pre_rejected)
        counts["results"] = list(stored.get("results") or []) + list(pre_rejected)
        counts["run_id"] = stored.get("run_id")
        counts["received"] = stored.get("received", len(records)) + len(pre_rejected)
    return {
        "ok": True,
        "collector_id": collector_id,
        "received": counts["received"],
        "inserted": counts["inserted"],
        "unchanged": counts["unchanged"],
        "rejected": counts["rejected"],
        "committed": counts["inserted"],
        "duplicate": counts["unchanged"],
        "results": counts["results"],
    }
