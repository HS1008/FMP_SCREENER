"""Live previous-close -> current session return / RS (display only).

Streamlit never calls providers. Writers ingest quotes into PostgreSQL; this module
resolves among stored observations and computes:

    live_return = current / baseline_session_close - 1
    live_rs     = (1 + asset_live_return) / (1 + bench_live_return) - 1

Session concepts (never overloaded):

    current_session  — NYSE session the live quote belongs to (calendar session day D)
    baseline_session — previous NYSE session before D (close used as denominator)

After 16:00 ET on session D, baseline remains previous_session(D), not D itself.
On non-NYSE-session days, live 1D is unavailable (no fabricated session).
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from market_intelligence.baskets import aligned_session_return, ratio_change_rs
from market_intelligence.calendars import CAL_NYSE, is_session, last_completed_session, previous_session
from market_intelligence.taxonomy import BENCHMARK_SPY, SECTOR_PROXIES

ET = ZoneInfo("America/New_York")

SOURCE_IBKR = "IBKR_MARKET_DATA"
SOURCE_YAHOO_LIVE = "YAHOO_LIVE"
SOURCE_EQUITY_EOD = "EQUITY_EOD"
SOURCE_YAHOO_EOD = "YAHOO_EOD"
PROVIDER_IBKR = "IBKR"
PROVIDER_YAHOO = "YAHOO"

CURRENT_SOURCE_RANK = (SOURCE_IBKR, SOURCE_YAHOO_LIVE)
# Prefer IBKR EOD source, then Yahoo EOD fallback source, then provider ranks within EQUITY_EOD.
PRIOR_SOURCE_RANK = (SOURCE_EQUITY_EOD, SOURCE_YAHOO_EOD)
PRIOR_PROVIDER_RANK = (PROVIDER_IBKR, "FIXTURE", PROVIDER_YAHOO)

YAHOO_LIVE_FALLBACK_ENV = "MI_YAHOO_LIVE_FALLBACK"
YAHOO_EOD_FALLBACK_ENV = "MI_YAHOO_EOD_FALLBACK"
DEFAULT_QUOTE_MAX_AGE_SECONDS = 900

# Main Equities headline: SPY + 11 sector ETFs (not subgroup members).
REQUIRED_SECTOR_LIVE_SYMBOLS: tuple[str, ...] = (BENCHMARK_SPY, *tuple(SECTOR_PROXIES[s] for s in sorted(SECTOR_PROXIES)))


def yahoo_live_fallback_enabled(env: Mapping[str, str] | None = None) -> bool:
    raw = (env or os.environ).get(YAHOO_LIVE_FALLBACK_ENV, "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def yahoo_eod_fallback_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Yahoo prior-close fallback. Defaults on when live fallback is on; else explicit flag."""
    env = env or os.environ
    raw = env.get(YAHOO_EOD_FALLBACK_ENV, "")
    if str(raw).strip():
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return yahoo_live_fallback_enabled(env)


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
    """current / baseline_session_close - 1."""
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


@dataclass(frozen=True)
class LiveSessionPair:
    """current_session D with baseline_session = previous NYSE session before D."""

    current_session: date
    baseline_session: date


def live_session_pair(now: datetime | None = None) -> LiveSessionPair | None:
    """Resolve (current_session, baseline_session) for live 1D.

    On a NYSE session day D (including after 16:00 ET):
        current_session  = D
        baseline_session = previous_session(D)

    On weekends/holidays: returns None (do not fabricate a live session).
    """
    now_aware = now or datetime.now(timezone.utc)
    if now_aware.tzinfo is None:
        now_aware = now_aware.replace(tzinfo=timezone.utc)
    today = now_aware.astimezone(ET).date()
    if not is_session(today, CAL_NYSE):
        return None
    return LiveSessionPair(current_session=today, baseline_session=previous_session(today, CAL_NYSE))


def in_regular_trading_hours(now: datetime | None = None) -> bool:
    """True during NYSE regular hours 09:30–16:00 America/New_York on a session day."""
    now_et = (now or datetime.now(timezone.utc)).astimezone(ET)
    if not is_session(now_et.date(), CAL_NYSE):
        return False
    t = now_et.timetz().replace(tzinfo=None)
    return time(9, 30) <= t < time(16, 0)


def latest_completed_nyse_session(now: datetime | None = None) -> date:
    """Most recent completed NYSE session (EOD publication concept — not live baseline)."""
    now_aware = now or datetime.now(timezone.utc)
    if now_aware.tzinfo is None:
        now_aware = now_aware.replace(tzinfo=timezone.utc)
    return last_completed_session(now_aware, CAL_NYSE)


def quote_session_date(quote_ts: datetime | None, *, now: datetime | None = None) -> date | None:
    del now
    if quote_ts is None:
        return None
    ts = quote_ts if quote_ts.tzinfo is not None else quote_ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(ET).date()


def observation_timestamp(quote: Mapping[str, Any]) -> datetime | None:
    """Market observation time only. Never fall back to retrieved_at."""
    return _as_datetime(quote.get("quote_ts") or quote.get("source_ts") or quote.get("observation_ts"))


def is_usable_current_quote(
    quote: Mapping[str, Any],
    *,
    current_session: date,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_QUOTE_MAX_AGE_SECONDS,
) -> bool:
    """Usable live quote for current_session D (including after the 16:00 close)."""
    price = canonical_last_price(quote)
    if price is None:
        return False
    now_et = (now or datetime.now(timezone.utc)).astimezone(ET)
    ts = observation_timestamp(quote)
    if ts is None:
        return False
    ts_et = ts.astimezone(ET)
    if ts_et.date() != current_session:
        return False
    if not is_session(current_session, CAL_NYSE):
        return False
    age = (now_et - ts_et).total_seconds()
    if age < 0:
        return False
    # During RTH require freshness. After regular close, same-session last print remains usable.
    if now_et.date() == current_session and (now_et.hour < 16 or (now_et.hour == 16 and now_et.minute == 0)):
        return age <= max_age_seconds
    return age <= 20 * 3600


# Back-compat alias used by older call sites / tests during transition.
def is_usable_current_quote_legacy(
    quote: Mapping[str, Any],
    *,
    expected_session: date,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_QUOTE_MAX_AGE_SECONDS,
) -> bool:
    """expected_session historically meant baseline; current_session is today's session day."""
    now_et = (now or datetime.now(timezone.utc)).astimezone(ET)
    current = now_et.date() if is_session(now_et.date(), CAL_NYSE) else None
    if current is None:
        return False
    # Reject quotes stamped on the baseline session masquerading as current.
    ts = observation_timestamp(quote)
    if ts is not None and ts.astimezone(ET).date() == expected_session and expected_session != current:
        return False
    return is_usable_current_quote(quote, current_session=current, now=now, max_age_seconds=max_age_seconds)


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


def _prior_rank(row: Mapping[str, Any]) -> tuple[int, int]:
    source = str(row.get("source_id") or SOURCE_EQUITY_EOD)
    try:
        source_rank = PRIOR_SOURCE_RANK.index(source)
    except ValueError:
        source_rank = len(PRIOR_SOURCE_RANK) + 1
    provider = str(row.get("provider") or "").upper()
    try:
        provider_rank = PRIOR_PROVIDER_RANK.index(provider)
    except ValueError:
        provider_rank = len(PRIOR_PROVIDER_RANK) + 1
    return source_rank, provider_rank


def resolve_current_price(
    candidates: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    current_session: date,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_QUOTE_MAX_AGE_SECONDS,
    expected_session: date | None = None,
) -> PriceObservation | None:
    """Pick IBKR last over Yahoo last among usable current quotes."""
    del expected_session  # baseline is for prior close only
    usable: list[Mapping[str, Any]] = []
    for row in candidates:
        if str(row.get("symbol") or "").upper() != symbol.upper():
            continue
        if not is_usable_current_quote(
            row, current_session=current_session, now=now, max_age_seconds=max_age_seconds
        ):
            continue
        usable.append(row)
    if not usable:
        return None
    usable.sort(
        key=lambda row: (
            _source_rank(str(row.get("source_id") or "")),
            -(observation_timestamp(row) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
        )
    )
    chosen = usable[0]
    price = canonical_last_price(chosen)
    if price is None:
        return None
    ts = observation_timestamp(chosen)
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
    """Baseline-session close: IBKR EQUITY_EOD preferred; YAHOO_EOD only for exact same session."""
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
    matching.sort(key=_prior_rank)
    chosen = matching[0]
    price = _finite(chosen.get("adj_close_price") if chosen.get("adj_close_price") is not None else chosen.get("close_price"))
    if price is None:
        return None
    source_id = str(chosen.get("source_id") or SOURCE_EQUITY_EOD)
    provider = str(chosen.get("provider") or (PROVIDER_YAHOO if source_id == SOURCE_YAHOO_EOD else PROVIDER_IBKR)).upper()
    return PriceObservation(
        symbol=symbol.upper(),
        price=price,
        source_id=source_id,
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


def format_live_quotes_as_of(
    ts: datetime | None,
    *,
    available: bool,
    fresh_count: int | None = None,
    required_count: int | None = None,
) -> str:
    if not available or ts is None or not fresh_count:
        return "Live quotes unavailable"
    local = ts.astimezone(ET)
    stamp = local.strftime("%I:%M:%S %p").lstrip("0")
    if required_count and fresh_count is not None:
        if fresh_count >= required_count:
            return "Live quotes as of {0} ET · {1}/{2}".format(stamp, fresh_count, required_count)
        return "Live quotes partial · {0}/{1} · latest {2} ET".format(fresh_count, required_count, stamp)
    return "Live quotes as of {0} ET".format(stamp)


def normalize_to_100(series: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    usable = [(d, _finite(v)) for d, v in series]
    usable = [(d, v) for d, v in usable if v is not None and v != 0]
    if not usable:
        return []
    base = usable[0][1]
    assert base is not None and base != 0
    return [(d, 100.0 * (v / base)) for d, v in usable if v is not None]


def normalize_pair_to_100(
    left: Sequence[tuple[date, float]],
    right: Sequence[tuple[date, float]],
) -> list[tuple[date, float, float]]:
    """Intersect dates first, then normalize both series to 100 on the first common date."""
    left_map = {d: _finite(v) for d, v in left}
    right_map = {d: _finite(v) for d, v in right}
    common = sorted(
        d
        for d in set(left_map) & set(right_map)
        if left_map[d] is not None and right_map[d] is not None and left_map[d] != 0 and right_map[d] != 0
    )
    if not common:
        return []
    d0 = common[0]
    left0 = left_map[d0]
    right0 = right_map[d0]
    assert left0 is not None and right0 is not None
    return [(d, 100.0 * (left_map[d] / left0), 100.0 * (right_map[d] / right0)) for d in common]  # type: ignore[operator]


def sessions_aligned(asset_session: date | None, bench_session: date | None) -> bool:
    return asset_session is not None and bench_session is not None and asset_session == bench_session


__all__ = [
    "CURRENT_SOURCE_RANK",
    "DEFAULT_QUOTE_MAX_AGE_SECONDS",
    "ET",
    "LiveSessionPair",
    "PRIOR_PROVIDER_RANK",
    "PRIOR_SOURCE_RANK",
    "PROVIDER_IBKR",
    "PROVIDER_YAHOO",
    "PriceObservation",
    "REQUIRED_SECTOR_LIVE_SYMBOLS",
    "SOURCE_EQUITY_EOD",
    "SOURCE_IBKR",
    "SOURCE_YAHOO_EOD",
    "SOURCE_YAHOO_LIVE",
    "YAHOO_EOD_FALLBACK_ENV",
    "YAHOO_LIVE_FALLBACK_ENV",
    "canonical_last_price",
    "equal_dollar_live_return",
    "format_live_quotes_as_of",
    "in_regular_trading_hours",
    "is_usable_current_quote",
    "latest_completed_nyse_session",
    "live_relative_strength",
    "live_return",
    "live_rs_from_prices",
    "live_session_pair",
    "normalize_pair_to_100",
    "normalize_to_100",
    "observation_timestamp",
    "quote_session_date",
    "resolve_current_price",
    "resolve_prior_close",
    "sessions_aligned",
    "yahoo_eod_fallback_enabled",
    "yahoo_live_fallback_enabled",
]
