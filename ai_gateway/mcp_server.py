"""stdio MCP server for local Cursor / Claude Desktop.

A stdio session is spawned by a local client on the owner's own machine, so it is the one
transport that may receive the ``owner`` export mode (when the operator configured it).
Remote ChatGPT / HTTPS clients use the Streamable HTTP endpoint on the FastAPI app and
always receive the ``external`` policy.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ai_gateway.context import reset_context, set_context, stdio_context
from ai_gateway.mcp_protocol import handle_rpc


def _read_stdio_message() -> dict[str, Any] | None:
    """Read one LSP-style Content-Length framed message, or a raw JSON line."""
    header = sys.stdin.buffer.readline()
    if not header:
        return None
    if header.lower().startswith(b"content-length:"):
        length = int(header.split(b":", 1)[1].strip() or 0)
        while True:
            line = sys.stdin.buffer.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        raw = sys.stdin.buffer.read(length)
        return json.loads(raw.decode("utf-8"))
    text = header.decode("utf-8").strip()
    if not text:
        return _read_stdio_message()
    return json.loads(text)


def _write_stdio_message(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write("Content-Length: {0}\r\n\r\n".format(len(encoded)).encode("ascii"))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main() -> None:
    token = set_context(stdio_context())
    try:
        while True:
            try:
                message = _read_stdio_message()
            except Exception:  # noqa: BLE001
                break
            if message is None:
                break
            reply = handle_rpc(message)
            if reply is not None:
                _write_stdio_message(reply)
    finally:
        reset_context(token)


if __name__ == "__main__":  # pragma: no cover
    main()
