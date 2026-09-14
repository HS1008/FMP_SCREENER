"""Canonical option-chain normalization. No provider I/O."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from market_intelligence.calendars import NY_TZ, is_session, last_completed_session, previous_session
from market_intelligence.nulls import MalformedValueError, canonical_sha256, normalize_numeric
from market_intelligence.openbb_provider.client import RawChainFetch, RawCurveFetch
from market_intelligence.openbb_provider.config import (
    CHAIN_ENDPOINT,
    COVERAGE_NOTE,
    MULTIPLIER_RULE,
    NORMALIZATION_VERSION,
    OPENBB_OPTIONS_SOURCE_ID,
    PROVIDER_ID,
)
from market_intelligence.openbb_provider.contracts import OptionContract, OptionsSnapshot, QuoteFlags, VixPoint as ContractVixPoint, VixSnapshot
from market_intelligence.openbb_provider.errors import EMPTY, OpenBBAcquisitionError
from market_intelligence.openbb_provider.vix import normalize_curve

OCC_STANDARD = re.compile(r"^([A-Z.\-]+)(\d{6})([CP])(\d{8})$")
STANDARD_UNDERLYINGS = {"SPY", "QQQ", "IWM"}
STANDARD_MULTIPLIER = Decimal("100")


def _dec(raw: Any) -> tuple[Decimal | None, str | None]:
    if raw is None:
        return None, "missing"
    try:
        return normalize_numeric(raw)
    except MalformedValueError:
        return None, "malformed"


def _finite_dec(raw: Any) -> Decimal | None:
    value, reason = _dec(raw)
    if reason or value is None:
        return None
    return value


STRIKE_QUANTUM = Decimal("0.001")


def _strikes_agree(occ: Decimal, field: Decimal) -> bool:
    return occ.quantize(STRIKE_QUANTUM) == field.quantize(STRIKE_QUANTUM)


def _nonneg_int(raw: Any) -> tuple[int | None, str | None]:
    value, reason = _dec(raw)
    if reason == "missing":
        return None, "missing"
    if reason or value is None:
        return None, reason or "malformed"
    if value < 0:
        return None, "negative_count"
    if value != value.to_integral_value():
        # Some feeds emit OI as floats; accept whole-valued decimals.
        if value != int(value):
            return None, "non_integer_count"
    return int(value), None


def _is_missing_temporal(raw: Any) -> bool:
    if raw is None or raw == "":
        return True
    if type(raw).__name__ in {"NaTType", "NAType"}:
        return True
    text = str(raw).strip()
    return text in {"", "NaT", "NaN", "None", "<NA>"}


def _parse_dt(raw: Any) -> datetime | None:
    if _is_missing_temporal(raw):
        return None
    if hasattr(raw, "to_pydatetime"):
        try:
            raw = raw.to_pydatetime()
        except (ValueError, TypeError):
            return None
        if _is_missing_temporal(raw):
            return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if dt.tzinfo is None:
        # Cboe last_trade_timestamp is exchange-local Eastern without offset.
        return dt.replace(tzinfo=NY_TZ).astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_date(raw: Any) -> date | None:
    if _is_missing_temporal(raw):
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def parse_occ(symbol: str) -> dict[str, Any]:
    token = str(symbol or "").replace("_", "").upper()
    match = OCC_STANDARD.match(token)
    if not match:
        return {"ok": False, "reason": "unparsed_contract_symbol", "root": None, "expiration": None, "option_type": None, "strike": None, "adjusted": True}
    root, yymmdd, cp, strike_raw = match.groups()
    try:
        expiration = date(2000 + int(yymmdd[0:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
        strike = Decimal(strike_raw) / Decimal("1000")
    except ValueError:
        return {"ok": False, "reason": "invalid_occ_fields", "root": root, "expiration": None, "option_type": None, "strike": None, "adjusted": True}
    return {
        "ok": True,
        "reason": None,
        "root": root,
        "expiration": expiration,
        "option_type": "call" if cp == "C" else "put",
        "strike": strike,
        "adjusted": False,
    }


def session_from_source(*, source_ts: datetime | None, fetched_at: datetime, calendar: str = "NYSE") -> tuple[date, str]:
    """Trading session for this snapshot. Never uses the host timezone by itself."""
    if source_ts is not None:
        local = source_ts.astimezone(NY_TZ)
        candidate = local.date()
        if is_session(candidate, calendar):
            return candidate, "source_timestamp"
        return previous_session(candidate, calendar), "source_timestamp_prev_session"
    session = last_completed_session(fetched_at)
    return session, "fetch_last_completed_session"


def _quote_quality(bid: Decimal | None, ask: Decimal | None) -> str:
    if bid is None and ask is None:
        return "MISSING"
    if bid is not None and bid < 0:
        return "INVALID"
    if ask is not None and ask < 0:
        return "INVALID"
    if bid is not None and ask is not None and bid > ask:
        return "CROSSED"
    if bid is not None and ask is not None:
        return "OK"
    return "ONE_SIDED"


@dataclass
class NormalizedContract:
    contract_symbol: str
    expiration: date | None
    expiration_precision: str
    dte_session: int | None
    strike: Decimal | None
    option_type: str | None
    currency: str
    multiplier: Decimal | None
    multiplier_rule: str | None
    underlying_price: Decimal | None
    bid: Decimal | None
    bid_size: int | None
    ask: Decimal | None
    ask_size: int | None
    last: Decimal | None
    last_trade_time: datetime | None
    open_interest: int | None
    volume: int | None
    iv_decimal: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    rho: Decimal | None
    theoretical_price: Decimal | None
    quote_quality: str
    is_adjusted: bool
    identity_ok: bool
    flags: dict[str, Any] = field(default_factory=dict)

    def market_record(self) -> dict[str, Any]:
        return {
            "contract_symbol": self.contract_symbol,
            "expiration": self.expiration.isoformat() if self.expiration else None,
            "strike": str(self.strike) if self.strike is not None else None,
            "option_type": self.option_type,
            "multiplier": str(self.multiplier) if self.multiplier is not None else None,
            "underlying_price": str(self.underlying_price) if self.underlying_price is not None else None,
            "bid": str(self.bid) if self.bid is not None else None,
            "ask": str(self.ask) if self.ask is not None else None,
            "last": str(self.last) if self.last is not None else None,
            "open_interest": self.open_interest,
            "volume": self.volume,
            "iv_decimal": str(self.iv_decimal) if self.iv_decimal is not None else None,
            "delta": str(self.delta) if self.delta is not None else None,
            "gamma": str(self.gamma) if self.gamma is not None else None,
            "theta": str(self.theta) if self.theta is not None else None,
            "vega": str(self.vega) if self.vega is not None else None,
            "rho": str(self.rho) if self.rho is not None else None,
        }


@dataclass
class NormalizedChain:
    underlying: str
    session_date: date
    session_basis: str
    observation_time_utc: datetime | None
    observation_precision: str
    collected_at: datetime
    source_timestamp_utc: datetime | None
    underlying_price: Decimal | None
    contracts: list[NormalizedContract]
    rejected: list[dict[str, Any]]
    metadata: dict[str, Any]
    quality: dict[str, Any]
    content_hash: str
    openbb_version: str | None
    normalization_version: str = NORMALIZATION_VERSION

    @property
    def complete(self) -> bool:
        # Dropped OCC/field mismatches are coverage, not a reason to hide the kept chain.
        return bool(self.contracts) and int(self.quality.get("duplicate_identities", 0) or 0) == 0


def _row_get(row: MappingLike, *names: str) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


MappingLike = dict[str, Any]


def normalize_chain(raw: RawChainFetch, *, clock: datetime | None = None) -> NormalizedChain:
    collected = clock or raw.fetched_at or datetime.now(timezone.utc)
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=timezone.utc)
    meta = dict(raw.metadata or {})
    source_ts = _parse_dt(meta.get("last_trade_timestamp") or meta.get("last_trade_time"))
    session_date, session_basis = session_from_source(source_ts=source_ts, fetched_at=collected)
    if source_ts is None:
        observation_precision = "unknown"
        observation_time = None
    else:
        observation_precision = "timestamp"
        observation_time = source_ts
    spot = _finite_dec(meta.get("current_price") or meta.get("underlying_price") or meta.get("close"))
    contracts: list[NormalizedContract] = []
    rejected: list[dict[str, Any]] = []
    seen: dict[tuple[Any, ...], str] = {}
    identity_conflicts = 0
    duplicate_identities = 0
    for row in raw.contracts:
        if not isinstance(row, dict):
            rejected.append({"reason": "non_object_row"})
            continue
        symbol = str(_row_get(row, "contract_symbol", "contractSymbol", "symbol") or "").strip().upper()
        parsed = parse_occ(symbol) if symbol else {"ok": False, "reason": "missing_contract_symbol", "adjusted": True, "expiration": None, "option_type": None, "strike": None, "root": None}
        expiration = _parse_date(_row_get(row, "expiration", "expiration_date")) or parsed.get("expiration")
        strike = _finite_dec(_row_get(row, "strike"))
        if strike is None:
            strike = parsed.get("strike")
        option_type = str(_row_get(row, "option_type", "optionType") or parsed.get("option_type") or "").strip().lower() or None
        if option_type in {"c", "call"}:
            option_type = "call"
        elif option_type in {"p", "put"}:
            option_type = "put"
        if parsed.get("ok") and parsed.get("expiration") and expiration and parsed["expiration"] != expiration:
            identity_conflicts += 1
            rejected.append({"contract_symbol": symbol, "reason": "expiration_mismatch", "occ": parsed["expiration"].isoformat(), "field": expiration.isoformat()})
            continue
        if parsed.get("ok") and parsed.get("strike") is not None and strike is not None and not _strikes_agree(parsed["strike"], strike):
            identity_conflicts += 1
            rejected.append({"contract_symbol": symbol, "reason": "strike_mismatch", "occ": str(parsed["strike"]), "field": str(strike)})
            continue
        if parsed.get("ok") and parsed.get("option_type") and option_type and parsed["option_type"] != option_type:
            identity_conflicts += 1
            rejected.append({"contract_symbol": symbol, "reason": "type_mismatch"})
            continue
        is_adjusted = bool(parsed.get("adjusted")) or bool(row.get("is_adjusted"))
        flags: dict[str, Any] = {}
        if strike is not None and strike <= 0:
            flags["invalid_strike"] = True
            rejected.append({"contract_symbol": symbol, "reason": "invalid_strike"})
            continue
        if option_type not in {"call", "put"}:
            rejected.append({"contract_symbol": symbol, "reason": "invalid_option_type"})
            continue
        if expiration is None:
            rejected.append({"contract_symbol": symbol, "reason": "missing_expiration"})
            continue
        identity_key = (expiration, strike, option_type)
        if identity_key in seen:
            identity_conflicts += 1
            duplicate_identities += 1
            rejected.append({"contract_symbol": symbol, "reason": "duplicate_identity", "other": seen[identity_key]})
            continue
        seen[identity_key] = symbol
        oi, oi_reason = _nonneg_int(_row_get(row, "open_interest", "openInterest"))
        volume, vol_reason = _nonneg_int(_row_get(row, "volume"))
        bid = _finite_dec(_row_get(row, "bid"))
        ask = _finite_dec(_row_get(row, "ask"))
        last = _finite_dec(_row_get(row, "last_trade_price", "last", "close"))
        iv = _finite_dec(_row_get(row, "implied_volatility", "iv"))
        if iv is not None and iv < 0:
            flags["invalid_iv"] = True
            iv = None
        gamma = _finite_dec(_row_get(row, "gamma"))
        if gamma is not None and gamma < 0:
            flags["invalid_long_gamma"] = True
        delta = _finite_dec(_row_get(row, "delta"))
        theta = _finite_dec(_row_get(row, "theta"))
        vega = _finite_dec(_row_get(row, "vega"))
        rho = _finite_dec(_row_get(row, "rho"))
        row_spot = _finite_dec(_row_get(row, "underlying_price", "underlyingPrice")) or spot
        if raw.symbol in STANDARD_UNDERLYINGS and not is_adjusted:
            multiplier = STANDARD_MULTIPLIER
            multiplier_rule = MULTIPLIER_RULE
        else:
            multiplier = None
            multiplier_rule = None
            flags["unknown_multiplier"] = True
        dte = (expiration - session_date).days if expiration else None
        if oi_reason == "negative_count":
            flags["invalid_oi"] = True
        if vol_reason == "negative_count":
            flags["invalid_volume"] = True
        contracts.append(
            NormalizedContract(
                contract_symbol=symbol or "{0}_{1}_{2}".format(expiration, strike, option_type),
                expiration=expiration,
                expiration_precision="date",
                dte_session=dte,
                strike=strike,
                option_type=option_type,
                currency="USD",
                multiplier=multiplier,
                multiplier_rule=multiplier_rule,
                underlying_price=row_spot,
                bid=bid,
                bid_size=_nonneg_int(_row_get(row, "bid_size", "bidSize"))[0],
                ask=ask,
                ask_size=_nonneg_int(_row_get(row, "ask_size", "askSize"))[0],
                last=last,
                last_trade_time=_parse_dt(_row_get(row, "last_trade_time", "lastTradeTime")),
                open_interest=oi,
                volume=volume,
                iv_decimal=iv,
                delta=delta,
                gamma=gamma,
                theta=theta,
                vega=vega,
                rho=rho,
                theoretical_price=_finite_dec(_row_get(row, "theoretical_price", "theo")),
                quote_quality=_quote_quality(bid, ask),
                is_adjusted=is_adjusted,
                identity_ok=bool(parsed.get("ok")),
                flags=flags,
            )
        )
    payload = {
        "underlying": raw.symbol,
        "session_date": session_date.isoformat(),
        "source_timestamp_utc": source_ts.isoformat() if source_ts else None,
        "contracts": [item.market_record() for item in sorted(contracts, key=lambda c: (c.expiration or date.min, c.strike or Decimal(0), c.option_type or ""))],
        "normalization_version": NORMALIZATION_VERSION,
        "provider": PROVIDER_ID,
    }
    quality = {
        "contracts_in": len(raw.contracts),
        "contracts_kept": len(contracts),
        "contracts_rejected": len(rejected),
        "identity_conflicts": identity_conflicts,
        "duplicate_identities": duplicate_identities,
        "source_timestamp_known": source_ts is not None,
        "session_basis": session_basis,
        "coverage_note": COVERAGE_NOTE,
        "endpoint": CHAIN_ENDPOINT.format(symbol=raw.symbol),
        "source_id": OPENBB_OPTIONS_SOURCE_ID,
    }
    return NormalizedChain(
        underlying=raw.symbol,
        session_date=session_date,
        session_basis=session_basis,
        observation_time_utc=observation_time,
        observation_precision=observation_precision,
        collected_at=collected,
        source_timestamp_utc=source_ts,
        underlying_price=spot,
        contracts=contracts,
        rejected=rejected,
        metadata=meta,
        quality=quality,
        content_hash=canonical_sha256(payload),
        openbb_version=raw.openbb_version,
    )


def year_fraction(days: int) -> Decimal:
    return (Decimal(days) / Decimal("365.25")) if days > 0 else Decimal("0")


NORMALIZATION_VERSION = "openbb_cboe_options_v1"


def _parse_clock(clock, fetched_at) -> datetime:
    if callable(clock):
        now = clock()
    elif clock is not None:
        now = clock
    elif fetched_at:
        now = _parse_dt(fetched_at) or datetime.now(timezone.utc)
    else:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now


def normalize_options_envelope(envelope: dict[str, Any], *, clock=None):

    rows = list(envelope.get("results") or envelope.get("contracts") or [])
    if not rows:
        raise OpenBBAcquisitionError("empty options chain", category=EMPTY, retryable=False)
    unique_rows: list[dict[str, Any]] = []
    seen_raw: set[tuple[Any, ...]] = set()
    pre_duplicates = 0
    for row in rows:
        if not isinstance(row, dict):
            unique_rows.append(row)
            continue
        key = (str(row.get("expiration") or ""), str(row.get("strike") or ""), str(row.get("option_type") or row.get("call_put") or ""))
        if key in seen_raw:
            pre_duplicates += 1
            continue
        seen_raw.add(key)
        unique_rows.append(row)
    raw = RawChainFetch(
        symbol=str(envelope.get("symbol") or "SPY").upper(),
        contracts=unique_rows,
        metadata=dict(envelope.get("results_metadata") or envelope.get("metadata") or {}),
        extra={},
        openbb_version=envelope.get("openbb_version"),
        fetched_at=_parse_clock(clock, envelope.get("fetched_at")),
    )
    chain = normalize_chain(raw, clock=raw.fetched_at)
    # Envelope tests keep adjusted/unparsed rows that have explicit strike/type/expiry.
    kept = list(chain.contracts)
    seen: dict[tuple[Any, ...], NormalizedContract] = {}
    duplicates = 0
    conflicts = 0
    mapped: list[OptionContract] = []
    for contract in kept:
        key = (contract.expiration, contract.strike, contract.option_type)
        flags = QuoteFlags(
            crossed=contract.quote_quality == "CROSSED",
            missing_bid_ask=contract.quote_quality == "MISSING",
            invalid_count=bool(contract.flags.get("invalid_oi") or contract.flags.get("invalid_volume")),
            non_finite=False,
            adjusted=contract.is_adjusted,
            unknown_multiplier=bool(contract.flags.get("unknown_multiplier")),
            unknown_deliverable=contract.is_adjusted,
            parse_conflict=key in seen,
        )
        if key in seen:
            duplicates += 1
            conflicts += 1
            # keep first; mark it
            continue
        seen[key] = contract
        mapped.append(
            OptionContract(
                contract_symbol=contract.contract_symbol,
                expiration=contract.expiration,
                expiration_precision=contract.expiration_precision,
                strike=contract.strike,
                call_put=contract.option_type,
                currency=contract.currency,
                multiplier=contract.multiplier,
                multiplier_basis=contract.multiplier_rule or "UNKNOWN",
                underlying_price=contract.underlying_price,
                underlying_price_time=None,
                bid=contract.bid,
                ask=contract.ask,
                bid_size=Decimal(contract.bid_size) if contract.bid_size is not None else None,
                ask_size=Decimal(contract.ask_size) if contract.ask_size is not None else None,
                last_trade_price=contract.last,
                last_trade_time=contract.last_trade_time,
                open_interest=Decimal(contract.open_interest) if contract.open_interest is not None else None,
                volume=Decimal(contract.volume) if contract.volume is not None else None,
                implied_volatility=contract.iv_decimal,
                delta=contract.delta,
                gamma=contract.gamma,
                theta=contract.theta,
                vega=contract.vega,
                rho=contract.rho,
                oi_as_of=None,
                quote_flags=flags,
            )
        )
    if (duplicates or pre_duplicates) and mapped:
        first = mapped[0]
        mapped[0] = OptionContract(
            **{**first.__dict__, "quote_flags": QuoteFlags(**{**first.quote_flags.__dict__, "parse_conflict": True})}
        )
    quality = dict(chain.quality)
    quality.update({"duplicates": duplicates + pre_duplicates, "conflicts": conflicts + pre_duplicates, "acquisition_status": "COMPLETE" if mapped else "PARTIAL"})
    return OptionsSnapshot(
        underlying_symbol=chain.underlying,
        source_id="OPENBB_CBOE_OPTIONS",
        provider="cboe",
        observation_time=chain.observation_time_utc,
        observation_date=chain.session_date if chain.observation_precision != "unknown" else (chain.source_timestamp_utc.date() if chain.source_timestamp_utc else None),
        observation_precision=chain.observation_precision,
        session_date=chain.session_date,
        collected_at=chain.collected_at,
        payload_hash=chain.content_hash,
        normalization_version=NORMALIZATION_VERSION,
        openbb_version=chain.openbb_version,
        endpoint=CHAIN_ENDPOINT.format(symbol=chain.underlying),
        delay_label="CBOE_DELAYED",
        permitted_use="INTERNAL_ONLY",
        quality=quality,
        provenance={"provider": "cboe", "openbb_version": chain.openbb_version},
        contracts=tuple(mapped),
        underlying_price=chain.underlying_price,
        underlying_metadata=chain.metadata,
    )


def normalize_vix_envelope(envelope: dict[str, Any], *, clock=None):
    raw = RawCurveFetch(
        symbol="VX_EOD",
        points=list(envelope.get("results") or envelope.get("points") or []),
        metadata=dict(envelope.get("metadata") or {}),
        extra={},
        openbb_version=envelope.get("openbb_version"),
        fetched_at=_parse_clock(clock, envelope.get("fetched_at")),
    )
    curve = normalize_curve(raw, clock=raw.fetched_at)
    requested = envelope.get("requested_date")
    mismatch = bool(requested and curve.observation_date and str(curve.observation_date) != str(requested)[:10])
    quality = dict(curve.quality)
    quality["nearest_date_mismatch"] = mismatch
    quality["not_official_settlement"] = True
    return VixSnapshot(
        source_id="OPENBB_CBOE_VIX",
        provider="cboe",
        observation_date=curve.observation_date,
        observation_precision=curve.observation_precision,
        level_type=curve.level_type,
        collected_at=curve.collected_at,
        payload_hash=curve.content_hash,
        normalization_version=NORMALIZATION_VERSION,
        openbb_version=curve.openbb_version,
        delay_label="CBOE_VX_EOD",
        permitted_use="INTERNAL_ONLY",
        quality=quality,
        provenance={"provider": "cboe"},
        points=tuple(
            ContractVixPoint(
                expiration_label=point.expiration_label,
                expiration_precision=point.expiration_precision,
                price=point.price,
                contract_symbol=point.contract_symbol,
                observation_date=point.observation_date,
            )
            for point in curve.points
        ),
    )
