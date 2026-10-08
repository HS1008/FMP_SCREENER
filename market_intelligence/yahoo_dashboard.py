"""Yahoo quotes for the dashboard universe.

Streamlit does not call this module. A server job fetches prices, caches the
regular-session open separately, and writes rows to PostgreSQL.

The since-open return is latest price / regular-session open - 1. It is not
Yahoo's daily change and it is not a previous-close return.
"""

from __future__ import annotations

import logging
import math
import random
import time
import uuid
from datetime import date, datetime, time as clock_time, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import yfinance as yf

from market_intelligence.calendars import CAL_NYSE, is_session, nyse_early_close_dates, nyse_regular_close
from market_intelligence.cross_asset_universe import INSTRUMENT_BY_ID
from market_intelligence.ibkr_live_universe import approved_contracts, yahoo_symbol as _equity_yahoo_symbol
from market_intelligence.live_session import latest_opened_rth_session
from market_intelligence.nulls import strict_dumps
from market_intelligence.return_policy import (
    SessionState,
    completed_close_from_daily,
    live_quote_instruments,
    one_day_return,
    policy_for,
    session_state,
)

logger = logging.getLogger("market_intelligence.yahoo_dashboard")

ET = ZoneInfo("America/New_York")
SOURCE_YAHOO_DASHBOARD = "YAHOO_DASHBOARD"
POLL_SECONDS = 15 * 60
CLOSED_POLL_SECONDS = 12 * 60
ACTIVE_START = clock_time(4, 0)
ACTIVE_END = clock_time(20, 0)
REGULAR_OPEN = clock_time(9, 30)


def dashboard_symbols() -> tuple[str, ...]:
    """Equity/ETF universe plus VIX. These also receive YAHOO_PRICE_DAILY history."""
    return tuple(row["symbol"] for row in approved_contracts())


def cross_asset_quote_symbols() -> tuple[str, ...]:
    """Tracked FX, futures-proxy, and crypto instrument ids that receive intraday quotes."""
    return tuple(spec.instrument_id for spec in live_quote_instruments())


def quote_symbols() -> tuple[str, ...]:
    """Every tracked Yahoo instrument the quote collector polls. No new instruments."""
    return dashboard_symbols() + cross_asset_quote_symbols()


def is_cross_asset_symbol(symbol: str) -> bool:
    key = str(symbol or "").upper().strip()
    return key in INSTRUMENT_BY_ID and key != "VIX"


def yahoo_symbol(symbol: str) -> str:
    """Yahoo ticker for a dashboard symbol or a cross-asset instrument id."""
    key = str(symbol or "").upper().strip()
    if is_cross_asset_symbol(key):
        return INSTRUMENT_BY_ID[key].yahoo_symbol
    return _equity_yahoo_symbol(key)


def valid_price(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def early_close_dates(year: int) -> set[date]:
    """NYSE 13:00 ET closes. Shared with the read-side session policy."""
    return nyse_early_close_dates(year)


def regular_close(session: date) -> clock_time:
    return nyse_regular_close(session)


def session_open_at(session: date) -> datetime:
    return datetime.combine(session, REGULAR_OPEN, tzinfo=ET)


def classify_session(ts: datetime, *, instrument: str = "equity") -> str:
    """Label a real observation. VIX has no equity extended-hours session."""
    local = as_utc(ts).astimezone(ET)
    day = local.date()
    stamp = local.timetz().replace(tzinfo=None)
    if instrument == "VIX":
        if is_session(day, CAL_NYSE) and REGULAR_OPEN <= stamp < clock_time(16, 15):
            return "regular"
        return "closed"
    if not is_session(day, CAL_NYSE):
        if stamp >= clock_time(20, 0) or stamp < ACTIVE_START:
            return "overnight"
        return "closed"
    close = regular_close(day)
    if REGULAR_OPEN <= stamp < close:
        return "regular"
    if ACTIVE_START <= stamp < REGULAR_OPEN:
        return "premarket"
    if close <= stamp < ACTIVE_END:
        return "postmarket"
    if stamp >= ACTIVE_END or stamp < ACTIVE_START:
        return "overnight"
    return "unknown"


def in_active_collection_window(now: datetime) -> bool:
    local = as_utc(now).astimezone(ET)
    if not is_session(local.date(), CAL_NYSE):
        return False
    stamp = local.timetz().replace(tzinfo=None)
    return ACTIVE_START <= stamp < ACTIVE_END


def should_poll(now: datetime, last_success: datetime | None, *, closed_seconds: int = CLOSED_POLL_SECONDS) -> bool:
    if in_active_collection_window(now):
        return True
    if last_success is None:
        return True
    age = (as_utc(now) - as_utc(last_success)).total_seconds()
    return age >= closed_seconds


def newest_observation(candidates: Sequence[tuple[datetime | None, Any, str]]) -> tuple[datetime, float, str] | None:
    """Newest valid price by its own timestamp. Older regular prices do not win."""
    chosen: tuple[datetime, float, str] | None = None
    for raw_ts, raw_price, field in candidates:
        price = valid_price(raw_price)
        if raw_ts is None or price is None:
            continue
        ts = as_utc(raw_ts)
        if chosen is None or ts > chosen[0]:
            chosen = (ts, price, field)
    return chosen


def since_open_fraction(
    price: Any,
    price_ts: datetime | None,
    session_open: Any,
    open_session: date | None,
    now: datetime,
) -> float | None:
    """(price / regular-session open) - 1, or None when the pair is not usable.

    Before 09:30 ET the denominator is the prior session. At the new open, a
    price from before that open does not pair with the new open.
    """
    px = valid_price(price)
    opened = valid_price(session_open)
    if px is None or opened is None or price_ts is None or open_session is None:
        return None
    expected = latest_opened_rth_session(now)
    if open_session != expected:
        return None
    if as_utc(price_ts) < session_open_at(expected):
        return None
    return px / opened - 1.0


def split_blocks_comparison(split_dates: Sequence[date], open_session: date, price_day: date) -> bool:
    return any(open_session < day <= price_day for day in split_dates)


def reference_open(
    daily_opens: Mapping[date, float],
    minute_bars: Sequence[tuple[datetime, float]],
    now: datetime,
) -> tuple[date, float, str] | None:
    """Unadjusted regular open, else the first regular-session minute bar."""
    session = latest_opened_rth_session(now)
    official = valid_price(daily_opens.get(session))
    if official is not None:
        return session, official, "regular_session_open"
    opening = session_open_at(session)
    proxy: tuple[datetime, float] | None = None
    for ts, raw in minute_bars:
        price = valid_price(raw)
        if price is None:
            continue
        stamp = as_utc(ts)
        if stamp < opening or stamp.astimezone(ET).date() != session:
            continue
        if proxy is None or stamp < proxy[0]:
            proxy = (stamp, price)
    if proxy is None:
        return None
    return session, proxy[1], "first_regular_bar"


def backoff_seconds(attempt: int, *, base: float = 2.0, cap: float = 900.0) -> float:
    exponent = min(max(attempt, 0), 8)
    return min(cap, base * (2 ** exponent)) + random.uniform(0, 1.0)


def _frame_rows(frame: Any, yahoo: str) -> list[dict[str, Any]]:
    if frame is None or getattr(frame, "empty", True):
        return []
    data = frame
    columns = getattr(data, "columns", None)
    if columns is not None and hasattr(columns, "nlevels") and columns.nlevels > 1:
        level = 1 if yahoo in set(columns.get_level_values(-1)) else 0
        if yahoo not in set(columns.get_level_values(level)):
            return []
        data = data.xs(yahoo, axis=1, level=level)
    rows: list[dict[str, Any]] = []
    for idx, row in data.iterrows():
        if hasattr(idx, "to_pydatetime"):
            ts = idx.to_pydatetime()
        else:
            continue
        item = {str(col): row[col] for col in data.columns}
        item["_ts"] = ts
        rows.append(item)
    return rows


def parse_minute_bars(frame: Any, yahoo: str) -> list[tuple[datetime, float]]:
    bars: list[tuple[datetime, float]] = []
    for row in _frame_rows(frame, yahoo):
        price = valid_price(row.get("Close"))
        if price is None:
            continue
        bars.append((as_utc(row["_ts"]), price))
    return bars


def bar_session_date(idx: Any) -> date | None:
    """Yahoo daily bars are session dates. A naive midnight stamp is that date."""
    value = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.date()
        return as_utc(value).astimezone(ET).date()
    if isinstance(value, date):
        return value
    return None


def parse_daily_opens(frame: Any, yahoo: str) -> tuple[dict[date, float], list[date]]:
    opens: dict[date, float] = {}
    splits: list[date] = []
    for row in _frame_rows(frame, yahoo):
        day = bar_session_date(row["_ts"])
        if day is None:
            continue
        opened = valid_price(row.get("Open"))
        if opened is not None:
            opens[day] = opened
        split = valid_price(row.get("Stock Splits"))
        if split is not None and split != 1:
            splits.append(day)
    return opens, splits


def parse_daily_closes_map(frame: Any, yahoo: str) -> dict[date, float]:
    """Session date -> Close from the daily frame. The in-progress bar is included; the policy decides completeness."""
    closes: dict[date, float] = {}
    for row in _frame_rows(frame, yahoo):
        day = bar_session_date(row["_ts"])
        close = valid_price(row.get("Close"))
        if day is not None and close is not None:
            closes[day] = close
    return closes


def fetch_yahoo_frames(symbols: Sequence[str], *, minute_period: str = "1d") -> tuple[Any, Any]:
    """One intraday request and one daily request. No proxy, bounded timeout.

    ``minute_period="2d"`` is used for cross-asset instruments so the last print
    before a session roll is available as a close fallback.
    """
    tickers = [yahoo_symbol(symbol) for symbol in symbols]
    jitter = random.uniform(0, 1.5)
    time.sleep(jitter)
    minutes = yf.download(
        tickers,
        period=minute_period,
        interval="1m",
        prepost=True,
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=True,
        timeout=25,
        group_by="column",
    )
    daily = yf.download(
        tickers,
        period="10d",
        interval="1d",
        prepost=False,
        auto_adjust=False,
        actions=True,
        progress=False,
        threads=True,
        timeout=25,
        group_by="column",
    )
    return minutes, daily


def build_observations(
    symbols: Sequence[str],
    minute_frame: Any,
    daily_frame: Any,
    *,
    now: datetime,
    cached_opens: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Per-symbol rows. A bad symbol does not drop the others."""
    cache = cached_opens or {}
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        yahoo = yahoo_symbol(symbol)
        try:
            rows.append(_one_observation(symbol, yahoo, minute_frame, daily_frame, now=now, cached=cache.get(symbol)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("yahoo quote parse failed for %s: %s", symbol, exc.__class__.__name__)
            rows.append(_empty_observation(symbol, yahoo, "parse failed"))
    return rows


def _empty_observation(symbol: str, yahoo: str, error: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "yahoo_symbol": yahoo,
        "last_price": None,
        "quote_ts": None,
        "session": "unknown",
        "session_open": None,
        "session_date": None,
        "open_basis": None,
        "open_to_current": None,
        "quote_error": error,
        "price_field": None,
        "policy_id": policy_for(symbol).policy_id,
        "session_state": None,
        "last_close": None,
        "last_close_date": None,
        "close_basis": None,
        "one_day": None,
    }


def policy_session_open(
    daily_opens: Mapping[date, float],
    minute_bars: Sequence[tuple[datetime, float]],
    state: SessionState,
) -> tuple[date, float, str] | None:
    """Open of the policy's current session: the provider daily Open, else the first in-session print."""
    official = valid_price(daily_opens.get(state.session_date))
    if official is not None:
        return state.session_date, official, "regular_session_open"
    proxy: tuple[datetime, float] | None = None
    for ts, raw in minute_bars:
        price = valid_price(raw)
        if price is None:
            continue
        stamp = as_utc(ts)
        if stamp < state.open_at or stamp >= state.close_at:
            continue
        if proxy is None or stamp < proxy[0]:
            proxy = (stamp, price)
    if proxy is None:
        return None
    return state.session_date, proxy[1], "first_session_bar"


def policy_last_close(
    daily_closes: Mapping[date, float],
    minute_bars: Sequence[tuple[datetime, float]],
    state: SessionState,
    *,
    allow_minute_fallback: bool,
) -> tuple[date, float, str] | None:
    """Most recent completed close for the policy session, else the last print before the current open."""
    found = completed_close_from_daily(daily_closes, state)
    if found is not None:
        return found[0], found[1], "daily_close"
    if not allow_minute_fallback or not state.active:
        return None
    last: tuple[datetime, float] | None = None
    for ts, raw in minute_bars:
        price = valid_price(raw)
        if price is None:
            continue
        stamp = as_utc(ts)
        if stamp >= state.open_at:
            continue
        if last is None or stamp > last[0]:
            last = (stamp, price)
    if last is None:
        return None
    return state.previous_session_date, last[1], "last_bar_before_open"


def _one_observation(
    symbol: str,
    yahoo: str,
    minute_frame: Any,
    daily_frame: Any,
    *,
    now: datetime,
    cached: Mapping[str, Any] | None,
) -> dict[str, Any]:
    cross_asset = is_cross_asset_symbol(symbol)
    instrument = "VIX" if symbol == "VIX" else "equity"
    spec = policy_for(symbol)
    state = session_state(spec, now)
    bars = parse_minute_bars(minute_frame, yahoo)
    daily_opens, splits = parse_daily_opens(daily_frame, yahoo)
    daily_closes = parse_daily_closes_map(daily_frame, yahoo)
    latest = newest_observation([(ts, px, "minute_close") for ts, px in bars])
    if latest is None:
        return _empty_observation(symbol, yahoo, "no Yahoo price")
    price_ts, price, _field = latest
    change = None
    error = None
    open_px = None
    open_day = None
    basis = None
    session_name = classify_session(price_ts, instrument=instrument) if not cross_asset else state.state
    if cross_asset:
        opened = policy_session_open(daily_opens, bars, state)
        if opened is None:
            error = "session open unavailable"
        else:
            open_day, open_px, basis = opened
    else:
        opened = reference_open(daily_opens, [(ts, px) for ts, px in bars], now)
        if opened is None and cached and str(cached.get("session_date") or "") == latest_opened_rth_session(now).isoformat():
            cached_open = valid_price(cached.get("open"))
            if cached_open is not None:
                opened = (latest_opened_rth_session(now), cached_open, str(cached.get("basis") or "regular_session_open"))
        if opened is None:
            error = "regular-session open unavailable"
        else:
            open_day, open_px, basis = opened
            price_day = price_ts.astimezone(ET).date()
            if split_blocks_comparison(splits, open_day, price_day):
                error = "corporate action between the open and the price"
            elif instrument == "VIX" and session_name != "regular" and price_ts < session_open_at(open_day):
                error = "VIX since-open reference unavailable"
            else:
                change = since_open_fraction(price, price_ts, open_px, open_day, now)
                if change is None:
                    error = "since-open pending a price from this regular session"
    closed = policy_last_close(daily_closes, bars, state, allow_minute_fallback=cross_asset)
    one_day = one_day_return(
        symbol,
        price=price,
        price_ts=price_ts,
        session_open=open_px,
        session_open_date=open_day,
        last_close=None if closed is None else closed[1],
        last_close_date=None if closed is None else closed[0],
        now=now,
        corporate_action=bool(error and "corporate action" in error),
    )
    return {
        "symbol": symbol,
        "yahoo_symbol": yahoo,
        "last_price": price,
        "quote_ts": price_ts,
        "session": session_name,
        "session_open": open_px,
        "session_date": open_day.isoformat() if open_day else None,
        "open_basis": basis,
        "open_to_current": change,
        "quote_error": error,
        "price_field": "minute_close",
        "policy_id": spec.policy_id,
        "session_state": state.state,
        "last_close": None if closed is None else closed[1],
        "last_close_date": None if closed is None else closed[0].isoformat(),
        "close_basis": None if closed is None else closed[2],
        "one_day": one_day.as_dict(),
    }


def observation_record(row: Mapping[str, Any], *, retrieved_at: datetime) -> dict[str, Any] | None:
    price = valid_price(row.get("last_price"))
    quote_ts = row.get("quote_ts")
    if price is None or not isinstance(quote_ts, datetime):
        return None
    if as_utc(quote_ts) == as_utc(retrieved_at):
        return None
    change = row.get("open_to_current")
    provenance = {
        "provider": "YAHOO",
        "adapter": "yfinance",
        "symbol": row["symbol"],
        "yahoo_symbol": row["yahoo_symbol"],
        "current_price": price,
        "current_price_field": row.get("price_field"),
        "session": row.get("session"),
        "session_open": row.get("session_open"),
        "session_date": row.get("session_date"),
        "open_basis": row.get("open_basis"),
        "open_to_current": change,
        "since_open_pct": None if change is None else float(change) * 100.0,
        "quote_error": row.get("quote_error"),
        "rights": "INTERNAL_ONLY_UNVERIFIED",
        # Explicit 1D lineage: which session policy, whether that session was
        # open at collection time, and both reference prices with their dates.
        # The page recomputes the 1D basis from these at read time.
        "policy_id": row.get("policy_id") or policy_for(str(row["symbol"])).policy_id,
        "session_state": row.get("session_state"),
        "last_close": row.get("last_close"),
        "last_close_date": row.get("last_close_date"),
        "close_basis": row.get("close_basis"),
        "one_day": row.get("one_day"),
    }
    one_day = row.get("one_day") if isinstance(row.get("one_day"), Mapping) else None
    if one_day:
        provenance["reference_price"] = one_day.get("reference_price")
        provenance["reference_ts"] = one_day.get("reference_ts")
        provenance["reference_basis"] = one_day.get("basis")
    quote_iso = as_utc(quote_ts).isoformat()
    return {
        "symbol": row["symbol"],
        "last_price": price,
        "quote_ts": quote_iso,
        "retrieved_at": as_utc(retrieved_at).isoformat(),
        "source_ts": quote_iso,
        "record_id": "YAHOO_DASHBOARD:{0}:{1}:{2}".format(row["symbol"], quote_iso, price),
        "provenance": provenance,
    }


def cache_opens(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    cached: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("session_open") is None or not row.get("session_date"):
            continue
        cached[str(row["symbol"])] = {
            "open": row["session_open"],
            "session_date": row["session_date"],
            "basis": row.get("open_basis"),
        }
    return cached


def ingest_dashboard_quotes(conn, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    from sqlalchemy import text

    _ensure_source(conn)
    run_id = "yahoo-dashboard-{0}".format(uuid.uuid4().hex[:12])
    inserted = 0
    rejected = 0
    for raw in records:
        if raw is None:
            rejected += 1
            continue
        symbol = str(raw.get("symbol") or "")
        if not symbol or raw.get("last_price") is None or not raw.get("quote_ts"):
            rejected += 1
            continue
        _ensure_instrument(conn, symbol)
        result = conn.execute(
            text(
                """
                INSERT INTO mi_market_quotes (
                    instrument_id, source_id, quote_ts, last_price, currency,
                    delay_status, quote_status, retrieved_at, ingestion_run_id,
                    provenance, source_ts, record_id, market_data_type
                ) VALUES (
                    :instrument_id, :source_id, CAST(:quote_ts AS TIMESTAMPTZ), :last,
                    'USD', 'YAHOO_UNOFFICIAL', 'LAST', CAST(:retrieved_at AS TIMESTAMPTZ), :run_id,
                    CAST(:provenance AS JSONB), CAST(:source_ts AS TIMESTAMPTZ), :record_id, 'YAHOO'
                )
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "instrument_id": symbol,
                "source_id": SOURCE_YAHOO_DASHBOARD,
                "quote_ts": raw["quote_ts"],
                "last": float(raw["last_price"]),
                "retrieved_at": raw["retrieved_at"],
                "run_id": run_id,
                "provenance": strict_dumps(raw.get("provenance") or {}),
                "source_ts": raw["source_ts"],
                "record_id": raw["record_id"],
            },
        )
        if result.rowcount:
            inserted += 1
        else:
            rejected += 1
    return {"inserted": inserted, "rejected": rejected, "received": len(records), "run_id": run_id}


def _ensure_source(conn) -> None:
    from sqlalchemy import text

    conn.execute(
        text(
            """
            INSERT INTO mi_source_registry (
                source_id, provider, dataset, enabled, access_status, source_url, expected_cadence,
                units_metadata, usage_scope, terms_notes, attribution, catalog_version, updated_at
            ) VALUES (
                :sid, 'Yahoo Finance (yfinance, unofficial)', 'dashboard_quotes', TRUE, 'COLLECTOR_ACTIVE', '',
                'INTRADAY', CAST(:units AS JSONB), 'INTERNAL_ONLY',
                'Dashboard market quotes. Not a guaranteed real-time feed. Never an IBKR quote.',
                'Yahoo Finance via yfinance (unofficial; no SLA).',
                'yahoo_dashboard_v1', NOW()
            )
            ON CONFLICT (source_id) DO UPDATE SET
                enabled = TRUE,
                terms_notes = EXCLUDED.terms_notes,
                updated_at = NOW()
            """
        ),
        {"sid": SOURCE_YAHOO_DASHBOARD, "units": strict_dumps({"price": "last_trade"})},
    )


def _ensure_instrument(conn, symbol: str) -> None:
    from sqlalchemy import text

    conn.execute(
        text(
            """
            INSERT INTO mi_market_instruments (instrument_id, display_name, asset_type, security_type, currency)
            VALUES (:id, :id, 'equity', 'etf_or_stock', 'USD')
            ON CONFLICT (instrument_id) DO UPDATE SET updated_at = NOW()
            """
        ),
        {"id": symbol},
    )


def _quote_groups(symbols: Sequence[str]) -> list[tuple[list[str], str]]:
    """Equities share one request; cross-asset instruments share another with two days of prints."""
    equities = [symbol for symbol in symbols if not is_cross_asset_symbol(symbol)]
    cross = [symbol for symbol in symbols if is_cross_asset_symbol(symbol)]
    groups: list[tuple[list[str], str]] = []
    if equities:
        groups.append((equities, "1d"))
    if cross:
        groups.append((cross, "2d"))
    return groups


def collect_once(conn, *, now: datetime | None = None, symbols: Sequence[str] | None = None, cached_opens: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    wanted = list(symbols or quote_symbols())
    built: list[dict[str, Any]] = []
    failed_groups: list[str] = []
    groups = _quote_groups(wanted)
    for members, period in groups:
        try:
            minutes, daily = fetch_yahoo_frames(members, minute_period=period)
        except Exception as exc:  # noqa: BLE001 - one provider request must not drop the other group
            logger.warning("yahoo download failed (%s symbols): %s", len(members), exc.__class__.__name__)
            failed_groups.append(exc.__class__.__name__)
            built.extend(_empty_observation(symbol, yahoo_symbol(symbol), exc.__class__.__name__) for symbol in members)
            continue
        built.extend(build_observations(members, minutes, daily, now=moment, cached_opens=cached_opens))
    if groups and len(failed_groups) == len(groups):
        return {"status": "ERROR", "error": failed_groups[0], "inserted": 0, "symbols": len(wanted)}
    records = []
    priced = 0
    for row in built:
        record = observation_record(row, retrieved_at=moment)
        if record is None:
            continue
        records.append(record)
        priced += 1
    stored = ingest_dashboard_quotes(conn, records)
    stored.update(
        {
            "status": "OK" if priced else "EMPTY",
            "priced": priced,
            "symbols": len(wanted),
            "opens": cache_opens(built),
            "missing": [row["symbol"] for row in built if row.get("last_price") is None],
        }
    )
    return stored
