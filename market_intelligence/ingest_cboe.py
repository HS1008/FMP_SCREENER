"""Persist Cboe volatility metrics. Partial sections commit independently."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping

from sqlalchemy import text

from market_intelligence.cboe_analytics import (
    INDEX_SYMBOLS,
    METHOD_RV,
    METHOD_SKEW,
    METHOD_TERM,
    METHOD_VIX,
    SKEW_ROOT,
    SKEW_UNDERLYING,
    expiry_window,
    index_snapshots,
    iv_rv_spread,
    quote_level,
    realized_vol_20,
    skew_from_chain,
    strike_band,
    term_structure,
)
from market_intelligence.cboe_client import (
    SOURCE_ID,
    STATUS_READY,
    CboeBudgetError,
    CboeClient,
    CboeError,
    prior_weekdays,
    session_date,
)
from market_intelligence.nulls import strict_dumps
from market_intelligence.store import RUN_FAILED, RUN_PARTIAL, RUN_SUCCEEDED, finish_run, record_freshness, start_run

CATEGORY = "CBOE_VOL"
DATASETS = ("cboe_auth", "vix", "vix_term_structure", "spx_25d_skew", "iv_minus_rv")
HISTORY_LIMIT = 21

_UPSERT = text(
    """
    INSERT INTO mi_metric_snapshots (
        metric_id, series_id, category, as_of, value, units, transform_version, status, detail_json,
        computed_at, ingestion_run_id, inputs_retrieved_max
    ) VALUES (
        :metric_id, NULL, :category, :as_of, :value, :units, :transform_version, :status,
        CAST(:detail AS JSONB), NOW(), :run_id, :inputs_retrieved_max
    )
    ON CONFLICT (metric_id, as_of, transform_version) DO UPDATE SET
        value = EXCLUDED.value,
        units = EXCLUDED.units,
        status = EXCLUDED.status,
        detail_json = EXCLUDED.detail_json,
        computed_at = NOW(),
        ingestion_run_id = EXCLUDED.ingestion_run_id,
        inputs_retrieved_max = EXCLUDED.inputs_retrieved_max
    """
)


def ingest_cboe(engine, client: CboeClient, *, parent_run_id: str | None = None, today: date | None = None) -> dict[str, Any]:
    as_of = today or session_date()
    report: dict[str, Any] = {"source_id": SOURCE_ID, "as_of": as_of.isoformat(), "sections": {}, "points_used": 0, "failed": False}
    try:
        client.access_token()
        _mark(engine, "cboe_auth", as_of, transport="SUCCEEDED", success=True, error=None, run_id=parent_run_id, capability=STATUS_READY)
        report["sections"]["cboe_auth"] = {"status": "SUCCEEDED"}
        _set_access(engine, STATUS_READY)
    except CboeError as exc:
        report["failed"] = True
        report["sections"]["cboe_auth"] = {"status": exc.capability}
        for dataset in DATASETS:
            _mark(engine, dataset, as_of, transport=exc.capability, success=False, error=exc.capability, run_id=parent_run_id, capability=exc.capability)
        _set_access(engine, exc.capability)
        return report

    snaps = _ingest_indices(engine, client, as_of, parent_run_id, report)
    _ingest_skew(engine, client, as_of, parent_run_id, report, snaps)
    _ingest_spread(engine, as_of, parent_run_id, report, snaps)
    report["points_used"] = client.points_used
    report["requests_made"] = client.requests_made
    hard = {"AUTH_FAILED", "ENTITLEMENT_REQUIRED", "TRIAL_LIMIT", "RATE_LIMITED", "UNAVAILABLE", "FAILED"}
    report["failed"] = any(section.get("status") in hard for section in report["sections"].values())
    return report


def _ingest_indices(engine, client: CboeClient, as_of: date, parent_run_id: str | None, report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    run_id = _open(engine, "vix", parent_run_id, as_of)
    try:
        payload = None
        quote_day = as_of
        last_exc: CboeError | None = None
        # Delayed same-session quotes often need SIP entitlements (fields null / empty).
        # Prefer a completed prior weekday first for historical EOD snapshots (3 pts).
        delayed = "delayed" in (getattr(client, "api_root", "") or "")
        probe_days = list(prior_weekdays(as_of, 5)) + [as_of] if delayed else [as_of] + list(prior_weekdays(as_of, 5))
        for day in probe_days:
            try:
                # Probe VIX alone first (matches official examples), then the full tenor set.
                probe = client.underlying_quotes(["VIX"], day, session_date=as_of)
                if not isinstance(probe, list) or not probe:
                    last_exc = CboeError("UNAVAILABLE", "empty_vix_probe")
                    continue
                payload = client.underlying_quotes(list(INDEX_SYMBOLS), day, session_date=as_of)
                quote_day = day
                last_exc = None
                break
            except CboeError as exc:
                last_exc = exc
                continue
        if payload is None:
            raise last_exc or CboeError("UNAVAILABLE", "underlying_quotes_unavailable")
        snaps = index_snapshots(payload, quote_date=quote_day)
        _maybe_backfill(engine, client, as_of, snaps)
        rows = _index_rows(snaps, quote_day)
        written = _write_rows(engine, rows, run_id)
        vix_ok = any(row["metric_id"] == "VIX_SPOT" and row["status"] == "OK" for row in rows)
        term = term_structure(snaps)
        term_ok = term["curve_state"] is not None
        _close(engine, run_id, RUN_SUCCEEDED if vix_ok else RUN_PARTIAL, written, None if vix_ok else "missing_vix_level")
        _mark(engine, "vix", as_of, transport="SUCCEEDED" if vix_ok else "PARTIAL", success=vix_ok, error=None if vix_ok else "missing_vix_level", run_id=run_id, capability=STATUS_READY)
        _mark(engine, "vix_term_structure", as_of, transport="SUCCEEDED" if term_ok else "PARTIAL", success=term_ok, error=None if term_ok else "incomplete_tenor_set", run_id=run_id, capability=STATUS_READY)
        report["sections"]["vix"] = {"status": "SUCCEEDED" if vix_ok else "PARTIAL", "rows": written, "quote_date": quote_day.isoformat()}
        report["sections"]["vix_term_structure"] = {"status": "SUCCEEDED" if term_ok else "PARTIAL", "slope": term["front_to_back_slope"], "state": term["curve_state"]}
        return snaps
    except CboeError as exc:
        _close(engine, run_id, RUN_FAILED, 0, exc.capability)
        for dataset in ("vix", "vix_term_structure"):
            _mark(engine, dataset, as_of, transport=exc.capability, success=False, error=str(exc)[:160], run_id=run_id, capability=exc.capability)
        report["sections"]["vix"] = {"status": exc.capability, "reason": str(exc)[:160], "http_status": exc.http_status}
        report["sections"]["vix_term_structure"] = {"status": exc.capability, "reason": str(exc)[:160]}
        return {}
    except (ValueError, TypeError) as exc:
        _close(engine, run_id, RUN_FAILED, 0, "malformed_payload")
        for dataset in ("vix", "vix_term_structure"):
            _mark(engine, dataset, as_of, transport="FAILED", success=False, error="malformed_payload", run_id=run_id, capability="UNAVAILABLE")
        report["sections"]["vix"] = {"status": "FAILED", "reason": "malformed_payload:{0}".format(exc.__class__.__name__)}
        report["sections"]["vix_term_structure"] = {"status": "FAILED", "reason": "malformed_payload"}
        report["failed"] = True
        return {}


def _maybe_backfill(engine, client: CboeClient, as_of: date, snaps: dict[str, dict[str, Any]]) -> None:
    stored = _stored_closes(engine)
    level = (snaps.get("SPX") or {}).get("level")
    if level:
        stored[as_of] = float(level)
    if len([day for day in stored if day <= as_of]) >= HISTORY_LIMIT + 1:
        return
    for day in prior_weekdays(as_of, HISTORY_LIMIT):
        if day in stored:
            continue
        try:
            payload = client.underlying_quotes(list(INDEX_SYMBOLS), day, session_date=as_of)
        except CboeBudgetError:
            break
        except CboeError:
            continue
        day_snaps = index_snapshots(payload, quote_date=day)
        _write_rows(engine, _index_rows(day_snaps, day), None)
        day_level = (day_snaps.get("SPX") or {}).get("level")
        if day_level:
            stored[day] = float(day_level)


def _ingest_skew(engine, client: CboeClient, as_of: date, parent_run_id: str | None, report: dict[str, Any], snaps: Mapping[str, Mapping[str, Any]]) -> None:
    run_id = _open(engine, "spx_25d_skew", parent_run_id, as_of)
    try:
        spot = (snaps.get("SPX") or {}).get("level")
        if spot is None:
            raise CboeError("INCOMPLETE", "missing_spx_spot_for_strike_band")
        start, end = expiry_window(as_of)
        low, high = strike_band(float(spot))
        payload = client.option_quotes(
            symbol=SKEW_UNDERLYING,
            root=SKEW_ROOT,
            quote_date=as_of,
            session_date=as_of,
            min_expiry=start,
            max_expiry=end,
            min_strike=low,
            max_strike=high,
        )
        skew = skew_from_chain(payload, as_of=as_of)
        underlying = quote_level(payload) if isinstance(payload, dict) else None
        rows = _skew_rows(skew, as_of, underlying if underlying is not None else float(spot))
        written = _write_rows(engine, rows, run_id)
        ok = skew["status"] == "OK"
        _close(engine, run_id, RUN_SUCCEEDED if ok else RUN_PARTIAL, written, skew.get("reason"))
        _mark(engine, "spx_25d_skew", as_of, transport="SUCCEEDED" if ok else "PARTIAL", success=ok, error=skew.get("reason"), run_id=run_id, capability=STATUS_READY)
        report["sections"]["spx_25d_skew"] = {"status": "SUCCEEDED" if ok else "INCOMPLETE", "reason": skew.get("reason")}
    except CboeError as exc:
        incomplete = exc.capability == "INCOMPLETE"
        _close(engine, run_id, RUN_PARTIAL if incomplete else RUN_FAILED, 0, exc.capability if not incomplete else "missing_spx_spot_for_strike_band")
        _mark(engine, "spx_25d_skew", as_of, transport="PARTIAL" if incomplete else exc.capability, success=False, error=exc.capability if not incomplete else "missing_spx_spot_for_strike_band", run_id=run_id, capability=exc.capability)
        report["sections"]["spx_25d_skew"] = {"status": "INCOMPLETE" if incomplete else exc.capability}
        if not incomplete:
            report["failed"] = True
    except (ValueError, TypeError):
        _close(engine, run_id, RUN_FAILED, 0, "malformed_payload")
        _mark(engine, "spx_25d_skew", as_of, transport="FAILED", success=False, error="malformed_payload", run_id=run_id, capability="UNAVAILABLE")
        report["sections"]["spx_25d_skew"] = {"status": "FAILED", "reason": "malformed_payload"}
        report["failed"] = True


def _ingest_spread(engine, as_of: date, parent_run_id: str | None, report: dict[str, Any], snaps: Mapping[str, Mapping[str, Any]]) -> None:
    run_id = _open(engine, "iv_minus_rv", parent_run_id, as_of)
    closes = sorted(_stored_closes(engine).items())
    rv = realized_vol_20(closes)
    spx = snaps.get("SPX") or {}
    vix = snaps.get("VIX") or {}
    spread = iv_rv_spread(iv30=spx.get("iv30"), vix=vix.get("level"), rv=rv.get("value"))
    rows = []
    rows.append(_metric("SPX_REALIZED_VOL_20D", as_of, rv.get("value"), "vol_points", METHOD_RV, "OK" if rv.get("value") is not None else "INCOMPLETE", {**rv, "source": SOURCE_ID}))
    if spread.get("status") == "OK":
        rows.append(
            _metric(
                spread["metric_id"],
                as_of,
                spread["value"],
                "vol_points",
                spread["method"],
                "OK",
                {"source": SOURCE_ID, "iv": spread.get("iv"), "rv": spread.get("rv"), "window": 20, "construction": spread.get("construction")},
            )
        )
    else:
        rows.append(_metric("SPX_IV30_MINUS_SPX_RV20", as_of, None, "vol_points", "spx_iv30_minus_spx_rv20_v1", "INCOMPLETE", {"source": SOURCE_ID, "reason": spread.get("reason"), "iv": spread.get("iv"), "rv": rv.get("value")}))
    written = _write_rows(engine, rows, run_id)
    ok = spread.get("status") == "OK"
    _close(engine, run_id, RUN_SUCCEEDED if ok else RUN_PARTIAL, written, None if ok else spread.get("reason"))
    _mark(engine, "iv_minus_rv", as_of, transport="SUCCEEDED" if ok else "PARTIAL", success=ok, error=None if ok else spread.get("reason"), run_id=run_id, capability=STATUS_READY)
    report["sections"]["iv_minus_rv"] = {"status": "SUCCEEDED" if ok else "INCOMPLETE", "metric_id": spread.get("metric_id"), "reason": spread.get("reason")}


def _index_rows(snaps: Mapping[str, Mapping[str, Any]], as_of: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    curve = term_structure(snaps)
    vix = snaps.get("VIX") or {}
    if "VIX" in snaps:
        change = None
        if vix.get("level") is not None and vix.get("prev_close") is not None:
            change = float(vix["level"]) - float(vix["prev_close"])
        rows.append(_metric("VIX_SPOT", as_of, vix.get("level"), "vol_points", METHOD_VIX, "OK" if vix.get("level") is not None else "INCOMPLETE", _prov(vix, extra={"change_1d": change, "symbol": "VIX"})))
    for point in curve["points"]:
        change = None
        if point.get("prev_close") is not None:
            change = float(point["level"]) - float(point["prev_close"])
        rows.append(_metric(point["metric_id"], as_of, point["level"], "vol_points", METHOD_TERM, "OK", _prov(point, extra={"tenor": point["tenor"], "symbol": point["symbol"], "construction": curve["construction"], "change_1d": change})))
    if curve["front_to_back_slope"] is not None:
        rows.append(_metric("VIX_INDEX_FRONT_TO_BACK", as_of, curve["front_to_back_slope"], "vol_points", METHOD_TERM, "OK", {"source": SOURCE_ID, "construction": curve["construction"], "label": curve["label"], "curve_state": curve["curve_state"], "tenors": [p["tenor"] for p in curve["points"]]}))
    spx = snaps.get("SPX") or {}
    if spx.get("level") is not None:
        rows.append(_metric("SPX_CLOSE", as_of, spx["level"], "index_points", METHOD_RV, "OK", _prov(spx, extra={"symbol": "SPX"})))
    if spx.get("iv30") is not None:
        rows.append(_metric("SPX_IV30", as_of, spx["iv30"], "vol_points", "livevol_iv30_v1", "OK", _prov(spx, extra={"symbol": "SPX", "field": "iv30", "note": "30-day average implied volatility, not ATM and not VIX"})))
    return rows


def _skew_rows(skew: Mapping[str, Any], as_of: date, underlying_level: float | None) -> list[dict[str, Any]]:
    detail = {
        "source": SOURCE_ID,
        "method": METHOD_SKEW,
        "underlying": SKEW_UNDERLYING,
        "root": SKEW_ROOT,
        "expiry": skew.get("expiry"),
        "dte": skew.get("dte"),
        "put": skew.get("put"),
        "call": skew.get("call"),
        "underlying_level": underlying_level if underlying_level is not None else skew.get("underlying_level"),
        "observation_ts": skew.get("observation_ts"),
        "reason": skew.get("reason"),
        "target_calendar_days": 30,
        "target_abs_delta": 0.25,
    }
    status = "OK" if skew.get("status") == "OK" else "INCOMPLETE"
    return [
        _metric("SPX_25D_PUT_IV", as_of, skew.get("put_iv"), "vol_points", METHOD_SKEW, status, detail),
        _metric("SPX_25D_CALL_IV", as_of, skew.get("call_iv"), "vol_points", METHOD_SKEW, status, detail),
        _metric("SPX_25D_SKEW", as_of, skew.get("skew"), "vol_points", METHOD_SKEW, status, detail),
    ]


def _metric(metric_id: str, as_of: date, value: float | None, units: str, method: str, status: str, detail: Mapping[str, Any]) -> dict[str, Any]:
    observed = detail.get("observation_ts")
    observed_dt = None
    if isinstance(observed, str):
        try:
            observed_dt = datetime.fromisoformat(observed)
        except ValueError:
            observed_dt = None
    return {
        "metric_id": metric_id,
        "as_of": as_of,
        "value": value,
        "units": units,
        "transform_version": method,
        "status": status,
        "detail": dict(detail),
        "inputs_retrieved_max": observed_dt,
    }


def _prov(snap: Mapping[str, Any], *, extra: Mapping[str, Any]) -> dict[str, Any]:
    payload = {"source": SOURCE_ID, "observation_ts": snap.get("observation_ts"), "provider_timestamp": snap.get("provider_timestamp"), "as_of": snap.get("as_of")}
    payload.update(dict(extra))
    return payload


def _write_rows(engine, rows: list[dict[str, Any]], run_id: str | None) -> int:
    if not rows:
        return 0
    with engine.begin() as conn:
        for row in rows:
            conn.execute(
                _UPSERT,
                {
                    "metric_id": row["metric_id"],
                    "category": CATEGORY,
                    "as_of": row["as_of"],
                    "value": row["value"],
                    "units": row["units"],
                    "transform_version": row["transform_version"],
                    "status": row["status"],
                    "detail": strict_dumps(row["detail"]),
                    "run_id": run_id,
                    "inputs_retrieved_max": row.get("inputs_retrieved_max"),
                },
            )
    return len(rows)


def _stored_closes(engine) -> dict[date, float]:
    with engine.connect() as conn:
        found = conn.execute(
            text(
                """
                SELECT as_of, value FROM mi_metric_snapshots
                WHERE metric_id = 'SPX_CLOSE' AND category = 'CBOE_VOL' AND status = 'OK' AND value IS NOT NULL
                """
            )
        ).all()
    out: dict[date, float] = {}
    for as_of, value in found:
        if value is None:
            continue
        out[as_of] = float(value)
    return out


def _open(engine, dataset: str, parent_run_id: str | None, as_of: date) -> str:
    with engine.begin() as conn:
        return start_run(conn, source_id=SOURCE_ID, dataset=dataset, parent_run_id=parent_run_id, request_window=(as_of - timedelta(days=40), as_of))


def _close(engine, run_id: str, status: str, written: int, error: str | None) -> None:
    with engine.begin() as conn:
        finish_run(conn, run_id, status=status, counts={"received": written, "inserted": written}, error_redacted=error)


def _mark(engine, dataset: str, as_of: date, *, transport: str, success: bool, error: str | None, run_id: str | None, capability: str) -> None:
    with engine.begin() as conn:
        record_freshness(
            conn,
            source_id=SOURCE_ID,
            dataset=dataset,
            cadence="D",
            transport_status=transport,
            latest_observation=as_of if success else None,
            success=success,
            error_redacted=error,
            run_id=run_id,
            today=as_of,
            coverage_status="OK" if success else "PARTIAL",
            coverage_json={"capability": capability},
        )


def _set_access(engine, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE mi_source_registry SET access_status = :status, updated_at = NOW() WHERE source_id = :sid"),
            {"status": status[:32], "sid": SOURCE_ID},
        )
