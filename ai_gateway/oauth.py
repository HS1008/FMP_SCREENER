"""Minimal OAuth 2.1 authorization server for ChatGPT / MCP discovery.

Single-owner: the resource owner authenticates by entering the existing
``AI_CONTEXT_API_TOKEN``. No user database. PKCE S256 required.
"""

from __future__ import annotations

import hashlib
import html
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ai_gateway.auth import mint_access_token, server_token
from ai_gateway.config import is_loopback_host, oauth_enabled, public_base_url
from ai_gateway.logging import log_event

router = APIRouter(tags=["oauth"])

_clients: dict[str, dict[str, Any]] = {}
_codes: dict[str, dict[str, Any]] = {}
CODE_TTL_SECONDS = 300
TOKEN_TTL_SECONDS = 3600
MAX_REDIRECTS = 8


def normalize_redirect_uri(raw: object) -> str | None:
    """Exact registered callback. No wildcards, fragments, or arbitrary remote http."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or "*" in text or "\\" in text or any(ch.isspace() for ch in text):
        return None
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"https", "http"}:
        return None
    if parsed.fragment or parsed.username or parsed.password or not parsed.netloc:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if scheme == "http" and not is_loopback_host(host):
        return None
    if scheme == "https" and ("*" in host or host.startswith(".")):
        return None
    netloc = parsed.netloc.lower()
    path = parsed.path or ""
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def registered_redirects(client_id: str) -> list[str]:
    client = _clients.get(client_id) or {}
    return list(client.get("redirect_uris") or [])


def redirect_is_registered(client_id: str, redirect_uri: str) -> bool:
    normalized = normalize_redirect_uri(redirect_uri)
    if normalized is None:
        return False
    allowed = registered_redirects(client_id)
    return bool(allowed) and normalized in allowed


def issuer(request: Request) -> str:
    configured = public_base_url()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def resource_url(request: Request) -> str:
    return issuer(request) + "/mcp"


def _purge() -> None:
    now = time.time()
    expired = [key for key, value in _codes.items() if value["exp"] <= now]
    for key in expired:
        _codes.pop(key, None)


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    import base64

    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@router.get("/.well-known/oauth-protected-resource")
def protected_resource_metadata(request: Request) -> dict[str, Any]:
    base = issuer(request)
    return {
        "resource": resource_url(request),
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mi.read"],
        "resource_documentation": base + "/api/v1/openapi.json",
    }


@router.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata(request: Request) -> dict[str, Any]:
    base = issuer(request)
    return {
        "issuer": base,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mi.read"],
        "service_documentation": base + "/api/v1/openapi.json",
    }


@router.post("/oauth/register")
async def register_client(request: Request) -> JSONResponse:
    if not oauth_enabled():
        return JSONResponse({"error": "unauthorized_client"}, status_code=400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    client_id = "mcp_" + secrets.token_urlsafe(16)
    redirects = body.get("redirect_uris") or []
    if not isinstance(redirects, list) or not redirects:
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    if len(redirects) > MAX_REDIRECTS:
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in redirects:
        normalized = normalize_redirect_uri(item)
        if normalized is None:
            return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(normalized)
    if not cleaned:
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    _clients[client_id] = {
        "client_id": client_id,
        "redirect_uris": cleaned,
        "client_name": str(body.get("client_name") or "mcp-client"),
        "token_endpoint_auth_method": "none",
    }
    log_event("oauth_register", client_name=_clients[client_id]["client_name"], status="success")
    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": _clients[client_id]["redirect_uris"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        },
        status_code=201,
    )


def _authorize_page(*, error: str | None = None, **fields: str) -> HTMLResponse:
    message = "<p class='err'>{0}</p>".format(html.escape(error)) if error else ""
    hidden = "".join(
        "<input type='hidden' name='{0}' value='{1}' />".format(html.escape(key), html.escape(value))
        for key, value in fields.items()
        if value
    )
    body = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>FMP Market Intelligence</title>
<style>
body {{ font-family: sans-serif; max-width: 32rem; margin: 3rem auto; color: #111; }}
label {{ display: block; margin: 1rem 0 0.3rem; }}
input[type=password] {{ width: 100%; padding: 0.5rem; }}
button {{ margin-top: 1rem; padding: 0.5rem 1rem; }}
.err {{ color: #a00; }}
.note {{ color: #444; font-size: 0.9rem; }}
</style></head>
<body>
<h1>FMP Market Intelligence</h1>
<p>Enter the owner API token to allow this client read-only market data access.</p>
{message}
<form method="post" action="/oauth/authorize">
{hidden}
<label for="token">API token</label>
<input id="token" name="token" type="password" autocomplete="off" required />
<button type="submit">Authorize read-only access</button>
</form>
<p class="note">This grants read-only tools. It cannot trade, launch backtests, or open the Stage 2 holdout.</p>
</body></html>""".format(message=message, hidden=hidden)
    return HTMLResponse(body)


@router.get("/oauth/authorize")
def authorize_get(
    request: Request,
    response_type: str = "",
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    scope: str = "",
    resource: str = "",
) -> HTMLResponse:
    if not oauth_enabled():
        return _authorize_page(error="OAuth is disabled on this host.")
    if response_type != "code" or not client_id or not redirect_uri or not code_challenge:
        return _authorize_page(error="This authorization request is missing required OAuth parameters.")
    if code_challenge_method and code_challenge_method != "S256":
        return _authorize_page(error="Only PKCE S256 is supported.")
    if not redirect_is_registered(client_id, redirect_uri):
        return _authorize_page(error="redirect_uri is not registered for this client.")
    return _authorize_page(
        response_type=response_type,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method or "S256",
        scope=scope or "mi.read",
        resource=resource or resource_url(request),
    )


@router.post("/oauth/authorize")
def authorize_post(
    request: Request,
    token: str = Form(""),
    response_type: str = Form(""),
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
    state: str = Form(""),
    code_challenge: str = Form(""),
    code_challenge_method: str = Form("S256"),
    scope: str = Form("mi.read"),
    resource: str = Form(""),
):
    configured = server_token()
    if configured is None or not oauth_enabled():
        return _authorize_page(error="Authorization is not configured.")
    if not redirect_is_registered(client_id, redirect_uri):
        return _authorize_page(error="redirect_uri is not registered for this client.")
    normalized = normalize_redirect_uri(redirect_uri)
    if normalized is None:
        return _authorize_page(error="redirect_uri is not registered for this client.")
    import hmac as hmac_mod

    presented = token.strip().encode("utf-8")
    expected = configured.encode("utf-8")
    if not presented or len(presented) != len(expected) or not hmac_mod.compare_digest(presented, expected):
        log_event("oauth_authorize", status="denied")
        return _authorize_page(
            error="The token was not accepted.",
            response_type=response_type,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scope=scope,
            resource=resource,
        )
    _purge()
    code = secrets.token_urlsafe(24)
    _codes[code] = {
        "exp": time.time() + CODE_TTL_SECONDS,
        "client_id": client_id,
        "redirect_uri": normalized,
        "code_challenge": code_challenge,
        "resource": resource or resource_url(request),
        "scope": scope or "mi.read",
    }
    log_event("oauth_authorize", status="success")
    query = {"code": code}
    if state:
        query["state"] = state
    target = normalized + ("&" if "?" in normalized else "?") + urlencode(query)
    return RedirectResponse(target, status_code=302)


@router.post("/oauth/token")
async def token_endpoint(request: Request) -> JSONResponse:
    _purge()
    form = await request.form()
    grant = str(form.get("grant_type") or "")
    code = str(form.get("code") or "")
    redirect_uri = str(form.get("redirect_uri") or "")
    client_id = str(form.get("client_id") or "")
    verifier = str(form.get("code_verifier") or "")
    resource = str(form.get("resource") or "")
    record = _codes.pop(code, None)
    if grant != "authorization_code" or record is None:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    # client_id is required and must exactly match the authorization-code binding.
    # An omitted or substituted client_id is invalid_grant, not a skipped check.
    if not client_id or client_id not in _clients or record["client_id"] != client_id:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if record["redirect_uri"] != normalize_redirect_uri(redirect_uri):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if not verifier or _s256(verifier) != record["code_challenge"]:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    # RFC 8707: the token audience is THIS gateway's MCP resource. A client-supplied
    # ``resource`` may only restate it; anything else is refused instead of minted.
    expected_resource = resource_url(request)
    for candidate in (resource, record.get("resource")):
        if candidate and candidate.rstrip("/") not in {expected_resource, expected_resource.rstrip("/"), issuer(request)}:
            log_event("oauth_token", status="denied", reason="invalid_target")
            return JSONResponse({"error": "invalid_target"}, status_code=400)
    access = mint_access_token(audience=expected_resource, ttl_seconds=TOKEN_TTL_SECONDS, scope=record["scope"])
    log_event("oauth_token", status="success")
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL_SECONDS,
            "scope": record["scope"],
        }
    )
