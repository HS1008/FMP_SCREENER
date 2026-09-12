"""FastAPI routes that call the same service layer as MCP tools.

Every request installs a transport context (``ai_gateway.context``) before any tool runs so
the export policy can tell a proven local owner session from a remote client. The default
for anything reaching this module over HTTP is the restrictive ``external`` policy.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ai_gateway import SCHEMA_VERSION, SERVER_NAME
from ai_gateway.auth import authenticate
from ai_gateway.config import MAX_REQUEST_BYTES, bind_host, cors_origins, public_base_url
from ai_gateway.context import export_policy_for_request, http_context, reset_context, set_context
from ai_gateway.envelope import public_payload
from ai_gateway.errors import GatewayError, PAYLOAD_TOO_LARGE
from ai_gateway.logging import new_request_id, timed_call
from ai_gateway.mcp_protocol import handle_rpc, parse_messages, tools_list
from ai_gateway.oauth import issuer
from ai_gateway.rate_limit import enforce
from ai_gateway.services import invoke
from market_intelligence.readonly_db import probe_readonly

router = APIRouter()
v1 = APIRouter(prefix="/api/v1")


def require_auth(request: Request) -> dict[str, Any]:
    audience = public_base_url() or issuer(request)
    return authenticate(request.headers.get("authorization"), audience=audience + "/mcp")


def _client_identity(request: Request, auth: dict[str, Any]) -> str:
    fingerprint = auth.get("token_fp")
    if fingerprint:
        return str(fingerprint)
    return request.client.host if request.client else "unknown"


def _request_context(request: Request):
    return http_context(
        client_host=request.client.host if request.client else None,
        headers=request.headers,
        bind=bind_host(),
    )


def _call(tool: str, request: Request, auth: dict[str, Any], arguments: dict[str, Any] | None = None) -> JSONResponse:
    request_id = request.headers.get("x-request-id") or new_request_id()
    enforce(_client_identity(request, auth))
    token = set_context(_request_context(request))
    try:
        policy = export_policy_for_request()
        with timed_call("gateway_tool", tool=tool, request_id=request_id, auth=auth.get("method"), export_mode=policy["export_mode"], transport=policy["transport"]) as extra:
            envelope = invoke(tool, arguments)
            extra["rows"] = envelope.get("_row_count")
            extra["response_chars"] = envelope.get("_response_chars")
            extra["status"] = "success" if envelope.get("available") else "unavailable"
    finally:
        reset_context(token)
    payload = public_payload(envelope)
    return JSONResponse(payload, headers={"X-Request-ID": request_id, "Cache-Control": "no-store"})


@v1.get("/context/morning")
def morning(request: Request, auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_morning_context", request, auth)


@v1.get("/markets/pulse")
def pulse(request: Request, auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_market_pulse", request, auth)


@v1.get("/rates")
def rates(request: Request, auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_rates_curve", request, auth)


@v1.get("/macro")
def macro(request: Request, auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_macro_overview", request, auth)


@v1.get("/macro/{series_id}")
def macro_series(
    series_id: str,
    request: Request,
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
    auth: dict[str, Any] = Depends(require_auth),
) -> JSONResponse:
    return _call("get_macro_series", request, auth, {"series_id": series_id, "start_date": start_date, "end_date": end_date, "limit": limit})


@v1.get("/credit")
def credit(request: Request, auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_credit_overview", request, auth)


@v1.get("/sectors")
def sectors(request: Request, auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_sector_rotation", request, auth)


@v1.get("/sectors/{sector}")
def sector_detail(sector: str, request: Request, auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_sector_detail", request, auth, {"sector": sector})


@v1.get("/industries")
def industries(request: Request, sector: str | None = Query(default=None), auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_industry_rotation", request, auth, {"sector": sector} if sector else {})


@v1.get("/subindustries")
def subindustries(
    request: Request,
    sector: str | None = Query(default=None),
    industry: str | None = Query(default=None),
    auth: dict[str, Any] = Depends(require_auth),
) -> JSONResponse:
    args: dict[str, Any] = {}
    if sector:
        args["sector"] = sector
    if industry:
        args["industry"] = industry
    return _call("get_subindustry_rotation", request, auth, args)


@v1.get("/order-flow")
def order_flow(request: Request, include_history: bool = Query(default=False), auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_order_flow", request, auth, {"include_history": include_history})


@v1.get("/strategies")
def strategies(request: Request, strategy: str | None = Query(default=None), auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_strategy_summary", request, auth, {"strategy": strategy} if strategy else {})


@v1.get("/strategies/{strategy}")
def strategy_detail(strategy: str, request: Request, auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_strategy_summary", request, auth, {"strategy": strategy})


@v1.get("/strategies/{strategy}/oos")
def strategy_oos(strategy: str, request: Request, auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_strategy_oos_windows", request, auth, {"strategy": strategy})


@v1.get("/strategies/{strategy}/experiments")
def strategy_experiments(strategy: str, request: Request, auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_strategy_experiments", request, auth, {"strategy": strategy})


@v1.get("/data-health")
def data_health(request: Request, auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_data_health", request, auth)


@v1.get("/changes")
def changes(request: Request, since: str = Query(default="previous_session"), auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    return _call("get_market_changes", request, auth, {"since": since})


@v1.get("/tools")
def list_tools(request: Request, auth: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    enforce(_client_identity(request, auth))
    token = set_context(_request_context(request))
    try:
        policy = export_policy_for_request()
    finally:
        reset_context(token)
    return {"server": SERVER_NAME, "schema_version": SCHEMA_VERSION, "export_policy": policy, "tools": tools_list()}


@v1.get("/openapi.json")
def openapi_document(request: Request) -> dict[str, Any]:
    """Unauthenticated schema for Custom GPT / client setup. No data."""
    base = public_base_url() or str(request.base_url).rstrip("/")
    paths = {
        "/health": {"get": {"summary": "Liveness", "security": []}},
        "/ready": {"get": {"summary": "Readiness (authenticated)"}},
        "/api/v1/context/morning": {"get": {"summary": "Morning context"}},
        "/api/v1/markets/pulse": {"get": {"summary": "Market pulse"}},
        "/api/v1/rates": {"get": {"summary": "Rates curve (Treasury XML preferred, FRED fallback, same-date legs)"}},
        "/api/v1/macro": {"get": {"summary": "Macro overview"}},
        "/api/v1/macro/{series_id}": {"get": {"summary": "Macro series history"}},
        "/api/v1/credit": {"get": {"summary": "Credit overview"}},
        "/api/v1/sectors": {"get": {"summary": "Sector rotation (ret_1d, rs_1d, longer windows)"}},
        "/api/v1/sectors/{sector}": {"get": {"summary": "Sector detail"}},
        "/api/v1/industries": {"get": {"summary": "Industry ETF comparisons vs sector ETF"}},
        "/api/v1/subindustries": {"get": {"summary": "Curated current-context subgroups (not GICS)"}},
        "/api/v1/order-flow": {"get": {"summary": "FINRA TRACE aggregates"}},
        "/api/v1/strategies": {"get": {"summary": "Strategy summaries (holdout fail-closed)"}},
        "/api/v1/strategies/{strategy}": {"get": {"summary": "One strategy summary"}},
        "/api/v1/strategies/{strategy}/oos": {"get": {"summary": "Non-holdout OOS windows"}},
        "/api/v1/strategies/{strategy}/experiments": {"get": {"summary": "Non-holdout experiments and artifact status"}},
        "/api/v1/data-health": {"get": {"summary": "Data freshness (policy v2)"}},
        "/api/v1/changes": {"get": {"summary": "What changed"}},
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": SERVER_NAME, "version": SCHEMA_VERSION, "description": "Read-only Market Intelligence API. No SQL, no writes, no execution, no holdout."},
        "servers": [{"url": base}],
        "paths": paths,
        "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
        "security": [{"bearerAuth": []}],
    }


@router.get("/ready")
def ready_alias(request: Request, auth: dict[str, Any] = Depends(require_auth)) -> JSONResponse:
    probe = probe_readonly()
    token = set_context(_request_context(request))
    try:
        policy = export_policy_for_request()
    finally:
        reset_context(token)
    body = {
        "api": "v1",
        "database": probe,
        "token_configured": True,
        "ready": probe.get("status") == "OK",
        "gateway": SCHEMA_VERSION,
        "export_policy": policy,
    }
    return JSONResponse(body, status_code=200 if body["ready"] else 503, headers={"Cache-Control": "no-store"})


async def mcp_endpoint(request: Request) -> JSONResponse:
    if request.method == "GET":
        return JSONResponse({"error": "method_not_allowed", "message": "Use POST for MCP Streamable HTTP"}, status_code=405)
    audience = public_base_url() or issuer(request)
    auth = authenticate(request.headers.get("authorization"), audience=audience + "/mcp")
    enforce(_client_identity(request, auth))
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        raise GatewayError(PAYLOAD_TOO_LARGE, "request body too large", http_status=413)
    messages = parse_messages(raw)
    request_id = request.headers.get("x-request-id") or new_request_id()
    responses = []
    token = set_context(_request_context(request))
    try:
        policy = export_policy_for_request()
        for message in messages:
            method = message.get("method")
            with timed_call("mcp", tool=method, request_id=request_id, auth=auth.get("method"), export_mode=policy["export_mode"], transport=policy["transport"]) as extra:
                reply = handle_rpc(message)
                extra["status"] = "success" if reply and "result" in reply else "error"
            if reply is not None:
                responses.append(reply)
    finally:
        reset_context(token)
    if len(responses) == 1:
        return JSONResponse(responses[0], headers={"X-Request-ID": request_id, "Cache-Control": "no-store"})
    return JSONResponse(responses, headers={"X-Request-ID": request_id, "Cache-Control": "no-store"})


def attach(app) -> None:
    """Mount gateway routes on an existing FastAPI app."""
    from ai_gateway.oauth import router as oauth_router

    app.include_router(oauth_router)
    app.include_router(v1)
    app.include_router(router)
    app.add_api_route("/mcp", mcp_endpoint, methods=["GET", "POST"], include_in_schema=False)

    origins = cors_origins()
    if origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(CORSMiddleware, allow_origins=list(origins), allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type"])

    @app.exception_handler(GatewayError)
    async def _gateway_errors(_request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(exc.as_dict(), status_code=exc.http_status, headers={"Cache-Control": "no-store"})


def create_app():
    """Standalone app used by ``python -m ai_gateway`` (also mounted into ``ai_context_api``)."""
    from fastapi import FastAPI

    app = FastAPI(title=SERVER_NAME, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    attach(app)
    return app
