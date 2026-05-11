"""
Batch dividend-adjusted price load for **all** sector-dashboard industry rotation proxies.

``data_loader.get_price_histories_long`` already parallelizes per symbol; passing the full union
once avoids duplicating overlapping history requests when warming or switching sectors.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable

import pandas as pd

import comm_rotation_engine
import config
import consumer_cyclical_rotation_engine
import consumer_defensive_rotation_engine
import energy_rotation_engine
import financial_services_rotation_engine
import healthcare_rotation_engine
import industrials_rotation_engine
import materials_rotation_engine
import real_estate_rotation_engine
import tech_rotation_engine
import utilities_rotation_engine


def dashboard_rotation_symbols() -> tuple[str, ...]:
    """Sorted unique symbols required by any wired sector rotation engine (includes each benchmark)."""
    syms: set[str] = set()
    mods: Iterable[Any] = (
        tech_rotation_engine,
        materials_rotation_engine,
        comm_rotation_engine,
        consumer_cyclical_rotation_engine,
        consumer_defensive_rotation_engine,
        energy_rotation_engine,
        financial_services_rotation_engine,
        healthcare_rotation_engine,
        industrials_rotation_engine,
        real_estate_rotation_engine,
        utilities_rotation_engine,
    )
    for mod in mods:
        syms.update(mod.ALL_ROTATION_SYMBOLS)
    return tuple(sorted(syms))


ROTATION_BATCH_LOOKBACK_CAL_DAYS: int = 520


def fetch_all_dashboard_rotation_prices_long(
    session: Any,
    api_key: str,
    *,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """One ``get_price_histories_long`` over the dashboard rotation universe."""
    import data_loader

    end = date.today()
    start = end - timedelta(days=ROTATION_BATCH_LOOKBACK_CAL_DAYS)
    mw = max(1, int(getattr(config, "DASHBOARD_PRICE_FETCH_MAX_WORKERS", config.PRICE_FETCH_MAX_WORKERS)))
    return data_loader.get_price_histories_long(
        session,
        api_key,
        dashboard_rotation_symbols(),
        start,
        end,
        force_refresh=force_refresh,
        max_workers=mw,
    )
