"""Run the standalone gateway or the local stdio MCP server.

    python -m ai_gateway
    python -m ai_gateway --stdio
"""

from __future__ import annotations

import argparse

from ai_gateway.config import bind_host, bind_port


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Market Intelligence read-only gateway")
    parser.add_argument("--stdio", action="store_true", help="run the local MCP stdio server")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    if args.stdio:
        from ai_gateway.mcp_server import main as stdio_main

        stdio_main()
        return
    import uvicorn

    uvicorn.run("ai_gateway.api:create_app", factory=True, host=args.host or bind_host(), port=args.port or bind_port(), access_log=False)


if __name__ == "__main__":  # pragma: no cover
    main()
