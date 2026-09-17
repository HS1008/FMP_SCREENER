"""Yahoo EOD prior-close fallback (exact session only). IBKR EQUITY_EOD remains canonical.

Uses a separate source_id ``YAHOO_EOD`` so:
- IBKR can later supersede without mixed-provider conflicts on EQUITY_EOD
- longer-horizon history loaders stay EQUITY_EOD-only
- live 1D prior-close resolver prefers EQUITY_EOD/IBKR then YAHOO_EOD for the exact baseline session

Never fills D-1 with D-2. Streamlit never calls this module.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import text

from market_intelligence.equity_eod import EquityBar, YahooAdapter
from market_intelligence.live_session import (
    SOURCE_EQUITY_EOD,
    SOURCE_YAHOO_EOD,
    YAHOO_EOD_FALLBACK_ENV,
    live_session_pair,
    yahoo_eod_fallback_enabled,
)
from market_intelligence.nulls import strict_dumps

logger = logging.getLogger("market_intelligence.yahoo_eod_fallback")


def ensure_yahoo_eod_source(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mi_source_registry (
                source_id, provider, dataset, enabled, access_status, source_url, expected_cadence,
                units_metadata, usage_scope, terms_notes, attribution, catalog_version, updated_at
            ) VALUES (
                :sid, 'Yahoo Finance (yfinance, unofficial)', 'equity_daily_prior_close_fallback', FALSE,
                'OPTIONAL_FALLBACK', '', 'D', CAST(:units AS JSONB), 'INTERNAL_ONLY',
                'Exact-session prior-close fallback when IBKR EQUITY_EOD bar is missing. '
                'Never mixes into longer-horizon EQUITY_EOD history. Never labeled as IBKR.',
                'Yahoo Finance via yfinance (unofficial; no SLA).',
                'equity_live_v1', NOW()
            )
            ON CONFLICT (source_id) DO UPDATE SET
                terms_notes = EXCLUDED.terms_notes,
                attribution = EXCLUDED.attribution,
                updated_at = NOW()
            """
        ),
        {"sid": SOURCE_YAHOO_EOD, "units": strict_dumps({"price": "adjusted_close"})},
    )


def _ensure_instrument(conn, symbol: str) -> None:
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


def symbols_missing_ibkr_prior_close(
    conn,
    *,
    session: date,
    symbols: Sequence[str],
) -> list[str]:
    if not symbols:
        return []
    rows = conn.execute(
        text(
            """
            SELECT instrument_id FROM mi_market_bars
            WHERE source_id = :src AND bar_interval = '1D' AND bar_date = :session
              AND instrument_id = ANY(:symbols)
              AND (provider IS NULL OR provider = 'IBKR')
              AND COALESCE(adj_close_price, close_price) IS NOT NULL
            """
        ),
        {"src": SOURCE_EQUITY_EOD, "session": session, "symbols": list(symbols)},
    ).fetchall()
    have = {str(r[0]).upper() for r in rows}
    return [s.upper() for s in symbols if s.upper() not in have]


def upsert_yahoo_eod_bars(conn, bars: Sequence[EquityBar], *, run_id: str, retrieved_at: datetime) -> dict[str, int]:
    """Store Yahoo prior-close bars under YAHOO_EOD. Never writes EQUITY_EOD."""
    ensure_yahoo_eod_source(conn)
    inserted = unchanged = 0
    for bar in bars:
        if bar.bar_date is None or bar.adj_close is None:
            continue
        _ensure_instrument(conn, bar.instrument_id)
        existing = conn.execute(
            text(
                """
                SELECT adj_close_price FROM mi_market_bars
                WHERE instrument_id=:i AND source_id=:s AND bar_interval='1D' AND bar_date=:d
                """
            ),
            {"i": bar.instrument_id, "s": SOURCE_YAHOO_EOD, "d": bar.bar_date},
        ).mappings().first()
        if existing and existing["adj_close_price"] is not None and float(existing["adj_close_price"]) == float(bar.adj_close):
            unchanged += 1
            continue
        close_px = bar.close if bar.close is not None else bar.adj_close
        conn.execute(
            text(
                """
                INSERT INTO mi_market_bars (
                    instrument_id, source_id, bar_interval, bar_date, close_price, adj_close_price,
                    adjustment_basis, retrieved_at, ingestion_run_id, first_seen_at, last_seen_at,
                    provider_symbol, provider
                ) VALUES (
                    :i, :s, '1D', :d, :c, :a, 'YAHOO_AUTO_ADJUST', :t, :run, :t, :t, :sym, 'YAHOO'
                )
                ON CONFLICT (instrument_id, source_id, bar_interval, bar_date) DO UPDATE SET
                    close_price = EXCLUDED.close_price,
                    adj_close_price = EXCLUDED.adj_close_price,
                    last_seen_at = EXCLUDED.last_seen_at,
                    provider = 'YAHOO',
                    retrieved_at = EXCLUDED.retrieved_at
                """
            ),
            {
                "i": bar.instrument_id,
                "s": SOURCE_YAHOO_EOD,
                "d": bar.bar_date,
                "c": close_px,
                "a": bar.adj_close,
                "t": retrieved_at,
                "run": run_id,
                "sym": bar.provider_symbol or bar.instrument_id,
            },
        )
        inserted += 1
    return {"inserted": inserted, "unchanged": unchanged}


def refresh_yahoo_prior_closes(
    conn,
    *,
    symbols: Sequence[str],
    session: date | None = None,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not yahoo_eod_fallback_enabled(env):
        return {"status": "SKIPPED", "reason": "{0} not enabled".format(YAHOO_EOD_FALLBACK_ENV), "outcome": "SKIPPED_NOT_DUE", "inserted": 0}
    pair = live_session_pair(now)
    baseline = session or (pair.baseline_session if pair else None)
    if baseline is None:
        return {"status": "SKIPPED", "reason": "no_baseline_session", "outcome": "SKIPPED_NOT_DUE", "inserted": 0}
    missing = symbols_missing_ibkr_prior_close(conn, session=baseline, symbols=symbols)
    if not missing:
        return {
            "status": "SKIPPED",
            "reason": "ibkr_prior_close_present",
            "outcome": "SKIPPED_ALREADY_CURRENT",
            "inserted": 0,
            "session": baseline.isoformat(),
        }
    try:
        adapter = YahooAdapter()
        bars = adapter.fetch(missing, baseline - timedelta(days=3), baseline)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Yahoo prior-close fetch failed: %s", exc.__class__.__name__)
        return {"status": "FAILED", "reason": exc.__class__.__name__, "outcome": "FAILED_TRANSPORT", "inserted": 0}
    exact = [b for b in bars if b.bar_date == baseline and b.instrument_id.upper() in {m.upper() for m in missing}]
    stored: list[EquityBar] = []
    for bar in exact:
        stored.append(
            EquityBar(
                instrument_id=bar.instrument_id,
                bar_date=bar.bar_date,
                adj_close=bar.adj_close,
                close=bar.close,
                provider_symbol=bar.provider_symbol,
                source_id=SOURCE_YAHOO_EOD,
                provider="YAHOO",
                con_id=bar.con_id,
            )
        )
    run_id = "yahoo-eod-{0}".format(uuid.uuid4().hex[:12])
    retrieved = datetime.now(timezone.utc)
    stats = upsert_yahoo_eod_bars(conn, stored, run_id=run_id, retrieved_at=retrieved)
    return {
        "status": "OK",
        "outcome": "SUCCESS_NEW_DATA" if stats["inserted"] else "SUCCESS_NO_CHANGE",
        "session": baseline.isoformat(),
        "symbols_missing_ibkr": missing,
        "bars_exact_session": len(stored),
        **stats,
    }


__all__ = [
    "SOURCE_YAHOO_EOD",
    "YAHOO_EOD_FALLBACK_ENV",
    "ensure_yahoo_eod_source",
    "refresh_yahoo_prior_closes",
    "symbols_missing_ibkr_prior_close",
    "upsert_yahoo_eod_bars",
    "yahoo_eod_fallback_enabled",
]
