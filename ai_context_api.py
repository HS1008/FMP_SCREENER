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
* Every /v1/context/* section is served from the latest *published, frozen* morning snapshot
  so all routes share one provenance (``provenance.kind = FROZEN_SNAPSHOT``); the only live
  read is ``/v1/context/data-health/live`` and it says so (``LIVE_VIEW`` with its own capture
  time). Every payload passes the source-aware export policy and the complete response is
  hashed (``export_sha256`` covers every field except itself).
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from market_intelligence.export_policy import build_envelope, finalize_envelope
from market_intelligence.nulls import normalize_payload
from market_intelligence.read_models import data_health_context, morning_latest, snapshot_age
from market_intelligence.readonly_db import ReadOnlyUnavailable, probe_readonly, readonly_connection

logger = logging.getLogger("ai_context_api")

TOKEN_ENV = "AI_CONTEXT_API_TOKEN"
API_VERSION = "v1"

# Response contract (every /v1/context route). All fields are built BEFORE hashing:
#   export_schema_version, provenance{kind, source_snapshot_hash, snapshot_id, cutoff_at, ...},
#   source_snapshot_hash, export_filtered, restricted_entries, body, section, available,
#   unavailable_reason, degraded, latest_snapshot, delivery_health, export_sha256.
# export_sha256 = SHA-256(canonical JSON of the envelope without export_sha256).
SECTION_ROUTES = {
    "macro": "macro",
    "rates": "rates",
    "credit": "credit",
    "sectors": "sectors",
    "liquidity": "liquidity",
    "market": "market",
    "strategies": "strategy_monitor_summary",
    "data-health": "data_health",
    "order-flow": "order_flow",
}

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
def ready() -> JSONResponse:
    """Readiness = every curated view the API serves is readable through the read-only role."""
    probe = probe_readonly()
    body = {"api": API_VERSION, "database": probe, "token_configured": True, "ready": probe.get("status") == "OK"}
    return JSONResponse(status_code=200 if body["ready"] else 503, content=normalize_payload(body))


def _snapshot_meta(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot:
        return None
    return {k: snapshot.get(k) for k in ("snapshot_id", "schema_version", "generated_at", "cutoff_at", "as_of_date", "completeness", "snapshot_sha256", "content_sha256", "quality_status")}


def _provenance(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {"kind": "UNPUBLISHED", "source_snapshot_hash": None, "snapshot_id": None, "cutoff_at": None, "schema_version": None}
    return {
        "kind": "FROZEN_SNAPSHOT",
        "source_snapshot_hash": snapshot.get("snapshot_sha256"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "cutoff_at": snapshot.get("cutoff_at"),
        "generated_at": snapshot.get("generated_at"),
        "schema_version": snapshot.get("schema_version"),
        "quality_status": snapshot.get("quality_status"),
    }


def _load_snapshot() -> dict[str, Any] | None:
    try:
        with readonly_connection() as conn:
            return morning_latest(conn)
    except ReadOnlyUnavailable as exc:
        raise HTTPException(status_code=503, detail={"status": exc.reason, "message": "read-only database unavailable"}) from None


def _respond(section: str | None, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Build the complete envelope from the frozen snapshot, then hash it. Nothing is added later."""
    delivery = snapshot_age(snapshot)
    if not snapshot:
        envelope = build_envelope(
            None,
            provenance=_provenance(None),
            section=section,
            available=False,
            unavailable_reason="no published morning_context snapshot",
            degraded=True,
            latest_snapshot=None,
            delivery_health=delivery,
        )
        return finalize_envelope(envelope)
    body = snapshot.get("snapshot_json") or {}
    if section is None:
        data: Any = body
        section_status = None
        available = body.get("completeness") in ("COMPLETE", "PARTIAL")
        reason = None if available else "snapshot has no available sections"
    else:
        sec = (body.get("sections") or {}).get(section) or {}
        section_status = (body.get("sections_status") or {}).get(section) or {"status": sec.get("status"), "reason": sec.get("reason")}
        data = sec.get("data")
        available = sec.get("status") in ("OK", "PARTIAL", "STALE") and data is not None
        reason = None if available else (sec.get("reason") or "section not available in the published snapshot")
    degraded = (body.get("completeness") != "COMPLETE") or (section_status is not None and section_status.get("status") != "OK") or delivery.get("snapshot_age_status") != "CURRENT"
    envelope = build_envelope(
        data if available else None,
        provenance=_provenance(snapshot),
        section=section,
        section_status=section_status,
        available=available,
        unavailable_reason=reason,
        degraded=bool(degraded),
        latest_snapshot=_snapshot_meta(snapshot),
        delivery_health=delivery,
    )
    return finalize_envelope(envelope)


@app.get("/v1/context/morning/latest", dependencies=[Depends(require_token)])
def morning_latest_route() -> dict[str, Any]:
    return _respond(None, _load_snapshot())


def _section_route(section: str) -> Callable[[], dict[str, Any]]:
    def route() -> dict[str, Any]:
        return _respond(section, _load_snapshot())

    route.__name__ = "{0}_route".format(section)
    return route


for _path, _section in SECTION_ROUTES.items():
    app.add_api_route("/v1/context/{0}/latest".format(_path), _section_route(_section), methods=["GET"], dependencies=[Depends(require_token)])


@app.get("/v1/context/data-health/live", dependencies=[Depends(require_token)])
@app.get("/v1/context/data-health", dependencies=[Depends(require_token)])  # v1 alias; was a live query mislabelled with the snapshot hash
def data_health_live_route() -> dict[str, Any]:
    """Explicit LIVE view: current source health evaluated now; its provenance is its own capture."""
    try:
        with readonly_connection() as conn:
            captured_at = conn.execute(text("SELECT transaction_timestamp()")).scalar()
            health = data_health_context(conn)
            snapshot = morning_latest(conn)
    except ReadOnlyUnavailable as exc:
        raise HTTPException(status_code=503, detail={"status": exc.reason, "message": "read-only database unavailable"}) from None
    available = bool(health.get("sources"))
    provenance = {
        "kind": "LIVE_VIEW",
        "source_snapshot_hash": None,
        "captured_at": captured_at,
        "views": ["mi_v_source_health", "mi_v_macro_quarantine_summary"],
        "note": "Live read of curated views inside one READ ONLY transaction; not derived from a morning snapshot.",
    }
    envelope = build_envelope(
        health if available else None,
        provenance=provenance,
        section="data_health_live",
        available=available,
        unavailable_reason=None if available else "no sources registered",
        degraded=not available,
        latest_snapshot=_snapshot_meta(snapshot),
        delivery_health=snapshot_age(snapshot),
    )
    return finalize_envelope(envelope)


def main() -> None:  # pragma: no cover - operator entrypoint
    import uvicorn

    host = os.environ.get("AI_CONTEXT_API_HOST", "127.0.0.1")
    port = int(os.environ.get("AI_CONTEXT_API_PORT", "8765"))
    uvicorn.run("ai_context_api:app", host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":  # pragma: no cover
    main()
