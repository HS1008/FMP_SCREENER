"""Read-only Market Intelligence gateway for REST and MCP clients.

This package is a consumer of canonical PostgreSQL views. It does not ingest,
trade, launch QuantConnect jobs, or open the Stage 2 final holdout.
"""

from __future__ import annotations

SCHEMA_VERSION = "ai_gateway_v1"
SERVER_NAME = "FMP Market Intelligence"

__all__ = ["SCHEMA_VERSION", "SERVER_NAME"]
