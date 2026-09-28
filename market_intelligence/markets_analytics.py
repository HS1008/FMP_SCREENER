"""Canonical analytics for the US Markets and Global Markets pages.

Two different relative-strength quantities are used and must not be mixed:

* A point-in-time price ratio (``normalized_ratio``) is the leadership chart.
  ``ratio_t = adjusted_close_asset / adjusted_close_benchmark``, then rebased
  to 100 on the first common date. A rising line means the numerator
  outperformed the benchmark over the displayed interval.
* A windowed ratio change (``rs_chg_*`` on sector snapshots, from
  ``baskets.ratio_change_rs``) is ``(P_t/B_t) / (P_prev/B_prev) - 1`` across a
  fixed session window. Sector relative-strength bars use that stored field.

Drawdown here is a per-session trailing path for the dashboard. It is not the
snapshot scalar ``max_drawdown_252d``.

Session windows match ``equity_eod.RETURN_WINDOWS`` (1, 5, 21, 63, 126, 252).
This module does not import the equity ingest package, so Streamlit can import
it without loading a writer or a provider client.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from market_intelligence.baskets import window_return

DRAWDOWN_SESSIONS = 252

# Labels, snapshot field, stored-session count. Counts must match RETURN_WINDOWS.
HORIZONS: tuple[tuple[str, str, int], ...] = (
    ("1D", "ret_1d", 1),
    ("1W", "ret_1w", 5),
    ("1M", "ret_1m", 21),
    ("3M", "ret_3m", 63),
    ("6M", "ret_6m", 126),
    ("1Y", "ret_12m", 252),
)
RS_HORIZONS: tuple[tuple[str, str], ...] = (
    ("1D", "rs_chg_1d"),
    ("1W", "rs_chg_1w"),
    ("1M", "rs_chg_1m"),
    ("3M", "rs_chg_3m"),
    ("6M", "rs_chg_6m"),
    ("1Y", "rs_chg_12m"),
)
HORIZON_SESSIONS: dict[str, int] = {label: sessions for label, _field, sessions in HORIZONS}
HORIZON_RETURN_FIELD: dict[str, str] = {label: field for label, field, _sessions in HORIZONS}
HORIZON_RS_FIELD: dict[str, str] = {label: field for label, field in RS_HORIZONS}

LIVE_RETURN_STATE = (
    "Live 1D from stored quotes: current last divided by the prior completed session close. "
    "A Yahoo fallback is labeled as stored live quotes, not as IBKR."
)
EOD_RETURN_STATE = (
    "Finalized EOD 1D. Live quotes are unavailable or incomplete, so every sector uses the stored EOD return."
)
EOD_WINDOW_STATE = "Finalized EOD · {horizon} · {sessions} stored sessions."
LIVE_RS_STATE = (
    "Live 1D relative strength from stored quotes, using the same ratio-change formula as rs_chg_1d "
    "when the asset and SPY sessions align."
)
EOD_RS_STATE = (
    "Finalized EOD relative strength: (P_t/SPY_t) / (P_prev/SPY_prev) - 1 over the aligned session window. "
    "This is not a difference of the two returns."
)

US_METHODOLOGY: tuple[str, ...] = (
    "ETF proxies: SPY (S&P 500), QQQ (Nasdaq-100), IWM (Russell 2000), DIA (Dow Jones Industrial Average), RSP (S&P 500 equal weight). These are current investable proxies, not a point-in-time or survivorship-free universe.",
    "Historical comparisons use stored adjusted closes (`adj_close_price`) from EQUITY_EOD. Raw closes are not mixed into the same return. Each symbol keeps a single stored provider for its whole history; providers are not spliced. The stored `adjustment_basis` is shown with the coverage metadata. Yahoo history uses auto-adjusted closes labeled SPLIT_ADJUSTED_UNKNOWN_DIVIDEND when that is the stored basis. IBKR rows keep their own basis.",
    "Normalized performance rebases every selected series to 100 on the first date inside From/To where all selected series have a valid adjusted close. Changing the selection recomputes that date. Later missing sessions stay missing. Nothing is forward-filled.",
    "Relative leadership is the point-in-time price ratio (asset / SPY), rebased to 100 at the start of the selected interval. Rising means the numerator outperformed SPY. It is not the windowed `rs_chg_*` ratio change.",
    "Return windows are stored trading sessions: 1D = 1, 1W = 5, 1M = 21, 3M = 63, 6M = 126, 1Y = 252. They are not calendar-day offsets.",
    "Sector ETFs follow the repository taxonomy: XLK Technology, XLF Financials, XLI Industrials, XLY Consumer Discretionary, XLC Communication Services, XLV Health Care, XLP Consumer Staples, XLE Energy, XLU Utilities, XLRE Real Estate, XLB Materials.",
    "Sector relative strength reuses stored `rs_chg_*` (and live 1D only when every sector has a fresh stored quote). The formula is the change in the price ratio versus SPY, not a return spread.",
    "Live 1D is current last / prior completed session close, and only when the stored quote is fresh. Otherwise the page uses the latest finalized EOD 1D for every sector. Stale live quotes are not mixed with fresh EOD rows. The return heatmap stays on finalized EOD.",
    "Drawdown from the 52-week high is adjusted_close / max(adjusted_close over the trailing 252 stored sessions, including that session) - 1. Fewer than 252 sessions stays missing. The value is never positive. It is not replaced with zero.",
)

GLOBAL_METHODOLOGY: tuple[str, ...] = (
    "USD-listed ETF proxies: SPY United States, VEA Developed ex-US, VGK Europe, EWJ Japan, VWO Emerging Markets, MCHI China, INDA India, EWZ Brazil. One broad ETF per region. Returns are what a USD investor experienced in that ETF, including currency effects embedded in the USD share price.",
    "Local-index comparison (local currency versus USD ETF) is deferred. Free index history was not added as a second mode because a partial or unreliable ticker map would overstate coverage.",
    "Adjusted closes and provider lineage follow the same EQUITY_EOD rules as US Markets. History starts at the fund's first stored session. Nothing is fabricated before inception, and symbols are not required to share a start date.",
    "Return windows are stored trading sessions: 1D = 1, 1W = 5, 1M = 21, 3M = 63, 6M = 126, 1Y = 252.",
    "Regional performance is rebased to 100 on the first common valid date of the series currently selected, inside From/To. The default selection is United States, Developed ex-US, Europe, Japan, and Emerging Markets.",
    "VEA/SPY and VWO/SPY are point-in-time price ratios rebased to 100. Rising means that international ETF outperformed SPY. They are not windowed `rs_chg_*` values and they are not a bullish or bearish label.",
    "These pages monitor current benchmark proxies. They are not a research backtest and they do not change point-in-time universe methodology.",
)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def as_day(value: Any) -> date | None:
    """Date-only parsing. A `YYYY-MM-DD` string is not shifted through a timezone."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
    return None


def _price_map(points: Sequence[tuple[date, float | None] | Mapping[str, Any]]) -> dict[date, float]:
    out: dict[date, float] = {}
    for item in points:
        if isinstance(item, Mapping):
            day = as_day(item.get("date") if "date" in item else item.get("as_of"))
            number = _finite(item.get("value"))
        else:
            day = item[0]
            number = _finite(item[1])
        if day is None or number is None or number == 0:
            continue
        out[day] = number
    return out


def clip_points(
    points: Sequence[tuple[date, float]],
    *,
    start: date | None,
    end: date | None,
) -> list[tuple[date, float]]:
    if start is None or end is None or start > end:
        return []
    return [(day, value) for day, value in points if start <= day <= end]


def normalize_selected_to_100(
    series: Mapping[str, Sequence[tuple[date, float | None] | Mapping[str, Any]]],
    selected: Sequence[str],
) -> dict[str, Any]:
    """Rebase every selected series to 100 on their first common valid date.

    Dates before that common start are dropped. A later session missing on one
    series is omitted for that series only. Nothing is forward-filled.
    """
    chosen = [symbol for symbol in selected if symbol]
    maps = {symbol: _price_map(series.get(symbol) or ()) for symbol in chosen}
    empty = {"start": None, "series": {symbol: [] for symbol in chosen}}
    if not chosen or any(not maps[symbol] for symbol in chosen):
        return empty
    shared = set.intersection(*(set(maps[symbol]) for symbol in chosen))
    if not shared:
        return empty
    start = min(shared)
    scaled: dict[str, list[tuple[date, float]]] = {}
    for symbol in chosen:
        base = maps[symbol][start]
        scaled[symbol] = [
            (day, 100.0 * maps[symbol][day] / base)
            for day in sorted(maps[symbol])
            if day >= start
        ]
    return {"start": start, "series": scaled}


def price_ratio_points(
    asset: Sequence[tuple[date, float | None] | Mapping[str, Any]],
    benchmark: Sequence[tuple[date, float | None] | Mapping[str, Any]],
) -> list[tuple[date, float]]:
    """Point-in-time adjusted price ratio. Not a windowed ratio change."""
    left = _price_map(asset)
    right = _price_map(benchmark)
    points: list[tuple[date, float]] = []
    for day in sorted(set(left) & set(right)):
        bench = right[day]
        if bench == 0:
            continue
        points.append((day, left[day] / bench))
    return points


def normalized_ratio(
    asset: Sequence[tuple[date, float | None] | Mapping[str, Any]],
    benchmark: Sequence[tuple[date, float | None] | Mapping[str, Any]],
) -> dict[str, Any]:
    """Price ratio rebased to 100 on the first date both prices exist.

    Rising means the asset outperformed the benchmark. This is not `rs_chg_*`.
    """
    ratios = price_ratio_points(asset, benchmark)
    if not ratios or ratios[0][1] == 0:
        return {"start": None, "points": []}
    base = ratios[0][1]
    return {
        "start": ratios[0][0],
        "points": [(day, 100.0 * value / base) for day, value in ratios],
    }


def trailing_drawdown(
    points: Sequence[tuple[date, float | None] | Mapping[str, Any]],
    *,
    sessions: int = DRAWDOWN_SESSIONS,
) -> list[tuple[date, float]]:
    """Trailing drawdown versus the max adjusted close of ``sessions`` stored observations.

    Null prices are omitted and do not count as sessions. The path stays missing
    until ``sessions`` valid observations exist. The result is never positive.
    """
    if sessions < 1:
        return []
    valid: list[tuple[date, float]] = []
    for item in points:
        if isinstance(item, Mapping):
            day = as_day(item.get("date") if "date" in item else item.get("as_of"))
            number = _finite(item.get("value"))
        else:
            day = item[0]
            number = _finite(item[1])
        if day is None or number is None:
            continue
        valid.append((day, number))
    valid.sort(key=lambda item: item[0])
    out: list[tuple[date, float]] = []
    for index in range(sessions - 1, len(valid)):
        window = valid[index - sessions + 1 : index + 1]
        peak = max(price for _day, price in window)
        price = valid[index][1]
        if peak == 0:
            continue
        out.append((valid[index][0], min(price / peak - 1.0, 0.0)))
    return out


def session_window_returns(series: Mapping[date, float], as_of: date) -> dict[str, float | None]:
    """Canonical session returns for 1D, 1W, 1M, 3M, 6M, and 1Y."""
    return {label: window_return(series, as_of, sessions) for label, sessions in HORIZON_SESSIONS.items()}


def bars_to_series(points: Sequence[Mapping[str, Any]]) -> dict[date, float]:
    mapped = _price_map(points)
    return mapped


def live_field_complete(rows: Sequence[Mapping[str, Any]], field: str) -> bool:
    if not rows:
        return False
    for row in rows:
        metrics = row.get("metrics") or {}
        if _finite(metrics.get(field)) is None:
            return False
    return True


def sector_bar_pairs(
    rows: Sequence[Mapping[str, Any]],
    horizon: str,
    *,
    kind: str,
) -> tuple[list[tuple[str, float]], str]:
    """Rank inputs and an honest live-versus-EOD label.

    ``kind`` is ``return`` or ``rs``. Live 1D is used only when every row has
    a finite live value. Otherwise every row uses the finalized EOD field.
    Missing values are omitted, never drawn as zero.
    """
    if kind == "rs":
        eod_field = HORIZON_RS_FIELD.get(horizon)
        live_field = "live_rs_chg_1d"
        live_state = LIVE_RS_STATE
        eod_state = EOD_RS_STATE
    elif kind == "return":
        eod_field = HORIZON_RETURN_FIELD.get(horizon)
        live_field = "live_ret_1d"
        live_state = LIVE_RETURN_STATE
        eod_state = EOD_RETURN_STATE
    else:
        raise ValueError("kind must be return or rs")
    if eod_field is None:
        return [], "Unknown horizon."
    use_live = horizon == "1D" and live_field_complete(rows, live_field)
    field = live_field if use_live else eod_field
    if use_live:
        state = live_state
    elif horizon == "1D":
        state = eod_state
    else:
        state = EOD_WINDOW_STATE.format(horizon=horizon, sessions=HORIZON_SESSIONS[horizon])
    pairs: list[tuple[str, float]] = []
    for row in rows:
        metrics = row.get("metrics") or {}
        number = _finite(metrics.get(field))
        if number is None:
            continue
        label = str(row.get("canonical_sector") or row.get("sector_key") or row.get("label") or "")
        if not label:
            continue
        pairs.append((label, number))
    return pairs, state


def heatmap_rows(
    order: Sequence[tuple[str, str]],
    returns_by_symbol: Mapping[str, Mapping[str, float | None]],
) -> dict[str, Any]:
    """Fixed row order. Cell values are fractional returns or null. Not sorted."""
    columns = [label for label, _field, _sessions in HORIZONS]
    rows: list[dict[str, Any]] = []
    for symbol, label in order:
        values = returns_by_symbol.get(symbol) or {}
        rows.append({"symbol": symbol, "label": label, "values": [values.get(column) for column in columns]})
    return {"columns": columns, "rows": rows}


__all__ = [
    "DRAWDOWN_SESSIONS",
    "EOD_RETURN_STATE",
    "EOD_RS_STATE",
    "GLOBAL_METHODOLOGY",
    "HORIZON_RETURN_FIELD",
    "HORIZON_RS_FIELD",
    "HORIZON_SESSIONS",
    "HORIZONS",
    "LIVE_RETURN_STATE",
    "LIVE_RS_STATE",
    "RS_HORIZONS",
    "US_METHODOLOGY",
    "as_day",
    "bars_to_series",
    "clip_points",
    "heatmap_rows",
    "live_field_complete",
    "normalize_selected_to_100",
    "normalized_ratio",
    "price_ratio_points",
    "sector_bar_pairs",
    "session_window_returns",
    "trailing_drawdown",
]
