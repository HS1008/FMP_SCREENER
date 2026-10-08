"""Cheap stored-quote overlay on the published Market Overview snapshot.

The published snapshot is the EOD composition. This module replaces only the 1D
column (and the displayed level) of rows whose instrument has a stored Yahoo
quote, using the central 1D policy. Everything else (1W-1Y, risk, as-of dates)
is untouched, so a quote update never moves the longer horizons. Rows without
a live quote are labeled EOD close-to-close. Pure: no Streamlit, no database.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Mapping, Sequence

from market_intelligence.ibkr_live_universe import quotes_by_symbol
from market_intelligence.live_1d_ui import one_day_note, ratio_note
from market_intelligence.nulls import strict_dumps
from market_intelligence.overview_snapshot import (
    SECTION_COMMODITIES,
    SECTION_CRYPTO,
    SECTION_FOREX,
    SECTION_GLOBAL,
    SECTION_MARKET_RATIOS,
    SECTION_SECTORS,
    SECTION_US_INDEXES,
    SECTION_VIX_TERM,
    SECTION_YIELD_CURVE,
)
from market_intelligence.return_policy import BASIS_EOD_CLOSE, BASIS_LABELS, OneDay, one_day_from_quote, ratio_one_day
from market_intelligence.taxonomy import BENCHMARK_SPY

EQUITY_SECTIONS = frozenset({SECTION_US_INDEXES, SECTION_SECTORS, SECTION_GLOBAL})
CROSS_ASSET_SECTIONS = frozenset({SECTION_COMMODITIES, SECTION_FOREX, SECTION_CRYPTO})
EOD_LABEL = BASIS_LABELS[BASIS_EOD_CLOSE]


def _cross_asset_by_id(rows: Sequence[Mapping[str, Any]] | None) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for row in rows or []:
        key = str(row.get("instrument_id") or "").upper()
        if key:
            out[key] = row
    return out


def _apply(row: dict[str, Any], one_day: OneDay, *, note: str, level: float | None) -> None:
    changes = dict(row.get("changes") or {})
    if "1D" in changes or one_day.available:
        changes["1D"] = one_day.value
    row["changes"] = changes
    row["one_day_basis"] = one_day.basis
    row["one_day_label"] = one_day.basis_label if one_day.available else ("pending" if one_day.price_ts else "N/A")
    row["one_day_note"] = note
    row["observed_at"] = one_day.price_ts.isoformat() if one_day.price_ts else None
    if level is not None:
        row["eod_level"] = row.get("level")
        row["level"] = float(level)
        row["level_basis"] = "quote"


def _mark_eod(row: dict[str, Any], *, reason: str = "") -> None:
    row["one_day_basis"] = BASIS_EOD_CLOSE
    row["one_day_label"] = EOD_LABEL
    row["one_day_note"] = EOD_LABEL + (" · " + reason if reason else "")
    row["observed_at"] = None


def compose_overview(
    snapshot: Mapping[str, Any],
    *,
    equity_quotes: Sequence[Mapping[str, Any]] | None,
    cross_asset_quotes: Sequence[Mapping[str, Any]] | None,
    now: datetime,
) -> dict[str, Any]:
    """Published EOD snapshot plus the stored-quote 1D overlay, as one immutable composed snapshot.

    The composed ``snapshot_id`` folds in the overlay so the page and the Excel
    export (which both receive this dict) always describe the same numbers.
    """
    equities = quotes_by_symbol(list(equity_quotes or []))
    cross = _cross_asset_by_id(cross_asset_quotes)
    sections: list[dict[str, Any]] = []
    live_rows = 0
    for section in snapshot.get("sections") or []:
        section_id = str(section.get("section_id") or "")
        copied = dict(section)
        rows: list[dict[str, Any]] = []
        for raw in section.get("rows") or []:
            row = dict(raw)
            key = str(row.get("key") or "").upper()
            if section_id in EQUITY_SECTIONS:
                quote = equities.get(key)
                if quote is None:
                    _mark_eod(row, reason="no stored quote")
                else:
                    one_day = one_day_from_quote(key, quote, now=now)
                    _apply(row, one_day, note=one_day_note(one_day, symbol=key, now=now), level=one_day.price)
                    live_rows += 1
            elif section_id == SECTION_MARKET_RATIOS:
                numerator = key.rsplit("_", 1)[0] if key.endswith("_" + BENCHMARK_SPY) else key
                num_quote = equities.get(numerator)
                spy_quote = equities.get(BENCHMARK_SPY)
                if num_quote is None or spy_quote is None:
                    _mark_eod(row, reason="no stored quote for both legs")
                else:
                    num = one_day_from_quote(numerator, num_quote, now=now)
                    den = one_day_from_quote(BENCHMARK_SPY, spy_quote, now=now)
                    combined = ratio_one_day(num, den)
                    _apply(
                        row,
                        combined,
                        note=ratio_note(num, den, combined, labels=(numerator, BENCHMARK_SPY)),
                        level=combined.price,
                    )
                    live_rows += 1
            elif section_id in CROSS_ASSET_SECTIONS:
                quote = cross.get(key)
                if quote is None:
                    _mark_eod(row, reason="no stored quote")
                else:
                    one_day = one_day_from_quote(key, quote, now=now)
                    _apply(row, one_day, note=one_day_note(one_day, symbol=key, now=now), level=one_day.price)
                    live_rows += 1
            elif section_id == SECTION_VIX_TERM:
                if key == "VIX_1M" and equities.get("VIX") is not None:
                    one_day = one_day_from_quote("VIX", equities["VIX"], now=now)
                    _apply(row, one_day, note=one_day_note(one_day, symbol="VIX", now=now), level=one_day.price)
                    live_rows += 1
                else:
                    _mark_eod(row, reason="VIX term tenors publish end-of-day closes only")
            elif section_id == SECTION_YIELD_CURVE and key == "MOVE":
                _mark_eod(row, reason="ICE BofA MOVE publishes end-of-day values only")
            rows.append(row)
        copied["rows"] = rows
        sections.append(copied)
    composed = dict(snapshot)
    composed["sections"] = sections
    overlay_digest = hashlib.sha256(
        strict_dumps([[row.get("changes", {}).get("1D"), row.get("observed_at"), row.get("level")] for section in sections for row in section["rows"]]).encode("utf-8")
    ).hexdigest()[:8]
    composed["eod_snapshot_id"] = snapshot.get("snapshot_id")
    composed["snapshot_id"] = "{0}-{1}".format(snapshot.get("snapshot_id"), overlay_digest)
    composed["live_rows"] = live_rows
    composed["composed_at"] = now.isoformat()
    return composed


__all__ = ["EOD_LABEL", "compose_overview"]
