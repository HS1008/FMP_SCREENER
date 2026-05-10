"""
Read-only HTTP API over the same FMP-backed logic as ``dashboard.py``.

Typical clients: curl, scripts, or any tool that can call HTTPS + optional Bearer auth.
OpenAPI schema is served at ``/openapi.json`` (same host as this app).

Run locally:
  pip install fastapi uvicorn[standard]
  set FMP_API_KEY=...
  set CHATGPT_API_TOKEN=...   # optional; if set, all /v1/* routes require Bearer or X-API-Key
  uvicorn api_app:app --host 127.0.0.1 --port 8765

Public HTTPS (e.g. tunnel for remote callers):
  ngrok http 8765

Environment:
  FMP_API_KEY          required for data routes
  CHATGPT_API_TOKEN    optional shared secret; send as ``Authorization: Bearer ...`` or ``X-API-Key``
"""

from __future__ import annotations

import json
import math
import os
from datetime import date, datetime
from typing import Annotated, Any, Callable

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query

import comm_rotation_engine
import config
import consumer_cyclical_rotation_engine
import consumer_defensive_rotation_engine
import data_loader
import dispersion_engine
import energy_rotation_engine
import financial_services_rotation_engine
import healthcare_rotation_engine
import industrials_rotation_engine
import materials_rotation_engine
import real_estate_rotation_engine
import sector_pages
import tech_rotation_engine
import utilities_rotation_engine

load_dotenv()

# (url_slug, FMP sector name for dispersion / sector_pages)
_SECTOR_ROWS: tuple[tuple[str, str], ...] = (
    ("technology", "Technology"),
    ("basic-materials", "Basic Materials"),
    ("communication-services", "Communication Services"),
    ("consumer-cyclical", "Consumer Cyclical"),
    ("consumer-defensive", "Consumer Defensive"),
    ("energy", "Energy"),
    ("financial-services", "Financial Services"),
    ("healthcare", "Healthcare"),
    ("industrials", "Industrials"),
    ("real-estate", "Real Estate"),
    ("utilities", "Utilities"),
)

SLUG_TO_FMP_SECTOR: dict[str, str] = {a: b for a, b in _SECTOR_ROWS}

_ROTATION_BUILDERS: dict[str, Callable[..., dict[str, Any]]] = {
    "technology": tech_rotation_engine.build_tech_rotation_bundle,
    "basic-materials": materials_rotation_engine.build_materials_rotation_bundle,
    "communication-services": comm_rotation_engine.build_comm_rotation_bundle,
    "consumer-cyclical": consumer_cyclical_rotation_engine.build_consumer_cyclical_rotation_bundle,
    "consumer-defensive": consumer_defensive_rotation_engine.build_consumer_defensive_rotation_bundle,
    "energy": energy_rotation_engine.build_energy_rotation_bundle,
    "financial-services": financial_services_rotation_engine.build_financial_services_rotation_bundle,
    "healthcare": healthcare_rotation_engine.build_healthcare_rotation_bundle,
    "industrials": industrials_rotation_engine.build_industrials_rotation_bundle,
    "real-estate": real_estate_rotation_engine.build_real_estate_rotation_bundle,
    "utilities": utilities_rotation_engine.build_utilities_rotation_bundle,
}


def _fmp_api_key() -> str:
    key = (os.getenv("FMP_API_KEY") or "").strip()
    if not key:
        raise HTTPException(
            status_code=503,
            detail="FMP_API_KEY is not set on the server (.env or environment).",
        )
    return key


def require_api_token(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """If CHATGPT_API_TOKEN is set, require ``Authorization: Bearer`` or ``X-API-Key``."""
    token = (os.getenv("CHATGPT_API_TOKEN") or "").strip()
    if not token:
        return
    presented: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    elif x_api_key:
        presented = x_api_key.strip()
    if not presented or presented != token:
        raise HTTPException(status_code=401, detail="Invalid or missing API token.")


def _df_to_jsonable_records(df: pd.DataFrame | None, *, max_rows: int | None = None) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    d = df.reset_index()
    if max_rows is not None and len(d) > max_rows:
        d = d.tail(int(max_rows))
    # ISO dates + NaN -> null
    blob = d.to_json(orient="records", date_format="iso")
    return json.loads(blob)


def _scalar_json(x: Any) -> Any:
    if x is None:
        return None
    if isinstance(x, pd.Timestamp):
        return x.isoformat()
    if isinstance(x, (np.integer, np.floating)):
        xf = float(x) if isinstance(x, np.floating) else int(x)
        if isinstance(xf, float) and (math.isnan(xf) or math.isinf(xf)):
            return None
        return xf
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if isinstance(x, (date, datetime)):
        return x.isoformat() if hasattr(x, "isoformat") else str(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return x


def _jsonify_mapping(d: dict[str, Any]) -> dict[str, Any]:
    return {str(k): _scalar_json(v) for k, v in d.items()}


def _serialize_rotation_bundle(bundle: dict[str, Any], *, include_prices: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": bool(bundle.get("ok")),
        "error": bundle.get("error"),
        "as_of": str(bundle.get("as_of")) if bundle.get("as_of") is not None else None,
        "heatmap": _df_to_jsonable_records(bundle.get("heatmap")),
        "metrics": _df_to_jsonable_records(bundle.get("metrics")),
        "rs_ratio_history": _df_to_jsonable_records(bundle.get("rs_ratio_history"), max_rows=500),
    }
    if include_prices:
        out["prices"] = _df_to_jsonable_records(bundle.get("prices"), max_rows=5000)
    return out


def _serialize_dispersion_bundle(
    bundle: dict[str, Any],
    *,
    include_universe: bool,
    include_tables: bool,
    include_timeseries: bool,
    universe_max_rows: int,
) -> dict[str, Any]:
    summ = bundle.get("summary")
    if isinstance(summ, dict):
        summary_out = _jsonify_mapping(summ)
    else:
        summary_out = {}

    out: dict[str, Any] = {
        "ok": bool(bundle.get("ok")),
        "error": bundle.get("error"),
        "as_of": str(bundle.get("as_of")) if bundle.get("as_of") is not None else None,
        "summary": summary_out,
    }
    if include_universe:
        out["universe"] = _df_to_jsonable_records(bundle.get("universe"), max_rows=universe_max_rows)
    if include_timeseries:
        out["breadth_ts"] = _df_to_jsonable_records(bundle.get("breadth_ts"), max_rows=3000)
        out["dispersion_ts"] = _df_to_jsonable_records(bundle.get("dispersion_ts"), max_rows=3000)
    if include_tables:
        tables = bundle.get("tables") or {}
        if isinstance(tables, dict):
            out["tables"] = {
                str(name): _df_to_jsonable_records(tbl, max_rows=500)
                for name, tbl in tables.items()
                if isinstance(tbl, pd.DataFrame)
            }
    return out


app = FastAPI(
    title="FMP Screener API",
    description="JSON access to sector rotation and dispersion bundles (same sources as the Streamlit dashboard).",
    version="1.0.0",
)

v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_token)])


@v1.get("/meta/sectors")
def list_sectors() -> dict[str, Any]:
    rows = []
    for slug, fmp in _SECTOR_ROWS:
        etf = config.SECTOR_ETF_MAP.get(fmp)
        rows.append({"slug": slug, "fmp_sector": fmp, "sector_etf": etf})
    return {"sectors": rows}


@v1.get("/rotation/{slug}")
def get_rotation(
    slug: str,
    include_prices: bool = Query(False, description="Include long-format price rows (large payload)."),
    refresh: bool = Query(False, description="Bypass local price cache where supported."),
) -> dict[str, Any]:
    slug_l = slug.strip().lower()
    builder = _ROTATION_BUILDERS.get(slug_l)
    if builder is None:
        raise HTTPException(status_code=404, detail=f"Unknown sector slug `{slug}`. See /v1/meta/sectors.")
    session = data_loader.create_http_session()
    bundle = builder(session, _fmp_api_key(), force_refresh=refresh)
    return _serialize_rotation_bundle(bundle, include_prices=include_prices)


@v1.get("/dispersion/{slug}")
def get_dispersion(
    slug: str,
    refresh: bool = Query(False, description="Force profile refresh for universe build."),
    include_universe: bool = Query(False),
    include_tables: bool = Query(False),
    include_timeseries: bool = Query(False),
    universe_max_rows: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    slug_l = slug.strip().lower()
    fmp_sector = SLUG_TO_FMP_SECTOR.get(slug_l)
    if fmp_sector is None:
        raise HTTPException(status_code=404, detail=f"Unknown sector slug `{slug}`. See /v1/meta/sectors.")
    session = data_loader.create_http_session()
    bundle = dispersion_engine.run_dispersion_dashboard_bundle(
        session, _fmp_api_key(), sector=fmp_sector, force_refresh=refresh
    )
    return _serialize_dispersion_bundle(
        bundle,
        include_universe=include_universe,
        include_tables=include_tables,
        include_timeseries=include_timeseries,
        universe_max_rows=universe_max_rows,
    )


@v1.get("/sector/{slug}/snapshot")
def get_sector_snapshot(slug: str, refresh: bool = Query(False)) -> dict[str, Any]:
    """Compact KPI-style snapshot: trend summary, vs SPY summary, risk summary."""
    slug_l = slug.strip().lower()
    fmp_sector = SLUG_TO_FMP_SECTOR.get(slug_l)
    if fmp_sector is None:
        raise HTTPException(status_code=404, detail=f"Unknown sector slug `{slug}`. See /v1/meta/sectors.")
    etf = config.SECTOR_ETF_MAP.get(fmp_sector)
    if not etf:
        raise HTTPException(status_code=500, detail=f"No sector ETF configured for `{fmp_sector}`.")

    session = data_loader.create_http_session()
    key = _fmp_api_key()

    trend_summary: dict[str, Any] = {}
    try:
        _, trend_summary = sector_pages.get_sector_etf_trend_data(
            session, key, sector_etf=etf, force_refresh=refresh
        )
    except Exception as e:
        trend_summary = {"_error": str(e)}

    vs_summary: dict[str, Any] = {}
    try:
        _, vs_summary = sector_pages.get_sector_vs_spy_data(
            session,
            key,
            sector_etf=etf,
            sector_name=fmp_sector,
            force_refresh=refresh,
        )
    except Exception as e:
        vs_summary = {"_error": str(e)}

    risk_summary: dict[str, Any] = {}
    try:
        risk_summary, _, _ = sector_pages.get_sector_risk_data(
            session, key, sector_etf=etf, force_refresh=refresh
        )
    except Exception as e:
        risk_summary = {"_error": str(e)}

    return {
        "slug": slug_l,
        "fmp_sector": fmp_sector,
        "sector_etf": etf,
        "trend": _jsonify_mapping(trend_summary) if isinstance(trend_summary, dict) else {},
        "vs_spy": _jsonify_mapping(vs_summary) if isinstance(vs_summary, dict) else {},
        "risk": _jsonify_mapping(risk_summary) if isinstance(risk_summary, dict) else {},
    }


app.include_router(v1)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.on_event("startup")
def _startup_log() -> None:
    if not (os.getenv("CHATGPT_API_TOKEN") or "").strip():
        print(
            "[api_app] CHATGPT_API_TOKEN is unset: /v1/* is open to anyone who can reach this server. "
            "Set CHATGPT_API_TOKEN before exposing publicly."
        )
