"""Live previous-close -> current session return / RS (display only).

Streamlit never calls providers. Writers ingest quotes into PostgreSQL; this module
resolves among stored observations and computes:

    live_return = current / prior_close - 1
    live_rs     = (1 + asset_live_return) / (1 + bench_live_return) - 1

Not arithmetic excess return. Missing / NaN / infinity stay unavailable.
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from market_intelligence.baskets import aligned_session_return, ratio_change_rs
from market_intelligence.calendars import CAL_NYSE, is_session, last_completed_session

ET = ZoneInfo("America/New_York")

SOURCE_IBKR = "IBKR_MARKET_DATA"
SOURCE_YAHOO_LIVE = "YAHOO_LIVE"
PROVIDER_IBKR = "IBKR"
PROVIDER_YAHOO = "YAHOO"

# Prefer IBKR over Yahoo for both current price and prior close.
CURRENT_SOURCE_RANK = (SOURCE_IBKR, SOURCE_YAHOO_LIVE)
PRIOR_PROVIDER_RANK = (PROVIDER_IBKR, "FIXTURE", PROVIDER_YAHOO)

YAHOO_LIVE_FALLBACK_ENV = "MI_YAHOO_LIVE_FALLBACK"
# Quote older than this (wall clock) is not usable as "current" during RTH.
DEFAULT_QUOTE_MAX_AGE_SECONDS = 900


def yahoo_live_fallback_enabled(env: Mapping[str, str] | None = None) -> bool:
    raw = (env or os.environ).get(YAHOO_LIVE_FALLBACK_ENV, "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def live_return(current: Any, prior_close: Any) -> float | None:
    """current / previous_completed_session_close - 1."""
    return aligned_session_return(_finite(current), _finite(prior_close))


def live_relative_strength(asset_live_return: Any, benchmark_live_return: Any) -> float | None:
    """(1 + asset) / (1 + bench) - 1. Not arithmetic excess."""
    asset = _finite(asset_live_return)
    bench = _finite(benchmark_live_return)
    if asset is None or bench is None:
        return None
    denom = 1.0 + bench
    if denom == 0:
        return None
    return (1.0 + asset) / denom - 1.0


def live_rs_from_prices(
    asset_current: Any,
    asset_prior: Any,
    bench_current: Any,
    bench_prior: Any,
) -> float | None:
    """Aligned-session RS from prices (equivalent to live_relative_strength of returns)."""
    return ratio_change_rs(
        _finite(asset_current),
        _finite(asset_prior),
        _finite(bench_current),
        _finite(bench_prior),
    )


def equal_dollar_live_return(
    member_returns: Mapping[str, float | None],
    required_members: Sequence[str],
) -> tuple[float | None, tuple[str, ...], tuple[str, ...]]:
    """Mean of member live returns. Missing required members → NULL (no composition change)."""
    used: list[str] = []
    missing: list[str] = []
    values: list[float] = []
    for symbol in required_members:
        ret = _finite(member_returns.get(symbol))
        if ret is None:
            missing.append(symbol)
            continue
        used.append(symbol)
        values.append(ret)
    if missing or not values:
        return None, tuple(used), tuple(missing)
    return sum(values) / len(values), tuple(used), tuple(missing)


def canonical_last_price(quote: Mapping[str, Any] | None) -> float | None:
    """Prefer real last/current market price. Do not silently promote bid/ask/mid to last."""
    if not quote:
        return None
    return _finite(quote.get("last_price"))


@dataclass(frozen=True)
class PriceObservation:
    symbol: str
    price: float
    source_id: str
    provider: str
    observation_ts: datetime | None
    session_date: date | None
    quality: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.observation_ts is not None:
            payload["observation_ts"] = self.observation_ts.isoformat()
        if self.session_date is not None:
            payload["session_date"] = self.session_date.isoformat()
        return payload


def latest_completed_nyse_session(now: datetime | None = None) -> date:
    """Most recent completed NYSE session (not simply calendar_date - 1)."""
    now_aware = now or datetime.now(timezone.utc)
    if now_aware.tzinfo is None:
        now_aware = now_aware.replace(tzinfo=timezone.utc)
    return last_completed_session(now_aware, CAL_NYSE)


def quote_session_date(quote_ts: datetime | None, *, now: datetime | None = None) -> date | None:
    if quote_ts is None:
        return None
    ts = quote_ts if quote_ts.tzinfo is not None else quote_ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(ET).date()


def is_usable_current_quote(
    quote: Mapping[str, Any],
    *,
    expected_session: date,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_QUOTE_MAX_AGE_SECONDS,
) -> bool:
    """Reject stale prior-session quotes masquerading as today's live price.

    ``expected_session`` is the prior completed session used for prior_close alignment;
    a usable *current* quote must be stamped on the current calendar day in ET and must
    not be an aged print from a previous session.
    """
    del expected_session  # reserved for callers that align RS sessions; freshness uses calendar day
    price = canonical_last_price(quote)
    if price is None:
        return False
    now_et = (now or datetime.now(timezone.utc)).astimezone(ET)
    raw_ts = quote.get("quote_ts") or quote.get("source_ts") or quote.get("retrieved_at")
    ts = _as_datetime(raw_ts)
    if ts is None:
        return False
    ts_et = ts.astimezone(ET)
    session_today = now_et.date()
    # Prior-session last print is not today's current price.
    if ts_et.date() != session_today:
        return False
    # On non-sessions there is no in-progress live return vs a new prior close.
    if not is_session(session_today, CAL_NYSE):
        return False
    age = (now_et - ts_et).total_seconds()
    if age < 0:
        return False
    # Regular hours: require freshness. After the regular close, same-day last print is OK.
    if now_et.hour < 16 or (now_et.hour == 16 and now_et.minute == 0):
        return age <= max_age_seconds
    return age <= 20 * 3600


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _source_rank(source_id: str) -> int:
    try:
        return CURRENT_SOURCE_RANK.index(source_id)
    except ValueError:
        return len(CURRENT_SOURCE_RANK) + 1


def _provider_rank(provider: str | None) -> int:
    key = str(provider or "").upper()
    try:
        return PRIOR_PROVIDER_RANK.index(key)
    except ValueError:
        return len(PRIOR_PROVIDER_RANK) + 1


def resolve_current_price(
    candidates: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    expected_session: date,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_QUOTE_MAX_AGE_SECONDS,
) -> PriceObservation | None:
    """Pick IBKR last over Yahoo last among usable current quotes."""
    usable: list[Mapping[str, Any]] = []
    for row in candidates:
        if str(row.get("symbol") or "").upper() != symbol.upper():
            continue
        if not is_usable_current_quote(
            row, expected_session=expected_session, now=now, max_age_seconds=max_age_seconds
        ):
            continue
        usable.append(row)
    if not usable:
        return None

    def sort_key(row: Mapping[str, Any]) -> tuple[int, datetime]:
        ts = _as_datetime(row.get("quote_ts")) or datetime.min.replace(tzinfo=timezone.utc)
        return (_source_rank(str(row.get("source_id") or "")), ts)

    # Prefer lower source rank (IBKR), then newer quote_ts within that source.
    usable.sort(key=lambda row: (sort_key(row)[0], sort_key(row)[1]), reverse=False)
    best_rank = _source_rank(str(usable[0].get("source_id") or ""))
    same = [row for row in usable if _source_rank(str(row.get("source_id") or "")) == best_rank]
    same.sort(key=lambda row: _as_datetime(row.get("quote_ts")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    chosen = same[0]
    price = canonical_last_price(chosen)
    if price is None:
        return None
    ts = _as_datetime(chosen.get("quote_ts"))
    source_id = str(chosen.get("source_id") or "")
    return PriceObservation(
        symbol=symbol.upper(),
        price=price,
        source_id=source_id,
        provider=PROVIDER_IBKR if source_id == SOURCE_IBKR else PROVIDER_YAHOO,
        observation_ts=ts,
        session_date=quote_session_date(ts, now=now),
        quality="FRESH",
    )


def resolve_prior_close(
    bars: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    session: date,
) -> PriceObservation | None:
    """Prior completed-session close: IBKR preferred, Yahoo only if IBKR missing for that session."""
    matching = []
    for row in bars:
        sym = str(row.get("symbol") or row.get("instrument_id") or "").upper()
        if sym != symbol.upper():
            continue
        if _as_date(row.get("bar_date") or row.get("observation_date")) != session:
            continue
        price = _finite(row.get("adj_close_price") if row.get("adj_close_price") is not None else row.get("close_price"))
        if price is None:
            continue
        matching.append(row)
    if not matching:
        return None
    matching.sort(key=lambda row: _provider_rank(row.get("provider")))
    chosen = matching[0]
    price = _finite(chosen.get("adj_close_price") if chosen.get("adj_close_price") is not None else chosen.get("close_price"))
    if price is None:
        return None
    provider = str(chosen.get("provider") or PROVIDER_IBKR).upper()
    return PriceObservation(
        symbol=symbol.upper(),
        price=price,
        source_id=str(chosen.get("source_id") or "EQUITY_EOD"),
        provider=provider,
        observation_ts=None,
        session_date=session,
        quality="EOD",
    )


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def format_live_quotes_as_of(ts: datetime | None, *, available: bool) -> str:
    if not available or ts is None:
        return "Live quotes unavailable"
    local = ts.astimezone(ET)
    return "Live quotes as of {0} ET".format(local.strftime("%I:%M:%S %p").lstrip("0"))


def normalize_to_100(series: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    """Normalize price series so the first observation is 100."""
    usable = [(d, _finite(v)) for d, v in series]
    usable = [(d, v) for d, v in usable if v is not None and v != 0]
    if not usable:
        return []
    base = usable[0][1]
    assert base is not None and base != 0
    return [(d, 100.0 * (v / base)) for d, v in usable if v is not None]


def sessions_aligned(asset_session: date | None, bench_session: date | None) -> bool:
    return asset_session is not None and bench_session is not None and asset_session == bench_session


__all__ = [
    "CURRENT_SOURCE_RANK",
    "DEFAULT_QUOTE_MAX_AGE_SECONDS",
    "ET",
    "PRIOR_PROVIDER_RANK",
    "PROVIDER_IBKR",
    "PROVIDER_YAHOO",
    "PriceObservation",
    "SOURCE_IBKR",
    "SOURCE_YAHOO_LIVE",
    "YAHOO_LIVE_FALLBACK_ENV",
    "canonical_last_price",
    "equal_dollar_live_return",
    "format_live_quotes_as_of",
    "is_usable_current_quote",
    "latest_completed_nyse_session",
    "live_relative_strength",
    "live_return",
    "live_rs_from_prices",
    "normalize_to_100",
    "quote_session_date",
    "resolve_current_price",
    "resolve_prior_close",
    "sessions_aligned",
    "yahoo_live_fallback_enabled",
]
