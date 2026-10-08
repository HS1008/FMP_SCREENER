"""Central session policy and 1D return rule for every Yahoo-backed instrument.

This module is pure: it does not import yfinance, Streamlit, or the database
driver. The quote collector (a server job) and the Streamlit pages both call
it, so the denominator of a 1D change is decided by one function.

1D policy
---------
* While an instrument's session is active::

      1D = current_price / current_session_open - 1

* After the close and until the next open (including weekends and holidays)::

      1D = current_price / most_recent_completed_session_close - 1

The denominator switches at the next open. A quote observed before the
reference boundary is never paired with a newer open or close; the cell is
"pending" instead. The current price is the newest valid provider observation
by its own timestamp, including pre-market, post-market, and overnight prints.

Sessions are instrument specific (see :data:`POLICY_TABLE`). US equities use
the NYSE cash session with early closes; CME, CBOT, and ICE futures proxies
use their documented electronic session on the exchange trade date; spot FX
rolls at the 17:00 New York close; crypto uses the UTC calendar day; EOD-only
indexes (MOVE and the VIX term tenors) never receive a live 1D.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from market_intelligence.calendars import CAL_NYSE, NY_TZ, is_session, nyse_regular_close, previous_session
from market_intelligence.cross_asset_universe import (
    COMMODITY_INSTRUMENTS,
    CRYPTO_INSTRUMENTS,
    FX_INSTRUMENTS,
    INSTRUMENT_BY_ID,
    MOVE_INSTRUMENT_ID,
    YahooInstrument,
)

UTC = timezone.utc
CT_TZ = ZoneInfo("America/Chicago")

BASIS_SESSION_OPEN = "SESSION_OPEN"
BASIS_LAST_CLOSE = "LAST_CLOSE"
BASIS_EOD_CLOSE = "EOD_CLOSE_TO_CLOSE"
EOD_ONLY = "EOD_ONLY"

STATE_ACTIVE = "active"
STATE_CLOSED = "closed"
CLOSE_BAR_TOLERANCE = timedelta(minutes=1)

POLICY_US_EQUITY = "US_EQUITY_RTH"
POLICY_CBOE_VIX = "CBOE_VIX_INDEX"
POLICY_CME_GLOBEX = "CME_GLOBEX_23H"
POLICY_CBOT_GRAINS = "CBOT_GRAINS"
POLICY_ICE_BRENT = "ICE_BRENT"
POLICY_ICE_USDX = "ICE_USDX"
POLICY_FX_SPOT = "FX_SPOT_NY_CLOSE"
POLICY_CRYPTO = "CRYPTO_UTC_DAILY"
POLICY_EOD_ONLY = EOD_ONLY

BASIS_LABELS = {
    BASIS_SESSION_OPEN: "Since session open",
    BASIS_LAST_CLOSE: "Since last session close",
    BASIS_EOD_CLOSE: "EOD close-to-close",
}


@dataclass(frozen=True)
class SessionSpec:
    """One instrument session convention.

    ``open_time`` / ``close_time`` are wall-clock times in ``tz``. When
    ``opens_prior_day`` is set the session for trade date ``D`` starts on the
    calendar day before ``D`` (CME, ICE, CBOT electronic sessions). ``continuous``
    sessions (crypto) are always active and roll at ``open_time``.
    ``trading_weekdays`` are the weekdays that carry a trade date. ``calendar``
    adds exchange holidays (NYSE only; futures holidays are not modeled).
    """

    policy_id: str
    tz: ZoneInfo
    open_time: time
    close_time: time
    description: str
    opens_prior_day: bool = False
    continuous: bool = False
    live: bool = True
    calendar: str | None = None
    trading_weekdays: frozenset[int] = frozenset({0, 1, 2, 3, 4})
    extended_hours: bool = False
    source_note: str = ""


POLICY_TABLE: dict[str, SessionSpec] = {
    POLICY_US_EQUITY: SessionSpec(
        POLICY_US_EQUITY,
        NY_TZ,
        time(9, 30),
        time(16, 0),
        "NYSE regular cash session 09:30-16:00 America/New_York (13:00 on early-close days), "
        "NYSE holiday calendar. Pre-market, post-market, and overnight prints count as the current price.",
        calendar=CAL_NYSE,
        extended_hours=True,
        source_note="NYSE hours and holidays; early closes the day after Thanksgiving, July 3, December 24.",
    ),
    POLICY_CBOE_VIX: SessionSpec(
        POLICY_CBOE_VIX,
        NY_TZ,
        time(9, 30),
        time(16, 15),
        "Cboe VIX index regular dissemination 09:31-16:15 America/New_York on NYSE trading days; "
        "global-hours values (03:15-09:15 ET) are current prices but not an open reference.",
        calendar=CAL_NYSE,
        extended_hours=True,
        source_note="Cboe Volatility Index methodology: RTH dissemination 09:31-16:15 ET, GTH 03:15-09:15 ET.",
    ),
    POLICY_CME_GLOBEX: SessionSpec(
        POLICY_CME_GLOBEX,
        NY_TZ,
        time(18, 0),
        time(17, 0),
        "CME Globex energy, metals, and FX futures: trade date session 18:00 America/New_York on the prior "
        "calendar day to 17:00; weekly open Sunday 18:00, weekly close Friday 17:00; exchange holidays not modeled.",
        opens_prior_day=True,
        source_note="CME Group trading hours: Sunday-Friday 6:00 p.m.-5:00 p.m. ET with a 60-minute daily break.",
    ),
    POLICY_CBOT_GRAINS: SessionSpec(
        POLICY_CBOT_GRAINS,
        CT_TZ,
        time(19, 0),
        time(13, 20),
        "CBOT grains and oilseeds: trade date session 19:00 America/Chicago on the prior calendar day to 13:20 "
        "(overnight 19:00-07:45, day 08:30-13:20; the 07:45-08:30 pause is inside the session); exchange holidays not modeled.",
        opens_prior_day=True,
        source_note="CME Group grain and oilseed fact card: 7:00 p.m.-7:45 a.m. CT Sun-Fri and 8:30 a.m.-1:20 p.m. CT Mon-Fri.",
    ),
    POLICY_ICE_BRENT: SessionSpec(
        POLICY_ICE_BRENT,
        NY_TZ,
        time(20, 0),
        time(18, 0),
        "ICE Brent: trade date session 20:00 America/New_York on the prior calendar day to 18:00; exchange holidays not modeled.",
        opens_prior_day=True,
        source_note="ICE Brent Crude futures product page: New York 8:00 PM-6:00 PM next day.",
    ),
    POLICY_ICE_USDX: SessionSpec(
        POLICY_ICE_USDX,
        NY_TZ,
        time(20, 0),
        time(17, 0),
        "ICE US Dollar Index: trade date session 20:00 America/New_York on the prior calendar day to 17:00 "
        "(Sunday open 18:00 for Monday's trade date); exchange holidays not modeled.",
        opens_prior_day=True,
        source_note="ICE U.S. Dollar Index futures: New York 8:00 PM-5:00 PM next day.",
    ),
    POLICY_FX_SPOT: SessionSpec(
        POLICY_FX_SPOT,
        NY_TZ,
        time(17, 0),
        time(17, 0),
        "Spot FX: 24x5 market whose daily session rolls at the 17:00 America/New_York close; the trade date session "
        "runs 17:00 on the prior calendar day to 17:00. Closed Friday 17:00 to Sunday 17:00 ET.",
        opens_prior_day=True,
        source_note="Interbank FX convention: the trading day ends at 5:00 p.m. New York time.",
    ),
    POLICY_CRYPTO: SessionSpec(
        POLICY_CRYPTO,
        UTC,
        time(0, 0),
        time(0, 0),
        "Crypto: 24x7, UTC calendar-day session (00:00-24:00 UTC). The denominator rolls to the new UTC day's open at midnight UTC.",
        continuous=True,
        trading_weekdays=frozenset(range(7)),
        source_note="Yahoo crypto daily bars are UTC calendar days.",
    ),
    POLICY_EOD_ONLY: SessionSpec(
        POLICY_EOD_ONLY,
        NY_TZ,
        time(16, 0),
        time(16, 0),
        "End-of-day index publication only (ICE BofA MOVE, VIX term tenors). No live price; 1D is the EOD close-to-close change.",
        live=False,
        calendar=CAL_NYSE,
    ),
}

_CME_IDS = frozenset({"CL", "NG", "GC", "SI", "HG", "USDCNH"})
_GRAIN_IDS = frozenset({"ZC", "ZW", "ZS_F"})


def policy_id_for(symbol: str) -> str:
    """Policy id for a dashboard symbol or cross-asset instrument id."""
    key = str(symbol or "").upper().strip()
    if key == MOVE_INSTRUMENT_ID:
        return POLICY_EOD_ONLY
    if key == "VIX":
        return POLICY_CBOE_VIX
    spec = INSTRUMENT_BY_ID.get(key)
    if spec is None:
        return POLICY_US_EQUITY
    if spec.asset_class == "CRYPTO":
        return POLICY_CRYPTO
    if key == "DXY":
        return POLICY_ICE_USDX
    if key == "BZ":
        return POLICY_ICE_BRENT
    if key in _GRAIN_IDS:
        return POLICY_CBOT_GRAINS
    if key in _CME_IDS:
        return POLICY_CME_GLOBEX
    if spec.asset_class == "FX":
        return POLICY_FX_SPOT
    if spec.asset_class == "INDEX":
        return POLICY_EOD_ONLY
    return POLICY_CME_GLOBEX


def policy_for(symbol: str) -> SessionSpec:
    return POLICY_TABLE[policy_id_for(symbol)]


def live_quote_instruments() -> tuple[YahooInstrument, ...]:
    """Cross-asset instruments that receive intraday quotes. VIX is collected with the equity universe."""
    rows: list[YahooInstrument] = []
    for spec in (*FX_INSTRUMENTS, *COMMODITY_INSTRUMENTS, *CRYPTO_INSTRUMENTS):
        if spec.instrument_id == "VIX" or not POLICY_TABLE[policy_id_for(spec.instrument_id)].live:
            continue
        rows.append(spec)
    return tuple(rows)


@dataclass(frozen=True)
class SessionState:
    """The session that is open now, or the most recently completed one."""

    policy_id: str
    session_date: date
    active: bool
    open_at: datetime
    close_at: datetime
    previous_session_date: date

    @property
    def state(self) -> str:
        return STATE_ACTIVE if self.active else STATE_CLOSED


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def is_trading_day(spec: SessionSpec, day: date) -> bool:
    if day.weekday() not in spec.trading_weekdays:
        return False
    if spec.calendar:
        return is_session(day, spec.calendar)
    return True


def _previous_trading_day(spec: SessionSpec, day: date) -> date:
    cur = day - timedelta(days=1)
    while not is_trading_day(spec, cur):
        cur -= timedelta(days=1)
    return cur


def _close_time(spec: SessionSpec, day: date) -> time:
    if spec.policy_id == POLICY_US_EQUITY:
        return nyse_regular_close(day)
    if spec.policy_id == POLICY_CBOE_VIX and nyse_regular_close(day) != time(16, 0):
        return time(13, 15)
    return spec.close_time


def session_open_at(spec: SessionSpec, day: date) -> datetime:
    start_day = day - timedelta(days=1) if spec.opens_prior_day else day
    return datetime.combine(start_day, spec.open_time, tzinfo=spec.tz)


def session_close_at(spec: SessionSpec, day: date) -> datetime:
    if spec.continuous:
        return datetime.combine(day + timedelta(days=1), spec.close_time, tzinfo=spec.tz)
    return datetime.combine(day, _close_time(spec, day), tzinfo=spec.tz)


def session_state(spec: SessionSpec, now: datetime) -> SessionState:
    """Session containing ``now``, else the most recently completed one."""
    local = as_utc(now).astimezone(spec.tz)
    candidate = local.date() + timedelta(days=1 if spec.opens_prior_day else 0)
    for _ in range(14):
        if is_trading_day(spec, candidate) and session_open_at(spec, candidate) <= local:
            break
        candidate -= timedelta(days=1)
    open_at = session_open_at(spec, candidate)
    close_at = session_close_at(spec, candidate)
    active = bool(spec.continuous) or local < close_at
    return SessionState(
        policy_id=spec.policy_id,
        session_date=candidate,
        active=active,
        open_at=open_at.astimezone(UTC),
        close_at=close_at.astimezone(UTC),
        previous_session_date=_previous_trading_day(spec, candidate),
    )


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return as_utc(value)
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return as_utc(parsed)


def parse_day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class OneDay:
    """One resolved 1D change with its explicit reference."""

    value: float | None
    basis: str | None
    reason: str
    policy_id: str
    state: str
    session_date: date | None
    price: float | None = None
    price_ts: datetime | None = None
    reference_price: float | None = None
    reference_ts: datetime | None = None
    reference_date: date | None = None

    @property
    def available(self) -> bool:
        return self.value is not None

    @property
    def basis_label(self) -> str:
        return BASIS_LABELS.get(self.basis or "", "N/A")

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "basis": self.basis,
            "basis_label": self.basis_label,
            "reason": self.reason,
            "policy_id": self.policy_id,
            "state": self.state,
            "session_date": None if self.session_date is None else self.session_date.isoformat(),
            "price": self.price,
            "price_ts": None if self.price_ts is None else self.price_ts.isoformat(),
            "reference_price": self.reference_price,
            "reference_ts": None if self.reference_ts is None else self.reference_ts.isoformat(),
            "reference_date": None if self.reference_date is None else self.reference_date.isoformat(),
        }


def one_day_return(
    symbol: str,
    *,
    price: Any,
    price_ts: Any,
    session_open: Any,
    session_open_date: Any,
    last_close: Any,
    last_close_date: Any,
    now: datetime,
    corporate_action: bool = False,
) -> OneDay:
    """Apply the 1D policy to one stored quote and its stored references.

    ``session_open`` / ``session_open_date`` are the open of the session named by
    the date; ``last_close`` / ``last_close_date`` are the newest completed
    close. A reference whose date does not match what the policy expects now is
    "pending", never substituted.
    """
    spec = policy_for(symbol)
    state = session_state(spec, now)
    px = _finite(price)
    ts = parse_ts(price_ts)
    base = dict(policy_id=spec.policy_id, state=state.state, session_date=state.session_date, price=px, price_ts=ts)
    if not spec.live:
        return OneDay(None, EOD_ONLY, "EOD-only instrument: no intraday price", **base)
    if px is None or ts is None:
        return OneDay(None, None, "no price", **base)
    if corporate_action:
        return OneDay(None, None, "corporate action between the reference and the price", **base)
    if state.active:
        if ts < state.open_at:
            return OneDay(
                None,
                BASIS_SESSION_OPEN,
                "pending: no price from the {0} session yet".format(state.session_date.isoformat()),
                reference_ts=state.open_at,
                reference_date=state.session_date,
                **base,
            )
        opened = _finite(session_open)
        if opened is None or parse_day(session_open_date) != state.session_date:
            return OneDay(
                None,
                BASIS_SESSION_OPEN,
                "pending: {0} session open not stored yet".format(state.session_date.isoformat()),
                reference_ts=state.open_at,
                reference_date=state.session_date,
                **base,
            )
        return OneDay(
            px / opened - 1.0,
            BASIS_SESSION_OPEN,
            "price / {0} session open - 1".format(state.session_date.isoformat()),
            reference_price=opened,
            reference_ts=state.open_at,
            reference_date=state.session_date,
            **base,
        )
    closed = _finite(last_close)
    if closed is None or parse_day(last_close_date) != state.session_date:
        return OneDay(
            None,
            BASIS_LAST_CLOSE,
            "pending: {0} close not stored yet".format(state.session_date.isoformat()),
            reference_ts=state.close_at,
            reference_date=state.session_date,
            **base,
        )
    # A one-minute provider bar labeled 15:59 carries the closing print; it is
    # not an observation from before the close.
    if ts < state.close_at - CLOSE_BAR_TOLERANCE:
        return OneDay(
            None,
            BASIS_LAST_CLOSE,
            "pending: no price at or after the {0} close".format(state.session_date.isoformat()),
            reference_price=closed,
            reference_ts=state.close_at,
            reference_date=state.session_date,
            **base,
        )
    return OneDay(
        px / closed - 1.0,
        BASIS_LAST_CLOSE,
        "price / {0} close - 1".format(state.session_date.isoformat()),
        reference_price=closed,
        reference_ts=state.close_at,
        reference_date=state.session_date,
        **base,
    )


def one_day_from_quote(symbol: str, row: Mapping[str, Any] | None, *, now: datetime) -> OneDay:
    """Resolve 1D from a stored ``mi_market_quotes`` row written by the Yahoo collector."""
    if not row:
        spec = policy_for(symbol)
        state = session_state(spec, now)
        return OneDay(None, None, "no stored quote", spec.policy_id, state.state, state.session_date)
    provenance = row.get("provenance") or {}
    if not isinstance(provenance, Mapping):
        provenance = {}
    price = provenance.get("current_price", row.get("last_price"))
    return one_day_return(
        symbol,
        price=price,
        price_ts=row.get("quote_ts"),
        session_open=provenance.get("session_open"),
        session_open_date=provenance.get("session_date"),
        last_close=provenance.get("last_close"),
        last_close_date=provenance.get("last_close_date"),
        now=now,
        corporate_action="corporate action" in str(provenance.get("quote_error") or ""),
    )


def ratio_one_day(numerator: OneDay, denominator: OneDay) -> OneDay:
    """1D of a price ratio: (A/B) / (ref_A/ref_B) - 1 with both legs on one basis and session."""
    older_ts = None
    if numerator.price_ts and denominator.price_ts:
        older_ts = min(numerator.price_ts, denominator.price_ts)
    base = dict(
        policy_id=numerator.policy_id,
        state=numerator.state,
        session_date=numerator.session_date,
        price_ts=older_ts,
    )
    if not numerator.available or not denominator.available:
        reason = numerator.reason if not numerator.available else denominator.reason
        return OneDay(None, numerator.basis or denominator.basis, reason, **base)
    if numerator.basis != denominator.basis or numerator.reference_date != denominator.reference_date:
        return OneDay(None, None, "legs on different sessions or bases", **base)
    if None in (numerator.price, denominator.price, numerator.reference_price, denominator.reference_price):
        return OneDay(None, numerator.basis, "reference unavailable", **base)
    current = numerator.price / denominator.price
    reference = numerator.reference_price / denominator.reference_price
    return OneDay(
        current / reference - 1.0,
        numerator.basis,
        "ratio now / ratio at reference - 1",
        price=current,
        reference_price=reference,
        reference_ts=numerator.reference_ts,
        reference_date=numerator.reference_date,
        **base,
    )


def completed_close_from_daily(
    daily: Mapping[date, float] | Sequence[tuple[date, float]],
    state: SessionState,
) -> tuple[date, float] | None:
    """Newest completed close from provider daily bars labeled by session date.

    While the session is active the bar dated ``session_date`` is in progress and
    is skipped. After the close that bar is the most recent completed close.
    """
    items = daily.items() if isinstance(daily, Mapping) else daily
    limit = state.session_date if not state.active else state.session_date - timedelta(days=1)
    chosen: tuple[date, float] | None = None
    for day, raw in items:
        close = _finite(raw)
        if close is None or day > limit:
            continue
        if chosen is None or day > chosen[0]:
            chosen = (day, close)
    return chosen


def bar_is_provisional(symbol: str, bar_date: Any, *, now: datetime) -> bool:
    """A stored daily bar dated on a still-active session is not a completed close."""
    day = parse_day(bar_date)
    if day is None:
        return False
    state = session_state(policy_for(symbol), now)
    return state.active and day >= state.session_date


def latest_completed_bar_date(symbol: str, *, now: datetime) -> date:
    """Session date of the newest close that can be complete right now."""
    state = session_state(policy_for(symbol), now)
    return state.previous_session_date if state.active else state.session_date


# ---- timestamps -----------------------------------------------------------------------------

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_eastern(value: Any) -> str:
    """``Oct 7, 2026, 3:15 PM EDT`` in America/New_York with the live EST/EDT abbreviation."""
    ts = parse_ts(value)
    if ts is None:
        return ""
    local = ts.astimezone(NY_TZ)
    hour = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"
    return "{0} {1}, {2}, {3}:{4:02d} {5} {6}".format(
        _MONTHS[local.month - 1], local.day, local.year, hour, local.minute, meridiem, local.tzname() or "ET"
    )


def last_updated_label(value: Any) -> str:
    text = format_eastern(value)
    return "Last updated: {0}".format(text) if text else "Last updated: unavailable"


FRESH_SECONDS = 20 * 60


def quote_freshness(symbol: str, price_ts: Any, *, now: datetime) -> str:
    """``current`` / ``delayed`` / ``stale`` / ``unavailable`` for one observation."""
    ts = parse_ts(price_ts)
    if ts is None:
        return "unavailable"
    spec = policy_for(symbol)
    if not spec.live:
        return "eod"
    state = session_state(spec, now)
    if state.active:
        if ts < session_open_at(spec, state.previous_session_date).astimezone(UTC):
            return "stale"
        if (as_utc(now) - ts).total_seconds() > FRESH_SECONDS:
            return "delayed"
        return "current"
    if ts < state.open_at:
        return "stale"
    return "current"


def policy_rows() -> list[dict[str, str]]:
    """Instrument -> policy table for documentation and tests."""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for symbol in ("SPY", "VIX", *(spec.instrument_id for spec in INSTRUMENT_BY_ID.values())):
        if symbol in seen:
            continue
        seen.add(symbol)
        spec = policy_for(symbol)
        rows.append(
            {
                "symbol": symbol,
                "policy_id": spec.policy_id,
                "timezone": str(spec.tz.key if hasattr(spec.tz, "key") else spec.tz),
                "open": spec.open_time.strftime("%H:%M"),
                "close": "24h" if spec.continuous else spec.close_time.strftime("%H:%M"),
                "live": "yes" if spec.live else "no",
                "description": spec.description,
                "source": spec.source_note,
            }
        )
    return rows


__all__ = [
    "BASIS_EOD_CLOSE",
    "BASIS_LABELS",
    "BASIS_LAST_CLOSE",
    "BASIS_SESSION_OPEN",
    "EOD_ONLY",
    "OneDay",
    "POLICY_TABLE",
    "SessionSpec",
    "SessionState",
    "bar_is_provisional",
    "completed_close_from_daily",
    "format_eastern",
    "last_updated_label",
    "latest_completed_bar_date",
    "live_quote_instruments",
    "one_day_from_quote",
    "one_day_return",
    "parse_day",
    "parse_ts",
    "policy_for",
    "policy_id_for",
    "policy_rows",
    "quote_freshness",
    "ratio_one_day",
    "session_close_at",
    "session_open_at",
    "session_state",
]
