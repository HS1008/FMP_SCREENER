"""Assemble live 1D returns / RS from stored quotes + EOD prior closes.

Read-only consumers (Streamlit) call :func:`equity_live_context`. Writers ingest IBKR
and optional Yahoo live quotes separately; this module never opens TWS or yfinance.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import text

from market_intelligence.live_session import (
    DEFAULT_QUOTE_MAX_AGE_SECONDS,
    PriceObservation,
    equal_dollar_live_return,
    format_live_quotes_as_of,
    latest_completed_nyse_session,
    live_relative_strength,
    live_return,
    normalize_to_100,
    resolve_current_price,
    resolve_prior_close,
    sessions_aligned,
)
from market_intelligence.sector_mapping import CANONICAL_SECTORS
from market_intelligence.source_resolve import prefer_rows_by_group, TIE_PREFERENCE
from market_intelligence.taxonomy import ALL_BASKETS, BENCHMARK_SPY, SECTOR_PROXIES, UNIVERSE_SYMBOLS


def _view_exists(conn, name: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM information_schema.views WHERE table_name = :n"),
        {"n": name},
    ).first()
    return row is not None


def _rows(conn, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(text(sql), dict(params or {})).mappings().all()]


def load_live_quote_candidates(conn) -> list[dict[str, Any]]:
    if not _view_exists(conn, "mi_v_live_quotes_by_source"):
        # Fallback: IBKR-only curated view with display_name as symbol.
        if not _view_exists(conn, "mi_v_ibkr_quotes_latest"):
            return []
        rows = _rows(conn, "SELECT * FROM mi_v_ibkr_quotes_latest")
        out = []
        for row in rows:
            item = dict(row)
            item["symbol"] = str(row.get("display_name") or row.get("instrument_id") or "").upper()
            out.append(item)
        return out
    return _rows(conn, "SELECT * FROM mi_v_live_quotes_by_source ORDER BY symbol, source_id")


def load_prior_close_bars(conn, *, session: date, symbols: Sequence[str]) -> list[dict[str, Any]]:
    if not symbols:
        return []
    if _view_exists(conn, "mi_v_equity_daily_closes"):
        return _rows(
            conn,
            """
            SELECT symbol, bar_date, adj_close_price, close_price, provider, source_id, adjustment_basis
            FROM mi_v_equity_daily_closes
            WHERE bar_date = :session AND symbol = ANY(:symbols)
            """,
            {"session": session, "symbols": list(symbols)},
        )
    # Writer-path / tests may have raw bars without the curated view.
    return _rows(
        conn,
        """
        SELECT instrument_id AS symbol, bar_date, adj_close_price, close_price, provider, source_id, adjustment_basis
        FROM mi_market_bars
        WHERE bar_interval = '1D' AND source_id = 'EQUITY_EOD'
          AND bar_date = :session AND instrument_id = ANY(:symbols)
        """,
        {"session": session, "symbols": list(symbols)},
    )


def load_adjusted_history(
    conn,
    *,
    symbols: Sequence[str],
    start: date,
    end: date | None = None,
) -> list[dict[str, Any]]:
    if not symbols:
        return []
    end = end or date.today()
    if _view_exists(conn, "mi_v_equity_daily_closes"):
        return _rows(
            conn,
            """
            SELECT symbol, bar_date, adj_close_price, close_price, provider, source_id
            FROM mi_v_equity_daily_closes
            WHERE symbol = ANY(:symbols) AND bar_date >= :start AND bar_date <= :end
            ORDER BY symbol, bar_date
            """,
            {"symbols": list(symbols), "start": start, "end": end},
        )
    return _rows(
        conn,
        """
        SELECT instrument_id AS symbol, bar_date, adj_close_price, close_price, provider, source_id
        FROM mi_market_bars
        WHERE bar_interval = '1D' AND source_id = 'EQUITY_EOD'
          AND instrument_id = ANY(:symbols) AND bar_date >= :start AND bar_date <= :end
        ORDER BY instrument_id, bar_date
        """,
        {"symbols": list(symbols), "start": start, "end": end},
    )


def resolve_symbol_live(
    *,
    symbol: str,
    quote_candidates: Sequence[Mapping[str, Any]],
    prior_bars: Sequence[Mapping[str, Any]],
    prior_session: date,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_QUOTE_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    current = resolve_current_price(
        quote_candidates,
        symbol=symbol,
        expected_session=prior_session,
        now=now,
        max_age_seconds=max_age_seconds,
    )
    prior = resolve_prior_close(prior_bars, symbol=symbol, session=prior_session)
    ret = None
    if current is not None and prior is not None:
        ret = live_return(current.price, prior.price)
    return {
        "symbol": symbol.upper(),
        "live_return": ret,
        "current": current.as_dict() if current else None,
        "prior_close": prior.as_dict() if prior else None,
        "prior_session": prior_session.isoformat(),
        "available": ret is not None,
    }


def live_rs_pair(
    asset: Mapping[str, Any] | None,
    benchmark: Mapping[str, Any] | None,
) -> float | None:
    if not asset or not benchmark:
        return None
    asset_prior = (asset.get("prior_close") or {}).get("session_date")
    bench_prior = (benchmark.get("prior_close") or {}).get("session_date")
    if not sessions_aligned(
        date.fromisoformat(asset_prior) if asset_prior else None,
        date.fromisoformat(bench_prior) if bench_prior else None,
    ):
        return None
    return live_relative_strength(asset.get("live_return"), benchmark.get("live_return"))


def preferred_canonical_sector_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Canonical SECTOR rows only; newest as_of + EQUITY_EOD over FMP_LEGACY on ties."""
    prepared: list[dict[str, Any]] = []
    for row in rows:
        kind = str(row.get("entity_kind") or "SECTOR").upper()
        sector = row.get("canonical_sector") or row.get("sector_key")
        if kind not in {"SECTOR", ""}:
            continue
        if sector not in CANONICAL_SECTORS:
            continue
        etf = str(row.get("instrument_id") or "")
        expected = SECTOR_PROXIES.get(str(sector))
        # Top-level table: only known sector ETF proxies (drop theme/legacy oddballs).
        if expected and etf and etf != expected:
            continue
        if not expected and etf and etf not in set(SECTOR_PROXIES.values()):
            continue
        item = dict(row)
        item["canonical_sector"] = sector
        prepared.append(item)
    chosen = prefer_rows_by_group(
        prepared,
        group_key="canonical_sector",
        date_keys=("as_of", "observation_date"),
        source_key="source_id",
        preference=TIE_PREFERENCE,
    )
    by_sector: dict[str, dict[str, Any]] = {}
    for row in chosen:
        sector = str(row.get("canonical_sector") or "")
        if sector not in by_sector:
            by_sector[sector] = dict(row)
    order = {name: i for i, name in enumerate(CANONICAL_SECTORS)}
    return sorted(by_sector.values(), key=lambda r: order.get(str(r.get("canonical_sector")), 99))


def equity_live_context(conn, *, now: datetime | None = None) -> dict[str, Any]:
    now_aware = now or datetime.now(timezone.utc)
    if now_aware.tzinfo is None:
        now_aware = now_aware.replace(tzinfo=timezone.utc)
    prior_session = latest_completed_nyse_session(now_aware)
    symbols = sorted(set(UNIVERSE_SYMBOLS) | {BENCHMARK_SPY, "RSP"} | set(SECTOR_PROXIES.values()))
    quotes = load_live_quote_candidates(conn)
    bars = load_prior_close_bars(conn, session=prior_session, symbols=symbols)
    by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        by_symbol[symbol] = resolve_symbol_live(
            symbol=symbol,
            quote_candidates=quotes,
            prior_bars=bars,
            prior_session=prior_session,
            now=now_aware,
        )
    spy = by_symbol.get(BENCHMARK_SPY)
    observation_times = [
        datetime.fromisoformat(str((row.get("current") or {}).get("observation_ts")).replace("Z", "+00:00"))
        for row in by_symbol.values()
        if (row.get("current") or {}).get("observation_ts")
    ]
    quotes_as_of = max(observation_times) if observation_times else None
    available = bool(observation_times)
    return {
        "prior_session": prior_session.isoformat(),
        "quotes_as_of": quotes_as_of.isoformat() if quotes_as_of else None,
        "quotes_as_of_label": format_live_quotes_as_of(quotes_as_of, available=available),
        "quotes_available": available,
        "by_symbol": by_symbol,
        "spy": spy,
        "note": (
            "Live 1D uses current last / prior completed-session close. "
            "Longer windows remain finalized EOD. Yahoo live is an unofficial fallback "
            "(MI_YAHOO_LIVE_FALLBACK); never labeled as IBKR. INTERNAL_ONLY; no redistribution claim."
        ),
    }


def attach_live_1d_to_sector_rows(
    rows: Sequence[Mapping[str, Any]],
    live: Mapping[str, Any],
    *,
    benchmark_symbol: str = BENCHMARK_SPY,
) -> list[dict[str, Any]]:
    by_symbol = live.get("by_symbol") or {}
    bench = by_symbol.get(benchmark_symbol)
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        metrics = dict(item.get("metrics") or {})
        etf = str(item.get("instrument_id") or "")
        asset = by_symbol.get(etf)
        live_ret = (asset or {}).get("live_return")
        live_rs = live_rs_pair(asset, bench)
        metrics["live_ret_1d"] = live_ret
        metrics["live_rs_chg_1d"] = live_rs
        item["metrics"] = metrics
        item["live_provenance"] = {
            "asset": (asset or {}).get("current"),
            "benchmark": (bench or {}).get("current"),
            "prior_session": live.get("prior_session"),
        }
        out.append(item)
    return out


def attach_live_1d_to_subgroup_rows(
    rows: Sequence[Mapping[str, Any]],
    live: Mapping[str, Any],
    *,
    parent_sector: str,
) -> list[dict[str, Any]]:
    by_symbol = live.get("by_symbol") or {}
    parent_etf = SECTOR_PROXIES.get(parent_sector)
    parent_live = by_symbol.get(parent_etf) if parent_etf else None
    # Basket definitions by label for equal-dollar membership.
    baskets_by_label = {b.label: b for b in ALL_BASKETS}
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        metrics = dict(item.get("metrics") or {})
        coverage = dict(item.get("coverage") or {})
        membership = list(coverage.get("membership") or [])
        label = str(item.get("industry_key") or "")
        basket = baskets_by_label.get(label)
        if basket is not None:
            membership = list(basket.members)
        live_ret = None
        used: tuple[str, ...] = ()
        missing: tuple[str, ...] = ()
        if len(membership) == 1:
            asset = by_symbol.get(membership[0])
            live_ret = (asset or {}).get("live_return")
            used = (membership[0],) if live_ret is not None else ()
            missing = () if live_ret is not None else (membership[0],)
        elif membership:
            member_returns = {m: (by_symbol.get(m) or {}).get("live_return") for m in membership}
            live_ret, used, missing = equal_dollar_live_return(member_returns, membership)
        else:
            # Single-instrument industry rows (legacy) use instrument_id when it looks like a ticker.
            inst = str(item.get("instrument_id") or "")
            if inst and inst.isupper() and len(inst) <= 6 and "_" not in inst:
                asset = by_symbol.get(inst)
                live_ret = (asset or {}).get("live_return")
        live_rs = live_relative_strength(live_ret, (parent_live or {}).get("live_return")) if parent_live else None
        metrics["live_ret_1d"] = live_ret
        metrics["live_rs_chg_1d"] = live_rs
        item["metrics"] = metrics
        coverage["live_members_used"] = list(used)
        coverage["live_members_missing"] = list(missing)
        item["coverage"] = coverage
        out.append(item)
    return out


def spy_rsp_chart_context(conn, *, lookback_days: int = 190, now: datetime | None = None) -> dict[str, Any]:
    """Normalized SPY vs RSP (first visible point = 100) from stored daily closes."""
    end = (now or datetime.now(timezone.utc)).date()
    start = end - timedelta(days=lookback_days)
    rows = load_adjusted_history(conn, symbols=["SPY", "RSP"], start=start, end=end)
    by_sym: dict[str, list[tuple[date, float]]] = {"SPY": [], "RSP": []}
    providers: dict[str, set[str]] = {"SPY": set(), "RSP": set()}
    for row in rows:
        sym = str(row.get("symbol") or "").upper()
        if sym not in by_sym:
            continue
        day = row.get("bar_date")
        if isinstance(day, datetime):
            day = day.date()
        elif not isinstance(day, date):
            try:
                day = date.fromisoformat(str(day)[:10])
            except ValueError:
                continue
        price = row.get("adj_close_price")
        if price is None:
            price = row.get("close_price")
        try:
            value = float(price)
        except (TypeError, ValueError):
            continue
        by_sym[sym].append((day, value))
        if row.get("provider"):
            providers[sym].add(str(row["provider"]))
    spy_n = normalize_to_100(by_sym["SPY"])
    rsp_n = normalize_to_100(by_sym["RSP"])
    # Align to common dates for display cleanliness.
    spy_map = {d: v for d, v in spy_n}
    rsp_map = {d: v for d, v in rsp_n}
    common = sorted(set(spy_map) & set(rsp_map))
    series = [{"date": d.isoformat(), "SPY": spy_map[d], "RSP": rsp_map[d]} for d in common]
    as_of = common[-1].isoformat() if common else None
    return {
        "series": series,
        "as_of": as_of,
        "start": common[0].isoformat() if common else None,
        "providers": {k: sorted(v) for k, v in providers.items()},
        "available": bool(series),
        "note": "Normalized to 100 at the first common observation in the window. Cap-weight (SPY) vs equal-weight (RSP) S&P 500 proxies.",
    }


def subgroup_rows_for_parent(
    industries: Mapping[str, Any], parent: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect industry / theme / ETF-comparison rows for a sector without a dataset selector."""
    datasets = industries.get("datasets") or {}
    collected: list[dict[str, Any]] = []
    for dataset_name in ("INDUSTRY_RS_VS_SECTOR_ETF", "THEME_RS"):
        by_parent = datasets.get(dataset_name) or {}
        for row in by_parent.get(parent) or []:
            item = dict(row)
            item["_dataset"] = dataset_name
            collected.append(item)
    # Prefer EQUITY_EOD over FMP_LEGACY on same industry_key + as_of ties.
    if collected:
        prepared = []
        for row in collected:
            item = dict(row)
            item["_group"] = "{0}|{1}".format(item.get("industry_key"), item.get("instrument_id"))
            prepared.append(item)
        chosen = prefer_rows_by_group(
            prepared,
            group_key="_group",
            date_keys=("as_of",),
            source_key="source_id",
            preference=TIE_PREFERENCE,
        )
        collected = [dict(r) for r in chosen]
    unavailable = list((datasets.get("SUBGROUP_UNAVAILABLE") or {}).get(parent) or [])
    return collected, unavailable


__all__ = [
    "PriceObservation",
    "attach_live_1d_to_sector_rows",
    "attach_live_1d_to_subgroup_rows",
    "equity_live_context",
    "live_rs_pair",
    "load_adjusted_history",
    "load_live_quote_candidates",
    "load_prior_close_bars",
    "preferred_canonical_sector_rows",
    "resolve_symbol_live",
    "spy_rsp_chart_context",
    "subgroup_rows_for_parent",
]
