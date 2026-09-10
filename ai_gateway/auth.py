"""Bearer and OAuth access-token authentication. Fail closed when the server token is unset."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from ai_gateway.config import server_token
from ai_gateway.errors import UNAUTHORIZED, GatewayError
from ai_gateway.logging import fingerprint


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def signing_key() -> bytes:
    token = server_token()
    if not token:
        raise GatewayError(UNAUTHORIZED, "server token not configured", http_status=503)
    return hashlib.sha256(("ai-gateway-oauth|" + token).encode("utf-8")).digest()


def mint_access_token(*, subject: str = "owner", audience: str, ttl_seconds: int = 3600, scope: str = "mi.read") -> str:
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    payload = _b64url(
        json.dumps(
            {"sub": subject, "aud": audience, "iat": now, "exp": now + ttl_seconds, "scope": scope, "token_use": "access"},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signing_input = "{0}.{1}".format(header, payload).encode("ascii")
    sig = _b64url(hmac.new(signing_key(), signing_input, hashlib.sha256).digest())
    return "{0}.{1}.{2}".format(header, payload, sig)


def verify_access_token(token: str, *, audience: str | None = None) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise GatewayError(UNAUTHORIZED, "unauthorized", http_status=401)
    signing_input = "{0}.{1}".format(parts[0], parts[1]).encode("ascii")
    expected = hmac.new(signing_key(), signing_input, hashlib.sha256).digest()
    try:
        presented = _b64url_decode(parts[2])
    except Exception as exc:  # noqa: BLE001
        raise GatewayError(UNAUTHORIZED, "unauthorized", http_status=401) from exc
    if not hmac.compare_digest(expected, presented):
        raise GatewayError(UNAUTHORIZED, "unauthorized", http_status=401)
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except Exception as exc:  # noqa: BLE001
        raise GatewayError(UNAUTHORIZED, "unauthorized", http_status=401) from exc
    if int(payload.get("exp") or 0) < int(time.time()):
        raise GatewayError(UNAUTHORIZED, "unauthorized", http_status=401)
    if audience:
        allowed = {audience, audience.rstrip("/"), audience.rstrip("/") + "/mcp", "*"}
        if payload.get("aud") not in allowed:
            raise GatewayError(UNAUTHORIZED, "unauthorized", http_status=401)
    return payload


def presented_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != "bearer" or not rest.strip():
        return None
    return rest.strip()


def authenticate(authorization: str | None, *, audience: str | None = None) -> dict[str, Any]:
    """Accept the static owner token or a minted OAuth access token."""
    configured = server_token()
    if configured is None:
        raise GatewayError(UNAUTHORIZED, "server token not configured", http_status=503)
    presented = presented_bearer(authorization)
    if not presented:
        raise GatewayError(UNAUTHORIZED, "unauthorized", http_status=401)
    presented_b = presented.encode("utf-8")
    configured_b = configured.encode("utf-8")
    if len(presented_b) == len(configured_b) and hmac.compare_digest(presented_b, configured_b):
        return {"subject": "owner", "method": "bearer", "token_fp": fingerprint(presented)}
    try:
        payload = verify_access_token(presented, audience=audience)
    except GatewayError:
        raise GatewayError(UNAUTHORIZED, "unauthorized", http_status=401) from None
    return {"subject": payload.get("sub") or "owner", "method": "oauth", "token_fp": fingerprint(presented)}
