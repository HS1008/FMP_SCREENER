"""PostgreSQL writers for OpenBB Cboe snapshots. Callers own transactions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import text

from market_intelligence.catalog import EXPORT_INTERNAL_ONLY
from market_intelligence.nulls import strict_dumps
from market_intelligence.openbb_provider.analytics import compute_options_metrics
from market_intelligence.catalog import SOURCE_REGISTRY_DEFAULTS
from market_intelligence.openbb_provider.config import (
    ANALYTICS_VERSION,
    NORMALIZATION_VERSION,
    OPENBB_OPTIONS_SOURCE_ID,
    OPENBB_VIX_SOURCE_ID,
    VIX_ANALYTICS_VERSION,
    probe_openbb,
)
from market_intelligence.openbb_provider.normalize import NormalizedChain, NormalizedContract
from market_intelligence.openbb_provider.vix import NormalizedCurve, front_curve_metrics
from market_intelligence.store import (
    TRANSPORT_FAILED,
    TRANSPORT_OK,
    record_freshness,
    upsert_source_registry,
)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return strict_dumps(value)


def new_snapshot_id() -> str:
    return "obb_{0}".format(uuid.uuid4().hex[:20])


def _sid(dataset: str) -> str:
    return OPENBB_VIX_SOURCE_ID if dataset == "vix_eod_curve" else OPENBB_OPTIONS_SOURCE_ID


def sync_registry(conn, env: Mapping[str, str] | None = None) -> None:
    """Write independent OPTIONS / VIX registry rows from the current probe."""
    probe = probe_openbb(env)
    entries = [
        entry
        for entry in SOURCE_REGISTRY_DEFAULTS
        if entry["source_id"] in {OPENBB_OPTIONS_SOURCE_ID, OPENBB_VIX_SOURCE_ID}
    ]
    upsert_source_registry(
        conn,
        entries,
        enabled={
            OPENBB_OPTIONS_SOURCE_ID: bool(probe.options.enabled),
            OPENBB_VIX_SOURCE_ID: bool(probe.vix.enabled),
        },
        access={
            OPENBB_OPTIONS_SOURCE_ID: probe.options.access_status,
            OPENBB_VIX_SOURCE_ID: probe.vix.access_status,
        },
    )


def ensure_source(conn, *, enabled: bool, access: str) -> None:
    """Deprecated wrapper. Prefer :func:`sync_registry` so siblings stay independent."""
    _ = (enabled, access)
    sync_registry(conn)


def current_complete_snapshot(conn, *, dataset: str, underlying: str) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT snapshot_id, session_date, content_hash, publication_status, observation_precision
            FROM mi_openbb_snapshots
            WHERE source_id = :sid AND dataset = :dataset AND underlying = :underlying
              AND is_current AND publication_status = 'COMPLETE'
            """
        ),
        {"sid": _sid(dataset), "dataset": dataset, "underlying": underlying},
    ).mappings().first()
    return dict(row) if row else None


def _demote_current(conn, *, dataset: str, underlying: str) -> None:
    conn.execute(
        text(
            """
            UPDATE mi_openbb_snapshots
            SET is_current = FALSE
            WHERE source_id = :sid AND dataset = :dataset AND underlying = :underlying AND is_current
            """
        ),
        {"sid": _sid(dataset), "dataset": dataset, "underlying": underlying},
    )


def _insert_snapshot(
    conn,
    *,
    snapshot_id: str,
    dataset: str,
    underlying: str,
    session_date,
    observation_time,
    observation_precision: str,
    collected_at: datetime,
    source_timestamp,
    source_timestamp_precision: str,
    content_hash: str,
    run_id: str | None,
    publication_status: str,
    is_current: bool,
    revision_seq: int,
    quality: Mapping[str, Any],
    coverage: Mapping[str, Any],
    openbb_version: str | None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mi_openbb_snapshots (
                snapshot_id, source_id, dataset, underlying, session_date, observation_time_utc,
                observation_precision, collected_at, source_timestamp_utc, source_timestamp_precision,
                content_hash, normalization_version, openbb_version, provider, endpoint_category,
                run_id, publication_status, is_current, revision_seq, quality_json, coverage_json,
                permitted_use_json, extra_json, export_scope
            ) VALUES (
                :snapshot_id, :source_id, :dataset, :underlying, :session_date, :observation_time_utc,
                :observation_precision, :collected_at, :source_timestamp_utc, :source_timestamp_precision,
                :content_hash, :normalization_version, :openbb_version, 'cboe', :endpoint_category,
                :run_id, :publication_status, :is_current, :revision_seq, CAST(:quality AS JSONB),
                CAST(:coverage AS JSONB), CAST(:permitted AS JSONB), CAST(:extra AS JSONB), :export_scope
            )
            """
        ),
        {
            "snapshot_id": snapshot_id,
            "source_id": _sid(dataset),
            "dataset": dataset,
            "underlying": underlying,
            "session_date": session_date,
            "observation_time_utc": observation_time,
            "observation_precision": observation_precision,
            "collected_at": collected_at,
            "source_timestamp_utc": source_timestamp,
            "source_timestamp_precision": source_timestamp_precision,
            "content_hash": content_hash,
            "normalization_version": NORMALIZATION_VERSION,
            "openbb_version": openbb_version,
            "endpoint_category": dataset,
            "run_id": run_id,
            "publication_status": publication_status,
            "is_current": is_current,
            "revision_seq": revision_seq,
            "quality": _json(quality),
            "coverage": _json(coverage),
            "permitted": _json({"terms": "cboe_website_personal_use_unresolved_recurring", "export_scope": EXPORT_INTERNAL_ONLY}),
            "extra": _json(extra or {}),
            "export_scope": EXPORT_INTERNAL_ONLY,
        },
    )


def _contract_row(snapshot_id: str, contract: NormalizedContract) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "contract_symbol": contract.contract_symbol,
        "expiration": contract.expiration,
        "expiration_precision": contract.expiration_precision,
        "dte_session": contract.dte_session,
        "strike": contract.strike,
        "option_type": contract.option_type,
        "currency": contract.currency,
        "multiplier": contract.multiplier,
        "multiplier_rule": contract.multiplier_rule,
        "underlying_price": contract.underlying_price,
        "bid": contract.bid,
        "bid_size": contract.bid_size,
        "ask": contract.ask,
        "ask_size": contract.ask_size,
        "last": contract.last,
        "last_trade_time": contract.last_trade_time,
        "open_interest": contract.open_interest,
        "volume": contract.volume,
        "iv_decimal": contract.iv_decimal,
        "delta": contract.delta,
        "gamma": contract.gamma,
        "theta": contract.theta,
        "vega": contract.vega,
        "rho": contract.rho,
        "theoretical_price": contract.theoretical_price,
        "quote_quality": contract.quote_quality,
        "is_adjusted": contract.is_adjusted,
        "identity_ok": contract.identity_ok,
        "flags_json": _json(contract.flags),
    }


def publish_chain(conn, chain: NormalizedChain, *, run_id: str | None, failed: bool = False, error: str | None = None) -> dict[str, Any]:
    dataset = "options_chain"
    existing = conn.execute(
        text(
            """
            SELECT snapshot_id, revision_seq, publication_status, is_current, session_date
            FROM mi_openbb_snapshots
            WHERE source_id = :sid AND dataset = :dataset AND underlying = :u AND content_hash = :h
            ORDER BY revision_seq DESC
            """
        ),
        {"sid": _sid(dataset), "dataset": dataset, "u": chain.underlying, "h": chain.content_hash},
    ).mappings().first()
    if existing and not failed:
        if not existing["is_current"] and existing["publication_status"] == "COMPLETE":
            _demote_current(conn, dataset=dataset, underlying=chain.underlying)
            conn.execute(text("UPDATE mi_openbb_snapshots SET is_current = TRUE WHERE snapshot_id = :id"), {"id": existing["snapshot_id"]})
        record_freshness(
            conn,
            source_id=_sid(dataset),
            dataset="options_chain:{0}".format(chain.underlying),
            cadence="D",
            transport_status=TRANSPORT_OK,
            latest_observation=chain.session_date,
            success=True,
            error_redacted=None,
            run_id=run_id,
            latest_observation_retrieved_at=chain.source_timestamp_utc or chain.collected_at,
        )
        return {"status": "REPLAY", "snapshot_id": existing["snapshot_id"], "content_hash": chain.content_hash, "published_new": False}

    same_time = None
    if chain.source_timestamp_utc is not None:
        same_time = conn.execute(
            text(
                """
                SELECT MAX(revision_seq) AS rev FROM mi_openbb_snapshots
                WHERE source_id = :sid AND dataset = :dataset AND underlying = :u AND source_timestamp_utc = :ts
                """
            ),
            {"sid": _sid(dataset), "dataset": dataset, "u": chain.underlying, "ts": chain.source_timestamp_utc},
        ).scalar()
    revision = int(same_time or 0) + 1 if same_time else 1
    publication = "FAILED" if failed else ("COMPLETE" if chain.complete else "PARTIAL")
    snapshot_id = new_snapshot_id()
    is_current = publication == "COMPLETE"
    if is_current:
        _demote_current(conn, dataset=dataset, underlying=chain.underlying)
    quality = dict(chain.quality)
    if error:
        quality["error"] = error
    _insert_snapshot(
        conn,
        snapshot_id=snapshot_id,
        dataset=dataset,
        underlying=chain.underlying,
        session_date=chain.session_date,
        observation_time=chain.observation_time_utc,
        observation_precision=chain.observation_precision,
        collected_at=chain.collected_at,
        source_timestamp=chain.source_timestamp_utc,
        source_timestamp_precision=chain.observation_precision,
        content_hash=chain.content_hash,
        run_id=run_id,
        publication_status=publication,
        is_current=is_current,
        revision_seq=revision,
        quality=quality,
        coverage={"rejected": chain.rejected[:50], "contracts_kept": len(chain.contracts)},
        openbb_version=chain.openbb_version,
        extra={"results_metadata": chain.metadata},
    )
    if not failed:
        rows = [_contract_row(snapshot_id, contract) for contract in chain.contracts]
        if rows:
            conn.execute(
                text(
                    """
                    INSERT INTO mi_openbb_option_contracts (
                        snapshot_id, contract_symbol, expiration, expiration_precision, dte_session, strike,
                        option_type, currency, multiplier, multiplier_rule, underlying_price, bid, bid_size,
                        ask, ask_size, last, last_trade_time, open_interest, volume, iv_decimal, delta, gamma,
                        theta, vega, rho, theoretical_price, quote_quality, is_adjusted, identity_ok, flags_json
                    ) VALUES (
                        :snapshot_id, :contract_symbol, :expiration, :expiration_precision, :dte_session, :strike,
                        :option_type, :currency, :multiplier, :multiplier_rule, :underlying_price, :bid, :bid_size,
                        :ask, :ask_size, :last, :last_trade_time, :open_interest, :volume, :iv_decimal, :delta, :gamma,
                        :theta, :vega, :rho, :theoretical_price, :quote_quality, :is_adjusted, :identity_ok,
                        CAST(:flags_json AS JSONB)
                    )
                    """
                ),
                rows,
            )
        metrics = compute_options_metrics(chain)
        conn.execute(
            text(
                """
                INSERT INTO mi_openbb_options_metrics (snapshot_id, method_version, metrics_json, coverage_json)
                VALUES (:snapshot_id, :method_version, CAST(:metrics AS JSONB), CAST(:coverage AS JSONB))
                """
            ),
            {"snapshot_id": snapshot_id, "method_version": ANALYTICS_VERSION, "metrics": _json(metrics), "coverage": _json(chain.quality)},
        )
    record_freshness(
        conn,
        source_id=_sid(dataset),
        dataset="options_chain:{0}".format(chain.underlying),
        cadence="D",
        transport_status=TRANSPORT_FAILED if failed else TRANSPORT_OK,
        latest_observation=chain.session_date if publication == "COMPLETE" else None,
        success=publication == "COMPLETE",
        error_redacted=error,
        run_id=run_id,
        latest_observation_retrieved_at=chain.source_timestamp_utc or chain.collected_at if publication == "COMPLETE" else None,
        coverage_status=publication,
    )
    return {"status": publication, "snapshot_id": snapshot_id, "content_hash": chain.content_hash, "published_new": True, "is_current": is_current}


def publish_curve(conn, curve: NormalizedCurve, *, run_id: str | None, failed: bool = False, error: str | None = None) -> dict[str, Any]:
    dataset = "vix_eod_curve"
    existing = conn.execute(
        text(
            """
            SELECT snapshot_id, is_current, publication_status FROM mi_openbb_snapshots
            WHERE source_id = :sid AND dataset = :dataset AND underlying = 'VX' AND content_hash = :h
            ORDER BY revision_seq DESC
            """
        ),
        {"sid": _sid(dataset), "dataset": dataset, "h": curve.content_hash},
    ).mappings().first()
    if existing and not failed:
        if not existing["is_current"] and existing["publication_status"] == "COMPLETE":
            _demote_current(conn, dataset=dataset, underlying="VX")
            conn.execute(text("UPDATE mi_openbb_snapshots SET is_current = TRUE WHERE snapshot_id = :id"), {"id": existing["snapshot_id"]})
        record_freshness(conn, source_id=_sid(dataset), dataset=dataset, cadence="D", transport_status=TRANSPORT_OK, latest_observation=curve.observation_date or curve.session_date, success=True, error_redacted=None, run_id=run_id)
        return {"status": "REPLAY", "snapshot_id": existing["snapshot_id"], "content_hash": curve.content_hash, "published_new": False}
    publication = "FAILED" if failed else ("COMPLETE" if any(p.price is not None for p in curve.points) else "PARTIAL")
    snapshot_id = new_snapshot_id()
    is_current = publication == "COMPLETE"
    if is_current:
        _demote_current(conn, dataset=dataset, underlying="VX")
    quality = dict(curve.quality)
    if error:
        quality["error"] = error
    _insert_snapshot(
        conn,
        snapshot_id=snapshot_id,
        dataset=dataset,
        underlying="VX",
        session_date=curve.session_date,
        observation_time=None,
        observation_precision=curve.observation_precision,
        collected_at=curve.collected_at,
        source_timestamp=None,
        source_timestamp_precision=curve.observation_precision,
        content_hash=curve.content_hash,
        run_id=run_id,
        publication_status=publication,
        is_current=is_current,
        revision_seq=1,
        quality=quality,
        coverage={"rejected": curve.rejected},
        openbb_version=curve.openbb_version,
    )
    if not failed:
        for idx, point in enumerate(curve.points):
            conn.execute(
                text(
                    """
                    INSERT INTO mi_openbb_vix_points (
                        snapshot_id, point_index, expiration_label, expiration_precision, price, contract_symbol, observation_date, flags_json
                    ) VALUES (
                        :snapshot_id, :point_index, :expiration_label, :expiration_precision, :price, :contract_symbol, :observation_date, CAST(:flags AS JSONB)
                    )
                    """
                ),
                {
                    "snapshot_id": snapshot_id,
                    "point_index": idx,
                    "expiration_label": point.expiration_label,
                    "expiration_precision": point.expiration_precision,
                    "price": point.price,
                    "contract_symbol": point.contract_symbol,
                    "observation_date": point.observation_date,
                    "flags": _json(point.flags),
                },
            )
        metrics = front_curve_metrics(curve)
        conn.execute(
            text(
                """
                INSERT INTO mi_openbb_vix_metrics (snapshot_id, method_version, metrics_json)
                VALUES (:snapshot_id, :method_version, CAST(:metrics AS JSONB))
                """
            ),
            {"snapshot_id": snapshot_id, "method_version": VIX_ANALYTICS_VERSION, "metrics": _json(metrics)},
        )
    record_freshness(
        conn,
        source_id=_sid(dataset),
        dataset=dataset,
        cadence="D",
        transport_status=TRANSPORT_FAILED if failed else TRANSPORT_OK,
        latest_observation=(curve.observation_date or curve.session_date) if publication == "COMPLETE" else None,
        success=publication == "COMPLETE",
        error_redacted=error,
        run_id=run_id,
    )
    return {"status": publication, "snapshot_id": snapshot_id, "content_hash": curve.content_hash, "published_new": True, "is_current": is_current}


def purge_old_contract_snapshots(conn, *, keep: int, dry_run: bool = True) -> dict[str, Any]:
    """Drop bulky contract rows for old non-current snapshots. Summaries stay."""
    rows = conn.execute(
        text(
            """
            SELECT snapshot_id FROM mi_openbb_snapshots
            WHERE dataset = 'options_chain' AND NOT is_current
            ORDER BY collected_at DESC
            OFFSET :keep
            """
        ),
        {"keep": keep},
    ).fetchall()
    ids = [row[0] for row in rows]
    if dry_run or not ids:
        return {"dry_run": dry_run, "snapshot_ids": ids, "deleted_contracts": 0}
    deleted = conn.execute(text("DELETE FROM mi_openbb_option_contracts WHERE snapshot_id = ANY(:ids)"), {"ids": ids}).rowcount
    return {"dry_run": False, "snapshot_ids": ids, "deleted_contracts": int(deleted or 0)}
