"""Structured error codes returned to external clients. Details stay in server logs."""

from __future__ import annotations

from typing import Any

DATA_NOT_AVAILABLE = "DATA_NOT_AVAILABLE"
STALE_DATA = "STALE_DATA"
INVALID_DATE_RANGE = "INVALID_DATE_RANGE"
UNKNOWN_SECTOR = "UNKNOWN_SECTOR"
UNKNOWN_SERIES = "UNKNOWN_SERIES"
UNKNOWN_STRATEGY = "UNKNOWN_STRATEGY"
DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
UNAUTHORIZED = "UNAUTHORIZED"
RATE_LIMITED = "RATE_LIMITED"
INVALID_INPUT = "INVALID_INPUT"
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
TOOL_FORBIDDEN = "TOOL_FORBIDDEN"
HOLDOUT_FORBIDDEN = "HOLDOUT_FORBIDDEN"
INTERNAL_ERROR = "INTERNAL_ERROR"


class GatewayError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


def error_payload(code: str, message: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": code, "message": message}
    if details:
        payload["details"] = details
    return payload
