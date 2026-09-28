"""Yahoo daily bars for FX, futures proxies, and crypto.

Bars are stored in mi_market_bars under YAHOO_FX, YAHOO_FUTURES_PROXY, or
YAHOO_CRYPTO. Equity EQUITY_EOD and MARKET_MONITOR_EOD rows are not written
or rewritten. Incremental refresh requests a short lookback. Maximum history
is an explicit backfill.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

import yfinance as yf
from sqlalchemy import text

from market_intelligence.cross_asset_universe import YAHOO_CROSS_ASSET, YahooInstrument
from market_intelligence.crypto_analytics import utc_observation_date
from market_intelligence.store import RUN_FAILED, RUN_SUCCEEDED, coverage_with_provider_latest, finish_run, record_freshness, start_run

INCREMENTAL_LOOKBACK_DAYS = 21
PROVIDER = "YAHOO"


@dataclass
class CrossAssetBar:
    instrument: YahooInstrument
    bar_date: date
    open_price: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float | None


@dataclass
class YahooCrossAssetReport:
    status: str
    rows_written: int = 0
    failed: bool = False
    error: str | None = None
    symbols: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rows_written": self.rows_written,
            "failed": self.failed,
            "error": self.error,
            "symbols": list(self.symbols),
        }


def observation_date_for_bar(value: datetime | date, asset_class: str) -> date:
    """Crypto uses the UTC calendar day. Other Yahoo bars keep the provider timestamp's own date."""
    if isinstance(value, datetime) and asset_class == "CRYPTO":
        return utc_observation_date(value)
    if isinstance(value, datetime):
        return value.date()
    return value


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:
        return None
    return number


def bars_from_yahoo_frame(frame: Any, instrument: YahooInstrument) -> list[CrossAssetBar]:
    """Parse one yfinance history frame. Non-positive closes are omitted."""
    if frame is None or getattr(frame, "empty", True):
        return []
    bars: list[CrossAssetBar] = []
    for idx, row in frame.iterrows():
        close = _positive(row.get("Close") if hasattr(row, "get") else None)
        if close is None:
            continue
        stamp = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
        if not isinstance(stamp, datetime):
            continue
        bars.append(
            CrossAssetBar(
                instrument,
                observation_date_for_bar(stamp, instrument.asset_class),
                _positive(row.get("Open")),
                _positive(row.get("High")),
                _positive(row.get("Low")),
                close,
                _positive(row.get("Adj Close")),
            )
        )
    return bars


def fetch_yahoo_history(instrument: YahooInstrument, *, start: date | None) -> list[CrossAssetBar]:
    ticker = yf.Ticker(instrument.yahoo_symbol)
    if start is None:
        frame = ticker.history(period="max", auto_adjust=False, actions=False)
    else:
        frame = ticker.history(start=start.isoformat(), auto_adjust=False, actions=False)
    return bars_from_yahoo_frame(frame, instrument)


def _basis(asset_class: str) -> str:
    if asset_class == "FX":
        return "YAHOO_FX_CLOSE"
    if asset_class == "CRYPTO":
        return "YAHOO_CRYPTO_UTC"
    if asset_class == "FUTURES_PROXY":
        return "YAHOO_FUTURES_PROXY"
    return "YAHOO_INDEX_CLOSE"


def _ensure_instrument(conn, instrument: YahooInstrument) -> None:
    existing = conn.execute(
        text("SELECT asset_type FROM mi_market_instruments WHERE instrument_id = :id"),
        {"id": instrument.instrument_id},
    ).mappings().first()
    if existing is not None and str(existing["asset_type"]).lower() in {"equity", "etf"}:
        raise ValueError("refusing to reuse equity instrument {0}".format(instrument.instrument_id))
    meta = {
        "yahoo_symbol": instrument.yahoo_symbol,
        "base_currency": instrument.base_currency,
        "quote_currency": instrument.quote_currency,
        "invert_vs_usd": instrument.invert_vs_usd,
        "asset_class": instrument.asset_class,
    }
    conn.execute(
        text(
            """
            INSERT INTO mi_market_instruments (
                instrument_id, display_name, asset_type, security_type, currency, provider_classification
            ) VALUES (
                :id, :name, :asset_type, :security_type, :currency, CAST(:meta AS JSONB)
            )
            ON CONFLICT (instrument_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                asset_type = EXCLUDED.asset_type,
                security_type = EXCLUDED.security_type,
                currency = EXCLUDED.currency,
                provider_classification = EXCLUDED.provider_classification,
                updated_at = NOW()
            """
        ),
        {
            "id": instrument.instrument_id,
            "name": instrument.display_name,
            "asset_type": instrument.asset_class,
            "security_type": instrument.group,
            "currency": instrument.currency,
            "meta": json.dumps(meta),
        },
    )


def upsert_cross_asset_bars(conn, bars: Sequence[CrossAssetBar], *, run_id: str, retrieved_at: datetime) -> int:
    written = 0
    for bar in bars:
        if bar.close is None:
            continue
        _ensure_instrument(conn, bar.instrument)
        existing = conn.execute(
            text(
                """
                SELECT provider FROM mi_market_bars
                WHERE instrument_id=:i AND source_id=:s AND bar_interval='1D' AND bar_date=:d
                """
            ),
            {"i": bar.instrument.instrument_id, "s": bar.instrument.source_id, "d": bar.bar_date},
        ).mappings().first()
        if existing is not None and existing["provider"] and existing["provider"] != PROVIDER:
            raise ValueError("refusing to mix provider {0} over {1}".format(PROVIDER, existing["provider"]))
        conn.execute(
            text(
                """
                INSERT INTO mi_market_bars (
                    instrument_id, source_id, bar_interval, bar_date, open_price, high_price, low_price,
                    close_price, adj_close_price, currency, adjustment_basis, retrieved_at,
                    ingestion_run_id, first_seen_at, last_seen_at, provider_symbol, provider
                ) VALUES (
                    :i, :s, '1D', :d, :o, :h, :l, :c, :a, :currency, :adj, :t, :run, :t, :t, :sym, :p
                )
                ON CONFLICT (instrument_id, source_id, bar_interval, bar_date) DO UPDATE SET
                    open_price = EXCLUDED.open_price,
                    high_price = EXCLUDED.high_price,
                    low_price = EXCLUDED.low_price,
                    close_price = EXCLUDED.close_price,
                    adj_close_price = EXCLUDED.adj_close_price,
                    adjustment_basis = EXCLUDED.adjustment_basis,
                    last_seen_at = EXCLUDED.retrieved_at,
                    ingestion_run_id = EXCLUDED.ingestion_run_id,
                    provider_symbol = EXCLUDED.provider_symbol,
                    provider = EXCLUDED.provider
                """
            ),
            {
                "i": bar.instrument.instrument_id,
                "s": bar.instrument.source_id,
                "d": bar.bar_date,
                "o": bar.open_price,
                "h": bar.high,
                "l": bar.low,
                "c": bar.close,
                "a": bar.adj_close,
                "currency": bar.instrument.currency,
                "adj": _basis(bar.instrument.asset_class),
                "t": retrieved_at,
                "run": run_id,
                "sym": bar.instrument.yahoo_symbol,
                "p": PROVIDER,
            },
        )
        written += 1
    return written


def _latest_stored(conn, instrument: YahooInstrument) -> date | None:
    value = conn.execute(
        text(
            """
            SELECT MAX(bar_date) FROM mi_market_bars
            WHERE instrument_id=:i AND source_id=:s AND bar_interval='1D'
            """
        ),
        {"i": instrument.instrument_id, "s": instrument.source_id},
    ).scalar()
    return value


def coverage_rows(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT b.instrument_id, i.asset_type, b.source_id, b.provider_symbol,
                   MIN(b.bar_date) AS earliest, MAX(b.bar_date) AS latest, COUNT(*) AS rows
            FROM mi_market_bars b
            JOIN mi_market_instruments i ON i.instrument_id = b.instrument_id
            WHERE b.source_id IN ('YAHOO_FX', 'YAHOO_FUTURES_PROXY', 'YAHOO_CRYPTO')
              AND b.bar_interval = '1D'
            GROUP BY b.instrument_id, i.asset_type, b.source_id, b.provider_symbol
            ORDER BY b.source_id, b.instrument_id
            """
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def ingest_yahoo_cross_asset(engine, *, parent_run_id: str | None = None, today: date | None = None, mode: str = "incremental") -> YahooCrossAssetReport:
    """``mode='full'`` requests provider max history. Incremental uses a short lookback."""
    full = mode == "full"
    written = 0
    symbols: list[dict[str, Any]] = []
    errors: list[str] = []
    written_by_source: dict[str, int] = {}
    errors_by_source: dict[str, list[str]] = {}
    latest_by_source: dict[str, date] = {}
    retrieved = datetime.now(timezone.utc)
    groups: dict[tuple[str, str], list[YahooInstrument]] = {}
    for instrument in YAHOO_CROSS_ASSET:
        groups.setdefault((instrument.source_id, instrument.dataset), []).append(instrument)
    with engine.begin() as conn:
        runs: dict[tuple[str, str], str] = {}
        for key in groups:
            runs[key] = start_run(conn, source_id=key[0], dataset=key[1], parent_run_id=parent_run_id)
        for instrument in YAHOO_CROSS_ASSET:
            start = None
            if not full:
                stored = _latest_stored(conn, instrument)
                anchor = stored or (today or date.today())
                start = anchor - timedelta(days=INCREMENTAL_LOOKBACK_DAYS)
            try:
                bars = fetch_yahoo_history(instrument, start=start)
            except Exception as exc:  # noqa: BLE001
                errors.append(instrument.yahoo_symbol)
                errors_by_source.setdefault(instrument.source_id, []).append(instrument.yahoo_symbol)
                symbols.append({"symbol": instrument.yahoo_symbol, "error": exc.__class__.__name__})
                continue
            count = upsert_cross_asset_bars(conn, bars, run_id=runs[(instrument.source_id, instrument.dataset)], retrieved_at=retrieved)
            written += count
            written_by_source[instrument.source_id] = written_by_source.get(instrument.source_id, 0) + count
            if bars:
                last = max(bar.bar_date for bar in bars)
                prior = latest_by_source.get(instrument.source_id)
                if prior is None or last > prior:
                    latest_by_source[instrument.source_id] = last
            symbols.append(
                {
                    "symbol": instrument.yahoo_symbol,
                    "instrument_id": instrument.instrument_id,
                    "asset_class": instrument.asset_class,
                    "source_id": instrument.source_id,
                    "rows": count,
                }
            )
        any_failed = False
        for (source_id, dataset), run_id in runs.items():
            source_errors = errors_by_source.get(source_id) or []
            source_written = written_by_source.get(source_id, 0)
            source_failed = source_written == 0 and bool(source_errors)
            any_failed = any_failed or source_failed
            status_one = RUN_FAILED if source_failed else RUN_SUCCEEDED
            transport = "FAILED" if source_failed else ("PARTIAL" if source_errors else "OK")
            finish_run(conn, run_id, status=status_one, counts={"inserted": source_written}, details={"mode": mode, "errors": source_errors})
            record_freshness(
                conn,
                source_id=source_id,
                dataset=dataset,
                cadence="D",
                transport_status=transport,
                latest_observation=latest_by_source.get(source_id),
                success=not source_failed,
                error_redacted="yahoo_fetch_failed" if source_errors else None,
                run_id=run_id,
                today=today,
                coverage_json=coverage_with_provider_latest(latest_by_source.get(source_id), provider="YAHOO"),
            )
    status = RUN_FAILED if any_failed and written == 0 else RUN_SUCCEEDED
    return YahooCrossAssetReport(status=status, rows_written=written, failed=status == RUN_FAILED, error="yahoo_fetch_failed" if errors and written == 0 else None, symbols=symbols)
