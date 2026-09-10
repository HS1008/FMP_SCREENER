"""User-facing source status for main pages.

Engineering detail stays on Data Health. Main pages show only:
current / delayed / stale / unavailable / blocked.
"""

from __future__ import annotations

from typing import Any, Mapping

CURRENT = "current"
DELAYED = "delayed"
STALE = "stale"
UNAVAILABLE = "unavailable"
BLOCKED = "blocked"


def surface_status(row: Mapping[str, Any] | None) -> str:
    """Map a freshness/transport row to one user-facing state."""
    if not row:
        return UNAVAILABLE
    freshness = str(row.get("freshness_status") or "").upper()
    transport = str(row.get("transport_status") or "").upper()
    quote = str(row.get("quote_status") or row.get("observed_state") or "").upper()
    if transport in {"FAILED", "METADATA_REJECTED"} or freshness == "BLOCKED":
        return BLOCKED
    if freshness == "STALE" or quote in {"FROZEN", "OFFLINE"}:
        return STALE
    if quote in {"DELAYED", "OFFLINE_CACHED"} or transport == "PARTIAL":
        return DELAYED
    if freshness in {"FRESH", "OK", "CURRENT"} or transport == "OK":
        return CURRENT
    if transport in {"CONFIGURATION_REQUIRED", "NEVER_ATTEMPTED"} or freshness in {"UNKNOWN", ""}:
        if not row.get("latest_observation_date"):
            return UNAVAILABLE
    if not row.get("latest_observation_date"):
        return UNAVAILABLE
    return DELAYED


def worst_surface_status(rows: list[Mapping[str, Any]] | None) -> str:
    rank = {CURRENT: 0, DELAYED: 1, STALE: 2, UNAVAILABLE: 3, BLOCKED: 4}
    worst = CURRENT
    for row in rows or []:
        status = surface_status(row)
        if rank[status] > rank[worst]:
            worst = status
    if not rows:
        return UNAVAILABLE
    return worst
