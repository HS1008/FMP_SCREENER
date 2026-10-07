"""Trailing realized volatility and Sharpe for the individual-stock heatmap.

Inputs are the stored ``YAHOO_PRICE_DAILY`` bars that the dashboard already reads
from PostgreSQL (one row per NYSE session). The close used here is Yahoo's
split- and dividend-adjusted ``Adj Close`` (``adj_close``). The split-only
``close`` that drives the 1W-1Y price-return columns is never substituted, so a
window whose dividend-adjusted history is not stored yet is N/A rather than a
mix of bases.

Convention (the same one ``overview_metrics.RISK_METHODOLOGY`` documents for the
Market Overview's "Sharpe 3M"):

* simple daily returns ``r_t = P_t / P_(t-1) - 1`` over consecutive completed sessions;
* annualized volatility = sample standard deviation of ``r`` times ``sqrt(252)``;
* annualized Sharpe = mean daily excess return / sample standard deviation of
  daily excess returns times ``sqrt(252)`` with a 0% risk-free rate (the
  dashboard has no stored, date-aligned risk-free series; ``config.RISK_FREE_RATE``
  is a legacy FMP constant, not a historical series);
* 3M is the trailing 63 returns (64 closes) and 1Y the trailing 252 returns (253
  closes), counted on the NYSE session calendar ending at the newest completed
  bar. A missing session, an invalid close, or a different adjustment basis
  anywhere inside the window is N/A. Nothing is forward-filled or set to zero.
* The newest bar is excluded while it is still ``PROVISIONAL`` (the regular
  session has not closed), so every metric uses completed daily sessions only.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Mapping, Sequence

import numpy as np

from market_intelligence.calendars import CAL_NYSE, is_session, previous_session
from market_intelligence.price_returns import PRICE_RETURN_BASIS

RISK_WINDOWS: tuple[tuple[str, int], ...] = (("3M", 63), ("1Y", 252))
SESSIONS_PER_YEAR = 252
RISK_FREE_RATE_ANNUAL = 0.0
RISK_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("Vol 3M", "3M", "volatility"),
    ("Sharpe 3M", "3M", "sharpe"),
    ("Vol 1Y", "1Y", "volatility"),
    ("Sharpe 1Y", "1Y", "sharpe"),
)
RISK_COLUMN_LABELS: tuple[str, ...] = tuple(label for label, _window, _kind in RISK_COLUMNS)
RISK_COLUMN_KINDS: tuple[str, ...] = tuple(kind for _label, _window, kind in RISK_COLUMNS)
STOCK_RISK_CAPTION = (
    "Vol 3M / 1Y is the annualized volatility of the trailing 63 / 252 simple daily returns "
    "(sample standard deviation × √252) from 64 / 253 consecutive completed NYSE sessions of "
    "dividend- and split-adjusted Yahoo closes. Sharpe 3M / 1Y is the mean daily excess return divided "
    "by the sample standard deviation of daily excess returns × √252, with a 0% risk-free rate "
    "(no stored date-aligned risk-free series; the Market Overview's Sharpe 3M uses the same convention). "
    "The current session is excluded until its regular close. A missing or invalid close inside the window "
    "is N/A. Volatility uses its own amber scale (darker = more volatile, not better or worse); Sharpe uses a "
    "green/red scale centred on zero, separate from the return columns."
)
PROVISIONAL = "PROVISIONAL"


def _finite_positive(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _bar_day(row: Mapping[str, Any]) -> date | None:
    raw = row.get("bar_date")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def completed_session_bars(bars: Sequence[Mapping[str, Any]]) -> dict[date, Mapping[str, Any]]:
    """Completed bars keyed by session date. The newest row wins a duplicate date.

    The current session stays out while it is ``PROVISIONAL``. Dates that are not
    NYSE sessions are ignored so a stray weekend row cannot enter a window.
    """
    chosen: dict[date, Mapping[str, Any]] = {}
    for row in bars:
        day = _bar_day(row)
        if day is None or not is_session(day, CAL_NYSE):
            continue
        if str(row.get("quality") or "").upper() == PROVISIONAL:
            continue
        chosen[day] = row
    return chosen


def trailing_sessions(end: date, count: int) -> list[date]:
    """``count`` NYSE sessions ending at ``end`` (inclusive), ascending."""
    days = [end]
    while len(days) < count:
        days.append(previous_session(days[-1], CAL_NYSE))
    days.reverse()
    return days


def window_closes(bars: Sequence[Mapping[str, Any]], returns: int) -> dict[str, Any]:
    """Dividend-adjusted closes for the trailing ``returns`` daily returns.

    ``returns + 1`` consecutive completed sessions must each have a positive
    adjusted close on the split-adjusted price basis. The first problem found is
    reported as ``reason``; ``closes`` is empty when the window is incomplete.
    """
    by_day = completed_session_bars(bars)
    empty: dict[str, Any] = {"closes": [], "start": None, "end": None, "reason": "no completed sessions"}
    if returns < 1 or not by_day:
        return empty
    end = max(by_day)
    sessions = trailing_sessions(end, returns + 1)
    closes: list[float] = []
    for day in sessions:
        row = by_day.get(day)
        if row is None:
            return {"closes": [], "start": sessions[0], "end": end, "reason": "missing session {0}".format(day.isoformat())}
        if str(row.get("basis") or "") != PRICE_RETURN_BASIS:
            return {"closes": [], "start": sessions[0], "end": end, "reason": "adjustment mismatch {0}".format(day.isoformat())}
        close = _finite_positive(row.get("adj_close"))
        if close is None:
            return {
                "closes": [],
                "start": sessions[0],
                "end": end,
                "reason": "no dividend-adjusted close {0}".format(day.isoformat()),
            }
        closes.append(close)
    return {"closes": closes, "start": sessions[0], "end": end, "reason": None}


def simple_returns(closes: Sequence[float]) -> np.ndarray:
    prices = np.asarray(list(closes), dtype=float)
    if prices.size < 2:
        return np.empty(0, dtype=float)
    return prices[1:] / prices[:-1] - 1.0


def annualized_vol_and_sharpe(
    closes: Sequence[float],
    *,
    periods_per_year: int = SESSIONS_PER_YEAR,
    risk_free_daily: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Annualized volatility and Sharpe from one complete close window.

    ``risk_free_daily`` is the per-return risk-free rate aligned to each return
    date (same length as the returns). ``None`` is the documented 0% convention.
    A zero standard deviation leaves Sharpe ``None``; fewer than two returns
    leaves both ``None``.
    """
    returns = simple_returns(closes)
    if returns.size < 2:
        return {"vol": None, "sharpe": None, "returns": int(returns.size), "reason": "fewer than two returns"}
    if risk_free_daily is None:
        excess = returns
    else:
        rates = np.asarray(list(risk_free_daily), dtype=float)
        if rates.shape != returns.shape or not np.all(np.isfinite(rates)):
            return {"vol": None, "sharpe": None, "returns": int(returns.size), "reason": "risk-free series misaligned"}
        excess = returns - rates
    scale = math.sqrt(float(periods_per_year))
    stdev = float(np.std(returns, ddof=1))
    vol = stdev * scale if math.isfinite(stdev) else None
    excess_std = float(np.std(excess, ddof=1))
    sharpe: float | None
    if not math.isfinite(excess_std) or excess_std <= 0.0:
        sharpe = None
        reason = "zero volatility" if excess_std == 0.0 else "invalid returns"
    else:
        sharpe = float(np.mean(excess)) / excess_std * scale
        reason = None
    return {"vol": vol, "sharpe": sharpe, "returns": int(returns.size), "reason": reason}


def stock_risk_metrics(
    bars: Sequence[Mapping[str, Any]],
    *,
    windows: Sequence[tuple[str, int]] = RISK_WINDOWS,
) -> dict[str, dict[str, Any]]:
    """Per-window volatility and Sharpe for one symbol. Missing stays ``None`` with a reason."""
    out: dict[str, dict[str, Any]] = {}
    for label, count in windows:
        window = window_closes(bars, count)
        if not window["closes"]:
            out[label] = {
                "vol": None,
                "sharpe": None,
                "returns": 0,
                "start": window["start"],
                "end": window["end"],
                "reason": window["reason"],
            }
            continue
        stats = annualized_vol_and_sharpe(window["closes"])
        out[label] = {
            "vol": stats["vol"],
            "sharpe": stats["sharpe"],
            "returns": stats["returns"],
            "start": window["start"],
            "end": window["end"],
            "reason": stats["reason"],
        }
    return out


def risk_metrics_by_symbol(
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    symbols: Sequence[str],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Compute each symbol once from the shared stored-bar read (no per-symbol fetch)."""
    computed: dict[str, dict[str, dict[str, Any]]] = {}
    for symbol in symbols:
        key = str(symbol or "").upper()
        if not key or key in computed:
            continue
        computed[key] = stock_risk_metrics(bars_by_symbol.get(key) or [])
    return computed


def risk_cell_note(label: str, window: Mapping[str, Any], *, kind: str) -> str:
    """Tooltip text for one volatility or Sharpe cell."""
    count = dict(RISK_WINDOWS).get(label)
    start = window.get("start")
    end = window.get("end")
    span = ""
    if isinstance(start, date) and isinstance(end, date):
        span = " {0} → {1}".format(start.isoformat(), end.isoformat())
    if kind == "volatility":
        formula = "sample σ of {0} daily returns × √252".format(count)
    else:
        formula = "mean daily excess return / sample σ × √252, risk-free 0%"
    parts = [
        "{0}: {1}".format(label, formula),
        "{0} completed sessions{1}".format((count or 0) + 1, span),
        "dividend- and split-adjusted closes; current session excluded",
    ]
    reason = window.get("reason")
    if (window.get("vol") if kind == "volatility" else window.get("sharpe")) is None:
        parts.append("N/A: {0}".format(reason or "insufficient history"))
    return " · ".join(parts)


def risk_row_cells(metrics: Mapping[str, Mapping[str, Any]]) -> tuple[list[float | None], list[str]]:
    """Values and notes for the four risk columns in ``RISK_COLUMNS`` order."""
    values: list[float | None] = []
    notes: list[str] = []
    for _label, window_label, kind in RISK_COLUMNS:
        window = metrics.get(window_label) or {}
        values.append(window.get("vol") if kind == "volatility" else window.get("sharpe"))
        notes.append(risk_cell_note(window_label, window, kind=kind))
    return values, notes


__all__ = [
    "RISK_COLUMNS",
    "RISK_COLUMN_KINDS",
    "RISK_COLUMN_LABELS",
    "RISK_FREE_RATE_ANNUAL",
    "RISK_WINDOWS",
    "SESSIONS_PER_YEAR",
    "STOCK_RISK_CAPTION",
    "annualized_vol_and_sharpe",
    "completed_session_bars",
    "risk_cell_note",
    "risk_metrics_by_symbol",
    "risk_row_cells",
    "simple_returns",
    "stock_risk_metrics",
    "trailing_sessions",
    "window_closes",
]
