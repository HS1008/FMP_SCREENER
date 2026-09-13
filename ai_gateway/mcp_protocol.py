"""MCP Streamable HTTP + stdio JSON-RPC. Tools call the shared service layer."""

from __future__ import annotations

import json
from typing import Any

from ai_gateway import SCHEMA_VERSION, SERVER_NAME
from ai_gateway.envelope import public_payload
from ai_gateway.errors import GatewayError, TOOL_FORBIDDEN
from ai_gateway.services import FORBIDDEN_TOOLS, TOOL_SPECS, invoke

PROTOCOL_VERSION = "2025-03-26"


def tools_list() -> list[dict[str, Any]]:
    return [dict(spec) for spec in TOOL_SPECS]


def handle_rpc(message: dict[str, Any]) -> dict[str, Any] | None:
    if message.get("method") == "notifications/initialized":
        return None
    rpc_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    try:
        result = _dispatch(method, params)
    except GatewayError as exc:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32000, "message": exc.code, "data": exc.as_dict()}}
    except Exception:  # noqa: BLE001 - never leak internals to the client
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32603, "message": "internal error"}}
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _dispatch(method: str, params: dict[str, Any]) -> Any:
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SCHEMA_VERSION},
            "instructions": (
                "Read-only Market Intelligence. Use semantic tools. Do not ask for SQL, "
                "trades, backtests, model binaries, or the Stage 2 final holdout."
            ),
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tools_list()}
    if method == "tools/call":
        name = str(params.get("name") or "")
        if name in FORBIDDEN_TOOLS:
            raise GatewayError(TOOL_FORBIDDEN, "this tool is not available")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        payload = public_payload(invoke(name, arguments))
        return {
            "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"), default=str)}],
            "structuredContent": payload,
            "isError": payload.get("available") is False and payload.get("error") is not None,
        }
    raise GatewayError(TOOL_FORBIDDEN, "unknown method")


def parse_messages(raw: bytes | str) -> list[dict[str, Any]]:
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        payload = json.loads(text)
        return [item for item in payload if isinstance(item, dict)]
    parsed = json.loads(text)
    return [parsed] if isinstance(parsed, dict) else []
