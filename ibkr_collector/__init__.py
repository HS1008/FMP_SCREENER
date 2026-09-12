"""Windows-local, read-only IBKR/TWS market-data collector.

This package never places orders, requests account/position/execution data, or
stores an IBKR password. TWS connectivity is localhost-only.
"""

from __future__ import annotations

COLLECTOR_VERSION = "ibkr_collector_v1"
SOURCE_ID = "IBKR_MARKET_DATA"
DEFAULT_CLIENT_ID = 71
DEFAULT_EOD_CLIENT_ID = 72
DEFAULT_TWS_HOST = "127.0.0.1"
DEFAULT_TWS_PORT = 7496
TASK_NAME = "FMP_SCREENER_IBKR_Collector"
CREDENTIAL_TARGET = "FMP_SCREENER/ibkr-ingest"

__all__ = [
    "COLLECTOR_VERSION",
    "CREDENTIAL_TARGET",
    "DEFAULT_CLIENT_ID",
    "DEFAULT_EOD_CLIENT_ID",
    "DEFAULT_TWS_HOST",
    "DEFAULT_TWS_PORT",
    "SOURCE_ID",
    "TASK_NAME",
]
