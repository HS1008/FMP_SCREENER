"""PostgreSQL read models for FOREX, positioning, commodities, and crypto.

Streamlit calls these through ``read_models``. This module does not call Yahoo,
CFTC, EIA, or FRED.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from sqlalchemy import text

from market_intelligence.cftc_positions import (
    CATEGORY_LABELS,
    CONTRACTS,
    align_price_to_position,
    enrich_category_history,
)
from market_intelligence.commodity_analytics import close_points
from market_intelligence.cross_asset_universe import (
    COMMODITY_INSTRUMENTS,
    CRYPTO_INSTRUMENTS,
    CURRENCY_VS_USD,
    EIA_FUNDAMENTALS,
    FRED_COMMODITY_SERIES,
    FX_INSTRUMENTS,
    INSTRUMENT_BY_ID,
)
from market_intelligence.fx_analytics import levels_by_id
from market_intelligence.markets_analytics import as_day


def _rows(conn, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(text(sql), params or {}).mappings().all()]


def _has_relation(conn, name: str) -> bool:
    return bool(conn.execute(text("SELECT to_regclass(:name)"), {"name": name}).scalar())


def _points(pairs: Sequence[tuple[date, float]]) -> list[dict[str, Any]]:
    return [{"as_of": day, "value": value} for day, value in pairs]


def _bounds(groups: Sequence[Sequence[Mapping[str, Any]]]) -> tuple[date | None, date | None]:
    days = [as_day(row.get("as_of")) for rows in groups for row in rows if row.get("value") is not None]
    days = [day for day in days if day is not None]
    if not days:
        return None, None
    return min(days), max(days)


def _yahoo_bars(conn) -> list[dict[str, Any]]:
    if not _has_relation(conn, "mi_market_bars"):
        return []
    return _rows(
        conn,
        """
        SELECT instrument_id, source_id, bar_date, close_price AS close, adj_close_price,
               provider_symbol, provider
        FROM mi_market_bars
        WHERE source_id IN ('YAHOO_FX', 'YAHOO_FUTURES_PROXY', 'YAHOO_CRYPTO')
          AND bar_interval = '1D'
          AND provider = 'YAHOO'
        ORDER BY instrument_id, bar_date
        """,
    )


def _monitor_prices(conn) -> dict[str, dict[date, float]]:
    if not _has_relation(conn, "mi_market_bars"):
        return {}
    rows = _rows(
        conn,
        """
        SELECT instrument_id, bar_date, COALESCE(adj_close_price, close_price) AS close
        FROM mi_market_bars
        WHERE source_id = 'MARKET_MONITOR_EOD'
          AND provider = 'YAHOO'
          AND bar_interval = '1D'
          AND instrument_id IN ('SPY', 'QQQ', 'IWM')
        ORDER BY instrument_id, bar_date
        """,
    )
    grouped: dict[str, dict[date, float]] = {}
    for row in rows:
        day = as_day(row.get("bar_date"))
        close = row.get("close")
        if day is None or close is None:
            continue
        grouped.setdefault(str(row["instrument_id"]), {})[day] = float(close)
    return grouped


def _fred_history(conn) -> dict[str, list[dict[str, Any]]]:
    if not _has_relation(conn, "mi_macro_observations"):
        return {}
    ids = [series_id for series_id, _label in FRED_COMMODITY_SERIES]
    rows = _rows(
        conn,
        """
        SELECT series_id, observation_date, value
        FROM mi_macro_observations
        WHERE series_id = ANY(:ids) AND is_current IS TRUE AND value IS NOT NULL
        ORDER BY series_id, observation_date
        """,
        {"ids": ids},
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["series_id"]), []).append({"as_of": row["observation_date"], "value": float(row["value"])})
    return grouped


def _eia_history(conn) -> dict[str, dict[str, Any]]:
    if not _has_relation(conn, "mi_eia_observations"):
        return {}
    rows = _rows(
        conn,
        """
        SELECT series_id, observation_date, value, units
        FROM mi_eia_observations
        WHERE source_id = 'EIA_ENERGY' AND value IS NOT NULL
        ORDER BY series_id, observation_date
        """,
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = grouped.setdefault(str(row["series_id"]), {"units": row.get("units"), "points": []})
        if row.get("units"):
            bucket["units"] = row.get("units")
        bucket["points"].append({"as_of": row["observation_date"], "value": float(row["value"])})
    return grouped


def _card(label: str, points: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    last = next((row for row in reversed(list(points)) if row.get("value") is not None), None)
    return {"label": label, "value": None if last is None else last.get("value"), "as_of": None if last is None else last.get("as_of")}


def forex_context(conn) -> dict[str, Any]:
    bars = _yahoo_bars(conn)
    fx_ids = {row.instrument_id for row in FX_INSTRUMENTS}
    fx_bars = [row for row in bars if row["instrument_id"] in fx_ids]
    pairs = {row.instrument_id: _points(levels_by_id(fx_bars, instrument_id=row.instrument_id, orient=False)) for row in FX_INSTRUMENTS if row.instrument_id != "DXY"}
    versus = {instrument_id: _points(levels_by_id(fx_bars, instrument_id=instrument_id, orient=True)) for instrument_id, _label in CURRENCY_VS_USD}
    dxy = _points(levels_by_id(fx_bars, instrument_id="DXY", orient=False))
    earliest, latest = _bounds([dxy, *pairs.values(), *versus.values()])
    return {
        "status": "OK" if dxy or any(pairs.values()) else "EMPTY",
        "dxy": dxy,
        "pairs": pairs,
        "versus_usd": versus,
        "cards": [
            _card("DXY", dxy),
            _card("EUR/USD", pairs.get("EURUSD") or []),
            _card("USD/JPY", pairs.get("USDJPY") or []),
        ],
        "earliest": earliest,
        "latest": latest,
    }


def _price_book(conn, bars: Sequence[Mapping[str, Any]]) -> dict[str, dict[date, float]]:
    book = _monitor_prices(conn)
    for instrument_id in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "CL", "NG", "GC", "SI", "HG", "VIX"):
        spec = INSTRUMENT_BY_ID.get(instrument_id)
        if spec is None:
            continue
        orient = spec.asset_class == "FX"
        points = levels_by_id(bars, instrument_id=instrument_id, orient=orient)
        if points:
            book[instrument_id] = {day: value for day, value in points}
    return book


def positioning_context(conn) -> dict[str, Any]:
    if not _has_relation(conn, "mi_cftc_position_observations"):
        return {"status": "UNAVAILABLE", "markets": [], "as_of": None, "published": None}
    stored = _rows(
        conn,
        """
        SELECT market_key, trader_category, position_date, scheduled_publication_date,
               long_contracts, short_contracts, open_interest, asset_group, report_family
        FROM mi_cftc_position_observations
        ORDER BY market_key, trader_category, position_date
        """,
    )
    bars = _yahoo_bars(conn)
    prices = _price_book(conn, bars)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in stored:
        grouped.setdefault((row["market_key"], row["trader_category"]), []).append(row)
    markets: list[dict[str, Any]] = []
    latest_position = None
    latest_published = None
    for contract in CONTRACTS:
        categories: dict[str, list[dict[str, Any]]] = {}
        for (market_key, category), rows in grouped.items():
            if market_key != contract.market_key:
                continue
            history = enrich_category_history(rows)
            price_map = prices.get(contract.price_proxy or "", {})
            series: list[dict[str, Any]] = []
            for item in history:
                aligned = align_price_to_position(item["position_date"], price_map) if contract.price_proxy else None
                series.append({**item, "price": None if aligned is None else aligned[1], "price_date": None if aligned is None else aligned[0]})
                if latest_position is None or item["position_date"] > latest_position:
                    latest_position = item["position_date"]
                    latest_published = item.get("scheduled_publication_date")
            categories[category] = series
        markets.append(
            {
                "market_key": contract.market_key,
                "label": contract.label,
                "asset_group": contract.asset_group,
                "report_family": contract.report_family,
                "contract_code": contract.contract_code,
                "price_proxy": contract.price_proxy,
                "price_note": contract.price_note,
                "categories": categories,
                "category_labels": {key: CATEGORY_LABELS[key] for key in categories},
            }
        )
    return {
        "status": "OK" if stored else "EMPTY",
        "markets": markets,
        "as_of": latest_position,
        "published": latest_published,
    }


def commodities_context(conn) -> dict[str, Any]:
    bars = _yahoo_bars(conn)
    histories = {}
    for instrument in COMMODITY_INSTRUMENTS:
        if instrument.instrument_id == "VIX":
            continue
        histories[instrument.instrument_id] = _points(close_points([row for row in bars if row["instrument_id"] == instrument.instrument_id]))
    earliest, latest = _bounds(list(histories.values()))
    return {
        "status": "OK" if any(histories.values()) else "EMPTY",
        "prices": histories,
        "fred": _fred_history(conn),
        "eia": _eia_history(conn),
        "cards": [
            _card("WTI", histories.get("CL") or []),
            _card("Gold", histories.get("GC") or []),
            _card("Copper", histories.get("HG") or []),
            _card("Natural Gas", histories.get("NG") or []),
        ],
        "earliest": earliest,
        "latest": latest,
        "labels": {row.instrument_id: row.display_name for row in COMMODITY_INSTRUMENTS if row.instrument_id != "VIX"},
        "fred_labels": {series_id: label for series_id, label in FRED_COMMODITY_SERIES},
        "eia_labels": {row["alias"]: row["label"] for row in EIA_FUNDAMENTALS},
    }


def crypto_context(conn) -> dict[str, Any]:
    bars = _yahoo_bars(conn)
    histories = {
        instrument.instrument_id: _points(close_points([row for row in bars if row["instrument_id"] == instrument.instrument_id]))
        for instrument in CRYPTO_INSTRUMENTS
    }
    earliest, latest = _bounds(list(histories.values()))
    return {
        "status": "OK" if any(histories.values()) else "EMPTY",
        "prices": histories,
        "cards": [_card("BTC", histories.get("BTC") or []), _card("ETH", histories.get("ETH") or [])],
        "earliest": earliest,
        "latest": latest,
    }
