"""Presentation helpers for policy-based 1D cells and their observation timestamps.

Pure functions over stored quote rows. Streamlit pages call these to build
heatmap values, cell notes, badges, and the freshness summary line. The 1D
rule itself lives in :mod:`market_intelligence.return_policy`; this module
only words it.

Timestamps are the quote's own observation time (``quote_ts``), never the
render time or the retrieval time, formatted in America/New_York with the
live EST/EDT abbreviation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from market_intelligence.return_policy import (
    BASIS_EOD_CLOSE,
    BASIS_LABELS,
    BASIS_LAST_CLOSE,
    BASIS_SESSION_OPEN,
    EOD_ONLY,
    OneDay,
    format_eastern,
    last_updated_label,
    one_day_from_quote,
    parse_ts,
    policy_for,
    quote_freshness,
)

FRESHNESS_WORDS = {
    "current": "current",
    "delayed": "delayed",
    "stale": "stale",
    "unavailable": "unavailable",
    "eod": "EOD",
}

ONE_DAY_POLICY_CAPTION = (
    "1D during an instrument's session is the current price divided by that session's open minus one. "
    "After the close and until the next open (including weekends and holidays) it is the current price "
    "divided by the most recent completed close minus one; the denominator switches at the next open. "
    "The current price is the newest provider observation by its own timestamp, including pre-market, "
    "post-market, and overnight prints when the provider supplies them. A quote older than the reference "
    "boundary or a missing open/close is N/A (pending), never substituted."
)

EOD_ONLY_CAPTION = "EOD close-to-close: this instrument has no intraday price; 1D is the newest completed close over the prior completed close."


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def one_day_cell(symbol: str, quote: Mapping[str, Any] | None, *, now: datetime | None = None) -> OneDay:
    """Resolve one stored quote row to a 1D change under the instrument policy."""
    return one_day_from_quote(symbol, quote, now=_now(now))


def freshness_word(symbol: str, price_ts: Any, *, now: datetime | None = None) -> str:
    return FRESHNESS_WORDS.get(quote_freshness(symbol, price_ts, now=_now(now)), "unavailable")


def one_day_note(one_day: OneDay, *, symbol: str = "", extra: Sequence[str] = (), now: datetime | None = None) -> str:
    """Tooltip text for a 1D cell: basis, formula/reason, observation time, freshness."""
    parts: list[str] = []
    if one_day.basis == EOD_ONLY:
        parts.append(BASIS_LABELS[BASIS_EOD_CLOSE])
    elif one_day.basis in (BASIS_SESSION_OPEN, BASIS_LAST_CLOSE):
        parts.append(one_day.basis_label)
    parts.append(one_day.reason)
    if one_day.reference_price is not None and one_day.reference_date is not None:
        parts.append("reference {0:.4g} ({1})".format(one_day.reference_price, one_day.reference_date.isoformat()))
    if one_day.price_ts is not None:
        parts.append(last_updated_label(one_day.price_ts))
        if symbol:
            parts.append(freshness_word(symbol, one_day.price_ts, now=now))
    for item in extra:
        text = str(item or "").strip()
        if text:
            parts.append(text)
    return " · ".join(part for part in parts if part)


def ratio_note(numerator: OneDay, denominator: OneDay, combined: OneDay, *, labels: tuple[str, str]) -> str:
    """Ratio tooltip: both leg timestamps and which one is older."""
    parts = [combined.basis_label if combined.basis else "N/A", combined.reason]
    num_ts = format_eastern(numerator.price_ts) or "unavailable"
    den_ts = format_eastern(denominator.price_ts) or "unavailable"
    parts.append("{0} observed {1}".format(labels[0], num_ts))
    parts.append("{0} observed {1}".format(labels[1], den_ts))
    if numerator.price_ts and denominator.price_ts and numerator.price_ts != denominator.price_ts:
        older = labels[0] if numerator.price_ts < denominator.price_ts else labels[1]
        parts.append("older leg: {0}".format(older))
    return " · ".join(parts)


def observation_label(quote_ts: Any) -> str:
    """``Last updated: Oct 7, 2026, 3:15 PM EDT`` for one observation."""
    return last_updated_label(quote_ts)


def basis_badge_title(one_day: OneDay) -> str:
    if one_day.basis == EOD_ONLY:
        return EOD_ONLY_CAPTION
    if one_day.basis == BASIS_SESSION_OPEN:
        return "Since session open: current price / {0} session open − 1.".format(
            one_day.reference_date.isoformat() if one_day.reference_date else "current session"
        )
    if one_day.basis == BASIS_LAST_CLOSE:
        return "Since last session close: current price / {0} completed close − 1.".format(
            one_day.reference_date.isoformat() if one_day.reference_date else "last session"
        )
    return one_day.reason


def freshness_summary(cells: Mapping[str, OneDay], *, now: datetime | None = None) -> str:
    """One line for a heatmap or card group. It does not imply simultaneity.

    Counts the bases in use, pending cells, and the newest and oldest
    observation timestamps across the rows. Row-level notes carry each
    instrument's own timestamp.
    """
    moment = _now(now)
    basis_counts: dict[str, int] = {}
    pending = 0
    missing = 0
    stamps: list[datetime] = []
    stale = 0
    for symbol, one_day in cells.items():
        if one_day.basis == EOD_ONLY:
            basis_counts[BASIS_LABELS[BASIS_EOD_CLOSE]] = basis_counts.get(BASIS_LABELS[BASIS_EOD_CLOSE], 0) + 1
            continue
        if one_day.available:
            basis_counts[one_day.basis_label] = basis_counts.get(one_day.basis_label, 0) + 1
        elif one_day.price_ts is not None:
            pending += 1
        else:
            missing += 1
        if one_day.price_ts is not None:
            stamps.append(one_day.price_ts)
            if quote_freshness(symbol, one_day.price_ts, now=moment) == "stale":
                stale += 1
    parts: list[str] = []
    for label, count in basis_counts.items():
        parts.append("{0}: {1}".format(label, count))
    if pending:
        parts.append("pending: {0}".format(pending))
    if missing:
        parts.append("no quote: {0}".format(missing))
    if stale:
        parts.append("stale: {0}".format(stale))
    if stamps:
        newest = max(stamps)
        oldest = min(stamps)
        if newest == oldest:
            parts.append("observed {0}".format(format_eastern(newest)))
        else:
            parts.append("observations from {0} to {1}".format(format_eastern(oldest), format_eastern(newest)))
    if not parts:
        return "1D: no stored quotes"
    return "1D · " + " · ".join(parts)


def policy_caption_for(symbols: Sequence[str]) -> str:
    """Caption naming the session conventions the listed symbols use."""
    seen: list[str] = []
    for symbol in symbols:
        spec = policy_for(symbol)
        if spec.description not in seen:
            seen.append(spec.description)
    if not seen:
        return ONE_DAY_POLICY_CAPTION
    return ONE_DAY_POLICY_CAPTION + " Sessions: " + " | ".join(seen)


def quote_price_and_ts(quote: Mapping[str, Any] | None) -> tuple[float | None, datetime | None]:
    """Current price and its observation timestamp from a stored quote row."""
    if not quote:
        return None, None
    provenance = quote.get("provenance") or {}
    if not isinstance(provenance, Mapping):
        provenance = {}
    price = provenance.get("current_price", quote.get("last_price"))
    try:
        number = float(price) if price is not None and not isinstance(price, bool) else None
    except (TypeError, ValueError):
        number = None
    return number, parse_ts(quote.get("quote_ts"))


__all__ = [
    "EOD_ONLY_CAPTION",
    "ONE_DAY_POLICY_CAPTION",
    "basis_badge_title",
    "freshness_summary",
    "freshness_word",
    "observation_label",
    "one_day_cell",
    "one_day_note",
    "policy_caption_for",
    "quote_price_and_ts",
    "ratio_note",
]
