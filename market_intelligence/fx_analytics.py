"""FOREX display orientation and provider-observation returns.

A rising standardized series means the foreign currency strengthened versus
the US dollar. USD/JPY, USD/CAD, USD/CHF, and USD/CNH are inverted for that view only.
Raw pair charts keep the Yahoo quote convention.

Windows count valid Yahoo daily observations on that series. They are not
NYSE sessions and they are not crypto calendar days. Missing closes stay
missing: nothing is filled or set to zero.
"""

from __future__ import annotations

from datetime import date
from typing import Mapping, Sequence

from market_intelligence.cross_asset_universe import CURRENCY_VS_USD, FX_WINDOWS, INSTRUMENT_BY_ID
from market_intelligence.markets_analytics import _finite, as_day


FX_METHODOLOGY = (
    "Yahoo daily closes. DXY is DX-Y.NYB and is not rebased. "
    "Major Currencies vs USD rises when the foreign currency strengthens: "
    "EUR/USD, GBP/USD, and AUD/USD are used directly; USD/JPY, USD/CAD, USD/CHF, and USD/CNH are inverted. "
    "USD/CNH is the offshore renminbi (CNH, not onshore CNY) from the CME USD/Offshore RMB future on Yahoo (CNH=F), "
    "kept in USD/CNH quote convention so a rising value is USD appreciation. "
    "The comparable chart is rebased to 100 at the first common valid date in the selected range. "
    "Returns use that same orientation and count provider daily observations "
    "(1, 5, 21, 63, 126, 252), not NYSE holidays and not a 7-calendar-day crypto week. "
    "A window is left blank when its anchor observation is much older than the window implies (sparse provider history). "
    "Positive means the foreign currency strengthened versus USD."
)


def oriented_fx_level(instrument_id: str, close: float | None) -> float | None:
    """Foreign-currency strength versus USD. ``None`` stays missing."""
    spec = INSTRUMENT_BY_ID.get(instrument_id)
    number = _finite(close)
    if spec is None or number is None or number <= 0:
        return None
    if spec.invert_vs_usd:
        return 1.0 / number
    return number


def max_gap_days(lag: int) -> int:
    """Calendar span beyond which ``lag`` observations no longer mean the labelled window.

    ``lag`` sessions are nominally ``lag * 7 / 5`` calendar days; allow half again
    plus a few days for holidays (1 -> 6, 5 -> 14, 21 -> 48, 63 -> 136, 252 -> 533).
    A thinly quoted series (CME CNH=F skipped most of September 2026) would otherwise
    label a 25-day move as "1D".
    """
    return int(lag * 7 / 5 * 1.5) + 4


def observation_return(points: Sequence[tuple[date, float]], lag: int) -> float | None:
    """Return from ``lag`` valid observations earlier to the last point.

    ``None`` when the anchor observation is older than :func:`max_gap_days` for
    that lag, so sparse provider history reads as missing instead of as a return
    over a much longer span than the column says.
    """
    if lag < 1 or len(points) <= lag:
        return None
    past_day, past = points[-1 - lag]
    current_day, current = points[-1]
    if past is None or current is None or past == 0:
        return None
    if isinstance(past_day, date) and isinstance(current_day, date) and (current_day - past_day).days > max_gap_days(lag):
        return None
    return current / past - 1.0


def latest_window_returns(
    points: Sequence[tuple[date, float]],
    windows: Sequence[tuple[str, int]] = FX_WINDOWS,
) -> dict[str, float | None]:
    return {label: observation_return(points, lag) for label, lag in windows}


def levels_by_id(rows: Sequence[Mapping[str, object]], *, instrument_id: str, orient: bool) -> list[tuple[date, float]]:
    """Ascending (date, level) pairs. Non-positive and missing closes are dropped."""
    spec = INSTRUMENT_BY_ID.get(instrument_id)
    out: list[tuple[date, float]] = []
    for row in rows:
        if str(row.get("instrument_id") or "") != instrument_id:
            continue
        day = as_day(row.get("bar_date") if "bar_date" in row else row.get("as_of"))
        close = _finite(row.get("close"))
        if day is None or close is None or close <= 0:
            continue
        level = oriented_fx_level(instrument_id, close) if orient and spec is not None else close
        if level is None:
            continue
        out.append((day, level))
    out.sort(key=lambda item: item[0])
    deduped: list[tuple[date, float]] = []
    for day, level in out:
        if deduped and deduped[-1][0] == day:
            deduped[-1] = (day, level)
        else:
            deduped.append((day, level))
    return deduped


def currency_return_table(
    histories: Mapping[str, Sequence[tuple[date, float]]],
) -> list[dict[str, object]]:
    """One row per currency. Values are fractional returns in foreign-vs-USD orientation."""
    rows: list[dict[str, object]] = []
    for instrument_id, label in CURRENCY_VS_USD:
        points = list(histories.get(instrument_id) or [])
        returns = latest_window_returns(points)
        rows.append({"instrument_id": instrument_id, "label": label, "returns": returns})
    return rows
