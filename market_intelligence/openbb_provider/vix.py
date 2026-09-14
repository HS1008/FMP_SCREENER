"""VX EOD curve normalization and front-month analytics."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from market_intelligence.calendars import last_completed_session
from market_intelligence.nulls import canonical_sha256, normalize_numeric
from market_intelligence.openbb_provider.client import RawCurveFetch
from market_intelligence.openbb_provider.config import (
    FLAT_TOLERANCE_POINTS,
    NORMALIZATION_VERSION,
    OPENBB_VIX_SOURCE_ID,
    VIX_ANALYTICS_VERSION,
    VIX_LEVEL_TYPE,
)

MONTH_LABEL = re.compile(r"^(\d{4})-(\d{2})$")


@dataclass
class VixPoint:
    expiration_label: str
    expiration_precision: str
    price: Decimal | None
    contract_symbol: str | None
    observation_date: date | None
    flags: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedCurve:
    session_date: date
    observation_date: date | None
    observation_precision: str
    level_type: str
    collected_at: datetime
    points: list[VixPoint]
    rejected: list[dict[str, Any]]
    content_hash: str
    quality: dict[str, Any]
    openbb_version: str | None
    requested_symbol: str = "VX_EOD"


def _month_key(label: str) -> tuple[int, int] | None:
    match = MONTH_LABEL.match(label)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if month < 1 or month > 12:
        return None
    return year, month


def normalize_curve(raw: RawCurveFetch, *, clock: datetime | None = None) -> NormalizedCurve:
    collected = clock or raw.fetched_at or datetime.now(timezone.utc)
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=timezone.utc)
    session = last_completed_session(collected)
    points: list[VixPoint] = []
    rejected: list[dict[str, Any]] = []
    obs_dates: set[date] = set()
    for row in raw.points:
        if not isinstance(row, dict):
            rejected.append({"reason": "non_object_row"})
            continue
        label = str(row.get("expiration") or row.get("expiration_label") or "").strip()
        key = _month_key(label)
        if key is None:
            rejected.append({"reason": "non_month_expiration", "value": label})
            continue
        price_raw = row.get("price")
        price, reason = normalize_numeric(price_raw) if price_raw is not None else (None, "missing")
        flags: dict[str, Any] = {}
        if reason:
            flags["price_reason"] = reason
        if price is not None and price <= 0:
            flags["non_positive_price"] = True
            price = None
        obs = None
        if row.get("date"):
            try:
                obs = date.fromisoformat(str(row["date"])[:10])
                obs_dates.add(obs)
            except ValueError:
                flags["bad_observation_date"] = True
        points.append(
            VixPoint(
                expiration_label="{0:04d}-{1:02d}".format(key[0], key[1]),
                expiration_precision="month",
                price=price,
                contract_symbol=str(row["symbol"]) if row.get("symbol") else None,
                observation_date=obs,
                flags=flags,
            )
        )
    points.sort(key=lambda item: item.expiration_label)
    if len(obs_dates) == 1:
        observation_date = next(iter(obs_dates))
        observation_precision = "date"
    elif not obs_dates:
        observation_date = None
        observation_precision = "unknown"
    else:
        observation_date = None
        observation_precision = "mixed"
    payload = {
        "points": [{"expiration_label": p.expiration_label, "price": str(p.price) if p.price is not None else None, "symbol": p.contract_symbol} for p in points],
        "observation_date": observation_date.isoformat() if observation_date else None,
        "level_type": VIX_LEVEL_TYPE,
        "normalization_version": NORMALIZATION_VERSION,
    }
    return NormalizedCurve(
        session_date=session,
        observation_date=observation_date,
        observation_precision=observation_precision,
        level_type=VIX_LEVEL_TYPE,
        collected_at=collected,
        points=points,
        rejected=rejected,
        content_hash=canonical_sha256(payload),
        quality={
            "points_in": len(raw.points),
            "points_kept": len(points),
            "rejected": len(rejected),
            "source_id": OPENBB_VIX_SOURCE_ID,
            "not_official_settlement": True,
            "not_live_quote": True,
        },
        openbb_version=raw.openbb_version,
    )


def front_curve_metrics(curve: NormalizedCurve) -> dict[str, Any]:
    eligible = [p for p in curve.points if p.price is not None and p.price > 0]
    if len(eligible) < 2:
        return {
            "status": "UNAVAILABLE",
            "reason": "need_two_monthly_prices",
            "method_version": VIX_ANALYTICS_VERSION,
            "level_type": curve.level_type,
            "exact_dte": None,
            "not_official_settlement": True,
        }
    m1, m2 = eligible[0], eligible[1]
    ratio = m1.price / m2.price
    spread = m2.price - m1.price
    slope_pct = ((m2.price / m1.price) - Decimal("1")) * Decimal("100")
    if abs(spread) <= Decimal(str(FLAT_TOLERANCE_POINTS)):
        shape = "FLAT"
    elif spread > 0:
        shape = "CONTANGO"
    else:
        shape = "BACKWARDATION"
    return {
        "status": "OK",
        "method_version": VIX_ANALYTICS_VERSION,
        "level_type": curve.level_type,
        "observation_date": curve.observation_date.isoformat() if curve.observation_date else None,
        "observation_precision": curve.observation_precision,
        "session_date": curve.session_date.isoformat(),
        "m1": {"expiration": m1.expiration_label, "price": m1.price, "precision": m1.expiration_precision, "symbol": m1.contract_symbol},
        "m2": {"expiration": m2.expiration_label, "price": m2.price, "precision": m2.expiration_precision, "symbol": m2.contract_symbol},
        "m1_m2_ratio": ratio,
        "m2_minus_m1_points": spread,
        "m1_to_m2_slope_pct": slope_pct,
        "front_shape": shape,
        "points": [
            {"expiration": point.expiration_label, "precision": point.expiration_precision, "price": point.price}
            for point in curve.points
        ],
        "m1_label": m1.expiration_label,
        "m2_label": m2.expiration_label,
        "m1_price": m1.price,
        "m2_price": m2.price,
        "m1_symbol": m1.contract_symbol,
        "m2_symbol": m2.contract_symbol,
        "ratio": ratio,
        "spread_points": spread,
        "slope_pct": slope_pct,
        "shape": shape,
        "shape_label": "M1-M2 {0}".format(shape.lower()),
        "flat_tolerance_points": FLAT_TOLERANCE_POINTS,
        "exact_dte": None,
        "expiration_precision": "month",
        "not_official_settlement": True,
        "not_live_quote": True,
        "returned_date_may_differ_from_request": True,
        "export_scope": "INTERNAL_ONLY",
        "reason": None,
    }
