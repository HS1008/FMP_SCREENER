"""Persist approved IBKR market-data records and collector heartbeats.

Callers own the connection/transaction. No arbitrary SQL, research writes, or account rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import text

from market_intelligence import CODE_VERSION
from market_intelligence.adapters import IBKR_SOURCE_ID
from market_intelligence.nulls import normalize_numeric, strict_dumps
from market_intelligence.store import RUN_SUCCEEDED, finish_run, record_freshness, start_run

ALLOWED_STATES = frozenset(
    {
        "WAITING_FOR_TWS",
        "SOCKET_OPEN_HANDSHAKE_PENDING",
        "API_AUTHENTICATED",
        "CONNECTED",
        "DISCONNECTED",
        "ENTITLEMENT_ERROR",
        "DELIVERY_FAILURE",
        "COLLECTOR_OFFLINE",
    }
)
ALLOWED_MD_TYPES = frozenset({"LIVE", "DELAYED", "FROZEN", "DELAYED_FROZEN", "UNAVAILABLE"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_ibkr_source(conn) -> None:
    """Insert/update only the IBKR registry row. Never clobber FRED/FINRA/EDGAR flags."""
    conn.execute(
        text(
            """
            INSERT INTO mi_source_registry (
                source_id, provider, dataset, enabled, access_status, source_url, expected_cadence,
                units_metadata, usage_scope, terms_notes, attribution, catalog_version, updated_at
            ) VALUES (
                :source_id, 'Interactive Brokers', 'market_quotes', TRUE, 'COLLECTOR_ACTIVE', '',
                'INTRADAY', '{}'::jsonb, 'INTERNAL_ONLY',
                'Windows-local read-only TWS collector. Server never opens a TWS socket. No orders.',
                'Interactive Brokers market data (entitlement dependent).',
                :catalog, NOW()
            )
            ON CONFLICT (source_id) DO UPDATE SET
                enabled = TRUE,
                access_status = 'COLLECTOR_ACTIVE',
                dataset = EXCLUDED.dataset,
                expected_cadence = EXCLUDED.expected_cadence,
                terms_notes = EXCLUDED.terms_notes,
                attribution = EXCLUDED.attribution,
                updated_at = NOW()
            """
        ),
        {"source_id": IBKR_SOURCE_ID, "catalog": "ibkr_collector_v1"},
    )


def instrument_id_for(*, con_id: int | None, symbol: str, sec_type: str, currency: str | None, exchange: str | None) -> str:
    if con_id:
        return "IBKR:{0}".format(int(con_id))
    parts = ["IBKR", sec_type or "UNK", symbol or "UNK", currency or "USD", exchange or "SMART"]
    return ":".join(parts)


def upsert_instrument(
    conn,
    *,
    instrument_id: str,
    symbol: str,
    sec_type: str,
    con_id: int | None,
    currency: str | None,
    exchange: str | None,
    primary_exchange: str | None,
    display_name: str | None,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mi_market_instruments (
                instrument_id, display_name, asset_type, security_type, currency, exchange, con_id, primary_exchange, updated_at
            ) VALUES (
                :id, :name, :asset, :sec, :ccy, :exch, :con_id, :primary, NOW()
            )
            ON CONFLICT (instrument_id) DO UPDATE SET
                display_name = COALESCE(EXCLUDED.display_name, mi_market_instruments.display_name),
                asset_type = EXCLUDED.asset_type,
                security_type = EXCLUDED.security_type,
                currency = COALESCE(EXCLUDED.currency, mi_market_instruments.currency),
                exchange = COALESCE(EXCLUDED.exchange, mi_market_instruments.exchange),
                con_id = COALESCE(EXCLUDED.con_id, mi_market_instruments.con_id),
                primary_exchange = COALESCE(EXCLUDED.primary_exchange, mi_market_instruments.primary_exchange),
                updated_at = NOW()
            """
        ),
        {
            "id": instrument_id,
            "name": display_name or symbol,
            "asset": _asset_type(sec_type),
            "sec": sec_type,
            "ccy": currency,
            "exch": exchange,
            "con_id": con_id,
            "primary": primary_exchange,
        },
    )
    identifiers = [("SYMBOL", symbol)]
    if con_id:
        identifiers.append(("CONID", str(int(con_id))))
    for id_type, id_value in identifiers:
        if not id_value:
            continue
        conn.execute(
            text(
                """
                INSERT INTO mi_instrument_identifiers (instrument_id, id_type, id_value, source_id)
                VALUES (:id, :t, :v, :src)
                ON CONFLICT (id_type, id_value, source_id, valid_from) DO NOTHING
                """
            ),
            {"id": instrument_id, "t": id_type, "v": id_value, "src": IBKR_SOURCE_ID},
        )


def _asset_type(sec_type: str) -> str:
    mapping = {"STK": "EQUITY", "ETF": "ETF", "BOND": "BOND", "FUND": "FUND", "IND": "INDEX"}
    return mapping.get(sec_type or "", sec_type or "UNK")


def ingest_quotes(conn, records: list[Mapping[str, Any]], *, collector_id: str) -> dict[str, Any]:
    ensure_ibkr_source(conn)
    conn.execute(
        text(
            """
            INSERT INTO mi_collector_status (collector_id, source_id, reported_state, last_heartbeat_at, updated_at)
            VALUES (:cid, :src, 'CONNECTED', NOW(), NOW())
            ON CONFLICT (collector_id) DO NOTHING
            """
        ),
        {"cid": collector_id, "src": IBKR_SOURCE_ID},
    )
    run_id = start_run(conn, source_id=IBKR_SOURCE_ID, dataset="market_quotes")
    received = inserted = unchanged = rejected = 0
    results: list[dict[str, Any]] = []
    latest_callback = None
    for raw in records:
        received += 1
        record_id = str(raw.get("record_id") or "")
        nested = conn.begin_nested()
        try:
            inserted_flag = _insert_quote(conn, raw, run_id=run_id)
            nested.commit()
        except Exception as exc:
            nested.rollback()
            reason = exc.__class__.__name__
            results.append({"record_id": record_id, "outcome": "rejected", "reason": reason})
            rejected += 1
            continue
        if inserted_flag:
            inserted += 1
            results.append({"record_id": record_id, "outcome": "committed", "reason": None})
        else:
            unchanged += 1
            results.append({"record_id": record_id, "outcome": "duplicate", "reason": None})
        callback = raw.get("last_callback_at") or raw.get("source_ts")
        if callback and (latest_callback is None or str(callback) > str(latest_callback)):
            latest_callback = callback
    latest_date = None
    if latest_callback:
        parsed = datetime.fromisoformat(str(latest_callback).replace("Z", "+00:00"))
        latest_date = parsed.date()
    finish_run(
        conn,
        run_id,
        status=RUN_SUCCEEDED,
        counts={"received": received, "inserted": inserted, "unchanged": unchanged, "rejected": rejected},
        details={"collector_id": collector_id, "code_version": CODE_VERSION},
    )
    record_freshness(
        conn,
        source_id=IBKR_SOURCE_ID,
        dataset="market_quotes",
        cadence="INTRADAY",
        transport_status="OK" if rejected == 0 else "PARTIAL",
        latest_observation=latest_date,
        success=rejected < received,
        error_redacted=None if rejected == 0 else "{0} quote(s) rejected".format(rejected),
        run_id=run_id,
        latest_observation_retrieved_at=utcnow(),
        metadata_status="VALIDATED",
    )
    conn.execute(
        text(
            """
            UPDATE mi_collector_status SET
                last_ingest_ok_at = CASE WHEN :ok THEN NOW() ELSE last_ingest_ok_at END,
                last_quote_at = COALESCE(CAST(:last_quote AS TIMESTAMPTZ), last_quote_at),
                last_callback_at = COALESCE(CAST(:last_callback AS TIMESTAMPTZ), last_callback_at),
                updated_at = NOW()
            WHERE collector_id = :cid
            """
        ),
        {"ok": inserted + unchanged > 0, "last_quote": latest_callback, "last_callback": latest_callback, "cid": collector_id},
    )
    return {
        "received": received,
        "inserted": inserted,
        "unchanged": unchanged,
        "rejected": rejected,
        "committed": inserted,
        "duplicate": unchanged,
        "run_id": run_id,
        "results": results,
    }


def _num(value: Any):
    number, _reason = normalize_numeric(value) if value is not None else (None, "missing")
    return number


def _insert_quote(conn, raw: Mapping[str, Any], *, run_id: str) -> bool:
    con_id = raw.get("con_id")
    con_id_int = int(con_id) if con_id not in (None, "") else None
    instrument_id = raw.get("instrument_id") or instrument_id_for(
        con_id=con_id_int,
        symbol=str(raw.get("symbol") or ""),
        sec_type=str(raw.get("sec_type") or "STK"),
        currency=raw.get("currency"),
        exchange=raw.get("exchange"),
    )
    upsert_instrument(
        conn,
        instrument_id=instrument_id,
        symbol=str(raw.get("symbol") or ""),
        sec_type=str(raw.get("sec_type") or "STK"),
        con_id=con_id_int,
        currency=raw.get("currency"),
        exchange=raw.get("exchange"),
        primary_exchange=raw.get("primary_exchange"),
        display_name=raw.get("display_name"),
    )
    bid = _num(raw.get("bid"))
    ask = _num(raw.get("ask"))
    last = _num(raw.get("last_price"))
    mid = _num(raw.get("mid"))
    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2
    result = conn.execute(
        text(
            """
            INSERT INTO mi_market_quotes (
                instrument_id, source_id, quote_ts, bid, ask, last_price, mid, bid_size, ask_size,
                currency, delay_status, quote_status, retrieved_at, ingestion_run_id,
                con_id, market_data_type, provenance, source_ts, last_size, close_price, record_id, last_callback_at
            ) VALUES (
                :instrument_id, :source_id, CAST(:quote_ts AS TIMESTAMPTZ), :bid, :ask, :last, :mid, :bid_size, :ask_size,
                :currency, :delay_status, :quote_status, CAST(:retrieved_at AS TIMESTAMPTZ), :run_id,
                :con_id, :mdt, CAST(:provenance AS JSONB), CAST(:source_ts AS TIMESTAMPTZ), :last_size, :close_price, :record_id,
                CAST(:last_callback_at AS TIMESTAMPTZ)
            )
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "instrument_id": instrument_id,
            "source_id": IBKR_SOURCE_ID,
            "quote_ts": raw["quote_ts"],
            "bid": bid,
            "ask": ask,
            "last": last,
            "mid": mid,
            "bid_size": _num(raw.get("bid_size")),
            "ask_size": _num(raw.get("ask_size")),
            "currency": raw.get("currency"),
            "delay_status": raw.get("delay_status") or raw.get("market_data_type") or "UNKNOWN",
            "quote_status": raw.get("quote_status") or "UNKNOWN",
            "retrieved_at": raw.get("retrieved_at") or utcnow().isoformat(),
            "run_id": run_id,
            "con_id": con_id_int,
            "mdt": raw.get("market_data_type") or "UNAVAILABLE",
            "provenance": strict_dumps(raw.get("provenance") or {}),
            "source_ts": raw.get("source_ts"),
            "last_size": _num(raw.get("last_size")),
            "close_price": _num(raw.get("close_price")),
            "record_id": raw.get("record_id"),
            "last_callback_at": raw.get("last_callback_at"),
        },
    )
    return bool(result.rowcount)


def upsert_heartbeat(conn, payload: Mapping[str, Any]) -> None:
    ensure_ibkr_source(conn)
    state = str(payload.get("reported_state") or "DISCONNECTED")
    if state not in ALLOWED_STATES:
        state = "DISCONNECTED"
    mdt = payload.get("market_data_type")
    if mdt not in ALLOWED_MD_TYPES and mdt is not None:
        mdt = "UNAVAILABLE"
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    overflow = details.get("queue_overflow_count")
    try:
        overflow = int(overflow) if overflow is not None else None
    except (TypeError, ValueError):
        overflow = None
    conn.execute(
        text(
            """
            INSERT INTO mi_collector_status (
                collector_id, source_id, reported_state, last_heartbeat_at, last_socket_ok_at,
                last_api_handshake_at, last_tws_connect_at, last_quote_at, last_callback_at,
                last_delivery_error_redacted, market_data_type, client_id, watchlist_json, details_json,
                queue_overflow_count, updated_at
            ) VALUES (
                :cid, :src, :state, NOW(), CAST(:socket AS TIMESTAMPTZ), CAST(:handshake AS TIMESTAMPTZ),
                CAST(:tws AS TIMESTAMPTZ), CAST(:quote AS TIMESTAMPTZ), CAST(:callback AS TIMESTAMPTZ),
                :err, :mdt, :client_id, CAST(:watchlist AS JSONB), CAST(:details AS JSONB), COALESCE(:overflow, 0), NOW()
            )
            ON CONFLICT (collector_id) DO UPDATE SET
                source_id = EXCLUDED.source_id,
                reported_state = EXCLUDED.reported_state,
                last_heartbeat_at = NOW(),
                last_socket_ok_at = COALESCE(EXCLUDED.last_socket_ok_at, mi_collector_status.last_socket_ok_at),
                last_api_handshake_at = COALESCE(EXCLUDED.last_api_handshake_at, mi_collector_status.last_api_handshake_at),
                last_tws_connect_at = COALESCE(EXCLUDED.last_tws_connect_at, mi_collector_status.last_tws_connect_at),
                last_quote_at = COALESCE(EXCLUDED.last_quote_at, mi_collector_status.last_quote_at),
                last_callback_at = COALESCE(EXCLUDED.last_callback_at, mi_collector_status.last_callback_at),
                last_delivery_error_redacted = EXCLUDED.last_delivery_error_redacted,
                market_data_type = COALESCE(EXCLUDED.market_data_type, mi_collector_status.market_data_type),
                client_id = COALESCE(EXCLUDED.client_id, mi_collector_status.client_id),
                watchlist_json = COALESCE(EXCLUDED.watchlist_json, mi_collector_status.watchlist_json),
                details_json = EXCLUDED.details_json,
                queue_overflow_count = COALESCE(:overflow, mi_collector_status.queue_overflow_count),
                updated_at = NOW()
            """
        ),
        {
            "cid": payload["collector_id"],
            "src": IBKR_SOURCE_ID,
            "state": state,
            "socket": payload.get("last_socket_ok_at"),
            "handshake": payload.get("last_api_handshake_at"),
            "tws": payload.get("last_tws_connect_at"),
            "quote": payload.get("last_quote_at"),
            "callback": payload.get("last_callback_at") or payload.get("last_quote_at"),
            "err": payload.get("last_delivery_error_redacted"),
            "mdt": mdt,
            "client_id": payload.get("client_id"),
            "watchlist": strict_dumps(payload.get("watchlist") or []),
            "details": strict_dumps(payload.get("details") or {}),
            "overflow": overflow,
        },
    )


def ingest_equity_bars(conn, records: list[Mapping[str, Any]], *, collector_id: str, request=None) -> dict[str, Any]:
    """Stage collector-posted daily bars. Snapshots/freshness happen on finalize."""
    from ibkr_ingest.validate import EquityBarIngestRequest
    from market_intelligence.equity_eod_batch import finalize_equity_eod_batch, stage_equity_bars

    if request is None:
        raise ValueError("equity bar ingest requires a validated batch request")
    if not isinstance(request, EquityBarIngestRequest):
        raise TypeError("request must be EquityBarIngestRequest")
    staged = stage_equity_bars(conn, request)
    if not request.auto_finalize:
        staged["latest_observation"] = staged["latest_observation"].isoformat() if staged.get("latest_observation") else None
        return staged
    finalized = finalize_equity_eod_batch(
        conn,
        batch_id=request.batch_id,
        collector_id=collector_id,
        coverage=request.coverage,
        request_mode=request.request_mode,
        provider=request.provider,
        source_id=request.source_id,
        what_to_show=request.what_to_show,
        adjustment_basis=request.adjustment_basis,
    )
    received = staged.get("received", 0)
    inserted = staged.get("inserted", 0)
    unchanged = staged.get("unchanged", 0)
    latest = staged.get("latest_observation")
    staged.update(finalized)
    staged["finalized"] = True
    staged["received"] = received
    staged["inserted"] = inserted
    staged["unchanged"] = unchanged
    if latest is not None and not staged.get("latest_observation"):
        staged["latest_observation"] = latest.isoformat() if hasattr(latest, "isoformat") else latest
    elif hasattr(staged.get("latest_observation"), "isoformat"):
        staged["latest_observation"] = staged["latest_observation"].isoformat()
    return staged
