"""
FMP sector dashboard (Streamlit): ETF trend, industry rotation, vs SPY, risk, internal dispersion.

Run:
  pip install streamlit pandas openpyxl python-dotenv requests urllib3
  streamlit run dashboard.py
"""

from __future__ import annotations

import inspect
import os
import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

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
import rotation_price_batch
import sector_dashboard_ui
import tech_rotation_engine
import utilities_rotation_engine

ROOT = Path(__file__).resolve().parent
_CACHE_TTL_SECONDS: int = int(getattr(config, "DASHBOARD_CACHE_TTL_SECONDS", 86400))
_WARM_DELAY_S: float = float(getattr(config, "DASHBOARD_BACKGROUND_WARM_DELAY_SECONDS", 45.0))


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading sector dispersion…")
def _cached_sector_dispersion(api_key: str, sector: str) -> dict:
    """Session is created inside the cache (``Session`` objects are not cache-safe keys)."""
    session = data_loader.create_http_session()
    bundle_fn = dispersion_engine.run_dispersion_dashboard_bundle
    params = inspect.signature(bundle_fn).parameters
    if "sector" in params:
        if "price_fetch_max_workers" in params:
            return bundle_fn(
                session,
                api_key,
                sector=sector,
                force_refresh=False,
                price_fetch_max_workers=max(1, int(getattr(config, "DASHBOARD_PRICE_FETCH_MAX_WORKERS", 4))),
            )
        return bundle_fn(session, api_key, sector=sector, force_refresh=False)
    if str(sector).strip() != "Technology":
        return {
            "ok": False,
            "error": (
                "`dispersion_engine.run_dispersion_dashboard_bundle` is missing the `sector` parameter "
                "(reload `dispersion_engine.py` from this project and restart Streamlit so multi-sector "
                "dispersion tabs can filter the universe)."
            ),
            "universe": pd.DataFrame(),
            "summary": {},
            "tables": {},
            "wide_close": pd.DataFrame(),
            "breadth_ts": pd.DataFrame(),
            "dispersion_ts": pd.DataFrame(),
            "as_of": date.today(),
        }
    return bundle_fn(session, api_key, force_refresh=False)


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading industry rotation prices (all sectors)…")
def _cached_all_dashboard_rotation_prices_long(api_key: str) -> pd.DataFrame:
    """One parallelized price pull for the union of all sector rotation symbols (see ``rotation_price_batch``)."""
    session = data_loader.create_http_session()
    return rotation_price_batch.fetch_all_dashboard_rotation_prices_long(
        session, api_key, force_refresh=False
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Technology industry rotation…")
def _cached_tech_rotation(api_key: str) -> dict:
    session = data_loader.create_http_session()
    bulk = _cached_all_dashboard_rotation_prices_long(api_key)
    return tech_rotation_engine.build_tech_rotation_bundle(
        session, api_key, force_refresh=False, prefetched_prices_long=bulk
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Basic Materials industry rotation…")
def _cached_materials_rotation(api_key: str) -> dict:
    session = data_loader.create_http_session()
    bulk = _cached_all_dashboard_rotation_prices_long(api_key)
    return materials_rotation_engine.build_materials_rotation_bundle(
        session, api_key, force_refresh=False, prefetched_prices_long=bulk
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Communication Services industry rotation…")
def _cached_comm_rotation(api_key: str) -> dict:
    session = data_loader.create_http_session()
    bulk = _cached_all_dashboard_rotation_prices_long(api_key)
    return comm_rotation_engine.build_comm_rotation_bundle(
        session, api_key, force_refresh=False, prefetched_prices_long=bulk
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Consumer Cyclical industry rotation…")
def _cached_consumer_cyclical_rotation(api_key: str) -> dict:
    session = data_loader.create_http_session()
    bulk = _cached_all_dashboard_rotation_prices_long(api_key)
    return consumer_cyclical_rotation_engine.build_consumer_cyclical_rotation_bundle(
        session, api_key, force_refresh=False, prefetched_prices_long=bulk
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Consumer Defensive industry rotation…")
def _cached_consumer_defensive_rotation(api_key: str) -> dict:
    session = data_loader.create_http_session()
    bulk = _cached_all_dashboard_rotation_prices_long(api_key)
    return consumer_defensive_rotation_engine.build_consumer_defensive_rotation_bundle(
        session, api_key, force_refresh=False, prefetched_prices_long=bulk
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Energy industry rotation…")
def _cached_energy_rotation(api_key: str) -> dict:
    session = data_loader.create_http_session()
    bulk = _cached_all_dashboard_rotation_prices_long(api_key)
    return energy_rotation_engine.build_energy_rotation_bundle(
        session, api_key, force_refresh=False, prefetched_prices_long=bulk
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Financial Services industry rotation…")
def _cached_financial_services_rotation(api_key: str) -> dict:
    session = data_loader.create_http_session()
    bulk = _cached_all_dashboard_rotation_prices_long(api_key)
    return financial_services_rotation_engine.build_financial_services_rotation_bundle(
        session, api_key, force_refresh=False, prefetched_prices_long=bulk
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Healthcare industry rotation…")
def _cached_healthcare_rotation(api_key: str) -> dict:
    session = data_loader.create_http_session()
    bulk = _cached_all_dashboard_rotation_prices_long(api_key)
    return healthcare_rotation_engine.build_healthcare_rotation_bundle(
        session, api_key, force_refresh=False, prefetched_prices_long=bulk
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Industrials industry rotation…")
def _cached_industrials_rotation(api_key: str) -> dict:
    session = data_loader.create_http_session()
    bulk = _cached_all_dashboard_rotation_prices_long(api_key)
    return industrials_rotation_engine.build_industrials_rotation_bundle(
        session, api_key, force_refresh=False, prefetched_prices_long=bulk
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Real Estate industry rotation…")
def _cached_real_estate_rotation(api_key: str) -> dict:
    session = data_loader.create_http_session()
    bulk = _cached_all_dashboard_rotation_prices_long(api_key)
    return real_estate_rotation_engine.build_real_estate_rotation_bundle(
        session, api_key, force_refresh=False, prefetched_prices_long=bulk
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner="Loading Utilities industry rotation…")
def _cached_utilities_rotation(api_key: str) -> dict:
    session = data_loader.create_http_session()
    bulk = _cached_all_dashboard_rotation_prices_long(api_key)
    return utilities_rotation_engine.build_utilities_rotation_bundle(
        session, api_key, force_refresh=False, prefetched_prices_long=bulk
    )


SectorTabSpec = sector_dashboard_ui.SectorTabSpec

SECTOR_SPECS: tuple[SectorTabSpec, ...] = (
    SectorTabSpec("Technology Sector", "Technology", "XLK", "Technology", _cached_tech_rotation),
    SectorTabSpec("Basic Materials Sector", "Basic Materials", "XLB", "Basic Materials", _cached_materials_rotation),
    SectorTabSpec(
        "Communication Services Sector",
        "Communication Services",
        "XLC",
        "Communication Services",
        _cached_comm_rotation,
    ),
    SectorTabSpec(
        "Consumer Cyclical Sector", "Consumer Cyclical", "XLY", "Consumer Cyclical", _cached_consumer_cyclical_rotation
    ),
    SectorTabSpec(
        "Consumer Defensive Sector",
        "Consumer Defensive",
        "XLP",
        "Consumer Defensive",
        _cached_consumer_defensive_rotation,
    ),
    SectorTabSpec("Energy Sector", "Energy", "XLE", "Energy", _cached_energy_rotation),
    SectorTabSpec(
        "Financial Services Sector",
        "Financial Services",
        "XLF",
        "Financial Services",
        _cached_financial_services_rotation,
    ),
    SectorTabSpec("Healthcare Sector", "Healthcare", "XLV", "Healthcare", _cached_healthcare_rotation),
    SectorTabSpec("Industrials Sector", "Industrials", "XLI", "Industrials", _cached_industrials_rotation),
    SectorTabSpec(
        "Real Estate Sector",
        "Real Estate",
        "XLRE",
        "Real Estate",
        _cached_real_estate_rotation,
        rotation_instruments="REITs and ETFs listed per row",
    ),
    SectorTabSpec("Utilities Sector", "Utilities", "XLU", "Utilities", _cached_utilities_rotation),
)

_SECTOR_WARM_SPECS: tuple[tuple[str, str, Callable[[str], dict]], ...] = tuple(
    (s.page_radio_label, s.fmp_sector, s.rotation_cache) for s in SECTOR_SPECS
)

_WARM_THREAD: threading.Thread | None = None
_WARM_START_LOCK = threading.Lock()


def _spawn_background_sector_warm(api_key: str, active_page: str) -> None:
    key = (api_key or "").strip()
    if not key:
        return

    def _worker() -> None:
        global _WARM_THREAD
        try:
            for page_label, fmp_sector, rot_cache in _SECTOR_WARM_SPECS:
                if page_label == active_page:
                    continue
                try:
                    _cached_sector_dispersion(key, fmp_sector)
                except Exception:
                    pass
                try:
                    rot_cache(key)
                except Exception:
                    pass
        finally:
            with _WARM_START_LOCK:
                _WARM_THREAD = None

    global _WARM_THREAD
    with _WARM_START_LOCK:
        if _WARM_THREAD is not None and _WARM_THREAD.is_alive():
            return
        _WARM_THREAD = threading.Thread(
            target=_worker,
            daemon=True,
            name="dashboard-sector-cache-warm",
        )
        _WARM_THREAD.start()


def main() -> None:
    st.set_page_config(page_title="FMP Sector Dashboard", layout="wide")
    st.title("FMP Sector Dashboard")
    ttl_h = _CACHE_TTL_SECONDS / 3600.0
    col_cap, col_btn = st.columns([5, 1])
    with col_cap:
        st.caption(
            f"Sector ETF dashboards using FMP data. Cached up to ~{ttl_h:g}h (`@st.cache_data` TTL). "
            "Pick a sector to view trend, relative strength, industry rotation, risk, and breadth."
        )
    with col_btn:
        if st.button("Force reload", type="secondary", key="force_reload_cache", help="Clear dashboard cache and refetch from FMP on next load."):
            st.cache_data.clear()
            st.rerun()

    load_dotenv(ROOT / ".env")
    api_key = (os.getenv("FMP_API_KEY") or "").strip()

    sector_options = tuple(s.page_radio_label for s in SECTOR_SPECS)
    st.markdown("##### Sectors")
    page = st.radio(
        "Active sector",
        options=sector_options,
        horizontal=True,
        key="fmp_dashboard_sector_tab",
        label_visibility="collapsed",
        help=(
            "Selected sector loads first; other sectors prefetch only after "
            f"~{_WARM_DELAY_S:.0f}s so FMP/cache work favors the tab you chose (config: DASHBOARD_BACKGROUND_WARM_DELAY_SECONDS)."
        ),
    )

    for spec in SECTOR_SPECS:
        if page == spec.page_radio_label:
            sector_dashboard_ui.render_sector_tab(spec, cached_sector_dispersion=_cached_sector_dispersion)
            break

    if api_key:
        skip_prefetch = bool(st.session_state.pop("_dashboard_skip_background_warm_once", False))
        if not skip_prefetch:
            if _WARM_DELAY_S > 0:
                threading.Timer(
                    _WARM_DELAY_S,
                    lambda k=api_key, p=page: _spawn_background_sector_warm(k, p),
                ).start()
            else:
                _spawn_background_sector_warm(api_key, page)


if __name__ == "__main__":
    main()
