"""Deterministic multi-source observation selection.

Compare by observation date, then an explicit source preference. Retrieval time
is never a tie-break. Raw provider rows stay in ``mi_provider_observations``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

TIE_PREFERENCE = ("TREASURY", "FRED", "EQUITY_EOD", "YAHOO", "FMP_LEGACY")

# Display/canonical series -> provider series that may supply it.
EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "DGS3MO": ("UST_NOM_3M", "DGS3MO"),
    "DGS6MO": ("UST_NOM_6M", "DGS6MO"),
    "DGS1": ("UST_NOM_1Y", "DGS1"),
    "DGS2": ("UST_NOM_2Y", "DGS2"),
    "DGS3": ("UST_NOM_3Y", "DGS3"),
    "DGS5": ("UST_NOM_5Y", "DGS5"),
    "DGS7": ("UST_NOM_7Y", "DGS7"),
    "DGS10": ("UST_NOM_10Y", "DGS10"),
    "DGS20": ("UST_NOM_20Y", "DGS20"),
    "DGS30": ("UST_NOM_30Y", "DGS30"),
    "DFII5": ("UST_REAL_5Y", "DFII5"),
    "DFII10": ("UST_REAL_10Y", "DFII10"),
    "DFII20": ("UST_REAL_20Y", "DFII20"),
    "DFII30": ("UST_REAL_30Y", "DFII30"),
}


@dataclass(frozen=True)
class ResolvedObservation:
    canonical_series_id: str
    series_id: str
    source_id: str
    observation_date: date | None
    value: Any
    retrieved_at: Any = None
    revision_seq: Any = None
    selection_reason: str = ""
    fallback: bool = False
    candidates: tuple[str, ...] = ()


def observation_date_of(row: Mapping[str, Any] | None, *keys: str) -> date | None:
    """Parse the first present date field. Retrieval timestamps are never accepted."""
    if not row:
        return None
    fields = keys or ("observation_date", "as_of")
    for key in fields:
        raw = row.get(key)
        if raw in (None, ""):
            continue
        if isinstance(raw, date):
            return raw
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
    return None


def _obs_date(row: Mapping[str, Any] | None) -> date | None:
    return observation_date_of(row, "observation_date")


def _source_rank(source_id: str) -> int:
    try:
        return TIE_PREFERENCE.index(source_id)
    except ValueError:
        return len(TIE_PREFERENCE) + 1


def resolve_observation(
    canonical_series_id: str,
    candidates: Sequence[Mapping[str, Any]],
    *,
    preference: Sequence[str] = TIE_PREFERENCE,
) -> ResolvedObservation | None:
    usable = [row for row in candidates if row and row.get("value") is not None and _obs_date(row) is not None]
    if not usable:
        return None
    best = None
    for row in usable:
        obs = _obs_date(row)
        source = str(row.get("source_id") or "")
        if best is None:
            best = row
            continue
        best_obs = _obs_date(best)
        if obs > best_obs:
            best = row
            continue
        if obs == best_obs:
            left = preference.index(source) if source in preference else 99
            right = preference.index(str(best.get("source_id") or "")) if str(best.get("source_id") or "") in preference else 99
            if left < right:
                best = row
    assert best is not None
    best_source = str(best.get("source_id") or "")
    fallback = best_source != "TREASURY" and any(str(r.get("source_id")) == "TREASURY" for r in usable)
    reason = "newer_observation_date"
    others = [r for r in usable if r is not best]
    if others and _obs_date(others[0]) == _obs_date(best):
        reason = "tie_preference_{0}".format(best_source)
    elif fallback:
        reason = "fred_fallback_no_newer_treasury"
    elif best_source == "TREASURY":
        reason = "preferred_treasury_latest"
    return ResolvedObservation(
        canonical_series_id=canonical_series_id,
        series_id=str(best.get("series_id") or canonical_series_id),
        source_id=best_source,
        observation_date=_obs_date(best),
        value=best.get("value"),
        retrieved_at=best.get("retrieved_at"),
        revision_seq=best.get("revision_seq"),
        selection_reason=reason,
        fallback=best_source == "FRED",
        candidates=tuple(sorted({str(r.get("series_id")) for r in usable})),
    )


def prefer_rows_by_group(
    rows: Sequence[Mapping[str, Any]],
    *,
    group_key: str,
    date_keys: Sequence[str] = ("observation_date", "as_of"),
    source_key: str = "source_id",
    preference: Sequence[str] = TIE_PREFERENCE,
) -> list[Mapping[str, Any]]:
    """Newest valid observation date wins; same-date ties use ``preference``.

    Rows with a missing/unparseable observation date are dropped so they cannot
    masquerade as current. ``retrieved_at`` is ignored.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if not row:
            continue
        key = str(row.get(group_key) or "")
        if not key:
            continue
        if observation_date_of(row, *date_keys) is None:
            continue
        grouped.setdefault(key, []).append(row)
    chosen: list[Mapping[str, Any]] = []
    for key, candidates in grouped.items():
        mapped = []
        originals: list[Mapping[str, Any]] = []
        for row in candidates:
            mapped.append(
                {
                    "series_id": key,
                    "source_id": str(row.get(source_key) or ""),
                    "observation_date": observation_date_of(row, *date_keys),
                    "value": True,
                }
            )
            originals.append(row)
        resolved = resolve_observation(key, mapped, preference=preference)
        if resolved is None:
            continue
        for row, candidate in zip(originals, mapped):
            if (
                str(candidate.get("source_id") or "") == resolved.source_id
                and candidate.get("observation_date") == resolved.observation_date
            ):
                chosen.append(row)
                break
    return chosen


def latest_common_observation_date(
    per_tenor: Mapping[str, Mapping[date, Any]],
    required: Sequence[str],
) -> date | None:
    """Latest date that has a value for every required tenor. Mixed-date sets are rejected."""
    common: set[date] | None = None
    for tenor in required:
        have = {day for day, value in (per_tenor.get(tenor) or {}).items() if value is not None}
        common = have if common is None else common & have
    if not common:
        return None
    return max(common)


__all__ = [
    "EQUIVALENTS",
    "ResolvedObservation",
    "TIE_PREFERENCE",
    "latest_common_observation_date",
    "observation_date_of",
    "prefer_rows_by_group",
    "resolve_observation",
]
