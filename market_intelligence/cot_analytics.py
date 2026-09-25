"""Descriptive COT analytics. Not buy/sell signals and not Stage 2 features."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from market_intelligence.ingest_cot import net_and_pct

NY = ZoneInfo("America/New_York")
COT_ANALYTICS_VERSION = "cot_descriptive_v1"
MIN_HISTORY = 13


def expected_publish_at(position_date: date) -> datetime:
    """Normal CFTC pattern: Tuesday positions published Friday 15:30 ET. Not historical proof."""
    weekday = position_date.weekday()
    days_to_friday = (4 - weekday) % 7
    if weekday > 4:
        days_to_friday = (4 - weekday) + 7
    publish_date = position_date + timedelta(days=days_to_friday)
    return datetime(publish_date.year, publish_date.month, publish_date.day, 15, 30, tzinfo=NY)


def availability_status(*, position_date: date, retrieved_at: datetime, published_at: datetime | None = None) -> dict[str, Any]:
    expected = expected_publish_at(position_date)
    actual = published_at or retrieved_at
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=timezone.utc)
    delayed = actual > expected + timedelta(hours=1)
    return {
        "position_date": position_date.isoformat(),
        "expected_publish_at": expected.astimezone(timezone.utc).isoformat(),
        "available_at_basis": "FIRST_SEEN" if published_at is None else "PROVIDER_PUBLISHED",
        "delayed_relative_to_normal_friday": delayed,
        "note": "A normal Friday schedule is not proof of historical availability.",
        "version": COT_ANALYTICS_VERSION,
    }


def crowding(*, net_oi: Decimal | None, history: list[Decimal], min_history: int = MIN_HISTORY) -> dict[str, Any]:
    if net_oi is None:
        return {"status": "UNAVAILABLE", "reason": "missing_net_oi", "descriptor": None}
    if len(history) < min_history:
        return {"status": "UNAVAILABLE", "reason": "insufficient_history", "descriptor": None, "n": len(history)}
    ordered = sorted(history)
    rank = sum(1 for item in ordered if item <= net_oi)
    percentile = Decimal(rank) / Decimal(len(ordered))
    mean = sum(history, Decimal(0)) / Decimal(len(history))
    var = sum((item - mean) ** 2 for item in history) / Decimal(len(history))
    std = var.sqrt() if var > 0 else Decimal(0)
    zscore = None if std == 0 else (net_oi - mean) / std
    if percentile >= Decimal("0.9"):
        descriptor = "elevated_net_long_vs_own_history"
    elif percentile <= Decimal("0.1"):
        descriptor = "elevated_net_short_vs_own_history"
    else:
        descriptor = "mid_range_vs_own_history"
    return {
        "status": "OK",
        "percentile": percentile,
        "zscore": zscore,
        "descriptor": descriptor,
        "version": COT_ANALYTICS_VERSION,
        "note": "Descriptive dashboard analytics, not an automatic buy/sell signal.",
    }


def net_change(current: Decimal | None, prior: Decimal | None) -> dict[str, Any]:
    if current is None or prior is None:
        return {"value": None, "status": "UNAVAILABLE", "reason": "missing_leg"}
    return {"value": current - prior, "status": "OK", "reason": None}


def category_snapshot(long_pos: Decimal | None, short_pos: Decimal | None, open_interest: Decimal | None) -> dict[str, Any]:
    payload = net_and_pct(long_pos, short_pos, open_interest)
    payload["version"] = COT_ANALYTICS_VERSION
    return payload
