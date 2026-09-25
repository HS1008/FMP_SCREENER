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
    BACK_TICKER,
    CATEGORY,
    FRONT_TICKER,
    METHOD_RV,
    METHOD_SKEW,
    METHOD_SPREAD,
    METHOD_TERM,
    METHOD_VIX,
    METRIC_RV,
    METRIC_SPREAD,
    RV_UNDERLYING_LABEL,
    RV_WINDOW,
    SOURCE_ID,
    TERM_TENORS,
    TICKER_RV,
    TICKER_SKEW,
    TICKER_VIX,
    align_vix_minus_rv,
    classify_slope,
    historical_realized_vol,
    historical_vix_minus_rv,
    positive_number,
    term_structure,
)

logger = logging.getLogger("market_intelligence.ingest_yahoo_vol")
NY = ZoneInfo("America/New_York")
# Calendar span covering VIX1D history (from 2023) plus the 22-close RV21 warmup.
HISTORY_LOOKBACK_DAYS = 1460
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
    run_day = today or session_date()
    start = run_day - timedelta(days=HISTORY_LOOKBACK_DAYS)
    report: dict[str, Any] = {"source_id": SOURCE_ID, "run_day": run_day.isoformat(), "sections": {}, "failed": False}
    _ensure_source(engine)

    series: dict[str, list[tuple[date, float]]] = {}
    tickers = [TICKER_VIX, TICKER_SKEW, TICKER_RV] + [t for t, _, _ in TERM_TENORS]
    for ticker in dict.fromkeys(tickers):
        try:
            series[ticker] = fetch_yahoo_closes(ticker, start=start, end=run_day)
        except Exception as exc:  # noqa: BLE001
            logger.warning("yahoo fetch failed for %s: %s", ticker, type(exc).__name__)
            series[ticker] = []
            report.setdefault("fetch_errors", {})[ticker] = type(exc).__name__

    _ingest_vix(engine, series.get(TICKER_VIX) or [], parent_run_id, report)
    _ingest_term(engine, series, parent_run_id, report)
    _ingest_skew(engine, series.get(TICKER_SKEW) or [], parent_run_id, report)
    _ingest_spread(engine, series.get(TICKER_VIX) or [], series.get(TICKER_RV) or [], parent_run_id, report)

    hard = {"FAILED", "UNAVAILABLE"}
    report["failed"] = any(section.get("status") in hard for section in report["sections"].values())
    return report


def _session_close_ts(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY)


def _iso_date(value: Any) -> str | None:
    if isinstance(value, date):
        return value.isoformat()
    return value if isinstance(value, str) else None


def _ingest_vix(engine, rows: list[tuple[date, float]], parent_run_id: str | None, report: dict[str, Any]) -> None:
    run_id = _open(engine, "vix", parent_run_id)
    try:
        if not rows:
            raise RuntimeError("no_vix_rows")
        written = []
        prev = None
        for day, close in rows:
            change = None if prev is None else close - prev
            detail = {
                "source": SOURCE_ID,
                "yahoo_ticker": TICKER_VIX,
                "observation_date": day.isoformat(),
                "change_1d": change,
            }
            written.append(
                _metric("VIX_SPOT", day, close, "vol_points", METHOD_VIX, "OK", detail, observed=_session_close_ts(day))
            )
            prev = close
        count = _write_rows(engine, written, run_id)
        obs = rows[-1][0]
        latest = rows[-1][1]
        _close(engine, run_id, RUN_SUCCEEDED, count, None)
        _mark(engine, "vix", obs, transport="SUCCEEDED", success=True, error=None, run_id=run_id)
        report["sections"]["vix"] = {
            "status": "SUCCEEDED",
            "value": latest,
            "observation_date": obs.isoformat(),
            "rows": count,
        }
    except Exception as exc:  # noqa: BLE001
        _close(engine, run_id, RUN_FAILED, 0, type(exc).__name__)
        _mark(engine, "vix", None, transport="FAILED", success=False, error=type(exc).__name__, run_id=run_id)
        report["sections"]["vix"] = {"status": "FAILED", "reason": type(exc).__name__}


def _levels_by_session(series: Mapping[str, list[tuple[date, float]]]) -> dict[date, dict[str, float]]:
    by_date: dict[date, dict[str, float]] = {}
    for ticker, _tenor, _metric_id in TERM_TENORS:
        for day, level in series.get(ticker) or []:
            number = positive_number(level)
            if number is None:
                continue
            by_date.setdefault(day, {})[ticker] = number
    return by_date


def _ingest_term(engine, series: Mapping[str, list[tuple[date, float]]], parent_run_id: str | None, report: dict[str, Any]) -> None:
    run_id = _open(engine, "vix_term_structure", parent_run_id)
    tenor_series = {ticker: series.get(ticker) or [] for ticker, _, _ in TERM_TENORS}
    curve = term_structure(tenor_series)
    curve_date = curve.get("observation_date")
    unavailable = list(curve.get("unavailable_tenors") or [])
    by_date = _levels_by_session(tenor_series)
    rows = []
    slope_dates: list[date] = []
    for day in sorted(by_date):
        levels = by_date[day]
        for ticker, tenor, metric_id in TERM_TENORS:
            level = levels.get(ticker)
            if level is None:
                continue
            rows.append(
                _metric(
                    metric_id,
                    day,
                    level,
                    "vol_points",
                    METHOD_TERM,
                    "OK",
                    {
                        "source": SOURCE_ID,
                        "yahoo_ticker": ticker,
                        "tenor": tenor,
                        "curve_observation_date": day.isoformat(),
                    },
                    observed=_session_close_ts(day),
                )
            )
        front = levels.get(FRONT_TICKER)
        back = levels.get(BACK_TICKER)
        if front is None or back is None:
            continue
        slope_info = classify_slope(front, back)
        slope_dates.append(day)
        rows.append(
            _metric(
                "VIX_INDEX_FRONT_TO_BACK",
                day,
                slope_info["front_to_back_slope"],
                "vol_points",
                METHOD_TERM,
                slope_info["slope_status"],
                {
                    "source": SOURCE_ID,
                    "construction": METHOD_TERM,
                    "label": "VIX index term structure",
                    "curve_state": slope_info["curve_state"],
                    "front_tenor": "9D",
                    "back_tenor": "1Y",
                    "note": curve["note"],
                    "slope_reason": slope_info["slope_reason"],
                    "curve_observation_date": day.isoformat(),
                },
                observed=_session_close_ts(day),
            )
        )
    if not slope_dates and isinstance(curve_date, date):
        rows.append(
            _metric(
                "VIX_INDEX_FRONT_TO_BACK",
                curve_date,
                None,
                "vol_points",
                METHOD_TERM,
                "INCOMPLETE",
                {
                    "source": SOURCE_ID,
                    "construction": curve["construction"],
                    "label": curve["label"],
                    "curve_state": None,
                    "front_tenor": curve.get("front_tenor"),
                    "back_tenor": curve.get("back_tenor"),
                    "note": curve["note"],
                    "unavailable_tenors": unavailable,
                    "slope_reason": curve.get("slope_reason"),
                    "curve_observation_date": curve_date.isoformat(),
                },
                observed=None,
            )
        )
    count = _write_rows(engine, rows, run_id)
    slope = curve["front_to_back_slope"]
    ok = slope is not None and curve_date is not None
    _close(engine, run_id, RUN_SUCCEEDED if ok else RUN_PARTIAL, count, None if ok else (curve.get("slope_reason") or "partial_tenors"))
    _mark(
        engine,
        "vix_term_structure",
        curve_date,
        transport="SUCCEEDED" if ok else "PARTIAL",
        success=curve_date is not None,
        error=None if ok else (curve.get("slope_reason") or "partial_tenors"),
        run_id=run_id,
    )
    report["sections"]["vix_term_structure"] = {
        "status": "SUCCEEDED" if ok else "PARTIAL",
        "curve_state": curve["curve_state"],
        "slope": slope,
        "observation_date": curve_date.isoformat() if isinstance(curve_date, date) else None,
        "unavailable_tenors": unavailable,
        "rows": count,
    }


def _ingest_skew(engine, rows: list[tuple[date, float]], parent_run_id: str | None, report: dict[str, Any]) -> None:
    run_id = _open(engine, "skew", parent_run_id)
    try:
        if not rows:
            raise RuntimeError("no_skew_rows")
        written = []
        for day, close in rows:
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
                        "observation_date": day.isoformat(),
                    },
                    observed=_session_close_ts(day),
                )
            )
        count = _write_rows(engine, written, run_id)
        obs = rows[-1][0]
        _close(engine, run_id, RUN_SUCCEEDED, count, None)
        _mark(engine, "skew", obs, transport="SUCCEEDED", success=True, error=None, run_id=run_id)
        report["sections"]["skew"] = {
            "status": "SUCCEEDED",
            "value": rows[-1][1],
            "observation_date": obs.isoformat(),
            "rows": count,
        }
    except Exception as exc:  # noqa: BLE001
        _close(engine, run_id, RUN_FAILED, 0, type(exc).__name__)
        _mark(engine, "skew", None, transport="FAILED", success=False, error=type(exc).__name__, run_id=run_id)
        report["sections"]["skew"] = {"status": "FAILED", "reason": type(exc).__name__}


def _rv_row_detail(point: Mapping[str, Any]) -> dict[str, Any]:
    window_start = point.get("window_start_date")
    return {
        "source": SOURCE_ID,
        "method": point.get("method") or METHOD_RV,
        "underlying": RV_UNDERLYING_LABEL,
        "yahoo_ticker": TICKER_RV,
        "window": RV_WINDOW,
        "returns": point.get("returns"),
        "closes_required": point.get("closes_required"),
        "ddof": 1,
        "construction": "rv21",
        "reason": point.get("reason"),
        "window_start_date": window_start.isoformat() if isinstance(window_start, date) else window_start,
        "observation_date": _iso_date(point.get("observation_date")),
    }


def _ingest_spread(
    engine,
    vix_rows: list[tuple[date, float]],
    gspc_rows: list[tuple[date, float]],
    parent_run_id: str | None,
    report: dict[str, Any],
) -> None:
    run_id = _open(engine, "iv_minus_rv", parent_run_id)
    rv_points = historical_realized_vol(gspc_rows)
    spreads = historical_vix_minus_rv(vix_rows, rv_points)
    latest_align = align_vix_minus_rv(vix_rows, gspc_rows)
    rows: list[dict[str, Any]] = []
    for point in rv_points:
        obs = point.get("observation_date")
        if not isinstance(obs, date) or point.get("value") is None:
            continue
        rows.append(
            _metric(
                METRIC_RV,
                obs,
                point.get("value"),
                "vol_points",
                METHOD_RV,
                "OK",
                _rv_row_detail(point),
                observed=_session_close_ts(obs),
            )
        )
    spread_dates: set[date] = set()
    for spread in spreads:
        obs = spread.get("observation_date")
        if not isinstance(obs, date) or spread.get("value") is None:
            continue
        spread_dates.add(obs)
        rows.append(
            _metric(
                METRIC_SPREAD,
                obs,
                spread.get("value"),
                "vol_points",
                METHOD_SPREAD,
                "OK",
                {
                    "source": SOURCE_ID,
                    "method": METHOD_SPREAD,
                    "underlying": RV_UNDERLYING_LABEL,
                    "yahoo_ticker_rv": TICKER_RV,
                    "yahoo_ticker_vix": TICKER_VIX,
                    "window": RV_WINDOW,
                    "vix": spread.get("vix"),
                    "rv21": spread.get("rv"),
                    "reason": None,
                    "vix_observation_date": _iso_date(spread.get("vix_observation_date")),
                    "rv_observation_date": _iso_date(spread.get("rv_observation_date")),
                    "observation_date": obs.isoformat(),
                    "construction": "vix_minus_rv21",
                },
                observed=_session_close_ts(obs),
            )
        )
    rv_obs = rv_points[-1].get("observation_date") if rv_points else latest_align.get("rv_observation_date")
    if latest_align.get("status") != "OK":
        incomplete_as_of = latest_align.get("observation_date") or rv_obs
        if isinstance(incomplete_as_of, date) and incomplete_as_of not in spread_dates:
            rows.append(
                _metric(
                    METRIC_SPREAD,
                    incomplete_as_of,
                    None,
                    "vol_points",
                    METHOD_SPREAD,
                    "INCOMPLETE",
                    {
                        "source": SOURCE_ID,
                        "method": METHOD_SPREAD,
                        "underlying": RV_UNDERLYING_LABEL,
                        "yahoo_ticker_rv": TICKER_RV,
                        "yahoo_ticker_vix": TICKER_VIX,
                        "window": RV_WINDOW,
                        "vix": latest_align.get("vix"),
                        "rv21": latest_align.get("rv"),
                        "reason": latest_align.get("reason"),
                        "vix_observation_date": _iso_date(latest_align.get("vix_observation_date")),
                        "rv_observation_date": _iso_date(latest_align.get("rv_observation_date") or rv_obs),
                        "observation_date": incomplete_as_of.isoformat(),
                        "construction": "vix_minus_rv21",
                    },
                    observed=None,
                )
            )
    count = _write_rows(engine, rows, run_id)
    latest_spread = spreads[-1] if spreads else None
    ok = latest_spread is not None
    spread_obs = latest_spread.get("observation_date") if latest_spread else (latest_align.get("observation_date") or rv_obs)
    freshness_obs = spread_obs if ok and isinstance(spread_obs, date) else (rv_obs if isinstance(rv_obs, date) else None)
    reason = None if ok else latest_align.get("reason")
    _close(engine, run_id, RUN_SUCCEEDED if ok else RUN_PARTIAL, count, reason)
    _mark(
        engine,
        "iv_minus_rv",
        freshness_obs if isinstance(freshness_obs, date) else None,
        transport="SUCCEEDED" if ok else "PARTIAL",
        success=isinstance(freshness_obs, date),
        error=reason,
        run_id=run_id,
    )
    report["sections"]["iv_minus_rv"] = {
        "status": "SUCCEEDED" if ok else "INCOMPLETE",
        "metric_id": METRIC_SPREAD,
        "value": None if latest_spread is None else latest_spread.get("value"),
        "reason": reason,
        "underlying": RV_UNDERLYING_LABEL,
        "observation_date": _iso_date(spread_obs),
        "rv_observation_date": _iso_date(rv_obs),
        "rows": count,
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


def _open(engine, dataset: str, parent_run_id: str | None) -> str:
    with engine.begin() as conn:
        return start_run(conn, source_id=SOURCE_ID, dataset=dataset, parent_run_id=parent_run_id)


def _close(engine, run_id: str, status: str, written: int, error: str | None) -> None:
    with engine.begin() as conn:
        finish_run(conn, run_id, status=status, counts={"received": written, "inserted": written}, error_redacted=error)


def _mark(engine, dataset: str, observation: date | None, *, transport: str, success: bool, error: str | None, run_id: str | None) -> None:
    with engine.begin() as conn:
        record_freshness(
            conn,
            source_id=SOURCE_ID,
            dataset=dataset,
            cadence="D",
            transport_status=transport,
            latest_observation=observation if success and observation is not None else None,
            success=success,
            error_redacted=error,
            run_id=run_id,
            coverage_status="OK" if success and error is None else "PARTIAL",
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
                    'Free Yahoo closes for VIX, SKEW, VIX index tenors including ^VIX1D, and GSPC RV21. Streamlit is read-only.',
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
