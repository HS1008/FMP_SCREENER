"""Ingest Yahoo Finance volatility metrics into mi_metric_snapshots.

Writer-only. Streamlit never imports this module for live HTTP.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import text

from market_intelligence.nulls import strict_dumps
from market_intelligence.store import RUN_FAILED, RUN_PARTIAL, RUN_SUCCEEDED, finish_run, record_freshness, start_run
from market_intelligence.yahoo_vol import (
    CATEGORY,
    METHOD_SKEW,
    METHOD_SPREAD,
    METHOD_TERM,
    METHOD_VIX,
    RV_UNDERLYING_LABEL,
    RV_WINDOW,
    SOURCE_ID,
    TERM_TENORS,
    TICKER_RV,
    TICKER_SKEW,
    TICKER_VIX,
    positive_number,
    realized_vol_20,
    term_structure,
    vix_minus_rv,
)

logger = logging.getLogger("market_intelligence.ingest_yahoo_vol")
NY = ZoneInfo("America/New_York")
HISTORY_LOOKBACK_DAYS = 90
DATASETS = ("vix", "vix_term_structure", "skew", "iv_minus_rv")

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


def session_date(now: datetime | None = None) -> date:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(NY).date()


def fetch_yahoo_closes(ticker: str, *, start: date, end: date) -> list[tuple[date, float]]:
    """Return (session_date, close) ascending. Raises on import failure; empty on no data."""
    import yfinance as yf

    hist = yf.Ticker(ticker).history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=True,
    )
    if hist is None or hist.empty or "Close" not in hist.columns:
        return []
    out: list[tuple[date, float]] = []
    for idx, row in hist.iterrows():
        day = idx.date() if hasattr(idx, "date") else date.fromisoformat(str(idx)[:10])
        close = positive_number(row.get("Close"))
        if close is None:
            continue
        out.append((day, close))
    out.sort(key=lambda item: item[0])
    return out


def ingest_yahoo_vol(engine, *, parent_run_id: str | None = None, today: date | None = None) -> dict[str, Any]:
    as_of = today or session_date()
    start = as_of - timedelta(days=HISTORY_LOOKBACK_DAYS)
    report: dict[str, Any] = {"source_id": SOURCE_ID, "as_of": as_of.isoformat(), "sections": {}, "failed": False}
    _ensure_source(engine)

    series: dict[str, list[tuple[date, float]]] = {}
    tickers = [TICKER_VIX, TICKER_SKEW, TICKER_RV] + [t for t, _, _ in TERM_TENORS]
    for ticker in dict.fromkeys(tickers):
        try:
            series[ticker] = fetch_yahoo_closes(ticker, start=start, end=as_of)
        except Exception as exc:  # noqa: BLE001
            logger.warning("yahoo fetch failed for %s: %s", ticker, type(exc).__name__)
            series[ticker] = []
            report.setdefault("fetch_errors", {})[ticker] = type(exc).__name__

    levels = {ticker: (rows[-1][1] if rows else None) for ticker, rows in series.items()}
    level_dates = {ticker: (rows[-1][0] if rows else None) for ticker, rows in series.items()}

    vix_rows = _ingest_vix(engine, as_of, series.get(TICKER_VIX) or [], parent_run_id, report, levels, level_dates)
    _ingest_term(engine, as_of, levels, level_dates, parent_run_id, report)
    _ingest_skew(engine, as_of, series.get(TICKER_SKEW) or [], parent_run_id, report)
    _ingest_spread(engine, as_of, series.get(TICKER_RV) or [], vix_rows, parent_run_id, report)

    hard = {"FAILED", "UNAVAILABLE"}
    report["failed"] = any(section.get("status") in hard for section in report["sections"].values())
    return report


def _ingest_vix(engine, as_of: date, rows: list[tuple[date, float]], parent_run_id: str | None, report: dict[str, Any], levels: Mapping[str, float | None], level_dates: Mapping[str, date | None]) -> list[dict[str, Any]]:
    run_id = _open(engine, "vix", parent_run_id, as_of)
    try:
        if not rows:
            raise RuntimeError("no_vix_rows")
        written = []
        prev = None
        for day, close in rows[-(60):]:
            change = None if prev is None else close - prev
            detail = {
                "source": SOURCE_ID,
                "yahoo_ticker": TICKER_VIX,
                "observation_date": day.isoformat(),
                "change_1d": change,
            }
            written.append(
                _metric("VIX_SPOT", day, close, "vol_points", METHOD_VIX, "OK", detail, observed=datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY))
            )
            prev = close
        count = _write_rows(engine, written, run_id)
        latest = levels.get(TICKER_VIX)
        ok = latest is not None
        _close(engine, run_id, RUN_SUCCEEDED if ok else RUN_PARTIAL, count, None if ok else "missing_vix")
        _mark(engine, "vix", as_of, transport="SUCCEEDED" if ok else "PARTIAL", success=ok, error=None if ok else "missing_vix", run_id=run_id)
        report["sections"]["vix"] = {
            "status": "SUCCEEDED" if ok else "PARTIAL",
            "value": latest,
            "observation_date": (level_dates.get(TICKER_VIX) or as_of).isoformat(),
            "rows": count,
        }
        return written
    except Exception as exc:  # noqa: BLE001
        _close(engine, run_id, RUN_FAILED, 0, type(exc).__name__)
        _mark(engine, "vix", as_of, transport="FAILED", success=False, error=type(exc).__name__, run_id=run_id)
        report["sections"]["vix"] = {"status": "FAILED", "reason": type(exc).__name__}
        return []


def _ingest_term(engine, as_of: date, levels: Mapping[str, float | None], level_dates: Mapping[str, date | None], parent_run_id: str | None, report: dict[str, Any]) -> None:
    run_id = _open(engine, "vix_term_structure", parent_run_id, as_of)
    curve = term_structure(levels)
    rows = []
    unavailable = []
    for point in curve["points"]:
        ticker = point["ticker"]
        metric_id = point["metric_id"]
        level = point["level"]
        day = level_dates.get(ticker) or as_of
        if level is None:
            unavailable.append(point["tenor"])
            rows.append(
                _metric(
                    metric_id,
                    as_of,
                    None,
                    "vol_points",
                    METHOD_TERM,
                    "INCOMPLETE",
                    {"source": SOURCE_ID, "yahoo_ticker": ticker, "tenor": point["tenor"], "reason": "ticker_unavailable"},
                    observed=None,
                )
            )
            continue
        rows.append(
            _metric(
                metric_id,
                day,
                level,
                "vol_points",
                METHOD_TERM,
                "OK",
                {"source": SOURCE_ID, "yahoo_ticker": ticker, "tenor": point["tenor"]},
                observed=datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY),
            )
        )
    slope = curve["front_to_back_slope"]
    rows.append(
        _metric(
            "VIX_INDEX_FRONT_TO_BACK",
            as_of,
            slope,
            "vol_points",
            METHOD_TERM,
            "OK" if slope is not None else "INCOMPLETE",
            {
                "source": SOURCE_ID,
                "construction": curve["construction"],
                "label": curve["label"],
                "curve_state": curve["curve_state"],
                "note": curve["note"],
                "unavailable_tenors": unavailable,
            },
            observed=None,
        )
    )
    count = _write_rows(engine, rows, run_id)
    ok = slope is not None and len(unavailable) < len(TERM_TENORS)
    _close(engine, run_id, RUN_SUCCEEDED if ok else RUN_PARTIAL, count, None if not unavailable else "partial_tenors")
    _mark(engine, "vix_term_structure", as_of, transport="SUCCEEDED" if ok else "PARTIAL", success=ok, error=None if not unavailable else "partial_tenors", run_id=run_id)
    report["sections"]["vix_term_structure"] = {
        "status": "SUCCEEDED" if ok else "PARTIAL",
        "curve_state": curve["curve_state"],
        "slope": slope,
        "unavailable_tenors": unavailable,
    }


def _ingest_skew(engine, as_of: date, rows: list[tuple[date, float]], parent_run_id: str | None, report: dict[str, Any]) -> None:
    run_id = _open(engine, "skew", parent_run_id, as_of)
    try:
        if not rows:
            raise RuntimeError("no_skew_rows")
        written = []
        for day, close in rows[-(60):]:
            written.append(
                _metric(
                    "SKEW_INDEX",
                    day,
                    close,
                    "index_points",
                    METHOD_SKEW,
                    "OK",
                    {
                        "source": SOURCE_ID,
                        "yahoo_ticker": TICKER_SKEW,
                        "label": "Cboe SKEW Index",
                        "note": "Yahoo ^SKEW. Not a 25-delta SPX options skew.",
                    },
                    observed=datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY),
                )
            )
        count = _write_rows(engine, written, run_id)
        _close(engine, run_id, RUN_SUCCEEDED, count, None)
        _mark(engine, "skew", as_of, transport="SUCCEEDED", success=True, error=None, run_id=run_id)
        report["sections"]["skew"] = {"status": "SUCCEEDED", "value": rows[-1][1], "observation_date": rows[-1][0].isoformat(), "rows": count}
    except Exception as exc:  # noqa: BLE001
        _close(engine, run_id, RUN_FAILED, 0, type(exc).__name__)
        _mark(engine, "skew", as_of, transport="FAILED", success=False, error=type(exc).__name__, run_id=run_id)
        report["sections"]["skew"] = {"status": "FAILED", "reason": type(exc).__name__}


def _ingest_spread(engine, as_of: date, gspc_rows: list[tuple[date, float]], vix_metric_rows: list[dict[str, Any]], parent_run_id: str | None, report: dict[str, Any]) -> None:
    run_id = _open(engine, "iv_minus_rv", parent_run_id, as_of)
    closes = [close for _, close in gspc_rows]
    rv = realized_vol_20(closes)
    vix_latest = None
    for row in reversed(vix_metric_rows):
        if row.get("metric_id") == "VIX_SPOT" and row.get("status") == "OK" and row.get("value") is not None:
            vix_latest = float(row["value"])
            break
    if vix_latest is None and gspc_rows:
        # fall back to latest stored VIX if this run did not rewrite history
        pass
    spread = vix_minus_rv(vix_latest, rv)
    detail = {
        "source": SOURCE_ID,
        "method": spread.get("method") or METHOD_SPREAD,
        "underlying": RV_UNDERLYING_LABEL,
        "yahoo_ticker_rv": TICKER_RV,
        "yahoo_ticker_vix": TICKER_VIX,
        "window": RV_WINDOW,
        "vix": spread.get("vix"),
        "rv20": spread.get("rv"),
        "reason": spread.get("reason"),
        "rv_detail": {k: rv.get(k) for k in ("observations", "reason", "method")},
    }
    rows = [
        _metric(
            "GSPC_REALIZED_VOL_20D",
            as_of,
            rv.get("value"),
            "vol_points",
            rv.get("method") or "yahoo_gspc_rv20_v1",
            rv.get("status") or "INCOMPLETE",
            {**detail, "construction": "rv20"},
            observed=None,
        ),
        _metric(
            spread["metric_id"],
            as_of,
            spread.get("value"),
            "vol_points",
            METHOD_SPREAD,
            spread.get("status") or "INCOMPLETE",
            detail,
            observed=None,
        ),
    ]
    count = _write_rows(engine, rows, run_id)
    ok = spread.get("status") == "OK"
    _close(engine, run_id, RUN_SUCCEEDED if ok else RUN_PARTIAL, count, spread.get("reason"))
    _mark(engine, "iv_minus_rv", as_of, transport="SUCCEEDED" if ok else "PARTIAL", success=ok, error=spread.get("reason"), run_id=run_id)
    report["sections"]["iv_minus_rv"] = {
        "status": "SUCCEEDED" if ok else "INCOMPLETE",
        "metric_id": spread.get("metric_id"),
        "value": spread.get("value"),
        "reason": spread.get("reason"),
        "underlying": RV_UNDERLYING_LABEL,
    }


def _metric(metric_id: str, as_of: date, value: float | None, units: str, method: str, status: str, detail: Mapping[str, Any], *, observed: datetime | None) -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "category": CATEGORY,
        "as_of": as_of,
        "value": value,
        "units": units,
        "transform_version": method,
        "status": status,
        "detail": strict_dumps(dict(detail)),
        "inputs_retrieved_max": observed,
    }


def _write_rows(engine, rows: list[dict[str, Any]], run_id: str | None) -> int:
    if not rows:
        return 0
    with engine.begin() as conn:
        for row in rows:
            conn.execute(
                _UPSERT,
                {
                    "metric_id": row["metric_id"],
                    "category": row["category"],
                    "as_of": row["as_of"],
                    "value": row["value"],
                    "units": row["units"],
                    "transform_version": row["transform_version"],
                    "status": row["status"],
                    "detail": row["detail"],
                    "run_id": run_id,
                    "inputs_retrieved_max": row["inputs_retrieved_max"],
                },
            )
    return len(rows)


def _open(engine, dataset: str, parent_run_id: str | None, as_of: date) -> str:
    with engine.begin() as conn:
        return start_run(conn, source_id=SOURCE_ID, dataset=dataset, parent_run_id=parent_run_id)


def _close(engine, run_id: str, status: str, written: int, error: str | None) -> None:
    with engine.begin() as conn:
        finish_run(conn, run_id, status=status, counts={"received": written, "inserted": written}, error_redacted=error)


def _mark(engine, dataset: str, as_of: date, *, transport: str, success: bool, error: str | None, run_id: str | None) -> None:
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
            coverage_status="OK" if success else "PARTIAL",
        )


def _ensure_source(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO mi_source_registry (
                    source_id, provider, dataset, enabled, access_status, source_url, expected_cadence,
                    units_metadata, usage_scope, terms_notes, attribution, catalog_version, updated_at
                ) VALUES (
                    :sid, 'Yahoo Finance (yfinance, unofficial)', 'volatility_indices', TRUE, 'OPTIONAL_FALLBACK',
                    'https://finance.yahoo.com/', 'D', CAST(:units AS JSONB), 'INTERNAL_ONLY',
                    'Free Yahoo closes for VIX, SKEW, VIX-family tenors, and GSPC RV20. Streamlit is read-only.',
                    'Yahoo Finance via yfinance (unofficial; no SLA). Cboe SKEW Index name refers to the published index, not a LiveVol feed.',
                    'yahoo_vol_v1', NOW()
                )
                ON CONFLICT (source_id) DO UPDATE SET
                    dataset = EXCLUDED.dataset,
                    terms_notes = EXCLUDED.terms_notes,
                    attribution = EXCLUDED.attribution,
                    updated_at = NOW()
                """
            ),
            {"sid": SOURCE_ID, "units": strict_dumps({"vol": "vol_points", "skew": "index_points"})},
        )
