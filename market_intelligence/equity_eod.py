"""Independent daily equity/ETF bars and derived 1D return / RS snapshots.

Never calls FMP. Yahoo is an optional configured adapter whose production
export entitlement is not assumed. Missing access is an explicit UNAVAILABLE
state. QuantConnect research access and IBKR Pro are not treated as production
export entitlements here. IBKR connection settings are not changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Mapping, Protocol

from sqlalchemy import bindparam, text

from market_intelligence.baskets import (
    BASKET_METHOD_VERSION,
    aligned_session_return,
    daily_rebalanced_equal_weight,
    ratio_change_rs,
)
from market_intelligence.fmp_mode import equity_provider_name
from market_intelligence.nulls import strict_dumps
from market_intelligence.store import (
    RUN_FAILED,
    RUN_SKIPPED,
    RUN_SUCCEEDED,
    TRANSPORT_FAILED,
    TRANSPORT_OK,
    TRANSPORT_SKIPPED,
    finish_run,
    record_freshness,
    start_run,
    upsert_source_registry,
    utcnow,
)
from market_intelligence.calendars import CAL_NYSE, previous_session
from market_intelligence.taxonomy import (
    ALL_BASKETS,
    BENCHMARK_SPY,
    KIND_CUSTOM_BASKET,
    KIND_ETF_COMPARISON,
    SECTOR_PROXIES,
    TAXONOMY_VERSION,
    UNIVERSE_SYMBOLS,
    baskets_for_sector,
)

EQUITY_SOURCE_ID = "EQUITY_EOD"
METHODOLOGY_VERSION = "equity_metrics_v1"
SCHEMA_VERSION = "sector_snapshot_v2"
RETURN_WINDOWS = {"ret_1d": 1, "ret_1w": 5, "ret_1m": 21, "ret_3m": 63, "ret_6m": 126, "ret_12m": 252}
RS_WINDOWS = {"rs_chg_1d": 1, "rs_chg_1w": 5, "rs_chg_1m": 21, "rs_chg_3m": 63, "rs_chg_6m": 126, "rs_chg_12m": 252}


@dataclass(frozen=True)
class EquityBar:
    instrument_id: str
    bar_date: date
    adj_close: float
    close: float | None = None
    open_price: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    adjustment_basis: str = "SPLIT_ADJUSTED_UNKNOWN_DIVIDEND"
    provider_symbol: str = ""
    source_id: str = EQUITY_SOURCE_ID
    con_id: int | None = None
    provider: str | None = None


class EquityDailyAdapter(Protocol):
    source_id: str
    access_status: str
    reason: str

    def fetch(self, symbols: list[str], start: date, end: date) -> list[EquityBar]:
        ...


@dataclass
class UnavailableAdapter:
    source_id: str = "UNAVAILABLE"
    access_status: str = "ENTITLEMENT_UNVERIFIED"
    reason: str = "No authorized production equity EOD export is configured (MI_EQUITY_PROVIDER)."

    def fetch(self, symbols: list[str], start: date, end: date) -> list[EquityBar]:
        raise AdapterUnavailable(self.reason)


class AdapterUnavailable(RuntimeError):
    pass


@dataclass
class FixtureAdapter:
    bars: list[EquityBar]
    source_id: str = "FIXTURE"
    access_status: str = "CONFIGURED"
    reason: str = "Test/fixture adapter"

    def fetch(self, symbols: list[str], start: date, end: date) -> list[EquityBar]:
        wanted = set(symbols)
        return [b for b in self.bars if b.instrument_id in wanted and start <= b.bar_date <= end]


class YahooAdapter:
    """Optional Yahoo/yfinance adapter. Entitlement for production storage is unverified."""

    source_id = "YAHOO"
    access_status = "ENTITLEMENT_UNVERIFIED"
    reason = "yfinance is optional; production redistribution entitlement is not proven."

    def fetch(self, symbols: list[str], start: date, end: date) -> list[EquityBar]:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise AdapterUnavailable("yfinance is not installed") from exc
        out: list[EquityBar] = []
        data = yf.download(
            symbols,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=True,
            progress=False,
            group_by="ticker",
            threads=False,
        )
        if data is None or data.empty:
            return out
        if len(symbols) == 1:
            frame = data
            symbol = symbols[0]
            for idx, row in frame.iterrows():
                day = idx.date() if hasattr(idx, "date") else date.fromisoformat(str(idx)[:10])
                close = float(row.get("Close")) if row.get("Close") == row.get("Close") else None
                if close is None:
                    continue
                out.append(EquityBar(symbol, day, close, close, provider_symbol=symbol, source_id=EQUITY_SOURCE_ID, provider="YAHOO"))
            return out
        for symbol in symbols:
            try:
                frame = data[symbol]
            except Exception:
                continue
            for idx, row in frame.iterrows():
                day = idx.date() if hasattr(idx, "date") else date.fromisoformat(str(idx)[:10])
                close = row.get("Close")
                if close != close:
                    continue
                out.append(EquityBar(symbol, day, float(close), float(close), provider_symbol=symbol, source_id=EQUITY_SOURCE_ID, provider="YAHOO"))
        return out


@dataclass
class CollectorStoreAdapter:
    """Canonical IBKR equity EOD on a host that must not open a TWS socket.

    Windows collector pushes bars. DigitalOcean refresh rebuilds snapshots from
    stored EQUITY_EOD rows. ``fetch`` never connects to TWS.
    """

    source_id: str = "IBKR"
    access_status: str = "CONFIGURED"
    rebuild_only: bool = True
    reason: str = (
        "IBKR is the canonical EQUITY_EOD producer. Collection runs on the Windows "
        "TWS host (python -m ibkr_collector fetch-eod). This process consumes stored "
        "bars and never opens a TWS socket."
    )

    def fetch(self, symbols: list[str], start: date, end: date) -> list[EquityBar]:
        raise AdapterUnavailable(self.reason)


def adapter_from_env(env: Mapping[str, str] | None = None, *, fixture: EquityDailyAdapter | None = None) -> EquityDailyAdapter:
    if fixture is not None:
        return fixture
    name = equity_provider_name(env)
    if name in {"yahoo", "yfinance"}:
        return YahooAdapter()
    if name == "fixture":
        return FixtureAdapter([])
    if name in {"ibkr", "ibkr_collector"}:
        return CollectorStoreAdapter()
    return UnavailableAdapter()


def _ensure_instrument(conn, symbol: str, *, sector: str | None = None, con_id: int | None = None) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mi_market_instruments (instrument_id, display_name, asset_type, security_type, currency, canonical_sector, classification_version, con_id)
            VALUES (:id, :id, 'equity', 'etf_or_stock', 'USD', :sector, :ver, :con_id)
            ON CONFLICT (instrument_id) DO UPDATE SET
                updated_at = NOW(),
                con_id = COALESCE(EXCLUDED.con_id, mi_market_instruments.con_id)
            """
        ),
        {"id": symbol, "sector": sector, "ver": TAXONOMY_VERSION, "con_id": con_id},
    )


def upsert_bars(conn, bars: list[EquityBar], *, run_id: str, retrieved_at: datetime, provider: str | None = None) -> dict[str, int]:
    inserted = unchanged = 0
    for bar in bars:
        bar_provider = bar.provider or provider
        _ensure_instrument(conn, bar.instrument_id, con_id=bar.con_id)
        existing = conn.execute(
            text(
                """
                SELECT adj_close_price, provider FROM mi_market_bars
                WHERE instrument_id=:i AND source_id=:s AND bar_interval='1D' AND bar_date=:d
                """
            ),
            {"i": bar.instrument_id, "s": bar.source_id, "d": bar.bar_date},
        ).mappings().first()
        if existing is not None:
            prior_provider = existing["provider"]
            if prior_provider and bar_provider and prior_provider != bar_provider:
                raise ValueError("refusing to mix provider {0} over {1} for {2}".format(bar_provider, prior_provider, bar.instrument_id))
            if existing["adj_close_price"] is not None and float(existing["adj_close_price"]) == float(bar.adj_close):
                conn.execute(
                    text("UPDATE mi_market_bars SET last_seen_at=:t, con_id=COALESCE(:con_id, con_id), provider=COALESCE(:p, provider) WHERE instrument_id=:i AND source_id=:s AND bar_interval='1D' AND bar_date=:d"),
                    {"t": retrieved_at, "i": bar.instrument_id, "s": bar.source_id, "d": bar.bar_date, "con_id": bar.con_id, "p": bar_provider},
                )
                unchanged += 1
                continue
        conn.execute(
            text(
                """
                INSERT INTO mi_market_bars (
                    instrument_id, source_id, bar_interval, bar_date, open_price, high_price, low_price,
                    close_price, adj_close_price, volume, currency, adjustment_basis, retrieved_at,
                    ingestion_run_id, first_seen_at, last_seen_at, provider_symbol, con_id, provider
                ) VALUES (
                    :i, :s, '1D', :d, :o, :h, :l, :c, :a, :v, 'USD', :adj, :t, :run, :t, :t, :sym, :con_id, :p
                )
                ON CONFLICT (instrument_id, source_id, bar_interval, bar_date) DO UPDATE SET
                    adj_close_price = EXCLUDED.adj_close_price,
                    close_price = EXCLUDED.close_price,
                    last_seen_at = EXCLUDED.retrieved_at,
                    ingestion_run_id = EXCLUDED.ingestion_run_id,
                    adjustment_basis = EXCLUDED.adjustment_basis,
                    con_id = COALESCE(EXCLUDED.con_id, mi_market_bars.con_id),
                    provider = COALESCE(EXCLUDED.provider, mi_market_bars.provider)
                """
            ),
            {
                "i": bar.instrument_id,
                "s": bar.source_id,
                "d": bar.bar_date,
                "o": bar.open_price,
                "h": bar.high,
                "l": bar.low,
                "c": bar.close,
                "a": bar.adj_close,
                "v": bar.volume,
                "adj": bar.adjustment_basis,
                "t": retrieved_at,
                "run": run_id,
                "sym": bar.provider_symbol or bar.instrument_id,
                "con_id": bar.con_id,
                "p": bar_provider,
            },
        )
        inserted += 1
    return {"inserted": inserted, "unchanged": unchanged}


def load_adj_closes(
    conn,
    symbols: list[str],
    *,
    source_id: str = EQUITY_SOURCE_ID,
    provider: str | None = None,
) -> dict[str, dict[date, float]]:
    """Load a single-provider EQUITY_EOD history. Never mixes IBKR/Yahoo/fixture/legacy rows."""
    if not symbols:
        return {}
    sql = """
        SELECT instrument_id, bar_date, adj_close_price, provider
        FROM mi_market_bars
        WHERE instrument_id IN :syms AND bar_interval='1D' AND adj_close_price IS NOT NULL
          AND source_id = :source
    """
    params: dict[str, Any] = {"syms": symbols, "source": source_id}
    if provider:
        sql += " AND provider = :provider"
        params["provider"] = provider
    sql += " ORDER BY instrument_id, bar_date"
    rows = conn.execute(text(sql).bindparams(bindparam("syms", expanding=True)), params).all()
    if provider is None:
        providers = {row[3] for row in rows if row[3]}
        if len(providers) > 1:
            raise ValueError("mixed providers in EQUITY_EOD history: {0}".format(sorted(providers)))
    out: dict[str, dict[date, float]] = {s: {} for s in symbols}
    for inst, day, value, _prov in rows:
        out.setdefault(inst, {})[day] = float(value)
    return out


def session_pair(series: Mapping[date, float], as_of: date) -> tuple[date | None, float | None, date | None, float | None]:
    dates = [d for d in series if d <= as_of]
    if not dates:
        return None, None, None, None
    last = max(dates)
    prior_dates = [d for d in dates if d < last]
    prev = max(prior_dates) if prior_dates else None
    # Refuse to label a multi-session gap as 1D when an expected session is missing:
    # caller supplies the expected previous session.
    return last, series.get(last), prev, series.get(prev) if prev else None


def window_return(series: Mapping[date, float], as_of: date, sessions: int) -> float | None:
    dates = [d for d in sorted(series) if d <= as_of]
    if len(dates) <= sessions:
        return None
    end = dates[-1]
    start = dates[-1 - sessions]
    # Consecutive stored sessions only; do not jump a hole and call it 1D.
    if sessions == 1 and (end - start).days > 4:
        return None
    left, right = series[start], series[end]
    if left == 0:
        return None
    return right / left - 1.0


def pct_vs_dma(series: Mapping[date, float], as_of: date, window: int) -> float | None:
    """P[t] / SMA(window ending at t) - 1. No forward fill; requires ``window`` stored sessions."""
    dates = [d for d in sorted(series) if d <= as_of]
    if len(dates) < window:
        return None
    window_dates = dates[-window:]
    values = [series[d] for d in window_dates]
    if any(v is None for v in values):
        return None
    mean = sum(values) / float(window)
    if mean == 0:
        return None
    return values[-1] / mean - 1.0


def latest_stored_bar_date(conn, symbols: list[str], *, source_id: str = EQUITY_SOURCE_ID) -> date | None:
    if not symbols:
        return None
    row = conn.execute(
        text(
            """
            SELECT MAX(bar_date) FROM mi_market_bars
            WHERE source_id=:s AND bar_interval='1D' AND instrument_id IN :syms
            """
        ).bindparams(bindparam("syms", expanding=True)),
        {"s": source_id, "syms": symbols},
    ).scalar()
    if row is None:
        return None
    if isinstance(row, date):
        return row
    return date.fromisoformat(str(row)[:10])


def compute_metrics(
    asset: Mapping[date, float],
    bench: Mapping[date, float],
    as_of: date,
    *,
    expected_prev: date | None = None,
    adjustment_basis: str = "SPLIT_ADJUSTED_UNKNOWN_DIVIDEND",
) -> tuple[dict[str, float | None], dict[str, Any]]:
    last, px_t, prev, px_p = session_pair(asset, as_of)
    b_last, bx_t, b_prev, bx_p = session_pair(bench, as_of)
    session_prev = expected_prev if expected_prev is not None else (previous_session(last, CAL_NYSE) if last is not None else None)
    aligned = (
        last is not None
        and b_last is not None
        and last == b_last
        and prev is not None
        and b_prev is not None
        and prev == session_prev
        and b_prev == session_prev
    )
    metrics: dict[str, float | None] = {key: None for key in list(RETURN_WINDOWS) + list(RS_WINDOWS) + ["pct_vs_50dma", "pct_vs_200dma"]}
    if aligned:
        metrics["ret_1d"] = aligned_session_return(px_t, px_p)
        metrics["rs_chg_1d"] = ratio_change_rs(px_t, px_p, bx_t, bx_p)
    metrics["pct_vs_50dma"] = pct_vs_dma(asset, as_of, 50)
    metrics["pct_vs_200dma"] = pct_vs_dma(asset, as_of, 200)
    for key, n in RETURN_WINDOWS.items():
        if key == "ret_1d":
            continue
        metrics[key] = window_return(asset, as_of, n)
    for key, n in RS_WINDOWS.items():
        if key == "rs_chg_1d":
            continue
        dates = [d for d in sorted(set(asset) & set(bench)) if d <= as_of]
        if len(dates) <= n:
            continue
        a0, a1 = asset[dates[-1 - n]], asset[dates[-1]]
        b0, b1 = bench[dates[-1 - n]], bench[dates[-1]]
        if None in (a0, a1, b0, b1) or b0 == 0 or b1 == 0 or a0 == 0:
            continue
        metrics[key] = (a1 / b1) / (a0 / b0) - 1.0
    coverage = {
        "as_of": last.isoformat() if last else None,
        "prev_session": prev.isoformat() if prev else None,
        "aligned_with_benchmark": aligned,
        "adjustment_basis": adjustment_basis,
        "return_kind": (
            "ibkr_adjusted_last_price_return"
            if adjustment_basis == "IBKR_ADJUSTED_LAST"
            else "price_return_not_proven_total_return"
        ),
        "incomplete": not aligned,
    }
    return metrics, coverage


def _artifact_sha(payload: Mapping[str, Any]) -> str:
    return sha256(strict_dumps(payload).encode("utf-8")).hexdigest()


def write_snapshots(
    conn,
    *,
    as_of: date,
    prices: dict[str, dict[date, float]],
    source_id: str,
    run_id: str,
    adjustment_basis: str = "SPLIT_ADJUSTED_UNKNOWN_DIVIDEND",
    provider: str | None = None,
) -> int:
    spy = prices.get(BENCHMARK_SPY) or {}
    written = 0
    for sector, etf in SECTOR_PROXIES.items():
        series = prices.get(etf) or {}
        metrics, coverage = compute_metrics(series, spy, as_of, adjustment_basis=adjustment_basis)
        coverage = dict(coverage)
        if provider:
            coverage["provider"] = provider
        body = {
            "sector_key": sector,
            "instrument_id": etf,
            "as_of": as_of.isoformat(),
            "metrics": metrics,
            "coverage": coverage,
            "methodology_version": METHODOLOGY_VERSION,
        }
        conn.execute(
            text(
                """
                INSERT INTO mi_sector_snapshots (
                    source_id, dataset, entity_kind, sector_key, canonical_sector, provider_label,
                    instrument_id, as_of, schema_version, methodology_version, benchmark, return_basis,
                    value_basis, universe_method, research_eligible, metrics_json, coverage_json,
                    provenance_json, source_refs, artifact_sha256, ingestion_run_id
                ) VALUES (
                    :source_id, 'ETF_RS_VS_SPY', 'SECTOR', :sector, :sector, :etf, :etf, :as_of,
                    :schema, :method, 'SPY', 'adj_close_price_return', 'adjusted_close',
                    'etf_proxy_current_context', FALSE, CAST(:metrics AS JSONB), CAST(:coverage AS JSONB),
                    CAST(:prov AS JSONB), CAST(:refs AS JSONB), :sha, :run
                )
                ON CONFLICT (source_id, dataset, sector_key, as_of, methodology_version) DO UPDATE SET
                    metrics_json = EXCLUDED.metrics_json,
                    coverage_json = EXCLUDED.coverage_json,
                    ingested_at = NOW()
                """
            ),
            {
                "source_id": source_id,
                "sector": sector,
                "etf": etf,
                "as_of": as_of,
                "schema": SCHEMA_VERSION,
                "method": METHODOLOGY_VERSION,
                "metrics": strict_dumps(metrics),
                "coverage": strict_dumps(coverage),
                "prov": strict_dumps({"taxonomy_version": TAXONOMY_VERSION, "source_id": source_id, "provider": provider}),
                "refs": strict_dumps({"benchmark": BENCHMARK_SPY, "selection_reason": "latest_complete_session"}),
                "sha": _artifact_sha(body),
                "run": run_id,
            },
        )
        written += 1
        xlk = prices.get("XLK") or {}
        for basket in baskets_for_sector(sector):
            member_px = {m: prices.get(m) or {} for m in basket.members}
            index = daily_rebalanced_equal_weight(member_px, end=as_of)
            idx_series = {p.as_of: p.level for p in index}
            bench = xlk if sector == "Technology" else (prices.get(SECTOR_PROXIES[sector]) or {})
            metrics, coverage = compute_metrics(idx_series, bench or spy, as_of, adjustment_basis=adjustment_basis)
            last_pt = index[-1] if index else None
            coverage = dict(coverage)
            coverage["membership"] = list(basket.members)
            coverage["members_used"] = list(last_pt.members_used) if last_pt else []
            coverage["members_missing"] = list(last_pt.members_missing) if last_pt else list(basket.members)
            coverage["weighting"] = BASKET_METHOD_VERSION
            coverage["kind"] = basket.kind
            coverage["notes"] = basket.notes
            coverage["xlk_comparison"] = sector == "Technology"
            conn.execute(
                text(
                    """
                    INSERT INTO mi_industry_snapshots (
                        source_id, dataset, parent_sector_key, industry_key, instrument_id, as_of,
                        schema_version, methodology_version, benchmark, return_basis, value_basis,
                        metrics_json, provenance_json, artifact_sha256, ingestion_run_id
                    ) VALUES (
                        :source_id, :dataset, :parent, :key, :inst, :as_of, :schema, :method,
                        :bench, 'equal_dollar_daily_rebalance', 'index_level',
                        CAST(:metrics AS JSONB), CAST(:coverage AS JSONB), :sha, :run
                    )
                    ON CONFLICT (source_id, dataset, parent_sector_key, industry_key, as_of, methodology_version)
                    DO UPDATE SET metrics_json = EXCLUDED.metrics_json, provenance_json = EXCLUDED.provenance_json
                    """
                ),
                {
                    "source_id": source_id,
                    "dataset": "THEME_RS" if basket.kind != KIND_ETF_COMPARISON else "INDUSTRY_RS_VS_SECTOR_ETF",
                    "parent": basket.parent_sector,
                    "key": basket.label,
                    "inst": basket.key[:64],
                    "as_of": as_of,
                    "schema": SCHEMA_VERSION,
                    "method": METHODOLOGY_VERSION,
                    "bench": "XLK" if sector == "Technology" else SECTOR_PROXIES[sector],
                    "metrics": strict_dumps(metrics),
                    "coverage": strict_dumps(coverage),
                    "sha": _artifact_sha({"basket": basket.key, "as_of": as_of.isoformat(), "metrics": metrics}),
                    "run": run_id,
                },
            )
            written += 1
    covered = {basket.parent_sector for basket in ALL_BASKETS}
    for sector, etf in SECTOR_PROXIES.items():
        if sector in covered:
            continue
        coverage = {
            "status": "UNAVAILABLE",
            "reason": "no_curated_subgroup",
            "note": "A meaningful subgroup is not defined. Coverage is not guessed.",
            "membership": [],
            "members_used": [],
            "members_missing": [],
        }
        metrics = {key: None for key in list(RETURN_WINDOWS) + list(RS_WINDOWS)}
        conn.execute(
            text(
                """
                INSERT INTO mi_industry_snapshots (
                    source_id, dataset, parent_sector_key, industry_key, instrument_id, as_of,
                    schema_version, methodology_version, benchmark, return_basis, value_basis,
                    metrics_json, provenance_json, artifact_sha256, ingestion_run_id
                ) VALUES (
                    :source_id, 'SUBGROUP_UNAVAILABLE', :parent, 'NO_CURATED_SUBGROUP', NULL, :as_of,
                    :schema, :method, :bench, 'unavailable', 'unavailable',
                    CAST(:metrics AS JSONB), CAST(:coverage AS JSONB), :sha, :run
                )
                ON CONFLICT (source_id, dataset, parent_sector_key, industry_key, as_of, methodology_version)
                DO UPDATE SET metrics_json = EXCLUDED.metrics_json, provenance_json = EXCLUDED.provenance_json
                """
            ),
            {
                "source_id": source_id,
                "parent": sector,
                "as_of": as_of,
                "schema": SCHEMA_VERSION,
                "method": METHODOLOGY_VERSION,
                "bench": etf,
                "metrics": strict_dumps(metrics),
                "coverage": strict_dumps(coverage),
                "sha": _artifact_sha({"sector": sector, "as_of": as_of.isoformat(), "status": "UNAVAILABLE"}),
                "run": run_id,
            },
        )
        written += 1
    return written


@dataclass
class EquityIngestReport:
    status: str = RUN_SUCCEEDED
    bars_written: int = 0
    snapshots_written: int = 0
    latest_observation: date | None = None
    failed: bool = False
    access_status: str = "CONFIGURED"
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "bars_written": self.bars_written,
            "snapshots_written": self.snapshots_written,
            "latest_observation": self.latest_observation.isoformat() if self.latest_observation else None,
            "failed": self.failed,
            "access_status": self.access_status,
            "reason": self.reason,
        }


def _stored_adjustment_basis(conn, symbols: list[str]) -> str:
    if not symbols:
        return "SPLIT_ADJUSTED_UNKNOWN_DIVIDEND"
    value = conn.execute(
        text(
            """
            SELECT adjustment_basis FROM mi_market_bars
            WHERE source_id=:s AND bar_interval='1D' AND instrument_id IN :syms
              AND adjustment_basis IS NOT NULL
            ORDER BY bar_date DESC
            LIMIT 1
            """
        ).bindparams(bindparam("syms", expanding=True)),
        {"s": EQUITY_SOURCE_ID, "syms": symbols},
    ).scalar()
    return str(value or "SPLIT_ADJUSTED_UNKNOWN_DIVIDEND")


def _rebuild_from_stored(conn, *, symbols: list[str], run_id: str, provider: str | None) -> tuple[date | None, int]:
    load_provider = provider if provider in {"IBKR", "YAHOO", "FIXTURE"} else None
    prices = load_adj_closes(conn, symbols, source_id=EQUITY_SOURCE_ID, provider=load_provider)
    as_of = None
    for series in prices.values():
        if series:
            last = max(series)
            if as_of is None or last > as_of:
                as_of = last
    if as_of is None:
        return None, 0
    basis = _stored_adjustment_basis(conn, symbols)
    written = write_snapshots(
        conn,
        as_of=as_of,
        prices=prices,
        source_id=EQUITY_SOURCE_ID,
        run_id=run_id,
        adjustment_basis=basis,
        provider=provider,
    )
    return as_of, written


def ingest_equity_eod(
    engine,
    adapter: EquityDailyAdapter | None = None,
    *,
    today: date | None = None,
    parent_run_id: str | None = None,
    symbols: list[str] | None = None,
    lookback_days: int = 400,
    env: Mapping[str, str] | None = None,
    incremental: bool = True,
) -> EquityIngestReport:
    today = today or utcnow().date()
    adapter = adapter or adapter_from_env(env)
    report = EquityIngestReport(access_status=adapter.access_status, reason=adapter.reason)
    symbols = list(symbols or UNIVERSE_SYMBOLS)
    start = today - timedelta(days=lookback_days)
    if incremental and getattr(adapter, "source_id", "") == "IBKR":
        getattr(adapter, "__dict__", {}).update({"incremental": True})
    with engine.begin() as conn:
        upsert_source_registry(
            conn,
            [
                {
                    "source_id": EQUITY_SOURCE_ID,
                    "provider": adapter.source_id,
                    "dataset": "equity_etf_daily_bars",
                    "source_url": "",
                    "expected_cadence": "D",
                    "usage_scope": "INTERNAL_ONLY",
                    "attribution": "Independent equity EOD adapter; never labeled as FMP or FRED.",
                    "terms_notes": adapter.reason,
                    "units_metadata": {"price": "adjusted_close"},
                }
            ],
            enabled={EQUITY_SOURCE_ID: adapter.access_status == "CONFIGURED"},
            access={EQUITY_SOURCE_ID: adapter.access_status},
        )
        rid = start_run(conn, source_id=EQUITY_SOURCE_ID, dataset="equity_etf_daily_bars", parent_run_id=parent_run_id)
        if incremental and getattr(adapter, "source_id", "") == "IBKR":
            latest = latest_stored_bar_date(conn, symbols)
            if latest is not None:
                start = latest - timedelta(days=7)
        if getattr(adapter, "rebuild_only", False):
            as_of, snaps = _rebuild_from_stored(conn, symbols=symbols, run_id=rid, provider=adapter.source_id)
            report.latest_observation = as_of
            report.snapshots_written = snaps
            report.status = RUN_SKIPPED
            prior = conn.execute(
                text("SELECT transport_status FROM mi_data_freshness WHERE source_id=:s AND dataset='equity_etf_daily_bars'"),
                {"s": EQUITY_SOURCE_ID},
            ).scalar()
            transport = prior if prior in {TRANSPORT_OK, "PARTIAL"} else TRANSPORT_SKIPPED
            record_freshness(
                conn,
                source_id=EQUITY_SOURCE_ID,
                dataset="equity_etf_daily_bars",
                cadence="D",
                transport_status=transport,
                latest_observation=as_of,
                success=False,
                error_redacted=None,
                run_id=rid,
                today=today,
                series_id="SPY",
            )
            finish_run(conn, rid, status=RUN_SKIPPED, details={"reason": adapter.reason, "rebuilt_from_stored": as_of is not None, "snapshots": snaps, "tws_socket": False})
            return report
        try:
            bars = adapter.fetch(symbols, start, today)
        except AdapterUnavailable as exc:
            as_of, snaps = _rebuild_from_stored(conn, symbols=symbols, run_id=rid, provider=adapter.source_id)
            report.latest_observation = as_of
            report.snapshots_written = snaps
            report.reason = str(exc)
            report.status = RUN_SKIPPED
            record_freshness(
                conn,
                source_id=EQUITY_SOURCE_ID,
                dataset="equity_etf_daily_bars",
                cadence="D",
                transport_status=TRANSPORT_SKIPPED,
                latest_observation=as_of,
                success=as_of is not None,
                error_redacted=str(exc)[:200],
                run_id=rid,
                today=today,
                series_id="SPY",
            )
            finish_run(conn, rid, status=RUN_SKIPPED, details={"reason": str(exc), "rebuilt_from_stored": as_of is not None, "snapshots": snaps})
            return report
        except Exception as exc:  # noqa: BLE001
            as_of, snaps = _rebuild_from_stored(conn, symbols=symbols, run_id=rid, provider=adapter.source_id)
            report.latest_observation = as_of
            report.snapshots_written = snaps
            report.failed = True
            report.status = RUN_FAILED
            report.reason = exc.__class__.__name__
            record_freshness(
                conn,
                source_id=EQUITY_SOURCE_ID,
                dataset="equity_etf_daily_bars",
                cadence="D",
                transport_status=TRANSPORT_FAILED,
                latest_observation=as_of,
                success=False,
                error_redacted=exc.__class__.__name__,
                run_id=rid,
                today=today,
                series_id="SPY",
            )
            finish_run(conn, rid, status=RUN_FAILED, error_redacted=exc.__class__.__name__, details={"rebuilt_from_stored": as_of is not None})
            return report
        retrieved = utcnow()
        counts = upsert_bars(conn, bars, run_id=rid, retrieved_at=retrieved, provider=adapter.source_id if adapter.source_id in {"IBKR", "YAHOO", "FIXTURE"} else None)
        report.bars_written = counts["inserted"]
        load_provider = adapter.source_id if adapter.source_id in {"IBKR", "YAHOO", "FIXTURE"} else None
        prices = load_adj_closes(conn, symbols, source_id=EQUITY_SOURCE_ID, provider=load_provider)
        as_of = max((b.bar_date for b in bars), default=None)
        if as_of is None:
            as_of = latest_stored_bar_date(conn, symbols)
        report.latest_observation = as_of
        basis = bars[0].adjustment_basis if bars else _stored_adjustment_basis(conn, symbols)
        if as_of is not None:
            report.snapshots_written = write_snapshots(
                conn,
                as_of=as_of,
                prices=prices,
                source_id=EQUITY_SOURCE_ID,
                run_id=rid,
                adjustment_basis=basis,
                provider=adapter.source_id,
            )
        record_freshness(
            conn,
            source_id=EQUITY_SOURCE_ID,
            dataset="equity_etf_daily_bars",
            cadence="D",
            transport_status=TRANSPORT_OK,
            latest_observation=as_of,
            success=True,
            error_redacted=None,
            run_id=rid,
            today=today,
            series_id="SPY",
        )
        finish_run(conn, rid, status=RUN_SUCCEEDED, counts=counts, details={"snapshots": report.snapshots_written})
    return report


__all__ = [
    "CollectorStoreAdapter",
    "EQUITY_SOURCE_ID",
    "EquityBar",
    "EquityIngestReport",
    "FixtureAdapter",
    "UnavailableAdapter",
    "YahooAdapter",
    "adapter_from_env",
    "aligned_session_return",
    "compute_metrics",
    "ingest_equity_eod",
    "latest_stored_bar_date",
    "pct_vs_dma",
    "ratio_change_rs",
    "upsert_bars",
]
