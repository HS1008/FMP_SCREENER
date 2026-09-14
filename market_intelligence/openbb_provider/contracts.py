"""Immutable dataclasses for Cboe options / VIX snapshots. No I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _dec(value: Decimal | float | int | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    return Decimal(str(value))


@dataclass(frozen=True)
class QuoteFlags:
    crossed: bool = False
    missing_bid_ask: bool = False
    invalid_count: bool = False
    non_finite: bool = False
    adjusted: bool = False
    unknown_multiplier: bool = False
    unknown_deliverable: bool = False
    parse_conflict: bool = False


@dataclass(frozen=True)
class OptionContract:
    contract_symbol: str
    expiration: date | None
    expiration_precision: str
    strike: Decimal | None
    call_put: str | None
    currency: str | None
    multiplier: Decimal | None
    multiplier_basis: str
    underlying_price: Decimal | None
    underlying_price_time: datetime | None
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    last_trade_price: Decimal | None
    last_trade_time: datetime | None
    open_interest: Decimal | None
    volume: Decimal | None
    implied_volatility: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    rho: Decimal | None
    oi_as_of: date | None
    quote_flags: QuoteFlags = field(default_factory=QuoteFlags)
    quality_notes: tuple[str, ...] = ()

    @property
    def eligible_for_dollar_gex(self) -> bool:
        flags = self.quote_flags
        if flags.adjusted or flags.unknown_deliverable or flags.unknown_multiplier:
            return False
        if self.gamma is None or self.open_interest is None or self.multiplier is None:
            return False
        if self.underlying_price is None or self.underlying_price <= 0:
            return False
        if self.gamma < 0:
            return False
        return True


@dataclass(frozen=True)
class OptionsSnapshot:
    underlying_symbol: str
    source_id: str
    provider: str
    observation_time: datetime | None
    observation_date: date | None
    observation_precision: str
    session_date: date | None
    collected_at: datetime
    payload_hash: str
    normalization_version: str
    openbb_version: str | None
    endpoint: str
    delay_label: str
    permitted_use: str
    quality: dict[str, Any]
    provenance: dict[str, Any]
    contracts: tuple[OptionContract, ...]
    underlying_price: Decimal | None
    underlying_metadata: dict[str, Any] = field(default_factory=dict)
    sanitized_records: tuple[dict[str, Any], ...] = ()

    @property
    def complete(self) -> bool:
        return bool(self.contracts) and str(self.quality.get("acquisition_status") or "") == "COMPLETE"


@dataclass(frozen=True)
class VixPoint:
    expiration_label: str
    expiration_precision: str
    price: Decimal | None
    contract_symbol: str | None
    observation_date: date | None


@dataclass(frozen=True)
class VixSnapshot:
    source_id: str
    provider: str
    observation_date: date | None
    observation_precision: str
    level_type: str
    collected_at: datetime
    payload_hash: str
    normalization_version: str
    openbb_version: str | None
    delay_label: str
    permitted_use: str
    quality: dict[str, Any]
    provenance: dict[str, Any]
    points: tuple[VixPoint, ...]
    sanitized_records: tuple[dict[str, Any], ...] = ()
