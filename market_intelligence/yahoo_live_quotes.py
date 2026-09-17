"""Yahoo Finance live/intraday quote fallback writer (optional, INTERNAL_ONLY).

Priority remains IBKR live quotes first. This module runs only when
``MI_YAHOO_LIVE_FALLBACK=1`` and never from Streamlit.

Yahoo/yfinance is an unofficial fallback with no SLA. Observations are labeled
``YAHOO_LIVE`` / provider ``YAHOO`` and must never be presented as IBKR.
No redistribution rights are claimed.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import text

from market_intelligence.live_session import SOURCE_YAHOO_LIVE, YAHOO_LIVE_FALLBACK_ENV, yahoo_live_fallback_enabled
from market_intelligence.nulls import strict_dumps
from market_intelligence.taxonomy import BENCHMARK_SPY, SECTOR_PROXIES, UNIVERSE_SYMBOLS

logger = logging.getLogger("market_intelligence.yahoo_live_quotes")

YAHOO_LIVE_COLLECTOR_ID = "yahoo_live_fallback"


def dashboard_live_symbols() -> list[str]:
    return sorted(set(UNIVERSE_SYMBOLS) | {BENCHMARK_SPY, "RSP"} | set(SECTOR_PROXIES.values()))


def ensure_yahoo_live_source(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mi_source_registry (
                source_id, provider, dataset, enabled, access_status, source_url, expected_cadence,
                units_metadata, usage_scope, terms_notes, attribution, catalog_version, updated_at
            ) VALUES (
                :sid, 'Yahoo Finance (yfinance, unofficial)', 'live_quotes_fallback', FALSE, 'OPTIONAL_FALLBACK', '',
                'INTRADAY', CAST(:units AS JSONB), 'INTERNAL_ONLY',
                'Optional fallback when IBKR live quotes are missing or stale. '
                'Never labeled as IBKR. Entitlement/redistribution unverified; INTERNAL_ONLY.',
                'Yahoo Finance via yfinance (unofficial; no SLA).',
                'equity_live_v1', NOW()
            )
            ON CONFLICT (source_id) DO UPDATE SET
                terms_notes = EXCLUDED.terms_notes,
                attribution = EXCLUDED.attribution,
                updated_at = NOW()
            """
        ),
        {"sid": SOURCE_YAHOO_LIVE, "units": strict_dumps({"price": "last_trade_or_regular_market"})},
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
    conn.execute(
        text(
            """
            INSERT INTO mi_instrument_identifiers (instrument_id, id_type, id_value, source_id)
            VALUES (:id, 'SYMBOL', :id, :src)
            ON CONFLICT (id_type, id_value, source_id, valid_from) DO NOTHING
            """
        ),
        {"id": symbol, "src": SOURCE_YAHOO_LIVE},
    )


def fetch_yahoo_last_prices(symbols: Sequence[str]) -> list[dict[str, Any]]:
    """Fetch last/regular-market prices via yfinance. Raises if yfinance missing."""
    import yfinance as yf  # local import: optional dependency for this writer only

    out: list[dict[str, Any]] = []
    retrieved = datetime.now(timezone.utc)
    for symbol in symbols:
        ticker = yf.Ticker(symbol)
        info = {}
        try:
            # fast_info avoids heavier quoteSummary payloads when available
            fast = getattr(ticker, "fast_info", None)
            if fast is not None:
                info = dict(fast) if not isinstance(fast, dict) else fast
        except Exception:  # noqa: BLE001 - fall through to history
            info = {}
        last = None
        for key in ("last_price", "lastPrice", "regular_market_price", "regularMarketPrice"):
            raw = info.get(key) if isinstance(info, Mapping) else getattr(info, key, None)
            try:
                if raw is not None and raw == raw:
                    last = float(raw)
                    break
            except (TypeError, ValueError):
                continue
        quote_ts = retrieved
        if last is None:
            try:
                hist = ticker.history(period="1d", interval="1m")
            except Exception:  # noqa: BLE001
                hist = None
            if hist is not None and not hist.empty:
                close = hist["Close"].dropna()
                if not close.empty:
                    last = float(close.iloc[-1])
                    idx = close.index[-1]
                    if hasattr(idx, "to_pydatetime"):
                        quote_ts = idx.to_pydatetime()
                        if quote_ts.tzinfo is None:
                            quote_ts = quote_ts.replace(tzinfo=timezone.utc)
        if last is None:
            continue
        out.append(
            {
                "symbol": symbol.upper(),
                "last_price": last,
                "quote_ts": quote_ts.isoformat() if hasattr(quote_ts, "isoformat") else str(quote_ts),
                "retrieved_at": retrieved.isoformat(),
                "source_id": SOURCE_YAHOO_LIVE,
                "delay_status": "YAHOO_UNOFFICIAL",
                "quote_status": "LAST",
                "provenance": {
                    "provider": "YAHOO",
                    "adapter": "yfinance",
                    "fallback": True,
                    "rights": "INTERNAL_ONLY_UNVERIFIED",
                    "env_flag": YAHOO_LIVE_FALLBACK_ENV,
                },
            }
        )
    return out


def ingest_yahoo_live_quotes(conn, records: Sequence[Mapping[str, Any]], *, collector_id: str = YAHOO_LIVE_COLLECTOR_ID) -> dict[str, Any]:
    ensure_yahoo_live_source(conn)
    run_id = "yahoo-live-{0}".format(uuid.uuid4().hex[:12])
    inserted = 0
    for raw in records:
        symbol = str(raw.get("symbol") or "").upper()
        last = raw.get("last_price")
        if not symbol or last is None:
            continue
        _ensure_instrument(conn, symbol)
        result = conn.execute(
            text(
                """
                INSERT INTO mi_market_quotes (
                    instrument_id, source_id, quote_ts, last_price, currency,
                    delay_status, quote_status, retrieved_at, ingestion_run_id,
                    provenance, source_ts, record_id
                ) VALUES (
                    :instrument_id, :source_id, CAST(:quote_ts AS TIMESTAMPTZ), :last,
                    'USD', :delay_status, :quote_status, CAST(:retrieved_at AS TIMESTAMPTZ), :run_id,
                    CAST(:provenance AS JSONB), CAST(:source_ts AS TIMESTAMPTZ), :record_id
                )
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "instrument_id": symbol,
                "source_id": SOURCE_YAHOO_LIVE,
                "quote_ts": raw.get("quote_ts"),
                "last": float(last),
                "delay_status": raw.get("delay_status") or "YAHOO_UNOFFICIAL",
                "quote_status": raw.get("quote_status") or "LAST",
                "retrieved_at": raw.get("retrieved_at"),
                "run_id": run_id,
                "provenance": strict_dumps(raw.get("provenance") or {"provider": "YAHOO"}),
                "source_ts": raw.get("quote_ts"),
                "record_id": "YAHOO_LIVE:{0}:{1}".format(symbol, raw.get("quote_ts")),
            },
        )
        if result.rowcount:
            inserted += 1
    return {"received": len(records), "inserted": inserted, "run_id": run_id, "collector_id": collector_id}


def refresh_yahoo_live_quotes(conn, *, symbols: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    if not yahoo_live_fallback_enabled(env):
        return {
            "status": "SKIPPED",
            "reason": "{0} not enabled".format(YAHOO_LIVE_FALLBACK_ENV),
            "inserted": 0,
        }
    wanted = list(symbols or dashboard_live_symbols())
    try:
        records = fetch_yahoo_last_prices(wanted)
    except ImportError as exc:
        logger.warning("yfinance unavailable for Yahoo live fallback: %s", exc)
        return {"status": "UNAVAILABLE", "reason": "yfinance_not_installed", "inserted": 0}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Yahoo live fetch failed: %s", exc.__class__.__name__)
        return {"status": "FAILED", "reason": exc.__class__.__name__, "inserted": 0}
    stats = ingest_yahoo_live_quotes(conn, records)
    stats["status"] = "OK"
    stats["symbols_requested"] = len(wanted)
    stats["symbols_fetched"] = len(records)
    return stats


__all__ = [
    "SOURCE_YAHOO_LIVE",
    "YAHOO_LIVE_COLLECTOR_ID",
    "YAHOO_LIVE_FALLBACK_ENV",
    "dashboard_live_symbols",
    "ensure_yahoo_live_source",
    "fetch_yahoo_last_prices",
    "ingest_yahoo_live_quotes",
    "refresh_yahoo_live_quotes",
    "yahoo_live_fallback_enabled",
]
