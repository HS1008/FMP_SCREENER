"""Bounded input validation. No arbitrary SQL, dates, or unbounded history."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from market_intelligence.catalog import CATALOG_BY_ID
from market_intelligence.sector_mapping import CANONICAL_SECTORS, FMP_LEGACY_TO_CANONICAL, resolve_provider_sector

from ai_gateway.config import MAX_HISTORY_ROWS, MAX_LIST_ROWS
from ai_gateway.errors import INVALID_DATE_RANGE, INVALID_INPUT, UNKNOWN_SECTOR, UNKNOWN_SERIES, GatewayError

SINCE_VALUES = {"previous_session", "yesterday"}
MAX_SPAN_DAYS = 366 * 5


def clamp_limit(value: Any, *, default: int, maximum: int = MAX_LIST_ROWS) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise GatewayError(INVALID_INPUT, "limit must be an integer") from exc
    if parsed < 1:
        raise GatewayError(INVALID_INPUT, "limit must be at least 1")
    return min(parsed, maximum)


def parse_date(value: Any, *, field: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise GatewayError(INVALID_DATE_RANGE, "invalid {0}".format(field), details={"field": field}) from exc


def bounded_range(start: date | None, end: date | None) -> tuple[date | None, date | None]:
    if start and end and start > end:
        raise GatewayError(INVALID_DATE_RANGE, "start_date must be on or before end_date")
    if start and end and (end - start).days > MAX_SPAN_DAYS:
        raise GatewayError(INVALID_DATE_RANGE, "date range exceeds the maximum span")
    return start, end


def history_limit(value: Any) -> int:
    return clamp_limit(value, default=250, maximum=MAX_HISTORY_ROWS)


def _treasury_series_ids() -> frozenset[str]:
    from market_intelligence.treasury_xml import NOMINAL_FIELDS, REAL_FIELDS

    return frozenset(sid for sid, _fred, _tenor in NOMINAL_FIELDS.values()) | frozenset(sid for sid, _fred, _tenor in REAL_FIELDS.values())


def require_series_id(series_id: str | None) -> str:
    """Bounded allowlist: FRED catalog ids plus the official Treasury XML ``UST_*`` ids."""
    sid = (series_id or "").strip().upper()
    if not sid:
        raise GatewayError(UNKNOWN_SERIES, "series_id is required")
    if sid not in CATALOG_BY_ID and sid not in _treasury_series_ids():
        raise GatewayError(UNKNOWN_SERIES, "unknown series_id", details={"series_id": sid})
    return sid


def resolve_sector(sector: str | None) -> str | None:
    if sector in (None, ""):
        return None
    raw = str(sector).strip()
    mapped = FMP_LEGACY_TO_CANONICAL.get(raw, raw)
    if mapped in CANONICAL_SECTORS:
        return mapped
    resolved = resolve_provider_sector(raw)
    if resolved.canonical_sector:
        return resolved.canonical_sector
    raise GatewayError(UNKNOWN_SECTOR, "unknown sector", details={"sector": raw})


def parse_since(value: Any) -> str | date:
    if value in (None, ""):
        return "previous_session"
    text = str(value).strip()
    if text in SINCE_VALUES:
        return text
    parsed = parse_date(text, field="since")
    if parsed is None:
        raise GatewayError(INVALID_INPUT, "invalid since value")
    if parsed > date.today() + timedelta(days=1):
        raise GatewayError(INVALID_DATE_RANGE, "since cannot be in the future")
    return parsed


def optional_strategy(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if len(text) > 128 or "/" in text or "\\" in text or " " in text and text != text.strip():
        raise GatewayError(INVALID_INPUT, "invalid strategy identifier")
    if any(part in text.lower() for part in ("holdout", "ml_final_holdout")):
        raise GatewayError(INVALID_INPUT, "holdout identifiers are not queryable")
    return text
