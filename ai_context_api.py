"""Read-only AI context API (separate from the FMP-backed ``api_app.py``).

    AI_CONTEXT_API_TOKEN=... uvicorn ai_context_api:app --host 127.0.0.1 --port 8765

Security model
--------------
* Bearer token required for every ``/v1`` route; constant-time comparison; the server
  fails closed (503) when ``AI_CONTEXT_API_TOKEN`` is unset. Tokens never appear in URLs
  or logs.
* Reads only curated ``mi_v_*`` views through the read-only role
  (``DATABASE_READONLY_URL``). No writer fallback, no SQL executor, no provider proxy,
  no mutating routes, no idea-write API, no QC or order endpoints.
* Docs/schema routes are disabled. CORS is disabled by default (no origins).
* Bind to localhost by default and front with a private/TLS reverse proxy if needed.
* Every payload passes the source-aware export policy (restricted ICE series are redacted
  with reasons) and carries its own export hash plus the source snapshot hash.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from market_intelligence.export_policy import export_safe_payload
from market_intelligence.nulls import normalize_payload
from market_intelligence.read_models import (
    credit_context,
    data_health_context,
    macro_context,
    morning_latest,
    rates_context,
    sectors_context,
    strategies_context,
)
from market_intelligence.readonly_db import ReadOnlyUnavailable, probe_readonly, readonly_connection

logger = logging.getLogger("ai_context_api")

TOKEN_ENV = "AI_CONTEXT_API_TOKEN"
API_VERSION = "v1"

app = FastAPI(title="Market Intelligence AI Context API", docs_url=None, redoc_url=None, openapi_url=None)


def _server_token() -> str | None:
    token = (os.environ.get(TOKEN_ENV) or "").strip()
    return token or None


def require_token(request: Request) -> None:
    server_token = _server_token()
    if server_token is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="server token not configured")
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    if not hmac.compare_digest(presented.strip().encode("utf-8"), server_token.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


@app.exception_handler(Exception)
async def _sanitized_errors(_request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled %s", exc.__class__.__name__)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.middleware("http")
async def _reject_mutations(request: Request, call_next: Callable):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        return JSONResponse(status_code=405, content={"detail": "read-only API"})
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness only: no configuration, credentials, or data disclosure."""
    return {"status": "ok"}


@app.get("/{version}/ready".format(version=API_VERSION), dependencies=[Depends(require_token)])
def ready() -> dict[str, Any]:
    return {"api": API_VERSION, "database": probe_readonly(), "token_configured": True}


def _context(builder: Callable[[Any], dict[str, Any]], section: str) -> dict[str, Any]:
    try:
        with readonly_connection() as conn:
            body = builder(conn)
            snapshot = morning_latest(conn)
    except ReadOnlyUnavailable as exc:
        raise HTTPException(status_code=503, detail={"status": exc.reason, "message": "read-only database unavailable"}) from None
    available = bool(body) and any(v not in (None, [], {}) for v in body.values())
    envelope = export_safe_payload(
        {"section": section, "available": available, "data": body if available else None, "unavailable_reason": None if available else "no published data for this section"},
        source_snapshot_hash=(snapshot or {}).get("snapshot_sha256"),
    )
    envelope["latest_snapshot"] = _snapshot_meta(snapshot)
    return normalize_payload(envelope)


def _snapshot_meta(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot:
        return None
    return {k: snapshot.get(k) for k in ("snapshot_id", "schema_version", "generated_at", "cutoff_at", "as_of_date", "completeness", "snapshot_sha256")}


@app.get("/v1/context/morning/latest", dependencies=[Depends(require_token)])
def morning_latest_route() -> dict[str, Any]:
    try:
        with readonly_connection() as conn:
            snapshot = morning_latest(conn)
    except ReadOnlyUnavailable as exc:
        raise HTTPException(status_code=503, detail={"status": exc.reason, "message": "read-only database unavailable"}) from None
    if not snapshot:
        return {"available": False, "unavailable_reason": "no published morning_context snapshot", "export_schema_version": "export_safe_v1"}
    body = snapshot.get("snapshot_json") or {}
    envelope = export_safe_payload(body, source_snapshot_hash=snapshot.get("snapshot_sha256"))
    envelope["available"] = True
    envelope["latest_snapshot"] = _snapshot_meta(snapshot)
    envelope["degraded"] = snapshot.get("completeness") != "COMPLETE"
    return normalize_payload(envelope)


@app.get("/v1/context/macro/latest", dependencies=[Depends(require_token)])
def macro_route() -> dict[str, Any]:
    return _context(macro_context, "macro")


@app.get("/v1/context/rates/latest", dependencies=[Depends(require_token)])
def rates_route() -> dict[str, Any]:
    return _context(rates_context, "rates")


@app.get("/v1/context/credit/latest", dependencies=[Depends(require_token)])
def credit_route() -> dict[str, Any]:
    return _context(credit_context, "credit")


@app.get("/v1/context/sectors/latest", dependencies=[Depends(require_token)])
def sectors_route() -> dict[str, Any]:
    return _context(sectors_context, "sectors")


@app.get("/v1/context/strategies/latest", dependencies=[Depends(require_token)])
def strategies_route() -> dict[str, Any]:
    return _context(strategies_context, "strategies")


@app.get("/v1/context/data-health", dependencies=[Depends(require_token)])
def data_health_route() -> dict[str, Any]:
    return _context(data_health_context, "data_health")


def main() -> None:  # pragma: no cover - operator entrypoint
    import uvicorn

    host = os.environ.get("AI_CONTEXT_API_HOST", "127.0.0.1")
    port = int(os.environ.get("AI_CONTEXT_API_PORT", "8765"))
    uvicorn.run("ai_context_api:app", host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":  # pragma: no cover
    main()
