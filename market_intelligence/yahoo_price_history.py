"""Incremental Yahoo daily closes for dashboard price returns.

Streamlit does not import this module. Rows live in ``mi_market_bars`` under
``YAHOO_PRICE_DAILY``, separate from MARKET_MONITOR_EOD chart history.
``close_price`` is Yahoo's split-adjusted Close with ``auto_adjust=False`` and
drives the 1W-1Y price-return columns. ``adj_close_price`` is Yahoo's dividend-
and split-adjusted Adj Close from the same download; it feeds the realized
volatility and Sharpe columns only. Yahoo restates the whole Adj Close history
on every dividend, so a dividend (like a split) triggers a full-window repair
and the stored series stays on one vintage.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

import yfinance as yf
from sqlalchemy import bindparam, text

from market_intelligence.nulls import strict_dumps
from market_intelligence.price_returns import (
    OVERLAP_DAYS,
    PRICE_RETURN_BASIS,
    PRICE_RETURN_SOURCE,
    plan_history_window,
)
from market_intelligence.yahoo_dashboard import (
    ET,
    REGULAR_OPEN,
    _frame_rows,
    as_utc,
    bar_session_date,
    dashboard_symbols,
    regular_close,
    valid_price,
    yahoo_symbol,
)
from market_intelligence.calendars import previous_session
from market_intelligence.live_session import latest_opened_rth_session

logger = logging.getLogger("market_intelligence.yahoo_price_history")

CHUNK = 40
Download = Callable[[Sequence[str], date, date], Any]


def last_completed_session(now: datetime) -> date:
    opened = latest_opened_rth_session(now)
    local = as_utc(now).astimezone(ET)
    stamp = local.timetz().replace(tzinfo=None)
    if local.date() == opened and stamp < regular_close(opened):
        return previous_session(opened)
    return opened


def cash_session_open(now: datetime) -> bool:
    local = as_utc(now).astimezone(ET)
    opened = latest_opened_rth_session(now)
    if local.date() != opened:
        return False
    stamp = local.timetz().replace(tzinfo=None)
    return REGULAR_OPEN <= stamp < regular_close(opened)


def parse_daily_closes(frame: Any, symbol: str) -> list[dict[str, Any]]:
    """One symbol's split-adjusted closes. A split ratio other than 1 flags a repair."""
    rows: list[dict[str, Any]] = []
    for raw in _frame_rows(frame, yahoo_symbol(symbol)):
        day = bar_session_date(raw.get("_ts"))
        close = valid_price(raw.get("Close"))
        if day is None or close is None:
            continue
        split = valid_price(raw.get("Stock Splits"))
        rows.append(
            {
                "symbol": symbol,
                "bar_date": day,
                "close": close,
                "adj_close": valid_price(raw.get("Adj Close")),
                "open": valid_price(raw.get("Open")),
                "high": valid_price(raw.get("High")),
                "low": valid_price(raw.get("Low")),
                "volume": raw.get("Volume"),
                "split": split,
                "dividend": valid_price(raw.get("Dividends")),
                "basis": PRICE_RETURN_BASIS,
            }
        )
    return rows


def series_has_split(rows: Sequence[Mapping[str, Any]]) -> bool:
    for row in rows:
        split = row.get("split")
        if split is None:
            continue
        try:
            ratio = float(split)
        except (TypeError, ValueError):
            continue
        if ratio != 1:
            return True
    return False


def series_has_dividend(rows: Sequence[Mapping[str, Any]]) -> bool:
    """A cash dividend restates every earlier Adj Close, so it needs the same repair as a split."""
    for row in rows:
        dividend = _finite(row.get("dividend"))
        if dividend is not None and dividend > 0:
            return True
    return False


def series_needs_repair(rows: Sequence[Mapping[str, Any]]) -> bool:
    return series_has_split(rows) or series_has_dividend(rows)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def bar_quality(bar_date: date, now: datetime) -> str:
    local = as_utc(now).astimezone(ET)
    opened = latest_opened_rth_session(now)
    stamp = local.timetz().replace(tzinfo=None)
    if bar_date == opened and local.date() == opened and stamp < regular_close(opened):
        return "PROVISIONAL"
    return "COMPLETE"


def fetch_daily_closes(symbols: Sequence[str], start: date, end: date) -> Any:
    tickers = [yahoo_symbol(symbol) for symbol in symbols]
    time.sleep(random.uniform(0, 1.5))
    return yf.download(
        tickers,
        start=start.isoformat(),
        end=end.isoformat(),
        interval="1d",
        auto_adjust=False,
        actions=True,
        prepost=False,
        progress=False,
        threads=True,
        timeout=30,
        group_by="column",
    )


def _chunks(symbols: Sequence[str], size: int) -> list[list[str]]:
    return [list(symbols[index : index + size]) for index in range(0, len(symbols), size)]


def load_existing_dates(conn, symbols: Sequence[str]) -> dict[str, set[date]]:
    found: dict[str, set[date]] = {symbol: set() for symbol in symbols}
    if not symbols:
        return found
    statement = text(
        """
        SELECT instrument_id, bar_date
        FROM mi_market_bars
        WHERE source_id = :source
          AND bar_interval = '1D'
          AND instrument_id IN :symbols
        """
    ).bindparams(bindparam("symbols", expanding=True))
    rows = conn.execute(statement, {"source": PRICE_RETURN_SOURCE, "symbols": list(symbols)}).mappings().all()
    for row in rows:
        symbol = str(row["instrument_id"])
        day = row["bar_date"]
        if isinstance(day, datetime):
            day = day.date()
        if symbol in found and isinstance(day, date):
            found[symbol].add(day)
    return found


def symbols_missing_adj_close(conn, symbols: Sequence[str]) -> set[str]:
    """Symbols whose stored completed bars predate the Adj Close column.

    Those rows were written before dividend-adjusted closes were stored. One
    full-window repair fills them; the progress state remembers the attempt so
    a symbol Yahoo never adjusts does not repair on every cycle.
    """
    if not symbols:
        return set()
    statement = text(
        """
        SELECT DISTINCT instrument_id
        FROM mi_market_bars
        WHERE source_id = :source
          AND bar_interval = '1D'
          AND instrument_id IN :symbols
          AND adj_close_price IS NULL
          AND COALESCE(bar_quality, 'COMPLETE') <> 'PROVISIONAL'
        """
    ).bindparams(bindparam("symbols", expanding=True))
    rows = conn.execute(statement, {"source": PRICE_RETURN_SOURCE, "symbols": list(symbols)}).mappings().all()
    return {str(row["instrument_id"]) for row in rows}


def _ensure_source(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mi_source_registry (
                source_id, provider, dataset, enabled, access_status, source_url, expected_cadence,
                units_metadata, usage_scope, terms_notes, attribution, catalog_version, updated_at
            ) VALUES (
                :sid, 'Yahoo Finance (yfinance, unofficial)', 'dashboard_price_history', TRUE,
                'COLLECTOR_ACTIVE', '', 'D', CAST(:units AS JSONB), 'INTERNAL_ONLY',
                'Split-adjusted daily closes for dashboard price returns (cash dividends excluded) plus the '
                'dividend- and split-adjusted Adj Close used only for realized volatility and Sharpe. '
                'Not MARKET_MONITOR_EOD and not EQUITY_EOD.',
                'Yahoo Finance via yfinance (unofficial; no SLA).',
                'yahoo_price_daily_v2', NOW()
            )
            ON CONFLICT (source_id) DO UPDATE SET
                terms_notes = EXCLUDED.terms_notes,
                units_metadata = EXCLUDED.units_metadata,
                catalog_version = EXCLUDED.catalog_version,
                enabled = TRUE,
                updated_at = NOW()
            """
        ),
        {
            "sid": PRICE_RETURN_SOURCE,
            "units": strict_dumps(
                {
                    "price": "split_adjusted_close",
                    "dividends": "excluded",
                    "adj_close_price": "dividend_and_split_adjusted_close",
                    "adj_close_usage": "realized_volatility_and_sharpe_only",
                }
            ),
        },
    )


def grant_price_view(conn) -> None:
    """The migration owner can grant the new view. A missing role is logged and ignored."""
    try:
        with conn.begin_nested():
            conn.execute(text("GRANT SELECT ON mi_v_yahoo_price_daily TO mi_readonly"))
    except Exception as exc:  # noqa: BLE001 - the page grant is retried next cycle
        logger.warning("yahoo price view grant skipped: %s", exc.__class__.__name__)


def _write_bars(conn, rows: Sequence[Mapping[str, Any]], *, now: datetime, run_id: str) -> int:
    if not rows:
        return 0
    instruments = sorted({str(row["symbol"]) for row in rows})
    conn.execute(
        text(
            """
            INSERT INTO mi_market_instruments (instrument_id, display_name, asset_type, security_type, currency)
            VALUES (:id, :id, 'equity', 'etf_or_stock', 'USD')
            ON CONFLICT (instrument_id) DO UPDATE SET updated_at = NOW()
            """
        ),
        [{"id": symbol} for symbol in instruments],
    )
    payload = []
    for row in rows:
        day = row["bar_date"]
        if isinstance(day, datetime):
            day = day.date()
        close = valid_price(row.get("close"))
        if not isinstance(day, date) or close is None:
            continue
        quality = bar_quality(day, now)
        close_at = datetime.combine(day, regular_close(day), tzinfo=ET)
        payload.append(
            {
                "instrument_id": row["symbol"],
                "source_id": PRICE_RETURN_SOURCE,
                "bar_date": day,
                "open": valid_price(row.get("open")),
                "high": valid_price(row.get("high")),
                "low": valid_price(row.get("low")),
                "close": close,
                "adj_close": valid_price(row.get("adj_close")),
                "volume": _finite(row.get("volume")),
                "basis": PRICE_RETURN_BASIS,
                "quality": quality,
                "retrieved_at": as_utc(now),
                "run_id": run_id,
                "yahoo": yahoo_symbol(str(row["symbol"])),
                "bar_ts": as_utc(now) if quality == "PROVISIONAL" else close_at,
            }
        )
    if not payload:
        return 0
    statement = text(
        """
        INSERT INTO mi_market_bars (
            instrument_id, source_id, bar_interval, bar_date, bar_ts,
            open_price, high_price, low_price, close_price, adj_close_price, volume,
            currency, adjustment_basis, bar_quality, retrieved_at, ingestion_run_id,
            first_seen_at, last_seen_at, provider_symbol, provider
        ) VALUES (
            :instrument_id, :source_id, '1D', :bar_date, :bar_ts,
            :open, :high, :low, :close, :adj_close, :volume,
            'USD', :basis, :quality, :retrieved_at, :run_id,
            :retrieved_at, :retrieved_at, :yahoo, 'YAHOO'
        )
        ON CONFLICT (instrument_id, source_id, bar_interval, bar_date) DO UPDATE SET
            open_price = EXCLUDED.open_price,
            high_price = EXCLUDED.high_price,
            low_price = EXCLUDED.low_price,
            close_price = EXCLUDED.close_price,
            adj_close_price = EXCLUDED.adj_close_price,
            volume = EXCLUDED.volume,
            adjustment_basis = EXCLUDED.adjustment_basis,
            bar_quality = EXCLUDED.bar_quality,
            bar_ts = EXCLUDED.bar_ts,
            last_seen_at = EXCLUDED.last_seen_at,
            retrieved_at = EXCLUDED.retrieved_at,
            provider = EXCLUDED.provider,
            provider_symbol = EXCLUDED.provider_symbol
        """
    )
    written = 0
    for offset in range(0, len(payload), 500):
        batch = payload[offset : offset + 500]
        conn.execute(statement, batch)
        written += len(batch)
    return written


def _download_group(
    symbols: Sequence[str],
    start: date,
    end: date,
    *,
    download: Download,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    parsed: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for chunk in _chunks(symbols, CHUNK):
        try:
            frame = download(chunk, start, end)
        except Exception as exc:  # noqa: BLE001 - one chunk must not drop the others
            logger.warning("yahoo history chunk failed: %s", exc.__class__.__name__)
            for symbol in chunk:
                errors[symbol] = exc.__class__.__name__
            continue
        for symbol in chunk:
            try:
                rows = parse_daily_closes(frame, symbol)
            except Exception as exc:  # noqa: BLE001
                errors[symbol] = exc.__class__.__name__
                continue
            if not rows:
                errors[symbol] = "no bars"
                continue
            parsed.extend(rows)
    return parsed, errors


def ingest_price_history(
    conn,
    *,
    now: datetime | None = None,
    symbols: Sequence[str] | None = None,
    progress: Mapping[str, Mapping[str, Any]] | None = None,
    download: Download | None = None,
) -> dict[str, Any]:
    """Upsert missing and overlapping daily closes. A failed symbol keeps its stored rows."""
    moment = now or datetime.now(timezone.utc)
    wanted = list(symbols or dashboard_symbols())
    fetch = download or fetch_daily_closes
    prior = {symbol: dict(row) for symbol, row in (progress or {}).items()}
    today = as_utc(moment).astimezone(ET).date()
    completed = last_completed_session(moment)
    session_open = cash_session_open(moment)
    existing = load_existing_dates(conn, wanted)
    # Rows written before Adj Close was stored get one full-window repair so the
    # volatility and Sharpe windows see a single dividend-adjusted vintage.
    adj_backfill = {
        symbol
        for symbol in symbols_missing_adj_close(conn, wanted)
        if not bool((prior.get(symbol) or {}).get("adj_close_backfilled"))
    }
    groups: dict[tuple[date, date, str], list[str]] = {}
    skipped: list[str] = []
    for symbol in wanted:
        repair = str((prior.get(symbol) or {}).get("status") or "") == "repair" or symbol in adj_backfill
        window = plan_history_window(
            sorted(existing.get(symbol) or ()),
            today=today,
            last_completed=completed,
            session_open=session_open,
            repair=repair,
        )
        if window is None:
            skipped.append(symbol)
            continue
        groups.setdefault(window, []).append(symbol)
    _ensure_source(conn)
    grant_price_view(conn)
    run_id = "yahoo-price-{0}".format(uuid.uuid4().hex[:12])
    written = 0
    errors: dict[str, str] = {}
    repair_next: set[str] = set()
    attempted = datetime.now(timezone.utc).isoformat()
    for (start, end, kind), members in groups.items():
        rows, chunk_errors = _download_group(members, start, end, download=fetch)
        errors.update(chunk_errors)
        delivered = {str(row["symbol"]) for row in rows}
        if kind != "backfill":
            repair_next.update(str(row["symbol"]) for row in rows if series_needs_repair([row]))
        clean = [row for row in rows if row["symbol"] not in chunk_errors]
        written += _write_bars(conn, clean, now=moment, run_id=run_id)
        for symbol in members:
            if symbol in chunk_errors:
                continue
            if symbol not in delivered:
                errors[symbol] = "no bars"
    if repair_next:
        floor = today - timedelta(days=400)
        rows, chunk_errors = _download_group(sorted(repair_next), floor, today + timedelta(days=1), download=fetch)
        errors.update(chunk_errors)
        written += _write_bars(conn, [row for row in rows if row["symbol"] not in chunk_errors], now=moment, run_id=run_id)
    stored = load_existing_dates(conn, wanted)
    instruments: dict[str, dict[str, Any]] = {}
    for symbol in wanted:
        previous = prior.get(symbol) or {}
        latest = max(stored.get(symbol) or {date.min})
        latest_text = None if latest == date.min else latest.isoformat()
        status = "skipped" if symbol in skipped else "failed" if symbol in errors else "ok"
        if symbol in repair_next and symbol not in errors:
            status = "ok"
        instruments[symbol] = {
            "last_attempt_at": attempted,
            "last_success_at": previous.get("last_success_at") if symbol in errors or symbol in skipped else attempted,
            "last_bar_date": latest_text,
            "error": errors.get(symbol),
            "status": status,
            "adj_close_backfilled": bool(previous.get("adj_close_backfilled"))
            or (symbol in adj_backfill and symbol not in errors and symbol not in skipped),
        }
        if symbol in skipped:
            instruments[symbol]["last_success_at"] = previous.get("last_success_at")
    return {
        "status": "OK",
        "written": written,
        "skipped": skipped,
        "errors": errors,
        "instruments": instruments,
        "run_id": run_id,
        "overlap_days": OVERLAP_DAYS,
    }
