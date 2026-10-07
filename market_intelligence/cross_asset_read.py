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
    positive_price_days,
)
from market_intelligence.commodity_analytics import close_points
from market_intelligence.cross_asset_universe import (
    COMMODITY_INSTRUMENTS,
    CRYPTO_INSTRUMENTS,
    CURRENCY_VS_USD,
    EIA_FUNDAMENTALS,
    FRED_COMMODITY_SERIES,
    FX_INSTRUMENTS,
    FX_WINDOWS,
    INSTRUMENT_BY_ID,
    MOVE_INSTRUMENT,
    MOVE_INSTRUMENT_ID,
    MOVE_SOURCE_NOTE,
    USDCNH_SOURCE_NOTE,
)
from market_intelligence.fx_analytics import latest_window_returns, levels_by_id
from market_intelligence.markets_analytics import as_day
from market_intelligence.overview_metrics import observation_returns


def _rows(conn, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(text(sql), params or {}).mappings().all()]


def _has_relation(conn, name: str) -> bool:
    return bool(conn.execute(text("SELECT to_regclass(:name)"), {"name": name}).scalar())


def _has_column(conn, relation: str, column: str) -> bool:
    return bool(
        conn.execute(
            text("SELECT 1 FROM information_schema.columns WHERE table_name = :relation AND column_name = :column LIMIT 1"),
            {"relation": relation, "column": column},
        ).scalar()
    )


def _points(pairs: Sequence[tuple[date, float]]) -> list[dict[str, Any]]:
    return [{"as_of": day, "value": value} for day, value in pairs]


def _bounds(groups: Sequence[Sequence[Mapping[str, Any]]]) -> tuple[date | None, date | None]:
    days = [as_day(row.get("as_of")) for rows in groups for row in rows if row.get("value") is not None]
    days = [day for day in days if day is not None]
    if not days:
        return None, None
    return min(days), max(days)


def _yahoo_bars(conn, instrument_ids: Sequence[str] | None = None, *, since: date | None = None) -> list[dict[str, Any]]:
    """Stored Yahoo cross-asset closes. ``since`` bounds the read for window statistics."""
    if not _has_relation(conn, "mi_v_yahoo_cross_asset_history"):
        return []
    # Migration 046 appends retrieved_at. Older databases still serve the history.
    stamp = ", retrieved_at" if _has_column(conn, "mi_v_yahoo_cross_asset_history", "retrieved_at") else ", NULL AS retrieved_at"
    sql = """
        SELECT instrument_id, source_id, bar_date, close_price AS close, adj_close_price,
               provider_symbol, provider{stamp}
        FROM mi_v_yahoo_cross_asset_history
    """.format(stamp=stamp)
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if instrument_ids is not None:
        clauses.append("instrument_id = ANY(:instrument_ids)")
        params["instrument_ids"] = list(instrument_ids)
    if since is not None:
        clauses.append("bar_date >= :since")
        params["since"] = since
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY instrument_id, bar_date"
    return _rows(conn, sql, params)


def yahoo_cross_asset_bars(conn, instrument_ids: Sequence[str] | None = None, *, since: date | None = None) -> list[dict[str, Any]]:
    """Public read used by the Market Overview snapshot. Same view and columns as the full pages."""
    return _yahoo_bars(conn, instrument_ids, since=since)


def move_index_context(conn) -> dict[str, Any]:
    """ICE BofA MOVE (Yahoo ^MOVE) level, observation returns, and stored history for Rates & Curve."""
    bars = _yahoo_bars(conn, [MOVE_INSTRUMENT_ID])
    points = close_points(bars)
    latest_row = bars[-1] if bars else None
    returns = observation_returns(points, FX_WINDOWS)
    return {
        "status": "OK" if points else "UNAVAILABLE",
        "instrument_id": MOVE_INSTRUMENT_ID,
        "label": "MOVE",
        "yahoo_symbol": MOVE_INSTRUMENT.yahoo_symbol,
        "units": "index points",
        "value": points[-1][1] if points else None,
        "as_of": points[-1][0] if points else None,
        "retrieved_at": None if latest_row is None else latest_row.get("retrieved_at"),
        "returns": returns,
        "history": _points(points),
        "source_note": MOVE_SOURCE_NOTE,
        "windows": [label for label, _lag in FX_WINDOWS],
    }


def _monitor_prices(conn) -> dict[str, dict[date, float]]:
    if not _has_relation(conn, "mi_v_market_monitor_closes"):
        return {}
    rows = _rows(
        conn,
        """
        SELECT symbol AS instrument_id, bar_date, COALESCE(adj_close_price, close_price) AS close
        FROM mi_v_market_monitor_closes
        WHERE symbol IN ('SPY', 'QQQ', 'IWM')
        ORDER BY symbol, bar_date
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
    if not _has_relation(conn, "mi_v_macro_observations_current"):
        return {}
    ids = [series_id for series_id, _label in FRED_COMMODITY_SERIES]
    rows = _rows(
        conn,
        """
        SELECT series_id, observation_date, value
        FROM mi_v_macro_observations_current
        WHERE series_id = ANY(:ids) AND value IS NOT NULL
        ORDER BY series_id, observation_date
        """,
        {"ids": ids},
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["series_id"]), []).append({"as_of": row["observation_date"], "value": float(row["value"])})
    return grouped


def _eia_history(conn) -> dict[str, dict[str, Any]]:
    if not _has_relation(conn, "mi_v_eia_history"):
        return {}
    rows = _rows(
        conn,
        """
        SELECT series_id, observation_date, value, units
        FROM mi_v_eia_history
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


FX_CARD_WINDOWS: tuple[tuple[str, int], ...] = tuple(item for item in FX_WINDOWS if item[0] in {"1D", "1W", "1M"})
FX_CARD_STALE_AFTER_DAYS = 5


def _card(label: str, points: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    last = next((row for row in reversed(list(points)) if row.get("value") is not None), None)
    return {"label": label, "value": None if last is None else last.get("value"), "as_of": None if last is None else last.get("as_of")}


def fx_header_card(
    instrument_id: str,
    label: str,
    points: Sequence[tuple[date, float]],
    *,
    latest: date | None,
    stale_after_days: int = FX_CARD_STALE_AFTER_DAYS,
) -> dict[str, Any]:
    """Header card in the displayed quote convention (positive = the shown pair rose).

    Returns reuse ``latest_window_returns`` on the raw pair levels, so they are
    the heatmap's definitions (provider observations, gap guard) without the
    foreign-vs-USD inversion. A card whose newest observation trails the newest
    FX observation by more than ``stale_after_days`` is flagged stale.
    """
    series = [(day, value) for day, value in points if value is not None]
    last = series[-1] if series else None
    as_of = last[0] if last is not None else None
    returns = latest_window_returns(series, FX_CARD_WINDOWS) if series else {name: None for name, _lag in FX_CARD_WINDOWS}
    stale = bool(as_of is not None and latest is not None and (latest - as_of).days > stale_after_days)
    return {
        "instrument_id": instrument_id,
        "label": label,
        "value": None if last is None else last[1],
        "as_of": as_of,
        "returns": {name: returns.get(name) for name, _lag in FX_CARD_WINDOWS},
        "stale": stale,
    }


def forex_header_cards(
    levels: Mapping[str, Sequence[tuple[date, float]]],
    *,
    latest: date | None,
) -> list[dict[str, Any]]:
    """One card per ``FX_INSTRUMENTS`` entry in config order. DXY appears exactly once."""
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in FX_INSTRUMENTS:
        if spec.instrument_id in seen:
            continue
        seen.add(spec.instrument_id)
        label = "DXY" if spec.instrument_id == "DXY" else spec.display_name
        cards.append(fx_header_card(spec.instrument_id, label, levels.get(spec.instrument_id) or [], latest=latest))
    return cards


def forex_context(conn) -> dict[str, Any]:
    bars = _yahoo_bars(conn)
    fx_ids = {row.instrument_id for row in FX_INSTRUMENTS}
    fx_bars = [row for row in bars if row["instrument_id"] in fx_ids]
    raw_levels = {row.instrument_id: levels_by_id(fx_bars, instrument_id=row.instrument_id, orient=False) for row in FX_INSTRUMENTS}
    pairs = {instrument_id: _points(levels) for instrument_id, levels in raw_levels.items() if instrument_id != "DXY"}
    versus = {instrument_id: _points(levels_by_id(fx_bars, instrument_id=instrument_id, orient=True)) for instrument_id, _label in CURRENCY_VS_USD}
    dxy = _points(raw_levels.get("DXY") or [])
    earliest, latest = _bounds([dxy, *pairs.values(), *versus.values()])
    return {
        "status": "OK" if dxy or any(pairs.values()) else "EMPTY",
        "dxy": dxy,
        "pairs": pairs,
        "versus_usd": versus,
        "cards": forex_header_cards(raw_levels, latest=latest),
        "earliest": earliest,
        "latest": latest,
        "usdcnh_note": USDCNH_SOURCE_NOTE,
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
    if not _has_relation(conn, "mi_v_cftc_position_history"):
        return {"status": "UNAVAILABLE", "markets": [], "as_of": None, "published": None}
    stored = _rows(
        conn,
        """
        SELECT market_key, trader_category, position_date, scheduled_publication_date,
               long_contracts, short_contracts, open_interest, asset_group, report_family
        FROM mi_v_cftc_position_history
        ORDER BY market_key, trader_category, position_date
        """,
    )
    proxy_ids = [contract.price_proxy for contract in CONTRACTS if contract.price_proxy and contract.price_proxy not in {"SPY", "QQQ", "IWM"}]
    bars = _yahoo_bars(conn, proxy_ids)
    prices = _price_book(conn, bars)
    ordered_days = {instrument_id: positive_price_days(series) for instrument_id, series in prices.items()}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in stored:
        position_day = as_day(row.get("position_date"))
        if position_day is None:
            continue
        grouped.setdefault((row["market_key"], row["trader_category"]), []).append({**row, "position_date": position_day})
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
            price_days = ordered_days.get(contract.price_proxy or "", [])
            series: list[dict[str, Any]] = []
            for item in history:
                aligned = align_price_to_position(item["position_date"], price_map, ordered_days=price_days) if contract.price_proxy else None
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
